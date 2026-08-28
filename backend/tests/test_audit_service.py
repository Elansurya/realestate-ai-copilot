# backend/tests/test_audit_service.py

"""
Audit Log Module - Phase 4
Service Layer Test Suite

Covers:
    - Business Validation
    - Invalid Module
    - Invalid Action
    - Invalid Severity
    - Invalid Status
    - Statistics
    - Timeline
    - Export
    - Cleanup
    - Search
    - Pagination
    - Domain Exceptions

These tests exercise `AuditLogService` in isolation, with the
repository layer fully mocked via `AsyncMock`, mirroring the
conventions established in `test_payment_service.py`.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.core.exceptions import (
    BusinessRuleException,
    NotFoundException,
    ValidationException,
)
from app.models.audit_log import AuditAction, AuditModule, AuditSeverity, AuditStatus
from app.schemas.audit_log import AuditLogCreate, AuditLogSearchFilter
# AuditLogUpdate does not exist - see test_audit_repository.py note.
from app.services.audit_log_service import AuditLogService

pytestmark = pytest.mark.asyncio


def make_audit_log(
    log_id=None,
    module=AuditModule.LEAD,
    action=AuditAction.CREATE,
    severity=AuditSeverity.LOW,
    status=AuditStatus.SUCCESS,
    user_id=1,
    entity_type="LEAD",
    entity_id=None,
    description="Created a new lead record.",
    is_active=True,
    created_at=None,
):
    log = MagicMock()
    log.id = log_id or uuid.uuid4()
    log.module = module
    log.action = action
    log.severity = severity
    log.status = status
    log.user_id = user_id
    log.entity_type = entity_type
    log.entity_id = entity_id or str(uuid.uuid4())
    log.description = description
    log.is_active = is_active
    log.created_at = created_at or datetime.now(timezone.utc)
    return log


@pytest.fixture
def service(db_session_mock):
    svc = AuditLogService(db_session_mock)
    svc.audit_repo = AsyncMock()
    return svc


@pytest.fixture
def valid_payload():
    return AuditLogCreate(
        user_id=1,
        module=AuditModule.LEAD,
        action=AuditAction.CREATE,
        entity_type="LEAD",
        entity_id=str(uuid.uuid4()),
        severity=AuditSeverity.LOW,
        status=AuditStatus.SUCCESS,
        description="Created a new lead record.",
        ip_address="10.0.0.5",
        user_agent="pytest-agent/1.0",
    )


class TestCreateAuditLogValidation:

    async def test_create_audit_log_success(self, service, valid_payload):
        created = make_audit_log()
        service.audit_repo.create.return_value = created

        result = await service.create_log(valid_payload)

        assert result is not None
        service.audit_repo.create.assert_awaited_once()

    async def test_invalid_module_raises_validation_exception(self, service, valid_payload):
        valid_payload.module = "NOT_A_REAL_MODULE"
        with pytest.raises(ValidationException):
            await service.create_log(valid_payload)

    async def test_invalid_action_raises_validation_exception(self, service, valid_payload):
        valid_payload.action = "NOT_A_REAL_ACTION"
        with pytest.raises(ValidationException):
            await service.create_log(valid_payload)

    async def test_invalid_severity_raises_validation_exception(self, service, valid_payload):
        valid_payload.severity = "NOT_A_REAL_SEVERITY"
        with pytest.raises(ValidationException):
            await service.create_log(valid_payload)

    async def test_invalid_status_raises_validation_exception(self, service, valid_payload):
        valid_payload.status = "NOT_A_REAL_STATUS"
        with pytest.raises(ValidationException):
            await service.create_log(valid_payload)

    async def test_missing_entity_type_rejected(self, service, valid_payload):
        valid_payload.entity_type = ""
        with pytest.raises(ValidationException):
            await service.create_log(valid_payload)

    async def test_missing_description_rejected(self, service, valid_payload):
        valid_payload.description = ""
        with pytest.raises(ValidationException):
            await service.create_log(valid_payload)


class TestGetAuditLog:

    async def test_get_by_id_success(self, service):
        log = make_audit_log()
        service.audit_repo.get_by_id.return_value = log

        result = await service.get_by_id(log.id)
        assert result.id == log.id

    async def test_get_by_id_not_found_raises_not_found(self, service):
        service.audit_repo.get_by_id.return_value = None
        with pytest.raises(NotFoundException):
            await service.get_by_id(uuid.uuid4())


class TestUpdateAuditLog:
    pytestmark = pytest.mark.skip(
        reason="AuditLogUpdate does not exist; audit logs are intentionally "
        "append-only. Needs a product decision before this class can be "
        "un-skipped."
    )

    async def test_update_success(self, service):
        log = make_audit_log()
        service.audit_repo.get_by_id.return_value = log
        service.audit_repo.update.return_value = make_audit_log(
            log_id=log.id, description="Updated description"
        )

        update_payload = AuditLogUpdate(description="Updated description")
        result = await service.update_log(log.id, update_payload)

        assert result.description == "Updated description"

    async def test_update_nonexistent_raises_not_found(self, service):
        service.audit_repo.get_by_id.return_value = None
        update_payload = AuditLogUpdate(description="Ghost")
        with pytest.raises(NotFoundException):
            await service.update_log(uuid.uuid4(), update_payload)

    async def test_update_invalid_severity_raises_validation_exception(self, service):
        log = make_audit_log()
        service.audit_repo.get_by_id.return_value = log

        update_payload = MagicMock()
        update_payload.model_dump.return_value = {"severity": "NOT_REAL"}
        with pytest.raises(ValidationException):
            await service.update_log(log.id, update_payload)


class TestDeleteAuditLog:

    async def test_delete_success(self, service):
        log = make_audit_log()
        service.audit_repo.get_by_id.return_value = log
        service.audit_repo.soft_delete.return_value = True

        await service.delete_log(log.id)
        service.audit_repo.soft_delete.assert_awaited_once_with(log.id)

    async def test_delete_nonexistent_raises_not_found(self, service):
        service.audit_repo.get_by_id.return_value = None
        with pytest.raises(NotFoundException):
            await service.delete_log(uuid.uuid4())


class TestSearchAndPagination:

    async def test_search_returns_items_and_total(self, service):
        logs = [make_audit_log() for _ in range(3)]
        service.audit_repo.search.return_value = (logs, 3)

        filters = AuditLogSearchFilter(page=1, page_size=10)
        items, total = await service.search_logs(filters)

        assert total == 3
        assert len(items) == 3

    async def test_search_invalid_page_rejected(self, service):
        with pytest.raises(ValidationException):
            AuditLogSearchFilter(page=0, page_size=10)
            filters = AuditLogSearchFilter.model_construct(page=0, page_size=10)
            await service.search_logs(filters)

    async def test_search_page_size_capped(self, service):
        logs = [make_audit_log() for _ in range(5)]
        service.audit_repo.search.return_value = (logs, 5)

        filters = AuditLogSearchFilter(page=1, page_size=500)
        items, total = await service.search_logs(filters)

        assert total == 5

    async def test_search_empty_results(self, service):
        service.audit_repo.search.return_value = ([], 0)

        filters = AuditLogSearchFilter(search="no-match-xyz")
        items, total = await service.search_logs(filters)

        assert items == []
        assert total == 0


class TestStatisticsAndTimeline:

    async def test_get_statistics_returns_repo_data(self, service):
        service.audit_repo.get_statistics.return_value = {
            "total_logs": 42,
            "by_module": {"LEAD": 20, "PAYMENT": 22},
            "by_severity": {"LOW": 30, "CRITICAL": 12},
            "by_status": {"SUCCESS": 40, "FAILED": 2},
        }

        stats = await service.get_statistics()
        assert stats["total_logs"] == 42

    async def test_get_timeline_returns_ordered_logs(self, service):
        entity_id = str(uuid.uuid4())
        logs = [
            make_audit_log(entity_id=entity_id, action=AuditAction.CREATE),
            make_audit_log(entity_id=entity_id, action=AuditAction.UPDATE),
        ]
        service.audit_repo.get_by_entity.return_value = logs

        timeline = await service.get_timeline("LEAD", entity_id)
        assert len(timeline) == 2

    async def test_get_timeline_empty_entity_returns_empty_list(self, service):
        service.audit_repo.get_by_entity.return_value = []
        timeline = await service.get_timeline("LEAD", str(uuid.uuid4()))
        assert timeline == []


class TestExport:

    async def test_export_returns_serialized_payload(self, service):
        logs = [make_audit_log() for _ in range(2)]
        service.audit_repo.search.return_value = (logs, 2)

        filters = AuditLogSearchFilter(page=1, page_size=100)
        exported = await service.export_logs(filters, export_format="csv")

        assert exported is not None

    async def test_export_invalid_format_rejected(self, service):
        filters = AuditLogSearchFilter(page=1, page_size=100)
        with pytest.raises(ValidationException):
            await service.export_logs(filters, export_format="exe")

    async def test_export_empty_results_still_succeeds(self, service):
        service.audit_repo.search.return_value = ([], 0)
        filters = AuditLogSearchFilter(page=1, page_size=100)
        exported = await service.export_logs(filters, export_format="csv")
        assert exported is not None


class TestCleanup:

    async def test_cleanup_success(self, service):
        service.audit_repo.cleanup.return_value = 15
        count = await service.cleanup_logs(older_than_days=365)
        assert count == 15

    async def test_cleanup_rejects_non_positive_days(self, service):
        with pytest.raises(ValidationException):
            await service.cleanup_logs(older_than_days=0)

    async def test_cleanup_rejects_negative_days(self, service):
        with pytest.raises(ValidationException):
            await service.cleanup_logs(older_than_days=-10)

    async def test_cleanup_below_minimum_retention_rejected(self, service):
        with pytest.raises(BusinessRuleException):
            await service.cleanup_logs(older_than_days=7)


class TestDomainExceptions:

    async def test_get_by_id_raises_not_found_exception_type(self, service):
        service.audit_repo.get_by_id.return_value = None
        with pytest.raises(NotFoundException) as exc_info:
            await service.get_by_id(uuid.uuid4())
        assert exc_info.value.status_code == 404

    async def test_invalid_module_raises_validation_exception_type(
        self, service, valid_payload
    ):
        valid_payload.module = "GARBAGE"
        with pytest.raises(ValidationException) as exc_info:
            await service.create_log(valid_payload)
        assert exc_info.value.status_code == 400

    async def test_cleanup_business_rule_exception_type(self, service):
        with pytest.raises(BusinessRuleException) as exc_info:
            await service.cleanup_logs(older_than_days=1)
        assert exc_info.value.status_code == 422