"""
bot.py — Slack bot that scores Stage-2 containerisation submissions.

Usage in any channel the bot is in:
    submit https://github.com/username/their-repo

The bot acknowledges publicly, runs static + functional checks in a background
thread (~2-5 min), then DMs the full scored report to the student.
"""

import logging
import os
import re
import threading

from dotenv import load_dotenv
from slack_bolt import App
from slack_bolt.adapter.socket_mode import SocketModeHandler

from scorer import POINTS, STATIC_TOTAL, FUNC_TOTAL, score_repo

load_dotenv()
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger(__name__)

app = App(token=os.environ["SLACK_BOT_TOKEN"])

_GH_URL_RE = re.compile(r'https://github\.com/[\w.\-]+/[\w.\-]+(?:/[^\s>]*)?')


# ── Report builder ────────────────────────────────────────────────────────────

def _ok(passed) -> str:
    return "✅" if passed else "❌"


def _build_report(results: dict, repo_url: str) -> str:
    if "error" in results:
        return f"❌ *Scoring failed*\n>{results['error']}"

    api_df   = results.get("api_dockerfile")
    fe_df    = results.get("fe_dockerfile")
    compose  = results.get("compose")
    pipeline = results.get("pipeline") or {}
    tests    = results.get("tests") or {}
    tc       = results.get("test_count", 0)

    # Functional results
    f_pytest = results.get("func_pytest") or {}
    f_api    = results.get("func_api_build") or {}
    f_fe     = results.get("func_fe_build") or {}
    f_ci     = results.get("func_ci") or {}

    out = []
    s1_e = s1_p = s2_e = s2_p = fn_e = fn_p = 0

    def add(label, key, pts_map, source, bucket):
        nonlocal s1_e, s1_p, s2_e, s2_p
        pts    = pts_map[key]
        passed = bool((source or {}).get(key, False))
        if bucket == 1:
            s1_e += pts if passed else 0
            s1_p += pts
        else:
            s2_e += pts if passed else 0
            s2_p += pts
        out.append(f"  {'✅' if passed else '❌'} {label} (+{pts})")

    # ── Section 1: Containerisation ──────────────────────────────────────────
    df_rows = [
        ("Multi-stage build",                "multi_stage"),
        ("Named non-root USER",              "named_nonroot_user"),
        ("HEALTHCHECK instruction",          "healthcheck"),
        ("No .env files COPY'd in",          "no_env_copied"),
        ("Slim / Alpine base image",         "slim_alpine_base"),
        ("User created via adduser/useradd", "user_creation_cmd"),
    ]
    for title, src in [("API Dockerfile", api_df), ("Frontend Dockerfile", fe_df)]:
        note = " — ❌ _not found_" if src is None else ""
        out.append(f"\n*{title}*{note}")
        for label, key in df_rows:
            add(label, key, POINTS, src, 1)

    note = " — ❌ _not found_" if compose is None else ""
    out.append(f"\n*docker-compose.yml*{note}")
    for label, key in [
        ("Named bridge network",               "named_network"),
        ("Redis NOT exposed on host",          "redis_no_host_ports"),
        ("depends_on: service_healthy",        "depends_on_healthy"),
        ("CPU + memory limits (all services)", "resource_limits_all"),
        ("Restart policies (all services)",    "restart_policies"),
        ("All config via .env",                "all_config_via_env"),
    ]:
        add(label, key, POINTS, compose, 1)

    # ── Section 2: CI/CD Pipeline ────────────────────────────────────────────
    no_pipe = results.get("pipeline") is None

    for stage, rows in [
        ("Lint", [
            ("flake8 (Python lint)",       "has_flake8",        pipeline),
            ("ESLint (JS lint)",           "has_eslint",        pipeline),
            ("Hadolint (Dockerfile lint)", "has_hadolint",      pipeline),
        ]),
        ("Test", [
            ("pytest in pipeline",                      "has_pytest",        pipeline),
            (f"≥3 unit tests  _(found: {tc})_",         "min_3_tests",       tests),
            ("Redis mocked (no live Redis in tests)",   "redis_mocked",      tests),
            ("Coverage report uploaded as artifact",    "coverage_artifact", pipeline),
        ]),
        ("Build", [
            ("Local registry as service container", "local_registry_svc",  pipeline),
            ("Images tagged SHA + latest",          "sha_and_latest_tags", pipeline),
            ("Docker layer caching configured",     "layer_caching",       pipeline),
        ]),
        ("Security Scan", [
            ("Trivy scan present",        "has_trivy",         pipeline),
            ("Fails on CRITICAL CVEs",    "fails_on_critical", pipeline),
            ("SARIF results as artifact", "sarif_artifact",    pipeline),
        ]),
        ("Integration Test", [
            ("docker compose up in CI",      "compose_up_in_ci",    pipeline),
            ("integration_test.sh present",  "has_integration_sh",  tests),
            ("30 s timeout enforced",        "integration_timeout", tests),
            ("Teardown on if: always()",     "teardown_always",     pipeline),
        ]),
        ("Deploy", [
            ("main branch only",     "deploy_main_only", pipeline),
            ("Rolling update logic", "rolling_update",   pipeline),
        ]),
        ("Pipeline Hygiene", [
            ("Secrets in GH secrets (not hardcoded)", "secrets_not_hardcoded", pipeline),
            ("Every step has a name: field",          "all_steps_named",       pipeline),
        ]),
    ]:
        note = " — ❌ _no workflow YAML found_" if no_pipe else ""
        out.append(f"\n*{stage} Stage*{note}")
        for label, key, source in rows:
            add(label, key, POINTS, source, 2)

    # ── Functional validation section ─────────────────────────────────────────
    out.append("\n*🔬 Functional Validation*")

    # pytest
    if not f_pytest.get('ran'):
        pytest_line = f"  ⚠️  pytest could not run — `{f_pytest.get('error', 'unknown error')}`"
        pytest_pts  = 0
    elif f_pytest.get('all_passed'):
        summary = f_pytest.get('summary', '')
        pytest_line = f"  ✅ All tests pass — _{summary}_ (+{POINTS['pytest_all_pass']})"
        pytest_pts  = POINTS['pytest_all_pass']
    else:
        p = f_pytest.get('passed', 0)
        f = f_pytest.get('failed', 0)
        e = f_pytest.get('errors', 0)
        pytest_line = f"  ❌ Tests failed — {p} passed, {f} failed, {e} errors (+0)"
        pytest_pts  = 0
    out.append(pytest_line)
    fn_e += pytest_pts
    fn_p += POINTS['pytest_all_pass']

    # docker build – api
    if not f_api.get('built') and 'docker not found' in (f_api.get('error') or ''):
        out.append(f"  ⚠️  docker build skipped — Docker not available on scorer host")
        fn_p += POINTS['api_image_builds']
    elif f_api.get('built'):
        dur = f_api.get('duration_s', '?')
        out.append(f"  ✅ API image builds ({dur} s) (+{POINTS['api_image_builds']})")
        fn_e += POINTS['api_image_builds']
        fn_p += POINTS['api_image_builds']
    else:
        err = (f_api.get('error') or 'unknown')[:120]
        out.append(f"  ❌ API image build failed — `{err}` (+0)")
        fn_p += POINTS['api_image_builds']

    # docker build – frontend
    if not f_fe.get('built') and 'docker not found' in (f_fe.get('error') or ''):
        out.append(f"  ⚠️  docker build skipped — Docker not available on scorer host")
        fn_p += POINTS['frontend_image_builds']
    elif f_fe.get('built'):
        dur = f_fe.get('duration_s', '?')
        out.append(f"  ✅ Frontend image builds ({dur} s) (+{POINTS['frontend_image_builds']})")
        fn_e += POINTS['frontend_image_builds']
        fn_p += POINTS['frontend_image_builds']
    else:
        err = (f_fe.get('error') or 'unknown')[:120]
        out.append(f"  ❌ Frontend image build failed — `{err}` (+0)")
        fn_p += POINTS['frontend_image_builds']

    # GitHub Actions latest run
    ci_pts = POINTS['ci_pipeline_green']
    fn_p  += ci_pts
    if not f_ci.get('available'):
        out.append(f"  ⚠️  GitHub Actions API unavailable — `{f_ci.get('error', '')}`")
    elif f_ci.get('no_runs'):
        out.append(f"  ⚠️  No CI runs found yet")
    else:
        conclusion = f_ci.get('conclusion')
        branch     = f_ci.get('branch', '?')
        run_url    = f_ci.get('run_url', '')
        workflow   = f_ci.get('workflow', 'CI')
        total      = f_ci.get('total_runs', '?')
        if conclusion == 'success':
            out.append(f"  ✅ Latest CI run passed — _{workflow}_ on `{branch}` "
                       f"({total} total runs) (+{ci_pts})\n  🔗 {run_url}")
            fn_e += ci_pts
        elif conclusion in ('failure', 'cancelled'):
            out.append(f"  ❌ Latest CI run: *{conclusion}* — _{workflow}_ on `{branch}` (+0)"
                       f"\n  🔗 {run_url}")
        else:
            status = f_ci.get('status', '?')
            out.append(f"  ⏳ CI run is _{status}_ (not yet complete) — check back later"
                       f"\n  🔗 {run_url}")

    # ── Totals & grade ────────────────────────────────────────────────────────
    static_e = s1_e + s2_e
    static_p = s1_p + s2_p
    total_e  = static_e + fn_e
    total_p  = static_p + fn_p

    pct = round(100 * total_e / total_p) if total_p else 0
    grade = (
        "A 🏆" if pct >= 90 else
        "B 👍" if pct >= 80 else
        "C 😐" if pct >= 70 else
        "D 😬" if pct >= 60 else
        "F 😞"
    )

    header = [
        "📊 *Stage-2 Submission Score Report*",
        f"🔗 Repo: <{repo_url}|{repo_url}>",
        "",
        f"*Total: {total_e}/{total_p}  ({pct}%)  —  Grade: {grade}*",
        f"  Static checks:     {static_e}/{static_p}",
        f"  Functional checks: {fn_e}/{fn_p}",
        "",
        f"━━━━ Section 1 — Containerisation  ({s1_e}/{s1_p}) ━━━━",
    ]

    # Insert section 2 divider
    first_pipe = next((i for i, l in enumerate(out) if '*Lint Stage*' in l), len(out))
    out.insert(first_pipe, f"\n━━━━ Section 2 — CI/CD Pipeline  ({s2_e}/{s2_p}) ━━━━")

    # Insert functional divider
    func_idx = next((i for i, l in enumerate(out) if '*🔬 Functional' in l), len(out))
    out.insert(func_idx, f"\n━━━━ Functional Validation  ({fn_e}/{fn_p}) ━━━━")

    return "\n".join(header + out)


# ── Bot event handler ─────────────────────────────────────────────────────────

def _do_score(url: str, user_id: str, channel_id: str, client):
    """Run in a background thread: score repo and DM the result."""
    try:
        log.info("Scoring %s for user %s", url, user_id)
        results = score_repo(url)
        report  = _build_report(results, url)
    except Exception as exc:
        log.exception("Scoring crashed for %s", url)
        report = f"❌ Internal error while scoring:\n>`{exc}`"

    try:
        dm    = client.conversations_open(users=user_id)
        dm_id = dm["channel"]["id"]
        client.chat_postMessage(channel=dm_id, text=report, mrkdwn=True)
        client.chat_postMessage(
            channel=channel_id,
            text=f"✅ <@{user_id}> Scoring complete! Check your DMs for the full report.",
        )
    except Exception:
        log.exception("Failed to DM user %s", user_id)


@app.message(re.compile(r'\bsubmit\b.+https://github\.com/', re.IGNORECASE))
def handle_submission(message, say, client):
    text    = message.get("text", "")
    user_id = message["user"]
    chan_id = message["channel"]

    match = _GH_URL_RE.search(text)
    if not match:
        say(f"<@{user_id}> I couldn't find a GitHub URL. Try:\n"
            f"`submit https://github.com/you/repo`")
        return

    url = re.sub(r'/(?:tree|blob|commit|issues?|pulls?)/.*$', '',
                 match.group()).rstrip('/')

    say(
        f"⏳ <@{user_id}> Submission received! Scoring `{url}`…\n"
        f"_This includes running pytest and building Docker images — "
        f"expect results in ~3-5 minutes._"
    )

    threading.Thread(
        target=_do_score,
        args=(url, user_id, chan_id, client),
        daemon=True,
    ).start()


# ── Run ───────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    handler = SocketModeHandler(app, os.environ["SLACK_APP_TOKEN"])
    log.info("⚡ Stage-2 Scorer Bot running — waiting for submissions…")
    handler.start()
