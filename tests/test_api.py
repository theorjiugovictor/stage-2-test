"""
Unit tests for the Job Queue API.
Redis is fully mocked — no live Redis required.
"""
import json
import os
import sys
from unittest.mock import MagicMock, patch

import pytest

# Make `api/` importable from tests/
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "api"))

import main  # noqa: E402  (must come after sys.path manipulation)
from fastapi.testclient import TestClient  # noqa: E402


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture()
def mock_redis():
    """Patch main.get_redis for every test that requests this fixture."""
    with patch("main.get_redis") as mock_fn:
        redis_mock = MagicMock()
        mock_fn.return_value = redis_mock
        yield redis_mock


@pytest.fixture()
def client(mock_redis):  # noqa: F841 – mock_redis must be active before client is built
    """FastAPI test client with Redis patched out."""
    return TestClient(main.app)


# ---------------------------------------------------------------------------
# Health-check tests
# ---------------------------------------------------------------------------

def test_health_returns_200_when_redis_ok(client, mock_redis):
    """GET /health → 200 when Redis responds to ping."""
    mock_redis.ping.return_value = True

    resp = client.get("/health")

    assert resp.status_code == 200
    assert resp.json() == {"status": "healthy"}


def test_health_returns_503_when_redis_down(client, mock_redis):
    """GET /health → 503 when Redis raises an exception."""
    mock_redis.ping.side_effect = ConnectionError("Connection refused")

    resp = client.get("/health")

    assert resp.status_code == 503


# ---------------------------------------------------------------------------
# Job creation tests
# ---------------------------------------------------------------------------

def test_create_job_returns_pending_with_uuid(client, mock_redis):
    """POST /jobs → 200 with status='pending' and a UUID job_id."""
    mock_redis.set.return_value = True
    mock_redis.rpush.return_value = 1

    resp = client.post("/jobs", json={"payload": "hello world"})

    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "pending"
    assert "job_id" in data
    assert len(data["job_id"]) == 36  # UUID4 is 36 chars


def test_create_job_writes_to_redis(client, mock_redis):
    """POST /jobs must call redis.set() and redis.rpush() exactly once."""
    resp = client.post("/jobs", json={"payload": "test"})

    assert resp.status_code == 200
    mock_redis.set.assert_called_once()
    mock_redis.rpush.assert_called_once()


def test_create_job_stores_correct_payload(client, mock_redis):
    """The payload submitted by the client must be stored verbatim in Redis."""
    captured = {}

    def fake_set(key, value):
        captured["key"] = key
        captured["value"] = value

    mock_redis.set.side_effect = fake_set

    resp = client.post("/jobs", json={"payload": "my-special-payload"})

    assert resp.status_code == 200
    stored = json.loads(captured["value"])
    assert stored["payload"] == "my-special-payload"
    assert stored["status"] == "pending"


# ---------------------------------------------------------------------------
# Job retrieval tests
# ---------------------------------------------------------------------------

def test_get_existing_job_returns_data(client, mock_redis):
    """GET /jobs/{id} → 200 with the stored job document."""
    job_id = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
    job = {"id": job_id, "status": "completed", "payload": "done"}
    mock_redis.get.return_value = json.dumps(job)

    resp = client.get(f"/jobs/{job_id}")

    assert resp.status_code == 200
    body = resp.json()
    assert body["id"] == job_id
    assert body["status"] == "completed"


def test_get_missing_job_returns_404(client, mock_redis):
    """GET /jobs/{unknown-id} → 404."""
    mock_redis.get.return_value = None

    resp = client.get("/jobs/no-such-job-id")

    assert resp.status_code == 404
