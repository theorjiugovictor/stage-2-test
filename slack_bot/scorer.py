"""
scorer.py — Automated rubric checker for the Stage-2 containerisation task.

Clones the submitted GitHub repo to a temp directory, statically analyses
every required file, and returns a structured dict of pass/fail results plus
a calculated score.
"""

import re
import shutil
import subprocess
import tempfile
from pathlib import Path

import yaml

# ── Point values for every check ─────────────────────────────────────────────
POINTS = {
    # Dockerfile (applied to BOTH api and frontend)
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
    "has_pytest":          2,
    "min_3_tests":         3,
    "redis_mocked":        2,
    "coverage_artifact":   2,

    # Pipeline – build
    "local_registry_svc": 2,
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
}

TOTAL_POSSIBLE = sum(v for k, v in POINTS.items() if k not in ("multi_stage", "named_nonroot_user",
                     "healthcheck", "no_env_copied", "slim_alpine_base", "user_creation_cmd"))
# Dockerfile points count twice (api + frontend)
TOTAL_POSSIBLE += sum(POINTS[k] for k in ("multi_stage", "named_nonroot_user", "healthcheck",
                                           "no_env_copied", "slim_alpine_base", "user_creation_cmd")) * 2


# ── Helpers ───────────────────────────────────────────────────────────────────

def _clone(url: str) -> tuple:
    """Return (Path to repo, error_string | None)."""
    # Strip sub-paths like /tree/main, /blob/…
    url = re.sub(r'/(?:tree|blob|commit|issues?|pulls?)/.*$', '', url).rstrip('/')
    clone_url = url if url.endswith('.git') else url + '.git'

    tmpdir = tempfile.mkdtemp(prefix='scorer_')
    try:
        r = subprocess.run(
            ['git', 'clone', '--depth=1', clone_url, tmpdir],
            capture_output=True, text=True, timeout=90,
        )
        if r.returncode != 0:
            shutil.rmtree(tmpdir, ignore_errors=True)
            return None, r.stderr.strip() or 'git clone failed'
        return Path(tmpdir), None
    except subprocess.TimeoutExpired:
        shutil.rmtree(tmpdir, ignore_errors=True)
        return None, 'Clone timed out (90 s) — is the repo public?'
    except Exception as exc:
        shutil.rmtree(tmpdir, ignore_errors=True)
        return None, str(exc)


# ── Per-file checkers ─────────────────────────────────────────────────────────

def _check_dockerfile(content: str) -> dict:
    """Return pass/fail for each Dockerfile requirement."""
    c = content

    from_as = re.findall(r'^FROM\s+\S+\s+AS\s+\S+', c, re.MULTILINE | re.IGNORECASE)
    user_lines = re.findall(r'^USER\s+(\S+)', c, re.MULTILINE)

    return {
        'multi_stage': len(from_as) >= 2,
        'named_nonroot_user': any(
            not u.isdigit() and u.lower() not in ('root', '0')
            for u in user_lines
        ),
        'healthcheck': bool(re.search(r'^HEALTHCHECK\b', c, re.MULTILINE)),
        'no_env_copied': not any(
            re.search(r'\.env\b', line)
            for line in re.findall(r'^COPY\s+.+', c, re.MULTILINE)
        ),
        'slim_alpine_base': any(
            'slim' in f.lower() or 'alpine' in f.lower()
            for f in re.findall(r'^FROM\s+(\S+)', c, re.MULTILINE | re.IGNORECASE)
        ),
        'user_creation_cmd': bool(
            re.search(r'\b(useradd|adduser)\b', c) and
            re.search(r'\b(groupadd|addgroup)\b', c)
        ),
    }


def _check_compose(content: str) -> dict:
    """Return pass/fail for each docker-compose.yml requirement."""
    empty = {k: False for k in ('named_network', 'redis_no_host_ports', 'depends_on_healthy',
                                 'resource_limits_all', 'restart_policies', 'all_config_via_env')}
    try:
        data = yaml.safe_load(content)
    except yaml.YAMLError:
        return empty

    services = data.get('services', {}) or {}
    networks = data.get('networks', {}) or {}

    def has_healthy_dep(svc):
        dep = svc.get('depends_on', {})
        if isinstance(dep, dict):
            return any(v.get('condition') == 'service_healthy' for v in dep.values())
        return False

    def has_limits(svc):
        limits = svc.get('deploy', {}).get('resources', {}).get('limits', {})
        return 'cpus' in limits and 'memory' in limits

    non_redis = {k: v for k, v in services.items() if k != 'redis'}
    redis_svc = services.get('redis', {})

    return {
        'named_network':       len(networks) > 0,
        'redis_no_host_ports': 'ports' not in redis_svc,
        'depends_on_healthy':  bool(non_redis) and all(has_healthy_dep(v) for v in non_redis.values()),
        'resource_limits_all': bool(services) and all(has_limits(v) for v in services.values()),
        'restart_policies':    bool(services) and all('restart' in v for v in services.values()),
        'all_config_via_env':  '${' in content,
    }


def _check_pipeline(content: str) -> dict:
    """Return pass/fail for each CI/CD pipeline requirement."""
    c = content
    cl = content.lower()

    try:
        data = yaml.safe_load(c)
        jobs = data.get('jobs', {}) or {}
    except yaml.YAMLError:
        jobs = {}

    # Collect all steps across all jobs
    all_steps = [s for j in jobs.values() for s in (j.get('steps') or [])]

    # Find the deploy job
    deploy_job = next((v for k, v in jobs.items() if 'deploy' in k.lower()), None)
    deploy_if = str((deploy_job or {}).get('if', ''))

    return {
        # Lint
        'has_flake8':   'flake8' in c,
        'has_eslint':   'eslint' in c,
        'has_hadolint': 'hadolint' in cl,

        # Test
        'has_pytest':        'pytest' in c,
        'coverage_artifact': 'upload-artifact' in c and 'cov' in cl,

        # Build
        'local_registry_svc':  'registry:2' in c,
        'sha_and_latest_tags': 'github.sha' in c and ':latest' in c,
        'layer_caching':       'cache-from' in c,

        # Security scan
        'has_trivy':         'trivy' in cl,
        'fails_on_critical': 'CRITICAL' in c and ('exit-code' in c or 'severity' in c),
        'sarif_artifact':    'sarif' in cl and 'upload-artifact' in c,

        # Integration test
        'compose_up_in_ci':    'docker compose up' in c or 'docker-compose up' in c,
        'integration_timeout': bool(re.search(r'timeout.*3[05]|JOB_TIMEOUT|timeout.*30', c)),
        'teardown_always':     ('docker compose down' in c or 'docker-compose down' in c) and 'always' in c,

        # Deploy
        'deploy_main_only': 'main' in deploy_if,
        'rolling_update':   'rolling' in cl or bool(
            re.search(r'health.*old|old.*health|stop.*old|new.*health', cl)
        ),

        # Hygiene
        'secrets_not_hardcoded': 'secrets.' in c,
        'all_steps_named': bool(all_steps) and all('name' in s for s in all_steps),
    }


def _check_tests(repo: Path) -> tuple:
    """Return (results_dict, test_count)."""
    test_dir = repo / 'tests'
    results = {
        'min_3_tests':       False,
        'redis_mocked':      False,
        'has_integration_sh': False,
        'integration_timeout': False,
    }
    test_count = 0

    if test_dir.exists():
        for tf in test_dir.glob('test_*.py'):
            try:
                txt = tf.read_text()
                test_count += len(re.findall(r'^def test_', txt, re.MULTILINE))
                if re.search(r'mock|patch|MagicMock', txt, re.IGNORECASE):
                    results['redis_mocked'] = True
            except OSError:
                pass

        sh = test_dir / 'integration_test.sh'
        results['has_integration_sh'] = sh.exists()
        if sh.exists():
            try:
                sh_txt = sh.read_text()
                results['integration_timeout'] = bool(
                    re.search(r'TIMEOUT|timeout', sh_txt)
                )
            except OSError:
                pass

    results['min_3_tests'] = test_count >= 3
    return results, test_count


# ── Main entry point ──────────────────────────────────────────────────────────

def score_repo(repo_url: str) -> dict:
    """
    Clone repo_url and run all checks.
    Returns a dict suitable for format_report().
    """
    repo, err = _clone(repo_url)
    if err:
        return {'error': f'Could not clone repository: {err}'}

    try:
        out = {}

        # Dockerfiles
        for key, rel in [('api_dockerfile', 'api/Dockerfile'), ('fe_dockerfile', 'frontend/Dockerfile')]:
            p = repo / rel
            out[key] = _check_dockerfile(p.read_text()) if p.exists() else None

        # docker-compose.yml
        for name in ('docker-compose.yml', 'docker-compose.yaml'):
            p = repo / name
            if p.exists():
                out['compose'] = _check_compose(p.read_text())
                break
        else:
            out['compose'] = None

        # Pipeline YAML(s) — merge results from all workflow files
        wf_dir = repo / '.github' / 'workflows'
        pipeline: dict = {}
        if wf_dir.exists():
            for yf in list(wf_dir.glob('*.yml')) + list(wf_dir.glob('*.yaml')):
                try:
                    pipeline.update(_check_pipeline(yf.read_text()))
                except OSError:
                    pass
        out['pipeline'] = pipeline if pipeline else None

        # Tests
        test_results, test_count = _check_tests(repo)
        out['tests'] = test_results
        out['test_count'] = test_count

        return out

    finally:
        shutil.rmtree(repo, ignore_errors=True)
