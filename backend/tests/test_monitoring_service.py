"""
backend/tests/test_monitoring_service.py

Unit tests for `app.services.monitoring_service.MonitoringService`.

Scope:
    These tests exercise ONLY the business-rule layer. The
    `MonitoringRepository` collaborator is fully mocked (via
    `unittest.mock.AsyncMock`) so these tests never touch a real
    database and run fast/in-isolation. Repository *behavior* itself is
    covered separately in `test_monitoring_repository.py`.

    Every test asserts against the actual, documented business rules
    implemented in `monitoring_service.py`:
        - Status derivation thresholds (`derive_status_from_metrics`)
        - Component-identity uniqueness enforcement
        - Parent-component existence / self-reference validation
        - Timestamp sanity checks
        - Domain-exception contracts (`NotFoundException`,
          `ConflictException`, `ValidationException`)
        - Aggregation math for the whole-system overview and statistics

No new business rules, endpoints, or behaviors are invented; every
assertion traces back to a specific rule visible in
`app/services/monitoring_service.py`.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.core.exceptions import ConflictException, NotFoundException, ValidationException
from app.models.monitoring import ComponentType, HealthStatus
from app.schemas.monitoring import (
    HealthFilter,
    SystemHealthCreate,
    SystemHealthUpdate,
)
from app.services.monitoring_service import MonitoringService

pytestmark = pytest.mark.asyncio


# --------------------------------------------------------------------------
# Fixtures
# --------------------------------------------------------------------------
@pytest.fixture
def mock_repository() -> AsyncMock:
    """A fully-mocked `MonitoringRepository` collaborator."""
    return AsyncMock()


@pytest.fixture
def service(mock_repository: AsyncMock) -> MonitoringService:
    return MonitoringService(mock_repository)


def _make_record(**overrides) -> MagicMock:
    """Builds a MagicMock standing in for a `SystemHealth` ORM instance."""
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
    record = MagicMock()
    for key, value in defaults.items():
        setattr(record, key, value)
    return record


def _make_create_payload(**overrides) -> SystemHealthCreate:
    now = datetime.now(timezone.utc)
    defaults = dict(
        component_name="primary-postgres",
        component_type=ComponentType.DATABASE,
        status=HealthStatus.HEALTHY,
        last_health_check_at=now,
    )
    defaults.update(overrides)
    return SystemHealthCreate(**defaults)


# ==========================================================================
# derive_status_from_metrics -- Threshold Business Rules
# ==========================================================================
class TestDeriveStatusFromMetrics:
    def test_all_metrics_within_normal_range_is_healthy(self, service: MonitoringService) -> None:
        status = service.derive_status_from_metrics(
            cpu_usage_percent=10.0,
            memory_usage_percent=20.0,
            disk_usage_percent=30.0,
            response_time_ms=100.0,
            error_count=0,
        )
        assert status == HealthStatus.HEALTHY

    def test_cpu_at_warning_threshold_is_degraded(self, service: MonitoringService) -> None:
        status = service.derive_status_from_metrics(
            cpu_usage_percent=75.0,  # exactly at _RESOURCE_WARNING_THRESHOLD
            memory_usage_percent=None,
            disk_usage_percent=None,
            response_time_ms=None,
            error_count=0,
        )
        assert status == HealthStatus.DEGRADED

    def test_memory_at_critical_threshold_is_unhealthy(self, service: MonitoringService) -> None:
        status = service.derive_status_from_metrics(
            cpu_usage_percent=None,
            memory_usage_percent=90.0,  # exactly at _RESOURCE_CRITICAL_THRESHOLD
            disk_usage_percent=None,
            response_time_ms=None,
            error_count=0,
        )
        assert status == HealthStatus.UNHEALTHY

    def test_response_time_warning_threshold_is_degraded(self, service: MonitoringService) -> None:
        status = service.derive_status_from_metrics(
            cpu_usage_percent=None,
            memory_usage_percent=None,
            disk_usage_percent=None,
            response_time_ms=1000.0,
            error_count=0,
        )
        assert status == HealthStatus.DEGRADED

    def test_response_time_critical_threshold_is_unhealthy(self, service: MonitoringService) -> None:
        status = service.derive_status_from_metrics(
            cpu_usage_percent=None,
            memory_usage_percent=None,
            disk_usage_percent=None,
            response_time_ms=5000.0,
            error_count=0,
        )
        assert status == HealthStatus.UNHEALTHY

    def test_any_error_with_no_critical_metric_is_degraded(self, service: MonitoringService) -> None:
        status = service.derive_status_from_metrics(
            cpu_usage_percent=1.0,
            memory_usage_percent=1.0,
            disk_usage_percent=1.0,
            response_time_ms=1.0,
            error_count=1,
        )
        assert status == HealthStatus.DEGRADED

    def test_error_count_at_unhealthy_threshold_is_unhealthy(self, service: MonitoringService) -> None:
        status = service.derive_status_from_metrics(
            cpu_usage_percent=None,
            memory_usage_percent=None,
            disk_usage_percent=None,
            response_time_ms=None,
            error_count=10,  # _ERROR_COUNT_UNHEALTHY_THRESHOLD
        )
        assert status == HealthStatus.UNHEALTHY

    def test_critical_takes_precedence_over_degraded_signal(self, service: MonitoringService) -> None:
        status = service.derive_status_from_metrics(
            cpu_usage_percent=95.0,  # critical
            memory_usage_percent=76.0,  # would be degraded on its own
            disk_usage_percent=None,
            response_time_ms=None,
            error_count=0,
        )
        assert status == HealthStatus.UNHEALTHY

    def test_all_metrics_none_and_no_errors_is_healthy(self, service: MonitoringService) -> None:
        status = service.derive_status_from_metrics(
            cpu_usage_percent=None,
            memory_usage_percent=None,
            disk_usage_percent=None,
            response_time_ms=None,
            error_count=0,
        )
        assert status == HealthStatus.HEALTHY


# ==========================================================================
# execute_health_check
# ==========================================================================
class TestExecuteHealthCheck:
    async def test_healthy_probe_upserts_and_returns_healthy_response(
        self, service: MonitoringService, mock_repository: AsyncMock
    ) -> None:
        mock_repository.upsert_health_check_result.return_value = _make_record()

        result = await service.execute_health_check(
            component_name="primary-postgres",
            component_type=ComponentType.DATABASE,
            cpu_usage_percent=10.0,
            memory_usage_percent=10.0,
            disk_usage_percent=10.0,
            response_time_ms=10.0,
            error_count=0,
        )

        assert result.status == HealthStatus.HEALTHY
        assert result.is_healthy is True
        mock_repository.upsert_health_check_result.assert_awaited_once()
        _, call_kwargs = mock_repository.upsert_health_check_result.call_args
        payload = mock_repository.upsert_health_check_result.call_args.args[2]
        assert payload.last_success_at is not None
        assert payload.last_failure_at is None

    async def test_unhealthy_probe_sets_failure_timestamp(
        self, service: MonitoringService, mock_repository: AsyncMock
    ) -> None:
        mock_repository.upsert_health_check_result.return_value = _make_record(
            status=HealthStatus.UNHEALTHY
        )

        result = await service.execute_health_check(
            component_name="primary-postgres",
            component_type=ComponentType.DATABASE,
            error_count=99,
        )

        assert result.status == HealthStatus.UNHEALTHY
        assert result.is_healthy is False
        payload = mock_repository.upsert_health_check_result.call_args.args[2]
        assert payload.last_failure_at is not None
        assert payload.last_success_at is None

    @pytest.mark.parametrize(
        "field,value",
        [
            ("cpu_usage_percent", -1.0),
            ("cpu_usage_percent", 101.0),
            ("memory_usage_percent", 150.0),
            ("disk_usage_percent", -5.0),
        ],
    )
    async def test_out_of_range_percentage_raises_validation_exception(
        self, service: MonitoringService, field: str, value: float
    ) -> None:
        kwargs = {field: value}
        with pytest.raises(ValidationException):
            await service.execute_health_check(
                component_name="x",
                component_type=ComponentType.DATABASE,
                **kwargs,
            )

    async def test_negative_response_time_raises_validation_exception(
        self, service: MonitoringService
    ) -> None:
        with pytest.raises(ValidationException):
            await service.execute_health_check(
                component_name="x",
                component_type=ComponentType.DATABASE,
                response_time_ms=-1.0,
            )

    @pytest.mark.parametrize("field", ["error_count", "warning_count"])
    async def test_negative_counters_raise_validation_exception(
        self, service: MonitoringService, field: str
    ) -> None:
        with pytest.raises(ValidationException):
            await service.execute_health_check(
                component_name="x",
                component_type=ComponentType.DATABASE,
                **{field: -1},
            )


# ==========================================================================
# create_health_record
# ==========================================================================
class TestCreateHealthRecord:
    async def test_create_succeeds_when_identity_is_available(
        self, service: MonitoringService, mock_repository: AsyncMock
    ) -> None:
        mock_repository.get_by_name_and_type.return_value = None
        mock_repository.create.return_value = _make_record()

        result = await service.create_health_record(_make_create_payload())

        assert result.component_name == "primary-postgres"
        mock_repository.create.assert_awaited_once()

    async def test_create_raises_conflict_on_duplicate_identity(
        self, service: MonitoringService, mock_repository: AsyncMock
    ) -> None:
        mock_repository.get_by_name_and_type.return_value = _make_record()

        with pytest.raises(ConflictException):
            await service.create_health_record(_make_create_payload())

        mock_repository.create.assert_not_awaited()

    async def test_create_raises_not_found_when_parent_missing(
        self, service: MonitoringService, mock_repository: AsyncMock
    ) -> None:
        mock_repository.get_by_name_and_type.return_value = None
        mock_repository.get_by_id.return_value = None

        payload = _make_create_payload(parent_component_id=uuid.uuid4())

        with pytest.raises(NotFoundException):
            await service.create_health_record(payload)

    async def test_create_raises_validation_when_last_success_in_future(
        self, service: MonitoringService, mock_repository: AsyncMock
    ) -> None:
        mock_repository.get_by_name_and_type.return_value = None
        future = datetime.now(timezone.utc) + timedelta(days=1)

        payload = _make_create_payload(last_success_at=future)

        with pytest.raises(ValidationException):
            await service.create_health_record(payload)


# ==========================================================================
# update_health_record
# ==========================================================================
class TestUpdateHealthRecord:
    async def test_update_raises_not_found_when_record_missing(
        self, service: MonitoringService, mock_repository: AsyncMock
    ) -> None:
        mock_repository.get_by_id.return_value = None

        with pytest.raises(NotFoundException):
            await service.update_health_record(uuid.uuid4(), SystemHealthUpdate(status=HealthStatus.DEGRADED))

    async def test_update_raises_conflict_when_new_identity_taken(
        self, service: MonitoringService, mock_repository: AsyncMock
    ) -> None:
        existing = _make_record()
        other = _make_record(id=uuid.uuid4(), component_name="other-component")
        mock_repository.get_by_id.return_value = existing
        mock_repository.get_by_name_and_type.return_value = other

        with pytest.raises(ConflictException):
            await service.update_health_record(
                existing.id, SystemHealthUpdate(component_name="other-component")
            )

    async def test_update_raises_validation_on_self_parent(
        self, service: MonitoringService, mock_repository: AsyncMock
    ) -> None:
        existing = _make_record()
        mock_repository.get_by_id.return_value = existing

        with pytest.raises(ValidationException):
            await service.update_health_record(
                existing.id, SystemHealthUpdate(parent_component_id=existing.id)
            )

    async def test_update_succeeds_and_returns_response(
        self, service: MonitoringService, mock_repository: AsyncMock
    ) -> None:
        existing = _make_record()
        mock_repository.get_by_id.return_value = existing
        mock_repository.update.return_value = _make_record(status=HealthStatus.DEGRADED)

        result = await service.update_health_record(
            existing.id, SystemHealthUpdate(status=HealthStatus.DEGRADED)
        )

        assert result.status == HealthStatus.DEGRADED


# ==========================================================================
# Retrieval
# ==========================================================================
class TestRetrieval:
    async def test_get_component_status_raises_not_found(
        self, service: MonitoringService, mock_repository: AsyncMock
    ) -> None:
        mock_repository.get_component_status.return_value = None

        with pytest.raises(NotFoundException):
            await service.get_component_status("missing", ComponentType.DATABASE)

    async def test_get_component_status_returns_response(
        self, service: MonitoringService, mock_repository: AsyncMock
    ) -> None:
        mock_repository.get_component_status.return_value = _make_record()

        result = await service.get_component_status("primary-postgres", ComponentType.DATABASE)
        assert result.component_name == "primary-postgres"

    async def test_get_health_history_raises_not_found_when_empty(
        self, service: MonitoringService, mock_repository: AsyncMock
    ) -> None:
        mock_repository.get_health_history.return_value = []

        with pytest.raises(NotFoundException):
            await service.get_health_history("missing", ComponentType.DATABASE)

    async def test_get_health_history_raises_validation_on_non_positive_limit(
        self, service: MonitoringService
    ) -> None:
        with pytest.raises(ValidationException):
            await service.get_health_history("x", ComponentType.DATABASE, limit=0)

    async def test_list_health_records_computes_pagination(
        self, service: MonitoringService, mock_repository: AsyncMock
    ) -> None:
        mock_repository.list_paginated.return_value = ([_make_record()], 25)
        filters = HealthFilter(page=1, page_size=10)

        result = await service.list_health_records(filters)

        assert result.total == 25
        assert result.total_pages == 3
        assert len(result.items) == 1

    async def test_list_health_records_zero_total_pages_when_empty(
        self, service: MonitoringService, mock_repository: AsyncMock
    ) -> None:
        mock_repository.list_paginated.return_value = ([], 0)
        filters = HealthFilter()

        result = await service.list_health_records(filters)

        assert result.total_pages == 0


# ==========================================================================
# Delete / Restore
# ==========================================================================
class TestDeleteRestore:
    async def test_delete_raises_not_found_when_missing(
        self, service: MonitoringService, mock_repository: AsyncMock
    ) -> None:
        mock_repository.get_by_id.return_value = None

        with pytest.raises(NotFoundException):
            await service.delete_health_record(uuid.uuid4())

    async def test_delete_succeeds(
        self, service: MonitoringService, mock_repository: AsyncMock
    ) -> None:
        record = _make_record()
        mock_repository.get_by_id.return_value = record
        mock_repository.soft_delete.return_value = _make_record(is_deleted=True)

        result = await service.delete_health_record(record.id)
        assert result.is_deleted is True

    async def test_restore_raises_not_found_when_missing(
        self, service: MonitoringService, mock_repository: AsyncMock
    ) -> None:
        mock_repository.restore.return_value = None

        with pytest.raises(NotFoundException):
            await service.restore_health_record(uuid.uuid4())

    async def test_restore_succeeds(
        self, service: MonitoringService, mock_repository: AsyncMock
    ) -> None:
        mock_repository.restore.return_value = _make_record(is_deleted=False)

        result = await service.restore_health_record(uuid.uuid4())
        assert result.is_deleted is False


# ==========================================================================
# Whole-System Overview & Statistics
# ==========================================================================
class TestAggregation:
    async def test_overview_worst_case_precedence(
        self, service: MonitoringService, mock_repository: AsyncMock
    ) -> None:
        mock_repository.list_all_active.return_value = [
            _make_record(status=HealthStatus.HEALTHY),
            _make_record(status=HealthStatus.DEGRADED),
            _make_record(status=HealthStatus.DOWN),
        ]

        overview = await service.get_health_status_overview()

        assert overview.overall_status == HealthStatus.DOWN
        assert overview.healthy_count == 1
        assert overview.degraded_count == 1
        assert overview.down_count == 1

    async def test_overview_defaults_to_unknown_when_no_components(
        self, service: MonitoringService, mock_repository: AsyncMock
    ) -> None:
        mock_repository.list_all_active.return_value = []

        overview = await service.get_health_status_overview()

        assert overview.overall_status == HealthStatus.UNKNOWN
        assert overview.healthy_count == 0

    async def test_statistics_computes_uptime_percentage(
        self, service: MonitoringService, mock_repository: AsyncMock
    ) -> None:
        mock_repository.count_by_status.return_value = {
            HealthStatus.HEALTHY: 3,
            HealthStatus.DEGRADED: 1,
            HealthStatus.DOWN: 1,
        }
        mock_repository.count_by_component_type.return_value = {
            ComponentType.DATABASE: 3,
            ComponentType.STORAGE: 2,
        }
        mock_repository.get_aggregate_metrics.return_value = {
            "total_components": 5,
            "average_response_time_ms": 42.0,
            "total_error_count": 2,
            "total_warning_count": 4,
        }

        stats = await service.get_statistics()

        assert stats.total_components == 5
        assert stats.healthy_count == 3
        assert stats.degraded_count == 1
        assert stats.down_count == 1
        assert stats.uptime_percentage == 80.0  # (3+1)/5 * 100
        assert stats.by_component_type == {"DATABASE": 3, "STORAGE": 2}

    async def test_statistics_uptime_is_none_when_no_components(
        self, service: MonitoringService, mock_repository: AsyncMock
    ) -> None:
        mock_repository.count_by_status.return_value = {}
        mock_repository.count_by_component_type.return_value = {}
        mock_repository.get_aggregate_metrics.return_value = {
            "total_components": 0,
            "average_response_time_ms": None,
            "total_error_count": 0,
            "total_warning_count": 0,
        }

        stats = await service.get_statistics()

        assert stats.uptime_percentage is None
        assert stats.total_components == 0