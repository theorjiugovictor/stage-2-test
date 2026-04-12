import json
import logging
import os
import threading
import time
import uuid

import redis
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
logger = logging.getLogger(__name__)

REDIS_HOST = os.getenv("REDIS_HOST", "redis")
REDIS_PORT = int(os.getenv("REDIS_PORT", "6379"))
WORKER_ENABLED = os.getenv("WORKER_ENABLED", "true").lower() == "true"

app = FastAPI(title="Job Queue API", version="1.0.0")


def get_redis() -> redis.Redis:
    return redis.Redis(host=REDIS_HOST, port=REDIS_PORT, decode_responses=True)


class JobRequest(BaseModel):
    payload: str = ""


@app.get("/health")
def health_check():
    """Liveness + readiness probe: verifies Redis connectivity."""
    try:
        r = get_redis()
        r.ping()
        return {"status": "healthy"}
    except Exception as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc


@app.post("/jobs")
def create_job(req: JobRequest):
    """Submit a new job. Returns job_id and initial status 'pending'."""
    job_id = str(uuid.uuid4())
    r = get_redis()
    job = {"id": job_id, "status": "pending", "payload": req.payload}
    r.set(f"job:{job_id}", json.dumps(job))
    r.rpush("job_queue", job_id)
    logger.info("Created job %s", job_id)
    return {"job_id": job_id, "status": "pending"}


@app.get("/jobs/{job_id}")
def get_job(job_id: str):
    """Retrieve job status by ID."""
    r = get_redis()
    data = r.get(f"job:{job_id}")
    if not data:
        raise HTTPException(status_code=404, detail="Job not found")
    return json.loads(data)


def _worker_loop() -> None:
    """Background worker: pops jobs from Redis queue and processes them."""
    logger.info("Worker thread started")
    while True:
        try:
            r = get_redis()
            result = r.blpop("job_queue", timeout=5)
            if result is None:
                continue
            _, job_id = result
            raw = r.get(f"job:{job_id}")
            if not raw:
                logger.warning("Job %s not found in store, skipping", job_id)
                continue
            job = json.loads(raw)
            job["status"] = "processing"
            r.set(f"job:{job_id}", json.dumps(job))
            time.sleep(1)  # Simulate work
            job["status"] = "completed"
            r.set(f"job:{job_id}", json.dumps(job))
            logger.info("Job %s completed", job_id)
        except Exception:
            logger.exception("Worker error; retrying in 1 s")
            time.sleep(1)


@app.on_event("startup")
def start_worker() -> None:
    if WORKER_ENABLED:
        thread = threading.Thread(target=_worker_loop, name="job-worker", daemon=True)
        thread.start()
