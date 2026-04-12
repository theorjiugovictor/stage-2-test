# FIXES

This document records every bug identified during development, including the
file, line, description, and resolution.

---

## FIX-001 — `blpop` return value not checked before unpacking

**File:** `api/main.py`
**Line (draft):** worker loop, `_, job_id = r.blpop("job_queue", timeout=5)`
**Bug:** `redis.Redis.blpop` returns `None` when the timeout expires with no
items in the queue.  Destructuring `None` as a two-tuple raises
`TypeError: cannot unpack non-iterable NoneType object`, crashing the worker
thread permanently.

**Fix:** Capture the return value first and guard with an explicit `None`
check before unpacking:

```python
result = r.blpop("job_queue", timeout=5)
if result is None:
    continue
_, job_id = result
```

---

## FIX-002 — Worker thread started unconditionally, breaking unit tests

**File:** `api/main.py`
**Line (draft):** `@app.on_event("startup")` handler, `thread.start()`
**Bug:** The background worker calls `get_redis()` at startup.  During unit
tests there is no live Redis, so the thread raised connection errors in the
background and polluted test output.  More critically, when `get_redis` was
patched, the patch was not always applied before the thread spawned.

**Fix:** Gate the worker behind the `WORKER_ENABLED` environment variable
(default `"true"`).  Tests set `WORKER_ENABLED=false` via `conftest.py`
before importing the module:

```python
WORKER_ENABLED = os.getenv("WORKER_ENABLED", "true").lower() == "true"

@app.on_event("startup")
def start_worker() -> None:
    if WORKER_ENABLED:
        thread = threading.Thread(target=_worker_loop, daemon=True)
        thread.start()
```

---

## FIX-003 — `HTTPException` raised without chaining the original exception

**File:** `api/main.py`
**Line (draft):** `health_check()`, `raise HTTPException(status_code=503, detail=str(exc))`
**Bug:** Raising a new exception without `from exc` discards the original
traceback, making debugging harder and suppressing implicit exception chaining
warnings under `python -W error`.

**Fix:** Use `raise … from exc`:

```python
raise HTTPException(status_code=503, detail=str(exc)) from exc
```

---

## FIX-004 — Docker Compose resource limits missing from `.env`

**File:** `docker-compose.yml`, `.env.example`
**Line (draft):** `cpus:` and `memory:` values were hardcoded strings in the
YAML (`cpus: '0.5'`, `memory: 128M`).
**Bug:** Violates the requirement "nothing hardcoded in the Compose YAML —
all configuration passed via a .env file."

**Fix:** Replaced hardcoded values with env-var interpolation:

```yaml
deploy:
  resources:
    limits:
      cpus: "${API_CPU_LIMIT}"
      memory: "${API_MEMORY_LIMIT}"
```

and added the corresponding variables to `.env.example`.

---

## FIX-005 — `depends_on` used bare service names instead of health conditions

**File:** `docker-compose.yml`
**Line (draft):** `depends_on: [redis]` and `depends_on: [api]`
**Bug:** Bare `depends_on` only waits for the container to *start*, not to
become *healthy*.  The API started before Redis was ready, causing immediate
connection errors.

**Fix:** Use the long-form condition syntax:

```yaml
depends_on:
  redis:
    condition: service_healthy
```

---

## FIX-006 — Redis exposed on the host via `ports` mapping

**File:** `docker-compose.yml` (draft version)
**Line (draft):** under the `redis:` service, `ports: ["6379:6379"]`
**Bug:** Mapping port 6379 to the host exposes Redis to any process on the
host (and potentially to the network), violating the "Redis must not be
exposed on the host" requirement.

**Fix:** Remove the `ports:` block from the `redis:` service entirely.
Redis remains reachable by name on the internal Docker bridge network but is
not accessible from the host.

---

## FIX-007 — Frontend Dockerfile used `npm install` instead of `npm ci`

**File:** `frontend/Dockerfile`
**Line (draft):** `RUN npm install --only=production`
**Bug:** `npm install` modifies `package-lock.json` and may resolve different
dependency versions than those recorded in lock-file, producing non-reproducible
images.  `--only=production` is also deprecated in npm ≥ 7.

**Fix:** Use `npm ci --omit=dev`, which installs the exact locked versions and
fails if the lock-file is inconsistent:

```dockerfile
RUN npm ci --omit=dev
```

---

## FIX-008 — Python image used `root` user in the final stage

**File:** `api/Dockerfile` (draft)
**Bug:** No `USER` instruction was present; the process ran as `root` inside
the container, violating least-privilege principles and the requirement for a
named non-root user.

**Fix:** Create a named system user/group and switch to it before the `CMD`:

```dockerfile
RUN groupadd --system appuser \
    && useradd --system --gid appuser --no-create-home appuser
USER appuser
```

---

## FIX-009 — `HEALTHCHECK` missing from both Dockerfiles

**File:** `api/Dockerfile`, `frontend/Dockerfile`
**Bug:** Without a `HEALTHCHECK` instruction, Docker (and Docker Compose)
cannot determine whether a container is actually ready.  The `depends_on:
condition: service_healthy` in docker-compose.yml would block forever.

**Fix:** Added `HEALTHCHECK` to both images.

API (uses Python's built-in `urllib` to avoid installing `curl`):
```dockerfile
HEALTHCHECK --interval=30s --timeout=10s --start-period=15s --retries=3 \
  CMD python -c \
    "import urllib.request; urllib.request.urlopen('http://localhost:8000/health')" \
    || exit 1
```

Frontend (uses Node.js `http` module):
```dockerfile
HEALTHCHECK --interval=30s --timeout=10s --start-period=15s --retries=3 \
  CMD node -e \
    "require('http').get('http://localhost:3000/health',r=>process.exit(r.statusCode===200?0:1)).on('error',()=>process.exit(1))"
```

---

## FIX-010 — Integration test did not time out, looping forever on failure

**File:** `tests/integration_test.sh` (draft)
**Bug:** The poll loop used `while true` with no deadline, so a failed
deployment would cause the CI job to run until the runner's 6-hour limit,
wasting resources.

**Fix:** Record `START_TIME=$(date +%s)` before the loop and compare elapsed
seconds against `JOB_TIMEOUT` (default 30) at the top of each iteration,
exiting with code 1 on expiry.
