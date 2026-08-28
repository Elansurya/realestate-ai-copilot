"""
backend/tests/test_integration_api.py

Tests for the Integration Management API router
(`backend/app/api/v1/integration.py`).

Verified against: backend/app/api/v1/integration.py,
backend/app/schemas/integration.py, backend/app/models/integration.py.

Scope and a documented limitation:
    - `get_current_user` and `require_roles` are imported by the router
      from `app.api.deps` (a thin compatibility re-export of
      `app.api.dependencies.auth_dependency.get_current_user` and
      `app.api.dependencies.rbac.require_roles`). `require_roles`
      itself depends on `get_current_user` via `Depends(...)` and
      compares `current_user.role` (an `app.models.user.UserRole`
      member) against the route's allow-list, raising `HTTPException`
      (403) on mismatch. Because the test app only overrides
      `get_current_user` (not `require_roles`), the real
      `require_roles` dependency still executes on every request, so
      the stub user returned by the override must expose a genuine
      `UserRole` value via `.role` -- `INTEGRATION_WRITE_ROLES` is
      `(UserRole.ADMIN,)` and `INTEGRATION_READ_ROLES` is
      `(UserRole.ADMIN, UserRole.SALES_MANAGER)`, so `UserRole.ADMIN`
      satisfies every route exercised below. These tests do not
      attempt to assert specific RBAC-denial (403) behavior for a
      *different* role, since that would require a second stub/client
      wired up purely to exercise `require_roles`'s rejection branch,
      which is out of scope for these router-wiring-focused tests.
    - `app.core.exceptions.register_exception_handlers` (referenced by
      the service's own docstring) is likewise not one of the
      referenced files, so the exact `AppException` -> HTTP status
      mapping could not be verified. These tests run the router in
      isolation with `IntegrationService` mocked, so the exception
      translation layer is never exercised; endpoints that would
      surface a service-raised exception are instead verified at the
      service-test level (`test_integration_service.py`).
    - The `get_db` dependency is overridden with a stub session whose
      `commit()` is an `AsyncMock`, since only the commit boundary
      (not real persistence) is relevant to router-level tests.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from unittest.mock import AsyncMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api.v1.integration import (
    get_current_user,
    get_db,
    get_integration_service,
    router,
)
from app.models.integration import (
    AuthenticationType,
    Integration,
    IntegrationProvider,
    IntegrationStatus,
    IntegrationType,
)
from app.models.user import UserRole
from app.schemas.integration import (
    IntegrationHealthCheck,
    IntegrationListResponse,
    IntegrationStatisticsResponse,
)


# ---------------------------------------------------------------------------
# Helpers / fixtures
# ---------------------------------------------------------------------------
class _StubUser:
    """Minimal stand-in for the authenticated `User` the router expects.

    Attributes:
        id: A numeric user id, mirroring `Integration.created_by_id`'s
            `Integer` FK type.
        role: A real `app.models.user.UserRole` member. The real
            `require_roles` dependency (`app.api.dependencies.rbac`)
            is NOT overridden by this test app -- only
            `get_current_user` is -- so it genuinely runs on every
            request and checks `current_user.role not in
            allowed_roles`. `UserRole.ADMIN` is a member of both
            `INTEGRATION_WRITE_ROLES` and `INTEGRATION_READ_ROLES`
            (see `app/api/v1/integration.py`), so it satisfies every
            route exercised in this file.
    """

    id = 1
    role = UserRole.ADMIN


def _make_integration(**overrides) -> Integration:
    """Builds an in-memory `Integration` ORM instance for mocked responses.

    Args:
        **overrides: Attribute overrides applied after defaults.

    Returns:
        Integration: An unpersisted ORM instance.
    """
    now = datetime.now(timezone.utc)
    integration = Integration(
        id=overrides.pop("id", uuid.uuid4()),
        name=overrides.pop("name", "Primary SMTP"),
        provider=overrides.pop("provider", IntegrationProvider.SMTP),
        integration_type=overrides.pop("integration_type", IntegrationType.EMAIL),
        status=overrides.pop("status", IntegrationStatus.PENDING_VERIFICATION),
        authentication_type=overrides.pop(
            "authentication_type", AuthenticationType.BASIC_AUTH
        ),
        configuration=overrides.pop(
            "configuration", {"host": "smtp.example.com", "port": 587}
        ),
        credentials=overrides.pop(
            "credentials", {"username": "user", "password": "pass"}
        ),
        base_url=overrides.pop("base_url", None),
        api_version=overrides.pop("api_version", None),
        webhook_url=overrides.pop("webhook_url", None),
        timeout_seconds=overrides.pop("timeout_seconds", 30),
        retry_count=overrides.pop("retry_count", 3),
        rate_limit_per_minute=overrides.pop("rate_limit_per_minute", None),
        is_default=overrides.pop("is_default", False),
        last_sync_at=overrides.pop("last_sync_at", None),
        last_health_check_at=overrides.pop("last_health_check_at", None),
        created_by_id=overrides.pop("created_by_id", 1),
        is_deleted=overrides.pop("is_deleted", False),
        deleted_at=overrides.pop("deleted_at", None),
        created_at=overrides.pop("created_at", now),
        updated_at=overrides.pop("updated_at", now),
    )
    for key, value in overrides.items():
        setattr(integration, key, value)
    return integration


@pytest.fixture
def mock_service() -> AsyncMock:
    """Builds a fully-mocked `IntegrationService`.

    Returns:
        AsyncMock: A mock exposing every `IntegrationService` method
        used by the router as an `AsyncMock`.
    """
    return AsyncMock()


@pytest.fixture
def client(mock_service: AsyncMock) -> TestClient:
    """Builds a `TestClient` around an isolated app exposing only the router.

    Overrides `get_current_user` with a stub user, `get_db` with a stub
    session, and `get_integration_service` with the mocked service, so
    only router-level wiring (path/query binding, status codes,
    response-model shape) is under test.

    Args:
        mock_service: The mocked `IntegrationService` fixture.

    Returns:
        TestClient: A synchronous test client for the isolated app.
    """
    app = FastAPI()
    app.include_router(router, prefix="/api/v1")

    stub_session = AsyncMock()

    app.dependency_overrides[get_current_user] = lambda: _StubUser()
    app.dependency_overrides[get_db] = lambda: stub_session
    app.dependency_overrides[get_integration_service] = lambda: mock_service

    return TestClient(app)


# ---------------------------------------------------------------------------
# POST /integrations
# ---------------------------------------------------------------------------
def test_create_integration_returns_201(client, mock_service):
    """`POST /integrations` returns 201 with the created integration."""
    mock_service.create_integration.return_value = _make_integration()

    response = client.post(
        "/api/v1/integrations",
        json={
            "name": "Primary SMTP",
            "provider": "smtp",
            "integration_type": "email",
            "authentication_type": "basic_auth",
            "configuration": {"host": "smtp.example.com", "port": 587},
            "credentials": {"username": "user", "password": "pass"},
        },
    )

    assert response.status_code == 201
    body = response.json()
    assert body["name"] == "Primary SMTP"
    assert "credentials" not in body
    assert body["has_credentials"] is True


def test_create_integration_rejects_invalid_schema(client, mock_service):
    """`POST /integrations` returns 422 for a payload failing schema validation."""
    response = client.post(
        "/api/v1/integrations",
        json={
            "name": "X",  # below MIN_NAME_LENGTH
            "provider": "smtp",
            "integration_type": "email",
        },
    )

    assert response.status_code == 422
    mock_service.create_integration.assert_not_called()


# ---------------------------------------------------------------------------
# GET /integrations
# ---------------------------------------------------------------------------
def test_list_integrations_returns_200(client, mock_service):
    """`GET /integrations` returns 200 with a paginated listing."""
    mock_service.list_integrations.return_value = IntegrationListResponse(
        items=[], total=0, page=1, page_size=20, total_pages=0
    )

    response = client.get("/api/v1/integrations")

    assert response.status_code == 200
    body = response.json()
    assert body["items"] == []
    assert body["total"] == 0


def test_list_integrations_forwards_query_params(client, mock_service):
    """`GET /integrations` forwards filter/sort/pagination query params to the service."""
    mock_service.list_integrations.return_value = IntegrationListResponse(
        items=[], total=0, page=2, page_size=10, total_pages=0
    )

    response = client.get(
        "/api/v1/integrations",
        params={
            "integration_type": "email",
            "provider": "smtp",
            "status": "active",
            "page": 2,
            "page_size": 10,
            "sort_by": "name",
            "sort_order": "asc",
            "search": "primary",
        },
    )

    assert response.status_code == 200
    mock_service.list_integrations.assert_awaited_once()
    _, kwargs = mock_service.list_integrations.call_args
    assert kwargs["filters"].integration_type == IntegrationType.EMAIL
    assert kwargs["filters"].provider == IntegrationProvider.SMTP
    assert kwargs["filters"].status == IntegrationStatus.ACTIVE
    assert kwargs["filters"].search == "primary"
    assert kwargs["pagination"].page == 2
    assert kwargs["pagination"].page_size == 10
    assert kwargs["sorting"].sort_by == "name"
    assert kwargs["sorting"].sort_order == "asc"


# ---------------------------------------------------------------------------
# GET /integrations/{integration_id}
# ---------------------------------------------------------------------------
def test_get_integration_returns_200(client, mock_service):
    """`GET /integrations/{id}` returns 200 with the requested integration."""
    integration = _make_integration()
    mock_service.get_integration.return_value = integration

    response = client.get(f"/api/v1/integrations/{integration.id}")

    assert response.status_code == 200
    assert response.json()["id"] == str(integration.id)


def test_get_integration_rejects_malformed_uuid(client, mock_service):
    """`GET /integrations/{id}` returns 422 for a non-UUID path parameter."""
    response = client.get("/api/v1/integrations/not-a-uuid")

    assert response.status_code == 422
    mock_service.get_integration.assert_not_called()


# ---------------------------------------------------------------------------
# PUT /integrations/{integration_id}
# ---------------------------------------------------------------------------
def test_update_integration_returns_200(client, mock_service):
    """`PUT /integrations/{id}` returns 200 with the updated integration."""
    integration = _make_integration(timeout_seconds=45)
    mock_service.update_integration.return_value = integration

    response = client.put(
        f"/api/v1/integrations/{integration.id}",
        json={"timeout_seconds": 45},
    )

    assert response.status_code == 200
    assert response.json()["timeout_seconds"] == 45


# ---------------------------------------------------------------------------
# PATCH /integrations/{integration_id}/enable|disable|status
# ---------------------------------------------------------------------------
def test_enable_integration_returns_200(client, mock_service):
    """`PATCH /integrations/{id}/enable` returns 200 with `status="active"`."""
    integration = _make_integration(status=IntegrationStatus.ACTIVE)
    mock_service.enable_integration.return_value = integration

    response = client.patch(f"/api/v1/integrations/{integration.id}/enable")

    assert response.status_code == 200
    assert response.json()["status"] == "active"


def test_disable_integration_returns_200(client, mock_service):
    """`PATCH /integrations/{id}/disable` returns 200 with `status="inactive"`."""
    integration = _make_integration(status=IntegrationStatus.INACTIVE)
    mock_service.disable_integration.return_value = integration

    response = client.patch(f"/api/v1/integrations/{integration.id}/disable")

    assert response.status_code == 200
    assert response.json()["status"] == "inactive"


def test_update_integration_status_returns_200(client, mock_service):
    """`PATCH /integrations/{id}/status` returns 200 with the new status."""
    integration = _make_integration(status=IntegrationStatus.FAILED)
    mock_service.update_status.return_value = integration

    response = client.patch(
        f"/api/v1/integrations/{integration.id}/status",
        json={"status": "failed", "reason": "provider outage"},
    )

    assert response.status_code == 200
    assert response.json()["status"] == "failed"


# ---------------------------------------------------------------------------
# POST /integrations/{integration_id}/test-connection|health-check
# ---------------------------------------------------------------------------
def test_test_connection_returns_200(client, mock_service):
    """`POST /integrations/{id}/test-connection` returns 200 with the health-check outcome."""
    integration_id = uuid.uuid4()
    mock_service.test_connection.return_value = IntegrationHealthCheck(
        integration_id=integration_id,
        is_healthy=True,
        status=IntegrationStatus.ACTIVE,
        checked_at=datetime.now(timezone.utc),
        latency_ms=None,
        message="Integration is structurally ready; live connectivity was not tested.",
    )

    response = client.post(f"/api/v1/integrations/{integration_id}/test-connection")

    assert response.status_code == 200
    assert response.json()["is_healthy"] is True


def test_health_check_integration_returns_200_and_commits(client, mock_service):
    """`POST /integrations/{id}/health-check` returns 200 and commits the session."""
    integration_id = uuid.uuid4()
    mock_service.perform_health_check.return_value = IntegrationHealthCheck(
        integration_id=integration_id,
        is_healthy=False,
        status=IntegrationStatus.FAILED,
        checked_at=datetime.now(timezone.utc),
        latency_ms=None,
        message="configuration for provider 'aws_s3' is missing required key(s): bucket_name.",
    )

    response = client.post(f"/api/v1/integrations/{integration_id}/health-check")

    assert response.status_code == 200
    assert response.json()["status"] == "failed"


# ---------------------------------------------------------------------------
# GET /integrations/statistics
# ---------------------------------------------------------------------------
def test_get_integration_statistics_returns_200(client, mock_service):
    """`GET /integrations/statistics` returns 200 with the aggregate statistics."""
    mock_service.get_statistics.return_value = IntegrationStatisticsResponse(
        total_integrations=3,
        by_type={"email": 3},
        by_provider={"smtp": 3},
        by_status={"active": 3},
        by_authentication_type={"basic_auth": 3},
        active_count=3,
        failed_count=0,
        default_count=1,
    )

    response = client.get("/api/v1/integrations/statistics")

    assert response.status_code == 200
    body = response.json()
    assert body["total_integrations"] == 3
    assert body["active_count"] == 3


def test_statistics_route_is_not_shadowed_by_id_route(client, mock_service):
    """`/integrations/statistics` resolves to the statistics route, not `{integration_id}`."""
    mock_service.get_statistics.return_value = IntegrationStatisticsResponse(
        total_integrations=0,
        by_type={},
        by_provider={},
        by_status={},
        by_authentication_type={},
    )

    response = client.get("/api/v1/integrations/statistics")

    assert response.status_code == 200
    mock_service.get_integration.assert_not_called()