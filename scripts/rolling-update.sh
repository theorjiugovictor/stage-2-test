#!/usr/bin/env bash
# rolling-update.sh
#
# Scripted rolling update for the job-queue stack.
#
# Usage:
#   rolling-update.sh <image-tag> [health-timeout-seconds]
#
# Behaviour:
#   1. Start a new container alongside the old one.
#   2. Poll Docker's built-in health status for up to <health-timeout> seconds.
#   3. If healthy:   stop and remove the old container; rename new → current.
#   4. If not healthy: stop and remove the new container; old stays running.
#
# Exit codes:
#   0  – successful roll-out
#   1  – health check timed out; old container preserved

set -euo pipefail

IMAGE_TAG="${1:?Usage: rolling-update.sh <image-tag> [timeout]}"
HEALTH_TIMEOUT="${2:-60}"

# Services to roll (name, image, extra docker run flags)
declare -A IMAGES=(
  [api]="api:${IMAGE_TAG}"
  [frontend]="frontend:${IMAGE_TAG}"
)

REDIS_HOST="${REDIS_HOST:-redis}"
REDIS_PORT="${REDIS_PORT:-6379}"
FRONTEND_PORT="${FRONTEND_PORT:-3000}"
API_URL="${API_URL:-http://api:8000}"
NETWORK="jobqueue_app-network"

roll_service() {
  local SERVICE="$1"
  local IMAGE="$2"
  local -a EXTRA_ARGS=("${@:3}")

  local NEW="${SERVICE}-new"
  local CURRENT="${SERVICE}-current"

  echo ""
  echo "━━━ Rolling update: ${SERVICE} → ${IMAGE} ━━━"

  # Stop any leftover new container from a previous failed attempt
  if docker ps -aq --filter name="^/${NEW}$" | grep -q .; then
    echo "  Removing stale container: ${NEW}"
    docker rm -f "${NEW}" >/dev/null
  fi

  echo "  Starting new container: ${NEW}"
  docker run -d \
    --name "${NEW}" \
    --network "${NETWORK}" \
    "${EXTRA_ARGS[@]}" \
    "${IMAGE}"

  # Poll health status
  echo "  Waiting for health check (timeout: ${HEALTH_TIMEOUT}s)…"
  local ELAPSED=0
  while (( ELAPSED < HEALTH_TIMEOUT )); do
    local HEALTH
    HEALTH=$(docker inspect --format='{{if .State.Health}}{{.State.Health.Status}}{{else}}none{{end}}' \
             "${NEW}" 2>/dev/null || echo "inspect-failed")

    echo "  [${ELAPSED}s] health=${HEALTH}"

    if [[ "${HEALTH}" == "healthy" ]]; then
      echo "  Health check passed after ${ELAPSED}s."

      # Stop the old container (if running)
      if docker ps -q --filter name="^/${CURRENT}$" | grep -q .; then
        echo "  Stopping old container: ${CURRENT}"
        docker stop "${CURRENT}" >/dev/null
        docker rm   "${CURRENT}" >/dev/null
      fi

      docker rename "${NEW}" "${CURRENT}"
      echo "  ✓ ${SERVICE} successfully updated to ${IMAGE}"
      return 0
    fi

    sleep 2
    ELAPSED=$(( ELAPSED + 2 ))
  done

  # Health check failed → abort; old container stays
  echo "  ERROR: Health check did not pass within ${HEALTH_TIMEOUT}s."
  echo "  Stopping new container; old container (${CURRENT}) remains running."
  docker stop "${NEW}" >/dev/null 2>&1 || true
  docker rm   "${NEW}" >/dev/null 2>&1 || true
  return 1
}

# Ensure the bridge network exists (created by compose on first run)
if ! docker network inspect "${NETWORK}" >/dev/null 2>&1; then
  echo "Creating network ${NETWORK}…"
  docker network create --driver bridge "${NETWORK}"
fi

FAILED=0

roll_service "api" "api:${IMAGE_TAG}" \
  -e "REDIS_HOST=${REDIS_HOST}" \
  -e "REDIS_PORT=${REDIS_PORT}" \
  || FAILED=1

roll_service "frontend" "frontend:${IMAGE_TAG}" \
  -p "${FRONTEND_PORT}:3000" \
  -e "API_URL=${API_URL}" \
  || FAILED=1

if (( FAILED )); then
  echo ""
  echo "ERROR: Rolling update failed for one or more services."
  exit 1
fi

echo ""
echo "✓ Rolling update complete for all services."
