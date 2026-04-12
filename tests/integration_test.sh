#!/usr/bin/env bash
# integration_test.sh
#
# End-to-end integration test:
#   1. Submit a job through the frontend
#   2. Poll for completion
#   3. Assert final status is "completed"
#
# Exits 0 on success, 1 on failure or timeout.
#
# Environment variables:
#   FRONTEND_URL  – base URL of the frontend service (default: http://localhost:3000)
#   JOB_TIMEOUT   – seconds to wait for completion (default: 30)

set -euo pipefail

FRONTEND_URL="${FRONTEND_URL:-http://localhost:3000}"
JOB_TIMEOUT="${JOB_TIMEOUT:-30}"

echo "==> Integration test started"
echo "    FRONTEND_URL : ${FRONTEND_URL}"
echo "    JOB_TIMEOUT  : ${JOB_TIMEOUT}s"

# ── 1. Submit a job ───────────────────────────────────────────────────────────
echo ""
echo "==> Submitting job…"
SUBMIT_RESPONSE=$(curl -sf \
  -X POST "${FRONTEND_URL}/api/jobs" \
  -H "Content-Type: application/json" \
  -d '{"payload":"integration-test-payload"}')

echo "    Response: ${SUBMIT_RESPONSE}"

JOB_ID=$(echo "${SUBMIT_RESPONSE}" | jq -r '.job_id')
if [[ -z "${JOB_ID}" || "${JOB_ID}" == "null" ]]; then
  echo "ERROR: Could not extract job_id from response."
  exit 1
fi
echo "    Job ID   : ${JOB_ID}"

# ── 2. Poll until completed or timeout ───────────────────────────────────────
echo ""
echo "==> Polling for completion (timeout: ${JOB_TIMEOUT}s)…"
START_TIME=$(date +%s)

while true; do
  NOW=$(date +%s)
  ELAPSED=$(( NOW - START_TIME ))

  if (( ELAPSED >= JOB_TIMEOUT )); then
    echo "ERROR: Job did not complete within ${JOB_TIMEOUT} seconds."
    echo "       Timed out after ${ELAPSED}s."
    exit 1
  fi

  STATUS_RESPONSE=$(curl -sf "${FRONTEND_URL}/api/jobs/${JOB_ID}" || true)
  STATUS=$(echo "${STATUS_RESPONSE}" | jq -r '.status' 2>/dev/null || echo "unknown")

  echo "    [${ELAPSED}s] status = ${STATUS}"

  if [[ "${STATUS}" == "completed" ]]; then
    echo ""
    echo "==> SUCCESS: Job ${JOB_ID} completed after ${ELAPSED}s."
    exit 0
  fi

  sleep 2
done
