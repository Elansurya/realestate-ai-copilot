"""
backend/tests/test_monitoring_api.py

API-layer (router) tests for `app.api.v1.monitoring`.

Scope:
    These tests exercise the FastAPI router in isolation:
        - `/live` and `/ready` are unauthenticated probes.
        - Every other route requires a valid JWT + the correct
          `monitoring:read` / `monitoring:write` role per the module
          docstring's documented RBAC table.
        - Domain exceptions raised by a (mocked) `MonitoringService`
          are translated to the correct HTTP status codes by
          `_raise_http_from_domain_exception`.
        - Response payload shape matches the declared `response_model`.

    `MonitoringService` and the `health_checker` / `metrics_collector`
    utility modules are mocked so these tests never touch a real
    database or external dependency. Business-rule correctness itself
    is covered in `test_monitoring_service.py`.

Auth strategy:
    `app.api.deps.require_roles(*roles)` is documented (see
    `monitoring.py`'s module docstring) as building its authorization
    check on top of `get_current_user`. Overriding `get_current_user`
    via `app.dependency_overrides` therefore satisfies both the plain
    `Depends(get_current_user)` routes (`/health/check/...` etc. use
    `require_roles`, all read/write routes use `require_roles`) and the
    nested dependency inside `require_roles`. A helper,
    `_override_current_user`, swaps in a fake user with a given role
    per test so both allowed and forbidden-role paths are covered.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api import deps as api_deps
from app.api.v1 import monitoring as monitoring_module
from app.core.exceptions import ConflictException, NotFoundException, ValidationException
from app.models.monitoring import ComponentType, HealthStatus
from app.schemas.monitoring import (
    HealthCheckResponse,
    HealthStatusResponse,
    MonitoringStatisticsResponse,
    SystemHealthListResponse,
    SystemHealthResponse,
)


# --------------------------------------------------------------------------
# Test Fixtures -- App / Client / Fake Auth
# --------------------------------------------------------------------------
class _FakeUser:
    def __init__(self, user_id: int = 1, role: str = "ADMIN") -> None:
        self.id = user_id
        self.role = role


@pytest.fixture
def app() -> FastAPI:
    test_app = FastAPI()
    test_app.include_router(monitoring_module.router, prefix="/api/v1")
    return test_app


@pytest.fixture
def mock_service(mocker):
    """Replaces the real MonitoringService with a MagicMock/AsyncMock."""
    service = mocker.AsyncMock()
    return service


@pytest.fixture
def client(app: FastAPI, mock_service):
    async def _fake_get_db():
        yield object()  # never touched; service layer is mocked out

    def _override_get_monitoring_service():
        return mock_service

    app.dependency_overrides[api_deps.get_db] = _fake_get_db
    app.dependency_overrides[monitoring_module.get_monitoring_service] = (
        _override_get_monitoring_service
    )
    with TestClient(test_app := app) as test_client:
        yield test_client
    app.dependency_overrides.clear()


def _authenticate_as(app: FastAPI, role: str = "ADMIN", user_id: int = 1) -> None:
    """Overrides `get_current_user` to simulate an authenticated caller."""

    def _fake_current_user():
        return _FakeUser(user_id=user_id, role=role)

    app.dependency_overrides[api_deps.get_current_user] = _fake_current_user


def _sample_health_response(**overrides) -> SystemHealthResponse:
    now = datetime.now(timezone.utc)
    defaults = dict(
        id=uuid.uuid4(),
        parent_component_id=None,
        component_name="primary-postgres",
        component_type=ComponentType.DATABASE,
        status=HealthStatus.HEALTHY,
        cpu_usage_percent=10.0,
        memory_usage_percent=20.0,
        disk_usage_percent=30.0,
        response_time_ms=15.0,
        error_count=0,
        warning_count=0,
        last_health_check_at=now,
        last_success_at=now,
        last_failure_at=None,
        status_message=None,
        meta_data=None,
        is_active=True,
        is_deleted=False,
        deleted_at=None,
        deleted_by_id=None,
        created_by_id=None,
        updated_by_id=None,
        created_at=now,
        updated_at=now,
    )
    defaults.update(overrides)
    return SystemHealthResponse(**defaults)


# ==========================================================================
# Unauthenticated Probes
# ==========================================================================
class TestProbes:
    def test_liveness_probe_returns_200_without_auth(self, app, client, mocker) -> None:
        mocker.patch.object(
            monitoring_module.metrics_collector, "get_uptime_seconds", return_value=12.5
        )
        response = client.get("/api/v1/monitoring/live")
        assert response.status_code == 200
        body = response.json()
        assert body["status"] == "alive"
        assert body["uptime_seconds"] == 12.5

    def test_readiness_probe_returns_200_when_db_healthy(self, app, client, mocker) -> None:
        mocker.patch.object(
            monitoring_module.health_checker,
            "check_database_health",
            new=mocker.AsyncMock(
                return_value={"is_healthy": True, "response_time_ms": 3.2, "message": None}
            ),
        )
        response = client.get("/api/v1/monitoring/ready")
        assert response.status_code == 200
        assert response.json()["status"] == "ready"

    def test_readiness_probe_returns_503_when_db_unhealthy(self, app, client, mocker) -> None:
        mocker.patch.object(
            monitoring_module.health_checker,
            "check_database_health",
            new=mocker.AsyncMock(
                return_value={
                    "is_healthy": False,
                    "response_time_ms": None,
                    "message": "Database unreachable",
                }
            ),
        )
        response = client.get("/api/v1/monitoring/ready")
        assert response.status_code == 503


# ==========================================================================
# Authentication / RBAC Enforcement
# ==========================================================================
class TestAuthAndRbac:
    def test_overview_requires_authentication(self, app, client) -> None:
        response = client.get("/api/v1/monitoring")
        assert response.status_code in (401, 403)

    def test_read_endpoint_rejects_role_without_monitoring_read(
        self, app, client, mock_service
    ) -> None:
        _authenticate_as(app, role="SALES_REP")
        response = client.get("/api/v1/monitoring")
        assert response.status_code == 403

    @pytest.mark.parametrize("role", ["ADMIN", "MANAGER", "AUDITOR"])
    def test_read_endpoint_allows_all_monitoring_read_roles(
        self, app, client, mock_service, role: str
    ) -> None:
        _authenticate_as(app, role=role)
        mock_service.get_health_status_overview.return_value = HealthStatusResponse(
            overall_status=HealthStatus.HEALTHY,
            components=[],
            healthy_count=0,
            degraded_count=0,
            unhealthy_count=0,
            down_count=0,
            checked_at=datetime.now(timezone.utc),
        )
        response = client.get("/api/v1/monitoring")
        assert response.status_code == 200

    @pytest.mark.parametrize("role", ["MANAGER", "AUDITOR"])
    def test_write_endpoint_rejects_non_admin_roles(self, app, client, role: str) -> None:
        _authenticate_as(app, role=role)
        response = client.post(
            "/api/v1/monitoring/components",
            json={
                "component_name": "x",
                "component_type": "DATABASE",
                "last_health_check_at": datetime.now(timezone.utc).isoformat(),
            },
        )
        assert response.status_code == 403

    def test_write_endpoint_allows_admin(self, app, client, mock_service) -> None:
        _authenticate_as(app, role="ADMIN")
        mock_service.create_health_record.return_value = _sample_health_response()
        response = client.post(
            "/api/v1/monitoring/components",
            json={
                "component_name": "primary-postgres",
                "component_type": "DATABASE",
                "last_health_check_at": datetime.now(timezone.utc).isoformat(),
            },
        )
        assert response.status_code == 201


# ==========================================================================
# Domain Exception -> HTTP Status Translation
# ==========================================================================
class TestExceptionTranslation:
    def test_not_found_exception_maps_to_404(self, app, client, mock_service) -> None:
        _authenticate_as(app, role="ADMIN")
        mock_service.get_component_status.side_effect = NotFoundException("not found")
        response = client.get(
            "/api/v1/monitoring/components/status",
            params={"component_name": "missing", "component_type": "DATABASE"},
        )
        assert response.status_code == 404

    def test_conflict_exception_maps_to_409(self, app, client, mock_service) -> None:
        _authenticate_as(app, role="ADMIN")
        mock_service.create_health_record.side_effect = ConflictException("duplicate")
        response = client.post(
            "/api/v1/monitoring/components",
            json={
                "component_name": "primary-postgres",
                "component_type": "DATABASE",
                "last_health_check_at": datetime.now(timezone.utc).isoformat(),
            },
        )
        assert response.status_code == 409

    def test_validation_exception_maps_to_422(self, app, client, mock_service) -> None:
        _authenticate_as(app, role="ADMIN")
        mock_service.create_health_record.side_effect = ValidationException("bad value")
        response = client.post(
            "/api/v1/monitoring/components",
            json={
                "component_name": "primary-postgres",
                "component_type": "DATABASE",
                "last_health_check_at": datetime.now(timezone.utc).isoformat(),
            },
        )
        assert response.status_code == 422

    def test_unexpected_exception_maps_to_500(self, app, client, mock_service) -> None:
        _authenticate_as(app, role="ADMIN")
        mock_service.create_health_record.side_effect = RuntimeError("boom")
        response = client.post(
            "/api/v1/monitoring/components",
            json={
                "component_name": "primary-postgres",
                "component_type": "DATABASE",
                "last_health_check_at": datetime.now(timezone.utc).isoformat(),
            },
        )
        assert response.status_code == 500


# ==========================================================================
# Per-Component-Type Probes
# ==========================================================================
class TestComponentProbes:
    def test_check_database_health_delegates_to_health_checker_and_service(
        self, app, client, mock_service, mocker
    ) -> None:
        _authenticate_as(app, role="AUDITOR")
        mocker.patch.object(
            monitoring_module.health_checker,
            "check_database_health",
            new=mocker.AsyncMock(
                return_value={
                    "response_time_ms": 5.0,
                    "error_count": 0,
                    "message": "ok",
                    "meta_data": None,
                }
            ),
        )
        mock_service.execute_health_check.return_value = HealthCheckResponse(
            component_name="primary-database",
            component_type=ComponentType.DATABASE,
            status=HealthStatus.HEALTHY,
            is_healthy=True,
            response_time_ms=5.0,
            message="ok",
            checked_at=datetime.now(timezone.utc),
        )
        response = client.get("/api/v1/monitoring/health/database")
        assert response.status_code == 200
        assert response.json()["status"] == "HEALTHY"

    def test_trigger_health_check_requires_write_role(self, app, client, mock_service) -> None:
        _authenticate_as(app, role="AUDITOR")
        response = client.post("/api/v1/monitoring/health/check/DATABASE/primary-postgres")
        assert response.status_code == 403

    def test_trigger_health_check_admin_succeeds(self, app, client, mock_service) -> None:
        _authenticate_as(app, role="ADMIN")
        mock_service.execute_health_check.return_value = HealthCheckResponse(
            component_name="primary-postgres",
            component_type=ComponentType.DATABASE,
            status=HealthStatus.HEALTHY,
            is_healthy=True,
            response_time_ms=None,
            message=None,
            checked_at=datetime.now(timezone.utc),
        )
        response = client.post(
            "/api/v1/monitoring/health/check/DATABASE/primary-postgres",
            params={"error_count": 0},
        )
        assert response.status_code == 200


# ==========================================================================
# Metrics & Statistics
# ==========================================================================
class TestMetricsAndStatistics:
    def test_get_system_metrics_returns_system_and_process_sections(
        self, app, client, mocker
    ) -> None:
        _authenticate_as(app, role="MANAGER")
        mocker.patch.object(
            monitoring_module.metrics_collector,
            "get_system_metrics",
            new=mocker.AsyncMock(return_value={"cpu_usage_percent": 10.0}),
        )
        mocker.patch.object(
            monitoring_module.metrics_collector,
            "get_process_metrics",
            new=mocker.AsyncMock(return_value={"process_cpu_percent": 1.0}),
        )
        response = client.get("/api/v1/monitoring/metrics")
        assert response.status_code == 200
        body = response.json()
        assert "system" in body and "process" in body

    def test_get_statistics_returns_expected_shape(self, app, client, mock_service) -> None:
        _authenticate_as(app, role="AUDITOR")
        mock_service.get_statistics.return_value = MonitoringStatisticsResponse(
            total_components=5,
            healthy_count=4,
            degraded_count=1,
            unhealthy_count=0,
            down_count=0,
            maintenance_count=0,
            unknown_count=0,
            average_response_time_ms=10.0,
            total_error_count=0,
            total_warning_count=0,
            uptime_percentage=100.0,
            by_component_type={"DATABASE": 1},
            generated_at=datetime.now(timezone.utc),
        )
        response = client.get("/api/v1/monitoring/statistics")
        assert response.status_code == 200
        assert response.json()["total_components"] == 5


# ==========================================================================
# CRUD -- Components
# ==========================================================================
class TestComponentCrud:
    def test_list_health_records(self, app, client, mock_service) -> None:
        _authenticate_as(app, role="AUDITOR")
        mock_service.list_health_records.return_value = SystemHealthListResponse(
            items=[_sample_health_response()],
            total=1,
            page=1,
            page_size=20,
            total_pages=1,
        )
        response = client.get("/api/v1/monitoring/components")
        assert response.status_code == 200
        assert response.json()["total"] == 1

    def test_get_health_record_by_id_not_found(self, app, client, mock_service) -> None:
        _authenticate_as(app, role="AUDITOR")
        mock_service._get_existing_or_raise.side_effect = NotFoundException("nope")
        response = client.get(f"/api/v1/monitoring/components/{uuid.uuid4()}")
        assert response.status_code == 404

    def test_update_health_record(self, app, client, mock_service) -> None:
        _authenticate_as(app, role="ADMIN")
        mock_service.update_health_record.return_value = _sample_health_response(
            status=HealthStatus.DEGRADED
        )
        response = client.patch(
            f"/api/v1/monitoring/components/{uuid.uuid4()}",
            json={"status": "DEGRADED"},
        )
        assert response.status_code == 200
        assert response.json()["status"] == "DEGRADED"

    def test_delete_health_record(self, app, client, mock_service) -> None:
        _authenticate_as(app, role="ADMIN")
        mock_service.delete_health_record.return_value = _sample_health_response(is_deleted=True)
        response = client.delete(f"/api/v1/monitoring/components/{uuid.uuid4()}")
        assert response.status_code == 200
        assert response.json()["is_deleted"] is True

    def test_restore_health_record(self, app, client, mock_service) -> None:
        _authenticate_as(app, role="ADMIN")
        mock_service.restore_health_record.return_value = _sample_health_response(
            is_deleted=False
        )
        response = client.post(f"/api/v1/monitoring/components/{uuid.uuid4()}/restore")
        assert response.status_code == 200
        assert response.json()["is_deleted"] is False