"""
scorer.py — Automated rubric checker for the Stage-2 containerisation task.

Two layers of validation:
  1. Static analysis  – fast pattern/YAML checks on every required file (~5 s)
  2. Functional checks – actually run pytest, docker build, and query GitHub
                         Actions for real pass/fail evidence (~2-5 min)
"""

import json
import re
import shutil
import subprocess
import tempfile
import time
import urllib.request
from pathlib import Path

import yaml

# ── Point values ──────────────────────────────────────────────────────────────
POINTS = {
    # Dockerfile (applied to BOTH api and frontend separately)
    "multi_stage":        3,
    "named_nonroot_user": 3,
    "healthcheck":        2,
    "no_env_copied":      2,
    "slim_alpine_base":   1,
    "user_creation_cmd":  1,

    # docker-compose.yml
    "named_network":       3,
    "redis_no_host_ports": 4,
    "depends_on_healthy":  4,
    "resource_limits_all": 4,
    "restart_policies":    3,
    "all_config_via_env":  4,

    # Pipeline – lint
    "has_flake8":   2,
    "has_eslint":   3,
    "has_hadolint": 3,

    # Pipeline – test
    "has_pytest":        2,
    "min_3_tests":       3,
    "redis_mocked":      2,
    "coverage_artifact": 2,

    # Pipeline – build
    "local_registry_svc":  2,
    "sha_and_latest_tags": 3,
    "layer_caching":       3,

    # Pipeline – security scan
    "has_trivy":         3,
    "fails_on_critical": 2,
    "sarif_artifact":    3,

    # Pipeline – integration test
    "compose_up_in_ci":    2,
    "has_integration_sh":  2,
    "integration_timeout": 2,
    "teardown_always":     2,

    # Pipeline – deploy
    "deploy_main_only": 2,
    "rolling_update":   4,

    # Pipeline hygiene
    "secrets_not_hardcoded": 2,
    "all_steps_named":       2,

    # ── Functional checks (bonus points on top of static score) ──────────────
    "pytest_all_pass":         5,
    "api_image_builds":        5,
    "frontend_image_builds":   5,
    "ci_pipeline_green":       5,
}

# Static-only total (Dockerfile points × 2 for api + frontend)
_DF_KEYS = {"multi_stage", "named_nonroot_user", "healthcheck",
            "no_env_copied", "slim_alpine_base", "user_creation_cmd"}
_FUNC_KEYS = {"pytest_all_pass", "api_image_builds",
              "frontend_image_builds", "ci_pipeline_green"}

STATIC_TOTAL  = sum(v for k, v in POINTS.items() if k not in _FUNC_KEYS) \
                + sum(POINTS[k] for k in _DF_KEYS)   # Dockerfile counted twice
FUNC_TOTAL    = sum(POINTS[k] for k in _FUNC_KEYS)
GRAND_TOTAL   = STATIC_TOTAL + FUNC_TOTAL


# ── Shared helpers ────────────────────────────────────────────────────────────

def _run(cmd, cwd=None, timeout=120, env=None):
    """Run a subprocess, return CompletedProcess (never raises on non-zero)."""
    return subprocess.run(
        cmd, cwd=cwd, capture_output=True, text=True,
        timeout=timeout, env=env,
    )


def _clone(url: str):
    """Clone repo → (Path | None, error_str | None)."""
    url = re.sub(r'/(?:tree|blob|commit|issues?|pulls?)/.*$', '', url).rstrip('/')
    clone_url = url if url.endswith('.git') else url + '.git'
    tmp = tempfile.mkdtemp(prefix='scorer_')
    try:
        r = _run(['git', 'clone', '--depth=1', clone_url, tmp], timeout=90)
        if r.returncode != 0:
            shutil.rmtree(tmp, ignore_errors=True)
            return None, r.stderr.strip() or 'git clone failed'
        return Path(tmp), None
    except subprocess.TimeoutExpired:
        shutil.rmtree(tmp, ignore_errors=True)
        return None, 'Clone timed out (90 s) — is the repo public?'
    except Exception as exc:
        shutil.rmtree(tmp, ignore_errors=True)
        return None, str(exc)


# ── Static checkers ───────────────────────────────────────────────────────────

def _check_dockerfile(content: str) -> dict:
    c = content
    from_as   = re.findall(r'^FROM\s+\S+\s+AS\s+\S+', c, re.MULTILINE | re.IGNORECASE)
    user_lines = re.findall(r'^USER\s+(\S+)', c, re.MULTILINE)
    from_lines = re.findall(r'^FROM\s+(\S+)', c, re.MULTILINE | re.IGNORECASE)
    copy_lines = re.findall(r'^COPY\s+.+', c, re.MULTILINE)
    return {
        'multi_stage':        len(from_as) >= 2,
        'named_nonroot_user': any(not u.isdigit() and u.lower() not in ('root', '0')
                                  for u in user_lines),
        'healthcheck':        bool(re.search(r'^HEALTHCHECK\b', c, re.MULTILINE)),
        'no_env_copied':      not any(re.search(r'\.env\b', l) for l in copy_lines),
        'slim_alpine_base':   any('slim' in f.lower() or 'alpine' in f.lower()
                                  for f in from_lines),
        'user_creation_cmd':  bool(re.search(r'\b(useradd|adduser)\b', c) and
                                   re.search(r'\b(groupadd|addgroup)\b', c)),
    }


def _check_compose(content: str) -> dict:
    blank = {k: False for k in ('named_network', 'redis_no_host_ports',
                                 'depends_on_healthy', 'resource_limits_all',
                                 'restart_policies', 'all_config_via_env')}
    try:
        data = yaml.safe_load(content)
    except yaml.YAMLError:
        return blank

    services = data.get('services', {}) or {}
    networks = data.get('networks', {}) or {}

    def has_healthy(svc):
        dep = svc.get('depends_on', {})
        return isinstance(dep, dict) and any(
            v.get('condition') == 'service_healthy' for v in dep.values()
        )

    def has_limits(svc):
        lim = svc.get('deploy', {}).get('resources', {}).get('limits', {})
        return 'cpus' in lim and 'memory' in lim

    non_redis = {k: v for k, v in services.items() if k != 'redis'}
    redis_svc = services.get('redis', {})

    return {
        'named_network':       len(networks) > 0,
        'redis_no_host_ports': 'ports' not in redis_svc,
        'depends_on_healthy':  bool(non_redis) and all(has_healthy(v) for v in non_redis.values()),
        'resource_limits_all': bool(services) and all(has_limits(v) for v in services.values()),
        'restart_policies':    bool(services) and all('restart' in v for v in services.values()),
        'all_config_via_env':  '${' in content,
    }


def _check_pipeline(content: str) -> dict:
    c = content
    try:
        data = yaml.safe_load(c)
        jobs = data.get('jobs', {}) or {}
    except yaml.YAMLError:
        jobs = {}

    all_steps   = [s for j in jobs.values() for s in (j.get('steps') or [])]
    deploy_job  = next((v for k, v in jobs.items() if 'deploy' in k.lower()), None)
    deploy_if   = str((deploy_job or {}).get('if', ''))

    return {
        'has_flake8':   'flake8' in c,
        'has_eslint':   'eslint' in c,
        'has_hadolint': 'hadolint' in c.lower(),

        'has_pytest':        'pytest' in c,
        'coverage_artifact': 'upload-artifact' in c and 'cov' in c.lower(),

        'local_registry_svc':  'registry:2' in c,
        'sha_and_latest_tags': 'github.sha' in c and ':latest' in c,
        'layer_caching':       'cache-from' in c,

        'has_trivy':         'trivy' in c.lower(),
        'fails_on_critical': 'CRITICAL' in c and ('exit-code' in c or 'severity' in c),
        'sarif_artifact':    'sarif' in c.lower() and 'upload-artifact' in c,

        'compose_up_in_ci':    'docker compose up' in c or 'docker-compose up' in c,
        'integration_timeout': bool(re.search(r'timeout.*3[05]|JOB_TIMEOUT', c)),
        'teardown_always':     ('docker compose down' in c or 'docker-compose down' in c)
                               and 'always' in c,

        'deploy_main_only': 'main' in deploy_if,
        'rolling_update':   'rolling' in c.lower() or bool(
            re.search(r'health.*old|old.*health|stop.*old|new.*health', c.lower())
        ),
        'secrets_not_hardcoded': 'secrets.' in c,
        'all_steps_named':       bool(all_steps) and all('name' in s for s in all_steps),
    }


def _check_tests(repo: Path):
    """Return (results_dict, test_count)."""
    test_dir = repo / 'tests'
    r = {'min_3_tests': False, 'redis_mocked': False,
         'has_integration_sh': False, 'integration_timeout': False}
    count = 0
    if test_dir.exists():
        for tf in test_dir.glob('test_*.py'):
            try:
                txt = tf.read_text()
                count += len(re.findall(r'^def test_', txt, re.MULTILINE))
                if re.search(r'mock|patch|MagicMock', txt, re.IGNORECASE):
                    r['redis_mocked'] = True
            except OSError:
                pass
        sh = test_dir / 'integration_test.sh'
        r['has_integration_sh'] = sh.exists()
        if sh.exists():
            try:
                r['integration_timeout'] = bool(re.search(r'TIMEOUT|timeout', sh.read_text()))
            except OSError:
                pass
    r['min_3_tests'] = count >= 3
    return r, count


# ── Functional checkers ───────────────────────────────────────────────────────

def _run_pytest(repo: Path) -> dict:
    """
    Create an isolated venv, install API deps + pytest, then run the test suite.
    Returns a result dict with counts and a one-line summary.
    """
    venv = Path(tempfile.mkdtemp(prefix='scorer_venv_'))
    try:
        # Create venv
        r = _run(['python3', '-m', 'venv', str(venv)], timeout=30)
        if r.returncode != 0:
            return {'ran': False, 'error': 'Could not create venv'}

        pip    = venv / 'bin' / 'pip'
        pytest = venv / 'bin' / 'pytest'

        # Install API requirements
        reqs = repo / 'api' / 'requirements.txt'
        if reqs.exists():
            r = _run([str(pip), 'install', '-q', '-r', str(reqs)], timeout=120)
            if r.returncode != 0:
                return {'ran': False, 'error': f'pip install failed: {r.stderr[-300:]}'}

        # Install test tooling
        _run([str(pip), 'install', '-q', 'pytest', 'pytest-cov'], timeout=60)

        # Run tests
        r = _run(
            [str(pytest), 'tests/', '-v', '--tb=short', '--no-header'],
            cwd=str(repo), timeout=60,
        )
        out = r.stdout + r.stderr

        passed  = len(re.findall(r' PASSED', out))
        failed  = len(re.findall(r' FAILED', out))
        errors  = len(re.findall(r' ERROR',  out))

        # Extract pytest summary line e.g. "6 passed in 0.42s"
        summary_match = re.search(r'=+ (.+?) =+\s*$', out, re.MULTILINE)
        summary = summary_match.group(1) if summary_match else f"{passed} passed, {failed} failed"

        return {
            'ran':        True,
            'all_passed': r.returncode == 0,
            'passed':     passed,
            'failed':     failed,
            'errors':     errors,
            'summary':    summary,
        }
    except subprocess.TimeoutExpired:
        return {'ran': False, 'error': 'pytest timed out (60 s)'}
    except Exception as exc:
        return {'ran': False, 'error': str(exc)}
    finally:
        shutil.rmtree(venv, ignore_errors=True)


def _docker_build(repo: Path, service: str) -> dict:
    """
    Run `docker build` for the given service directory.
    Cleans up the test image on success.
    """
    context = repo / service
    if not (context / 'Dockerfile').exists():
        return {'built': False, 'error': 'Dockerfile not found', 'duration_s': 0}

    tag   = f'scorer-{service}-test:latest'
    start = time.monotonic()
    try:
        r = _run(['docker', 'build', '--no-cache', '-t', tag, '.'],
                 cwd=str(context), timeout=300)
        dur = round(time.monotonic() - start, 1)
        if r.returncode == 0:
            subprocess.run(['docker', 'rmi', '-f', tag],
                           capture_output=True, timeout=15)
            return {'built': True, 'duration_s': dur}
        # Return last 400 chars of stderr as the error snippet
        return {'built': False,
                'error': (r.stderr or r.stdout)[-400:].strip(),
                'duration_s': dur}
    except subprocess.TimeoutExpired:
        return {'built': False, 'error': 'Build timed out (5 min)',
                'duration_s': round(time.monotonic() - start, 1)}
    except FileNotFoundError:
        return {'built': False, 'error': 'docker not found — is Docker running?',
                'duration_s': 0}
    except Exception as exc:
        return {'built': False, 'error': str(exc), 'duration_s': 0}


def _check_ci_status(repo_url: str) -> dict:
    """
    Query the public GitHub Actions API for the latest workflow run.
    No authentication required for public repos.
    """
    m = re.search(r'github\.com/([^/]+)/([^/.\s]+)', repo_url)
    if not m:
        return {'available': False, 'error': 'Could not parse repo URL'}

    owner, repo = m.group(1), m.group(2).rstrip('.git')
    api = f'https://api.github.com/repos/{owner}/{repo}/actions/runs?per_page=5'

    try:
        req = urllib.request.Request(api, headers={
            'Accept':     'application/vnd.github.v3+json',
            'User-Agent': 'stage2-scorer/1.0',
        })
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read())

        runs = data.get('workflow_runs', [])
        if not runs:
            return {'available': True, 'no_runs': True}

        latest = runs[0]
        return {
            'available':   True,
            'conclusion':  latest.get('conclusion'),   # success | failure | cancelled | None
            'status':      latest.get('status'),       # completed | in_progress | queued
            'branch':      latest.get('head_branch'),
            'run_url':     latest.get('html_url'),
            'workflow':    latest.get('name'),
            'total_runs':  data.get('total_count', 0),
        }
    except urllib.error.HTTPError as exc:
        return {'available': False, 'error': f'GitHub API {exc.code}'}
    except Exception as exc:
        return {'available': False, 'error': str(exc)}


# ── Main entry point ──────────────────────────────────────────────────────────

def score_repo(repo_url: str) -> dict:
    """
    Full scoring pipeline:
      1. Clone repo
      2. Static analysis of all required files
      3. Functional: pytest, docker build ×2, GitHub Actions status
    """
    repo, err = _clone(repo_url)
    if err:
        return {'error': f'Could not clone repository: {err}'}

    try:
        out = {}

        # ── Static ────────────────────────────────────────────────────────────
        for key, rel in [('api_dockerfile', 'api/Dockerfile'),
                         ('fe_dockerfile',  'frontend/Dockerfile')]:
            p = repo / rel
            out[key] = _check_dockerfile(p.read_text()) if p.exists() else None

        for name in ('docker-compose.yml', 'docker-compose.yaml'):
            p = repo / name
            if p.exists():
                out['compose'] = _check_compose(p.read_text())
                break
        else:
            out['compose'] = None

        wf_dir   = repo / '.github' / 'workflows'
        pipeline: dict = {}
        if wf_dir.exists():
            for yf in list(wf_dir.glob('*.yml')) + list(wf_dir.glob('*.yaml')):
                try:
                    pipeline.update(_check_pipeline(yf.read_text()))
                except OSError:
                    pass
        out['pipeline'] = pipeline or None

        test_results, test_count = _check_tests(repo)
        out['tests']      = test_results
        out['test_count'] = test_count

        # ── Functional ────────────────────────────────────────────────────────
        out['func_pytest']    = _run_pytest(repo)
        out['func_api_build'] = _docker_build(repo, 'api')
        out['func_fe_build']  = _docker_build(repo, 'frontend')
        out['func_ci']        = _check_ci_status(repo_url)

        return out

    finally:
        shutil.rmtree(repo, ignore_errors=True)
