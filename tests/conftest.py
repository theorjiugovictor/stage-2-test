"""
Pytest configuration: set environment variables before any module is imported
so the API worker thread does not start and no real Redis is required.
"""
import os

os.environ.setdefault("WORKER_ENABLED", "false")
os.environ.setdefault("REDIS_HOST", "localhost")
os.environ.setdefault("REDIS_PORT", "6379")
