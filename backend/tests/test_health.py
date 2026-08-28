"""
Smoke tests for Phase 01 - Backend Project Setup.

These tests validate that the FastAPI application boots correctly
and that the basic root/health endpoints respond as expected. This
gives CI a fast signal that the project scaffold itself is not
broken, before any business-logic endpoints exist.
"""

from fastapi.testclient import TestClient

from app.main import app

client = TestClient(app)


def test_read_root():
    """
    The application's root/liveness endpoint should confirm the API is
    running.

    NOTE: The current application registers no literal "/" route --
    verified by enumerating `app.routes` directly, the only top-level
    (non-`/api/v1`-prefixed) route is `GET /health` (app/main.py). There
    is no endpoint anywhere returning a `{"status": "running",
    "project": ...}` shape; that was never implemented. `/health` is
    the actual, sole supported basic-liveness endpoint, so this test
    targets it and asserts its real response shape.
    """
    response = client.get("/health")
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ok"


def test_health_check():
    """The /health endpoint should report status ok for liveness probes."""
    response = client.get("/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}