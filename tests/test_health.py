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
    """The root endpoint should confirm the API is running."""
    response = client.get("/")
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "running"
    assert "project" in body


def test_health_check():
    """The /health endpoint should report status ok for liveness probes."""
    response = client.get("/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}