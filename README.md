# Job Queue – Full-Stack Containerised Application

A minimal job-queue system composed of three services:

| Service | Technology | Role |
|---------|-----------|------|
| `api` | Python 3.11 + FastAPI | Accepts job submissions, stores state in Redis, runs a background worker |
| `frontend` | Node.js 20 + Express | Serves the UI and proxies requests to the API |
| `redis` | Redis 7 (Alpine) | Job queue and state store |

---

## Prerequisites

| Tool | Minimum version |
|------|----------------|
| Docker | 24.x |
| Docker Compose plugin | 2.24.x |
| `git` | any recent |

No other tools are required on the host machine.

---

## Bring the stack up from scratch

```bash
# 1. Clone the repository
git clone https://github.com/theorjiugovictor/stage-2-test.git
cd stage-2-test

# 2. Create your local .env (edit values if needed)
cp .env.example .env

# 3. Build images and start all services
docker compose up --build -d

# 4. Verify all containers are healthy
docker compose ps
```

The frontend is available at **http://localhost:3000** (or whatever
`FRONTEND_PORT` is set to in `.env`).

---

## Environment variables

All configuration lives in `.env`.  See `.env.example` for the full list with
defaults.  Key variables:

| Variable | Default | Description |
|----------|---------|-------------|
| `FRONTEND_PORT` | `3000` | Host port for the UI |
| `REDIS_HOST` | `redis` | Redis hostname (Docker service name) |
| `API_URL` | `http://api:8000` | URL the frontend uses to reach the API |
| `API_CPU_LIMIT` | `1.00` | CPU quota for the API container |
| `API_MEMORY_LIMIT` | `256M` | Memory limit for the API container |

---

## Running the unit tests locally

```bash
pip install -r api/requirements.txt pytest pytest-cov
pytest tests/test_api.py -v --cov=api
```

No Redis instance is needed — Redis is fully mocked.

---

## Running the integration test locally

Start the stack first, then:

```bash
FRONTEND_URL=http://localhost:3000 bash tests/integration_test.sh
```

The script submits a job, polls for completion, and asserts the final status
is `completed`.  It times out after 30 seconds.

---

## Stopping the stack

```bash
docker compose down --volumes
```

---

## CI/CD Pipeline

The GitHub Actions pipeline (`.github/workflows/pipeline.yml`) runs the
following stages in strict order:

```
lint → test → build → security-scan → integration-test → deploy
```

Any failure stops all subsequent stages.  The `deploy` stage only runs on
pushes to `main`.

### Required GitHub Actions secrets

| Secret | Purpose |
|--------|---------|
| *(none required for the demo pipeline)* | The pipeline uses a local ephemeral registry; no external registry credentials are needed. |

For a real production deployment you would add:

| Secret | Purpose |
|--------|---------|
| `DEPLOY_SSH_KEY` | Private key for SSH access to the production host |
| `DEPLOY_HOST` | Hostname / IP of the production server |
| `REGISTRY_USERNAME` | Docker registry username |
| `REGISTRY_PASSWORD` | Docker registry password / token |

---

## Project layout

```
.
├── api/
│   ├── Dockerfile          # Multi-stage; final image runs as non-root 'appuser'
│   ├── main.py             # FastAPI application + background worker
│   └── requirements.txt
├── frontend/
│   ├── Dockerfile          # Multi-stage; final image runs as non-root 'appuser'
│   ├── server.js           # Express proxy + static file server
│   ├── package.json
│   ├── .eslintrc.json
│   └── public/
│       └── index.html
├── tests/
│   ├── conftest.py         # Pytest fixtures / env setup
│   ├── test_api.py         # ≥ 6 unit tests (Redis mocked)
│   └── integration_test.sh # End-to-end test script
├── scripts/
│   └── rolling-update.sh   # Scripted rolling deploy helper
├── .github/
│   └── workflows/
│       └── pipeline.yml    # CI/CD pipeline
├── docker-compose.yml
├── .env.example
├── FIXES.md
└── README.md
```
