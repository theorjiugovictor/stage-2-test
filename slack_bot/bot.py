"""
bot.py — Slack bot that scores Stage-2 containerisation submissions.

Usage in any channel the bot is in:
    submit https://github.com/username/their-repo

The bot acknowledges publicly, scores the repo in a background thread,
then DMs the full report to the submitting student.
"""

import logging
import os
import re
import threading

from dotenv import load_dotenv
from slack_bolt import App
from slack_bolt.adapter.socket_mode import SocketModeHandler

from scorer import POINTS, TOTAL_POSSIBLE, score_repo

load_dotenv()
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger(__name__)

app = App(token=os.environ["SLACK_BOT_TOKEN"])

_GH_URL_RE = re.compile(r'https://github\.com/[\w.\-]+/[\w.\-]+(?:/[^\s>]*)?')


# ── Formatting ────────────────────────────────────────────────────────────────

def _ok(passed: bool) -> str:
    return "✅" if passed else "❌"


def _pts(earned: int, possible: int) -> str:
    return f"({earned}/{possible})"


def format_report(results: dict, repo_url: str) -> str:
    if "error" in results:
        return f"❌ *Scoring failed*\n>{results['error']}"

    api_df   = results.get("api_dockerfile")
    fe_df    = results.get("fe_dockerfile")
    compose  = results.get("compose")
    pipeline = results.get("pipeline") or {}
    tests    = results.get("tests") or {}
    tc       = results.get("test_count", 0)

    lines = []
    s1_e = s1_p = s2_e = s2_p = 0

    def row(label, key, pts_dict, source):
        pts = pts_dict[key]
        passed = bool((source or {}).get(key, False))
        return passed, pts, f"  {_ok(passed)} {label} (+{pts})"

    # ── Section 1: Containerisation ──────────────────────────────────────────
    df_checks = [
        ("Multi-stage build",             "multi_stage"),
        ("Named non-root USER",           "named_nonroot_user"),
        ("HEALTHCHECK instruction",       "healthcheck"),
        ("No .env files COPY'd in",       "no_env_copied"),
        ("Slim / Alpine base image",      "slim_alpine_base"),
        ("User created via adduser/useradd", "user_creation_cmd"),
    ]

    for title, src in [("API Dockerfile", api_df), ("Frontend Dockerfile", fe_df)]:
        lines.append(f"\n*{title}*" + (" — ❌ _file not found_" if src is None else ""))
        sub_e = sub_p = 0
        for label, key in df_checks:
            passed, pts, line = row(label, key, POINTS, src)
            sub_e += pts if passed else 0
            sub_p += pts
            lines.append(line)
        s1_e += sub_e
        s1_p += sub_p

    compose_checks = [
        ("Named bridge network",              "named_network"),
        ("Redis NOT exposed on host",         "redis_no_host_ports"),
        ("depends_on: service_healthy",       "depends_on_healthy"),
        ("CPU + memory limits (all services)","resource_limits_all"),
        ("Restart policies (all services)",   "restart_policies"),
        ("All config via .env",               "all_config_via_env"),
    ]
    lines.append("\n*docker-compose.yml*" + (" — ❌ _file not found_" if compose is None else ""))
    for label, key in compose_checks:
        passed, pts, line = row(label, key, POINTS, compose)
        s1_e += pts if passed else 0
        s1_p += pts
        lines.append(line)

    # ── Section 2: CI/CD Pipeline ────────────────────────────────────────────
    pipeline_sections = [
        ("Lint", [
            ("flake8 (Python)",        "has_flake8",   pipeline),
            ("ESLint (JavaScript)",    "has_eslint",   pipeline),
            ("Hadolint (Dockerfiles)", "has_hadolint", pipeline),
        ]),
        ("Test", [
            ("pytest in pipeline",               "has_pytest",        pipeline),
            (f"≥3 unit tests  _(found: {tc})_",  "min_3_tests",       tests),
            ("Redis mocked in tests",            "redis_mocked",      tests),
            ("Coverage artifact uploaded",       "coverage_artifact", pipeline),
        ]),
        ("Build", [
            ("Local registry as service container", "local_registry_svc",  pipeline),
            ("Tagged with SHA + latest",            "sha_and_latest_tags", pipeline),
            ("Docker layer caching",               "layer_caching",       pipeline),
        ]),
        ("Security Scan", [
            ("Trivy scan",                "has_trivy",         pipeline),
            ("Fails on CRITICAL CVEs",    "fails_on_critical", pipeline),
            ("SARIF results uploaded",    "sarif_artifact",    pipeline),
        ]),
        ("Integration Test", [
            ("docker compose up in CI",      "compose_up_in_ci",    pipeline),
            ("integration_test.sh present",  "has_integration_sh",  tests),
            ("30 s timeout enforced",        "integration_timeout", tests),
            ("Stack torn down (if: always)", "teardown_always",     pipeline),
        ]),
        ("Deploy", [
            ("Triggers on main only",  "deploy_main_only", pipeline),
            ("Rolling update logic",   "rolling_update",   pipeline),
        ]),
        ("Pipeline Hygiene", [
            ("Secrets in GitHub secrets (not hardcoded)", "secrets_not_hardcoded", pipeline),
            ("Every step has a name: field",              "all_steps_named",       pipeline),
        ]),
    ]

    no_pipeline = results.get("pipeline") is None

    for stage, checks in pipeline_sections:
        lines.append(f"\n*{stage} Stage*" + (" — ❌ _no pipeline YAML found_" if no_pipeline else ""))
        for label, key, source in checks:
            pts = POINTS[key]
            passed = bool((source or {}).get(key, False))
            s2_e += pts if passed else 0
            s2_p += pts
            lines.append(f"  {_ok(passed)} {label} (+{pts})")

    # ── Header ────────────────────────────────────────────────────────────────
    total_e = s1_e + s2_e
    total_p = s1_p + s2_p
    pct = round(100 * total_e / total_p) if total_p else 0
    grade = (
        "A 🏆" if pct >= 90 else
        "B 👍" if pct >= 80 else
        "C 😐" if pct >= 70 else
        "D 😬" if pct >= 60 else
        "F 😞"
    )

    header = [
        f"📊 *Stage-2 Submission Score Report*",
        f"🔗 Repo: <{repo_url}|{repo_url}>",
        f"",
        f"*Total: {total_e}/{total_p}  ({pct}%)  —  Grade: {grade}*",
        f"",
        f"━━━━ Section 1 — Containerisation {_pts(s1_e, s1_p)} ━━━━",
    ]
    mid = [
        f"",
        f"━━━━ Section 2 — CI/CD Pipeline {_pts(s2_e, s2_p)} ━━━━",
    ]

    return "\n".join(header + lines[:lines.index("\n*docker-compose.yml*" if compose is None
                     else next(l for l in lines if 'docker-compose' in l)) + 1]
                     + lines) if False else "\n".join(header + lines[:] )
    # Simple join (the False branch above is unreachable; keeping join clean)


def _build_report(results, repo_url):
    """Build the full Slack-formatted report string."""
    if "error" in results:
        return f"❌ *Scoring failed*\n>{results['error']}"

    api_df   = results.get("api_dockerfile")
    fe_df    = results.get("fe_dockerfile")
    compose  = results.get("compose")
    pipeline = results.get("pipeline") or {}
    tests    = results.get("tests") or {}
    tc       = results.get("test_count", 0)

    out = []
    s1_e = s1_p = s2_e = s2_p = 0

    def add(label, key, pts_map, source, bucket):
        nonlocal s1_e, s1_p, s2_e, s2_p
        pts = pts_map[key]
        passed = bool((source or {}).get(key, False))
        if bucket == 1:
            s1_e += pts if passed else 0
            s1_p += pts
        else:
            s2_e += pts if passed else 0
            s2_p += pts
        out.append(f"  {'✅' if passed else '❌'} {label} (+{pts})")

    df_rows = [
        ("Multi-stage build",                  "multi_stage"),
        ("Named non-root USER",                "named_nonroot_user"),
        ("HEALTHCHECK instruction",            "healthcheck"),
        ("No .env files COPY'd in",            "no_env_copied"),
        ("Slim / Alpine base image",           "slim_alpine_base"),
        ("User created via adduser/useradd",   "user_creation_cmd"),
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

    no_pipe = results.get("pipeline") is None
    for stage, rows in [
        ("Lint", [
            ("flake8 (Python lint)",        "has_flake8",        pipeline),
            ("ESLint (JS lint)",            "has_eslint",        pipeline),
            ("Hadolint (Dockerfile lint)",  "has_hadolint",      pipeline),
        ]),
        ("Test", [
            ("pytest in pipeline",                        "has_pytest",        pipeline),
            (f"≥3 unit tests  _(found: {tc})_",           "min_3_tests",       tests),
            ("Redis mocked (no live Redis in tests)",     "redis_mocked",      tests),
            ("Coverage report uploaded as artifact",      "coverage_artifact", pipeline),
        ]),
        ("Build", [
            ("Local registry as service container",  "local_registry_svc",  pipeline),
            ("Images tagged SHA + latest",           "sha_and_latest_tags", pipeline),
            ("Docker layer caching configured",      "layer_caching",       pipeline),
        ]),
        ("Security Scan", [
            ("Trivy scan present",         "has_trivy",         pipeline),
            ("Fails on CRITICAL CVEs",     "fails_on_critical", pipeline),
            ("SARIF results as artifact",  "sarif_artifact",    pipeline),
        ]),
        ("Integration Test", [
            ("docker compose up in CI",       "compose_up_in_ci",    pipeline),
            ("integration_test.sh present",   "has_integration_sh",  tests),
            ("30 s timeout enforced",         "integration_timeout", tests),
            ("Teardown on if: always()",      "teardown_always",     pipeline),
        ]),
        ("Deploy", [
            ("main branch only",     "deploy_main_only", pipeline),
            ("Rolling update logic", "rolling_update",   pipeline),
        ]),
        ("Pipeline Hygiene", [
            ("Secrets in GH secrets (not hardcoded)", "secrets_not_hardcoded", pipeline),
            ("Every step has a name: field",           "all_steps_named",       pipeline),
        ]),
    ]:
        note = " — ❌ _no workflow YAML found_" if no_pipe else ""
        out.append(f"\n*{stage} Stage*{note}")
        for label, key, source in rows:
            add(label, key, POINTS, source, 2)

    total_e = s1_e + s2_e
    total_p = s1_p + s2_p
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
        "",
        f"━━━━ Section 1 — Containerisation  ({s1_e}/{s1_p}) ━━━━",
    ]

    # Insert section 2 divider before the first pipeline stage
    first_pipe_idx = next(
        (i for i, l in enumerate(out) if '*Lint Stage*' in l), len(out)
    )
    out.insert(first_pipe_idx, f"\n━━━━ Section 2 — CI/CD Pipeline  ({s2_e}/{s2_p}) ━━━━")

    return "\n".join(header + out)


# ── Bot event handler ─────────────────────────────────────────────────────────

def _do_score(url: str, user_id: str, channel_id: str, client):
    """Run in a background thread: score repo and DM the user."""
    try:
        log.info("Scoring %s for user %s", url, user_id)
        results = score_repo(url)
        report = _build_report(results, url)
    except Exception as exc:
        log.exception("Scoring crashed")
        report = f"❌ Internal error while scoring:\n>`{exc}`"

    try:
        dm = client.conversations_open(users=user_id)
        dm_id = dm["channel"]["id"]
        client.chat_postMessage(channel=dm_id, text=report, mrkdwn=True)
        client.chat_postMessage(
            channel=channel_id,
            text=f"✅ <@{user_id}> Done! Check your DMs for the full score report.",
        )
    except Exception:
        log.exception("Failed to send DM to %s", user_id)


@app.message(re.compile(r'\bsubmit\b.+https://github\.com/', re.IGNORECASE))
def handle_submission(message, say, client):
    text    = message.get("text", "")
    user_id = message["user"]
    chan_id = message["channel"]

    match = _GH_URL_RE.search(text)
    if not match:
        say(f"<@{user_id}> I couldn't find a GitHub URL. Try:\n`submit https://github.com/you/repo`")
        return

    # Normalise: strip /tree/…, /blob/…, trailing slashes
    url = re.sub(r'/(?:tree|blob|commit|issues?|pulls?)/.*$', '', match.group()).rstrip('/')

    say(f"⏳ <@{user_id}> Got it! Scoring `{url}` — I'll DM you the results shortly.")

    threading.Thread(
        target=_do_score,
        args=(url, user_id, chan_id, client),
        daemon=True,
    ).start()


# ── Run ───────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    handler = SocketModeHandler(app, os.environ["SLACK_APP_TOKEN"])
    log.info("⚡ Stage-2 Scorer Bot is running — waiting for submissions…")
    handler.start()
