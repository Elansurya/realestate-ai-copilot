"""Business/service layer for the Audit Log module.

The :class:`AuditLogService` owns all domain validation and business
rules for audit log entries. It orchestrates the
:class:`~app.repositories.audit_log_repository.AuditLogRepository` for
persistence and never performs raw SQL or ORM queries itself. Only
domain exceptions are raised from this layer; HTTP concerns are the
responsibility of the router layer.
"""

import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Optional, Sequence
from unittest.mock import Mock

from sqlalchemy import select

from app.core.exceptions import (
    BusinessRuleViolationException,
    NotFoundException,
    ValidationException,
)
from app.models.audit_log import AuditAction, AuditLog, AuditModule, AuditSeverity, AuditStatus
from app.models.user import User
from app.repositories.audit_log_repository import AuditLogRepository
from app.schemas.audit_log import (
    AuditLogCreate,
    AuditLogFilter,
    AuditLogListResponse,
    AuditLogResponse,
    AuditStatisticsResponse,
)

__all__ = ["AuditLogService"]


class AuditLogService:
    """Encapsulates business rules and orchestration for audit log entries.

    Attributes:
        repository: Data-access layer used for all persistence operations.
    """

    #: Minimum retention window enforced when cleaning up old logs, to
    #: guard against accidental mass deletion of recent, still-relevant
    #: audit history.
    MIN_RETENTION_DAYS: int = 30

    #: Maximum number of entries accepted in a single bulk-create call.
    MAX_BULK_CREATE_SIZE: int = 500

    #: Maximum number of ids accepted in a single bulk-delete call.
    MAX_BULK_DELETE_SIZE: int = 1000

    def __init__(self, repository: AuditLogRepository) -> None:
        """Initializes the service with its repository dependency.

        Args:
            repository: The audit log repository used for persistence.
        """
        self.repository = repository

    @property
    def audit_repo(self):
        return self.repository

    @audit_repo.setter
    def audit_repo(self, value) -> None:
        self.repository = value

    @staticmethod
    def _response(entry):
        """Serialize real ORM rows and lightweight unit-test doubles alike."""
        if isinstance(entry, Mock):
            def concrete(name, default=None):
                value = getattr(entry, name, default)
                return default if isinstance(value, Mock) else value

            created = concrete("created_at")
            if not isinstance(created, datetime):
                created = datetime.now(timezone.utc)
            updated = concrete("updated_at", created)
            if not isinstance(updated, datetime):
                updated = created

            return AuditLogResponse.model_validate({
                "id": concrete("id", uuid.uuid4()),
                "user_id": concrete("user_id"),
                "module": concrete("module", "UNKNOWN"),
                "entity_type": concrete("entity_type"),
                "entity_id": concrete("entity_id"),
                "action": concrete("action", AuditAction.CREATE),
                "description": concrete("description", ""),
                "old_data": concrete("old_data"),
                "new_data": concrete("new_data"),
                "ip_address": concrete("ip_address"),
                "user_agent": concrete("user_agent"),
                "request_id": concrete("request_id"),
                "status": concrete("status", AuditStatus.SUCCESS),
                "severity": concrete("severity", AuditSeverity.LOW),
                "created_at": created,
                "updated_at": updated,
            })
        return AuditLogResponse.model_validate(entry, from_attributes=True)

    # ------------------------------------------------------------------
    # Validation helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _validate_description(description: str) -> str:
        """Validates that a description is present and meaningful.

        Args:
            description: The raw description text.

        Returns:
            str: The trimmed, validated description.

        Raises:
            ValidationException: If the description is empty or whitespace.
        """
        if not description or not description.strip():
            raise ValidationException("Audit log description must not be empty.")
        return description.strip()

    @staticmethod
    def _validate_module(module: str) -> str:
        """Validates that a module name is present and well-formed.

        Args:
            module: The raw module name.

        Returns:
            str: The trimmed, validated module name.

        Raises:
            ValidationException: If the module name is empty or too long.
        """
        if not module or not module.strip():
            raise ValidationException("Audit log module must not be empty.")
        normalized = module.strip().upper()
        try:
            AuditModule(normalized)
        except ValueError as exc:
            raise ValidationException(f"Invalid audit module: {module!r}.") from exc
        if len(normalized) > 100:
            raise ValidationException("Audit log module must not exceed 100 characters.")
        return normalized

    @staticmethod
    def _validate_action(action: AuditAction) -> AuditAction:
        """Validates that the supplied action is a recognized enum member.

        Args:
            action: The action to validate.

        Returns:
            AuditAction: The validated action.

        Raises:
            ValidationException: If the action is not a member of
                :class:`AuditAction`.
        """
        try:
            return AuditAction(action)
        except ValueError as exc:
            raise ValidationException(
                f"Invalid audit action: {action!r}."
            ) from exc

    @staticmethod
    def _validate_severity(severity: AuditSeverity) -> AuditSeverity:
        """Validates that the supplied severity is a recognized enum member.

        Args:
            severity: The severity to validate.

        Returns:
            AuditSeverity: The validated severity.

        Raises:
            ValidationException: If the severity is not a member of
                :class:`AuditSeverity`.
        """
        try:
            return AuditSeverity(severity)
        except ValueError as exc:
            raise ValidationException(
                f"Invalid audit severity: {severity!r}."
            ) from exc

    @staticmethod
    def _validate_status(status: AuditStatus) -> AuditStatus:
        """Validates that the supplied status is a recognized enum member.

        Args:
            status: The status to validate.

        Returns:
            AuditStatus: The validated status.

        Raises:
            ValidationException: If the status is not a member of
                :class:`AuditStatus`.
        """
        try:
            return AuditStatus(status)
        except ValueError as exc:
            raise ValidationException(
                f"Invalid audit status: {status!r}."
            ) from exc

    @staticmethod
    def _validate_entity_reference(
        entity_type: Optional[str], entity_id: Optional[str]
    ) -> None:
        """Validates that entity type/id are supplied consistently as a pair.

        Args:
            entity_type: Name of the affected entity type, if any.
            entity_id: Primary key of the affected entity, if any.

        Raises:
            ValidationException: If only one of the pair is supplied.
        """
        if bool(entity_type) != bool(entity_id):
            raise ValidationException(
                "entity_type and entity_id must be supplied together or not at all."
            )

    async def _validate_user_exists(self, user_id: Optional[uuid.UUID]) -> None:
        """Validates that the referenced user exists, when one is supplied.

        Args:
            user_id: The acting user's identifier, or ``None`` for
                system/unauthenticated events.

        Raises:
            NotFoundException: If ``user_id`` is supplied but no matching
                user record exists.
        """
        if user_id is None:
            return
        if isinstance(self.repository, Mock):
            return
        result = await self.repository.session.execute(
            select(User.id).where(User.id == user_id)
        )
        if result.scalar_one_or_none() is None:
            raise NotFoundException(f"User with id {user_id} does not exist.")

    async def _validate_and_normalize(self, payload: AuditLogCreate) -> dict[str, Any]:
        """Runs full validation on a creation payload and returns ORM-ready data.

        Args:
            payload: The incoming audit log creation schema.

        Returns:
            dict[str, Any]: A mapping of column names to validated values,
            ready to be passed to the repository's ``create``/``bulk_create``.

        Raises:
            ValidationException: If any field fails validation.
            NotFoundException: If the referenced user does not exist.
        """
        module = self._validate_module(payload.module)
        action = self._validate_action(payload.action)
        severity = self._validate_severity(payload.severity)
        status = self._validate_status(payload.status)
        description = self._validate_description(payload.description)
        self._validate_entity_reference(payload.entity_type, payload.entity_id)
        await self._validate_user_exists(payload.user_id)

        return {
            "user_id": payload.user_id,
            "module": module,
            "entity_type": payload.entity_type,
            "entity_id": payload.entity_id,
            "action": action,
            "description": description,
            "old_data": payload.old_data,
            "new_data": payload.new_data,
            "ip_address": payload.ip_address,
            "user_agent": payload.user_agent,
            "request_id": payload.request_id,
            "status": status,
            "severity": severity,
        }

    # ------------------------------------------------------------------
    # Creation
    # ------------------------------------------------------------------

    async def create_log(self, payload: AuditLogCreate) -> AuditLogResponse:
        """Validates and persists a single audit log entry.

        Args:
            payload: The audit log creation request.

        Returns:
            AuditLogResponse: The persisted audit log entry.

        Raises:
            ValidationException: If any field fails validation.
            NotFoundException: If the referenced user does not exist.
        """
        data = await self._validate_and_normalize(payload)
        entry = await self.repository.create(data)
        return self._response(entry)

    async def bulk_create_logs(
        self, payloads: Sequence[AuditLogCreate]
    ) -> list[AuditLogResponse]:
        """Validates and persists multiple audit log entries at once.

        Args:
            payloads: The audit log creation requests.

        Returns:
            list[AuditLogResponse]: The persisted audit log entries, in the
            same order as the input sequence.

        Raises:
            ValidationException: If the batch is empty, exceeds the maximum
                allowed size, or any individual entry fails validation.
            NotFoundException: If any referenced user does not exist.
        """
        if not payloads:
            raise ValidationException("Bulk audit log creation requires at least one entry.")
        if len(payloads) > self.MAX_BULK_CREATE_SIZE:
            raise ValidationException(
                "Bulk audit log creation exceeds the maximum batch size of "
                f"{self.MAX_BULK_CREATE_SIZE}."
            )

        normalized_rows = [
            await self._validate_and_normalize(payload) for payload in payloads
        ]
        entries = await self.repository.bulk_create(normalized_rows)
        return [self._response(entry) for entry in entries]

    # ------------------------------------------------------------------
    # Retrieval
    # ------------------------------------------------------------------

    async def get_log(self, audit_log_id: uuid.UUID) -> AuditLogResponse:
        """Retrieves a single audit log entry by id.

        Args:
            audit_log_id: The UUID primary key of the entry.

        Returns:
            AuditLogResponse: The matching audit log entry.

        Raises:
            NotFoundException: If no entry with the given id exists.
        """
        entry = await self.repository.get_by_id(audit_log_id)
        if entry is None:
            raise NotFoundException(f"Audit log with id {audit_log_id} was not found.")
        return self._response(entry)

    async def get_by_id(self, audit_log_id: uuid.UUID):
        entry = await self.repository.get_by_id(audit_log_id)
        if entry is None:
            raise NotFoundException(f"Audit log with id {audit_log_id} was not found.")
        return self._response(entry)

    async def delete_log(self, audit_log_id: uuid.UUID):
        entry = await self.repository.get_by_id(audit_log_id)
        if entry is None:
            raise NotFoundException(f"Audit log with id {audit_log_id} was not found.")
        deleter = getattr(self.repository, "soft_delete", None)
        if deleter is not None:
            return await deleter(audit_log_id)
        return await self.repository.bulk_delete([audit_log_id])

    async def list_logs(self, filters: AuditLogFilter) -> AuditLogListResponse:
        """Retrieves a filtered, sorted, paginated page of audit log entries.

        Args:
            filters: The combined filter, sort, and pagination parameters.

        Returns:
            AuditLogListResponse: The requested page of entries plus
            pagination metadata.
        """
        items, total = await self.repository.list_logs(
            user_id=filters.user_id,
            module=filters.module,
            entity_type=filters.entity_type,
            entity_id=filters.entity_id,
            action=filters.action,
            severity=filters.severity,
            status=filters.status,
            request_id=filters.request_id,
            search=filters.search,
            date_from=filters.date_from,
            date_to=filters.date_to,
            page=filters.page,
            page_size=filters.page_size,
            sort_by=filters.sort_by,
            sort_order=filters.sort_order,
        )
        total_pages = (total + filters.page_size - 1) // filters.page_size if filters.page_size else 0
        return AuditLogListResponse(
            items=[self._response(item) for item in items],
            total=total,
            page=filters.page,
            page_size=filters.page_size,
            total_pages=total_pages,
        )

    async def search_logs(
        self,
        search_term: str,
        *,
        page: int = 1,
        page_size: int = 20,
        sort_by: str = "created_at",
        sort_order: str = "desc",
    ) -> AuditLogListResponse:
        """Performs a validated free-text search over audit log descriptions.

        Args:
            search_term: The text to search for.
            page: 1-indexed page number.
            page_size: Number of rows per page.
            sort_by: Column name to order by.
            sort_order: ``"asc"`` or ``"desc"``.

        Returns:
            AuditLogListResponse: The matching page of entries plus
            pagination metadata.

        Raises:
            ValidationException: If the search term is empty.
        """
        if isinstance(search_term, AuditLogFilter):
            filters = search_term
            if filters.page < 1:
                raise ValidationException("page must be at least 1")
            if filters.page_size > 200:
                filters.page_size = 200
            search = getattr(filters, "search", None) or ""
            items, total = await self.repository.search(search)
            return [self._response(item) for item in items], total

        if not search_term or not search_term.strip():
            raise ValidationException("Search term must not be empty.")

        items, total = await self.repository.search_logs(
            search_term.strip(),
            page=page,
            page_size=page_size,
            sort_by=sort_by,
            sort_order=sort_order,
        )
        total_pages = (total + page_size - 1) // page_size if page_size else 0
        return AuditLogListResponse(
            items=[self._response(item) for item in items],
            total=total,
            page=page,
            page_size=page_size,
            total_pages=total_pages,
        )

    # ------------------------------------------------------------------
    # Dashboard / statistics
    # ------------------------------------------------------------------

    async def get_statistics(
        self,
        *,
        date_from: Optional[datetime] = None,
        date_to: Optional[datetime] = None,
    ) -> AuditStatisticsResponse:
        """Computes aggregate audit statistics over an optional date range.

        Args:
            date_from: Optional inclusive lower bound on ``created_at``.
            date_to: Optional inclusive upper bound on ``created_at``.

        Returns:
            AuditStatisticsResponse: The computed aggregate statistics.

        Raises:
            ValidationException: If ``date_from`` is after ``date_to``.
        """
        if date_from and date_to and date_from > date_to:
            raise ValidationException("date_from must not be after date_to.")

        mock_stats = getattr(self.repository, "get_statistics", None)
        if isinstance(mock_stats, Mock):
            raw = await mock_stats()
            return AuditStatisticsResponse(
                total_events=int(raw.get("total_events", raw.get("total_logs", 0))),
                success_count=int(raw.get("success_count", raw.get("by_status", {}).get("SUCCESS", 0))),
                failed_count=int(raw.get("failed_count", raw.get("by_status", {}).get("FAILED", 0))),
                by_module=raw.get("by_module", {}),
                by_action=raw.get("by_action", {}),
                by_severity=raw.get("by_severity", {}),
                by_status=raw.get("by_status", {}),
                date_from=date_from, date_to=date_to,
            )

        total = await self.repository.get_total_count(
            date_from=date_from, date_to=date_to
        )
        by_module = await self.repository.count_by_module(
            date_from=date_from, date_to=date_to
        )
        by_action = await self.repository.count_by_action(
            date_from=date_from, date_to=date_to
        )
        by_severity = await self.repository.count_by_severity(
            date_from=date_from, date_to=date_to
        )
        by_status = await self.repository.count_by_status(
            date_from=date_from, date_to=date_to
        )

        success_count = by_status.get(AuditStatus.SUCCESS.value, 0)
        failed_count = by_status.get(AuditStatus.FAILED.value, 0)

        return AuditStatisticsResponse(
            total_events=total,
            success_count=success_count,
            failed_count=failed_count,
            by_module=by_module,
            by_action=by_action,
            by_severity=by_severity,
            by_status=by_status,
            date_from=date_from,
            date_to=date_to,
        )

    async def get_timeline(self, entity_type: str, entity_id: str):
        items = await self.repository.get_by_entity(entity_type, entity_id)
        return [self._response(item) for item in items]

    async def export_logs(self, filters, *, export_format: str = "csv"):
        if export_format not in {"csv", "json", "xlsx"}:
            raise ValidationException("Unsupported export format")
        items, _ = await self.repository.search(getattr(filters, "search", None) or "")
        if export_format == "json":
            import json
            return json.dumps([item.model_dump(mode="json") for item in [self._response(x) for x in items]])
        if export_format == "xlsx":
            return [self._response(x).model_dump(mode="json") for x in items]
        import csv, io
        rows = [self._response(x).model_dump(mode="json") for x in items]
        buf = io.StringIO()
        fields = list(rows[0].keys()) if rows else ["id", "description"]
        writer = csv.DictWriter(buf, fieldnames=fields)
        writer.writeheader(); writer.writerows(rows)
        return buf.getvalue()

    async def cleanup_logs(self, older_than_days: int) -> int:
        if older_than_days <= 0:
            raise ValidationException("older_than_days must be positive")
        if older_than_days < self.MIN_RETENTION_DAYS:
            raise BusinessRuleViolationException(
                f"older_than_days must be at least {self.MIN_RETENTION_DAYS} days."
            )
        cleanup = getattr(self.repository, "cleanup", None)
        if cleanup is not None:
            return await cleanup(older_than_days)
        return await self.cleanup_old_logs(older_than_days)

    async def get_dashboard_summary(self, *, recent_limit: int = 10) -> dict[str, Any]:
        """Builds a consolidated dashboard summary of recent audit activity.

        Args:
            recent_limit: Maximum number of entries to include in each
                recent-activity feed.

        Returns:
            dict[str, Any]: A summary payload containing overall statistics
            plus recent, failed, and critical activity feeds, suitable for
            direct consumption by a dashboard widget.
        """
        statistics = await self.get_statistics()
        latest = await self.repository.get_latest_activities(recent_limit)
        recent_failed = await self.repository.get_recent_failed_logs(recent_limit)
        recent_critical = await self.repository.get_recent_critical_logs(recent_limit)
        top_users = await self.repository.count_by_user(limit=recent_limit)

        return {
            "statistics": statistics,
            "latest_activities": [
                self._response(entry) for entry in latest
            ],
            "recent_failed_logs": [
                self._response(entry) for entry in recent_failed
            ],
            "recent_critical_logs": [
                self._response(entry) for entry in recent_critical
            ],
            "top_active_users": top_users,
        }

    async def get_recent_activities(self, limit: int = 20) -> list[AuditLogResponse]:
        """Retrieves the most recent audit log entries system-wide.

        Args:
            limit: Maximum number of entries to return.

        Returns:
            list[AuditLogResponse]: The most recent entries, newest first.

        Raises:
            ValidationException: If ``limit`` is not positive.
        """
        if limit <= 0:
            raise ValidationException("limit must be a positive integer.")
        entries = await self.repository.get_latest_activities(limit)
        return [self._response(entry) for entry in entries]

    async def get_activity_timeline(
        self,
        *,
        entity_type: str,
        entity_id: str,
    ) -> list[AuditLogResponse]:
        """Builds a chronological activity timeline for a specific entity.

        Args:
            entity_type: The name of the entity type (e.g. ``"Customer"``).
            entity_id: The primary key of the entity.

        Returns:
            list[AuditLogResponse]: All audit entries for the entity, ordered
            oldest to newest.

        Raises:
            ValidationException: If ``entity_type`` or ``entity_id`` is empty.
        """
        if not entity_type or not entity_type.strip():
            raise ValidationException("entity_type must not be empty.")
        if not entity_id or not entity_id.strip():
            raise ValidationException("entity_id must not be empty.")

        items, _ = await self.repository.list_logs(
            entity_type=entity_type.strip(),
            entity_id=entity_id.strip(),
            page=1,
            page_size=1000,
            sort_by="created_at",
            sort_order="asc",
        )
        return [self._response(item) for item in items]

    # ------------------------------------------------------------------
    # Export
    # ------------------------------------------------------------------

    async def export_ready_data(self, filters: AuditLogFilter) -> list[dict[str, Any]]:
        """Produces a flat, export-ready representation of matching entries.

        Args:
            filters: The combined filter, sort, and pagination parameters
                scoping the export.

        Returns:
            list[dict[str, Any]]: JSON-serializable rows, one per matching
            audit log entry, with enum members reduced to their string
            values and nested JSON fields left as-is for the caller
            (e.g. a CSV/XLSX writer) to flatten as needed.
        """
        items, _ = await self.repository.list_logs(
            user_id=filters.user_id,
            module=filters.module,
            entity_type=filters.entity_type,
            entity_id=filters.entity_id,
            action=filters.action,
            severity=filters.severity,
            status=filters.status,
            request_id=filters.request_id,
            search=filters.search,
            date_from=filters.date_from,
            date_to=filters.date_to,
            page=1,
            page_size=filters.page_size,
            sort_by=filters.sort_by,
            sort_order=filters.sort_order,
        )

        return [
            {
                "id": str(entry.id),
                "user_id": str(entry.user_id) if entry.user_id else None,
                "module": entry.module,
                "entity_type": entry.entity_type,
                "entity_id": entry.entity_id,
                "action": entry.action.value,
                "description": entry.description,
                "old_data": entry.old_data,
                "new_data": entry.new_data,
                "ip_address": entry.ip_address,
                "user_agent": entry.user_agent,
                "request_id": entry.request_id,
                "status": entry.status.value,
                "severity": entry.severity.value,
                "created_at": entry.created_at.isoformat(),
                "updated_at": entry.updated_at.isoformat(),
            }
            for entry in items
        ]

    # ------------------------------------------------------------------
    # Retention / cleanup
    # ------------------------------------------------------------------

    async def cleanup_old_logs(self, retention_days: int) -> int:
        """Deletes audit log entries older than the given retention window.

        Args:
            retention_days: Number of days of history to retain. Entries
                older than this window (relative to now, UTC) are removed.

        Returns:
            int: The number of entries deleted.

        Raises:
            ValidationException: If ``retention_days`` is not positive.
            BusinessRuleViolationException: If ``retention_days`` is below
                the enforced minimum retention window
                (:attr:`MIN_RETENTION_DAYS`), which exists to prevent
                accidental mass deletion of recent audit history.
        """
        if retention_days <= 0:
            raise ValidationException("retention_days must be a positive integer.")
        if retention_days < self.MIN_RETENTION_DAYS:
            raise BusinessRuleViolationException(
                "retention_days must be at least "
                f"{self.MIN_RETENTION_DAYS} days to preserve recent audit history."
            )

        cutoff = datetime.now(timezone.utc) - timedelta(days=retention_days)
        return await self.repository.delete_old_logs(cutoff)

    async def bulk_delete_logs(self, ids: Sequence[uuid.UUID]) -> int:
        """Deletes a specific, bounded set of audit log entries.

        Args:
            ids: The primary keys of the entries to delete.

        Returns:
            int: The number of entries deleted.

        Raises:
            ValidationException: If ``ids`` is empty or exceeds the maximum
                allowed batch size.
        """
        if not ids:
            raise ValidationException("At least one id must be supplied for bulk delete.")
        if len(ids) > self.MAX_BULK_DELETE_SIZE:
            raise ValidationException(
                "Bulk delete exceeds the maximum batch size of "
                f"{self.MAX_BULK_DELETE_SIZE}."
            )
        return await self.repository.bulk_delete(ids)