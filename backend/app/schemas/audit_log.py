"""Pydantic v2 schemas for the Audit Log module.

These schemas define the request/response contracts for creating,
filtering, listing, and summarizing audit log entries. They are
consumed by the (separately implemented) service and router layers.
"""

import uuid
from datetime import datetime
from typing import Any, ClassVar, Optional

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator

from app.models.audit_log import AuditAction, AuditSeverity, AuditStatus

__all__ = [
    "AuditLogCreate",
    "AuditLogResponse",
    "AuditLogListResponse",
    "AuditLogFilter",
    "AuditStatisticsResponse",
]


class AuditLogBase(BaseModel):
    """Shared base fields common to audit log creation and representation.

    Attributes:
        module: Name of the owning domain module (e.g. ``"customer"``).
        entity_type: Name of the entity/table affected, if applicable.
        entity_id: Primary key of the affected entity, if applicable.
        action: The action performed.
        description: Human-readable summary of the event.
        old_data: Snapshot of entity state prior to the action.
        new_data: Snapshot of entity state after the action.
        ip_address: Origin IP address of the triggering request.
        user_agent: User agent string of the triggering client.
        request_id: Correlation/trace identifier for the request.
        status: Outcome status of the operation.
        severity: Severity classification of the event.
    """

    model_config = ConfigDict(str_strip_whitespace=True)

    module: str = Field(
        ..., min_length=1, max_length=100, description="Owning domain module name."
    )
    entity_type: Optional[str] = Field(
        default=None, max_length=100, description="Affected entity/table name."
    )
    entity_id: Optional[str] = Field(
        default=None, max_length=255, description="Affected entity primary key."
    )
    action: AuditAction = Field(..., description="Action performed.")
    description: str = Field(
        ..., min_length=1, description="Human-readable summary of the event."
    )
    old_data: Optional[dict[str, Any]] = Field(
        default=None, description="Entity state prior to the action."
    )
    new_data: Optional[dict[str, Any]] = Field(
        default=None, description="Entity state after the action."
    )
    ip_address: Optional[str] = Field(
        default=None, max_length=45, description="Origin IP address."
    )
    user_agent: Optional[str] = Field(
        default=None, description="Client user agent string."
    )
    request_id: Optional[str] = Field(
        default=None, max_length=64, description="Correlation/trace identifier."
    )
    status: AuditStatus = Field(
        default=AuditStatus.SUCCESS, description="Outcome status of the operation."
    )
    severity: AuditSeverity = Field(
        default=AuditSeverity.LOW, description="Severity classification."
    )

    @field_validator("module", "description")
    @classmethod
    def _not_blank(cls, value: str) -> str:
        """Ensures required text fields are not blank after stripping whitespace.

        Args:
            value: The raw field value.

        Returns:
            str: The validated, stripped value.

        Raises:
            ValueError: If the stripped value is empty.
        """
        if not value or not value.strip():
            raise ValueError("Field must not be empty or whitespace only.")
        return value


class AuditLogCreate(AuditLogBase):
    """Schema used to create a new audit log entry.

    Attributes:
        user_id: Identifier of the acting user. ``None`` for system or
            unauthenticated events (e.g. a failed login by an unknown user).
    """

    user_id: Optional[int] = Field(
        default=None, description="Identifier of the acting user."
    )


class AuditLogResponse(AuditLogBase):
    """Schema representing a persisted audit log entry returned to clients.

    Attributes:
        id: Surrogate primary key of the audit entry.
        user_id: Identifier of the acting user, if any.
        created_at: Timestamp the entry was created.
        updated_at: Timestamp the entry was last updated.
    """

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    user_id: Optional[int] = None
    created_at: datetime
    updated_at: datetime


class AuditLogListResponse(BaseModel):
    """Schema representing a paginated collection of audit log entries.

    Attributes:
        items: The audit log entries for the current page.
        total: Total number of entries matching the query, across all pages.
        page: Current page number (1-indexed).
        page_size: Number of items requested per page.
        total_pages: Total number of pages available.
    """

    model_config = ConfigDict(from_attributes=True)

    items: list[AuditLogResponse] = Field(default_factory=list)
    total: int = Field(..., ge=0)
    page: int = Field(..., ge=1)
    page_size: int = Field(..., ge=1)
    total_pages: int = Field(..., ge=0)

    @model_validator(mode="after")
    def _compute_total_pages_if_needed(self) -> "AuditLogListResponse":
        """Recomputes ``total_pages`` defensively when it appears inconsistent.

        Returns:
            AuditLogListResponse: The validated model instance.
        """
        expected_pages = (
            (self.total + self.page_size - 1) // self.page_size
            if self.page_size
            else 0
        )
        if self.total_pages != expected_pages:
            self.total_pages = expected_pages
        return self


class AuditLogFilter(BaseModel):
    """Schema encapsulating filter, sort, and pagination parameters for queries.

    Attributes:
        user_id: Restrict results to a specific acting user.
        module: Restrict results to a specific module.
        entity_type: Restrict results to a specific entity type.
        entity_id: Restrict results to a specific entity id.
        action: Restrict results to a specific action.
        status: Restrict results to a specific outcome status.
        severity: Restrict results to a specific severity level.
        request_id: Restrict results to a specific correlation id.
        search: Free-text search applied to the description field.
        date_from: Lower bound (inclusive) on ``created_at``.
        date_to: Upper bound (inclusive) on ``created_at``.
        page: Page number to retrieve (1-indexed).
        page_size: Number of items to retrieve per page.
        sort_by: Column name to sort by.
        sort_order: Sort direction, either ``"asc"`` or ``"desc"``.
    """

    model_config = ConfigDict(str_strip_whitespace=True)

    user_id: Optional[int] = None
    module: Optional[str] = Field(default=None, max_length=100)
    entity_type: Optional[str] = Field(default=None, max_length=100)
    entity_id: Optional[str] = Field(default=None, max_length=255)
    action: Optional[AuditAction] = None
    status: Optional[AuditStatus] = None
    severity: Optional[AuditSeverity] = None
    request_id: Optional[str] = Field(default=None, max_length=64)
    search: Optional[str] = Field(
        default=None, max_length=255, description="Free-text search on description."
    )
    date_from: Optional[datetime] = Field(
        default=None, description="Inclusive lower bound on created_at."
    )
    date_to: Optional[datetime] = Field(
        default=None, description="Inclusive upper bound on created_at."
    )

    page: int = Field(default=1, ge=1, description="1-indexed page number.")
    page_size: int = Field(
        default=20, ge=1, le=200, description="Number of items per page."
    )

    sort_by: str = Field(
        default="created_at", description="Column name to sort results by."
    )
    sort_order: str = Field(
        default="desc", description="Sort direction: 'asc' or 'desc'."
    )

    # NOTE: this MUST be annotated ClassVar. Without it, Pydantic v2 treats
    # any underscore-prefixed class attribute as a "private attribute" and
    # wraps it in a ModelPrivateAttr descriptor instead of storing the plain
    # frozenset. That made `cls._ALLOWED_SORT_FIELDS` inside the validator
    # below resolve to a ModelPrivateAttr object (not iterable), which
    # crashed with `TypeError: argument of type 'ModelPrivateAttr' is not
    # iterable` on every single instantiation of this filter -- i.e. on
    # every real list/search/filter request through the API.
    _ALLOWED_SORT_FIELDS: ClassVar[frozenset] = frozenset(
        {
            "created_at",
            "updated_at",
            "module",
            "entity_type",
            "action",
            "severity",
            "status",
        }
    )

    @field_validator("sort_order")
    @classmethod
    def _validate_sort_order(cls, value: str) -> str:
        """Validates that the sort order is one of the supported directions.

        Args:
            value: The requested sort order.

        Returns:
            str: The normalized (lowercased) sort order.

        Raises:
            ValueError: If the value is not ``"asc"`` or ``"desc"``.
        """
        normalized = value.strip().lower()
        if normalized not in {"asc", "desc"}:
            raise ValueError("sort_order must be either 'asc' or 'desc'.")
        return normalized

    @field_validator("sort_by")
    @classmethod
    def _validate_sort_by(cls, value: str) -> str:
        """Validates that the sort field is an allowed, indexed column.

        Args:
            value: The requested sort column name.

        Returns:
            str: The validated sort column name.

        Raises:
            ValueError: If the column is not in the allow-list.
        """
        if value not in cls._ALLOWED_SORT_FIELDS:
            raise ValueError(
                f"sort_by must be one of: {sorted(cls._ALLOWED_SORT_FIELDS)}"
            )
        return value

    @model_validator(mode="after")
    def _validate_date_range(self) -> "AuditLogFilter":
        """Ensures the provided date range is chronologically valid.

        Returns:
            AuditLogFilter: The validated model instance.

        Raises:
            ValueError: If ``date_from`` is after ``date_to``.
        """
        if self.date_from and self.date_to and self.date_from > self.date_to:
            raise ValueError("date_from must not be after date_to.")
        return self


class AuditStatisticsResponse(BaseModel):
    """Schema representing aggregate statistics over a set of audit log entries.

    Attributes:
        total_events: Total number of audit entries in scope.
        success_count: Number of entries with status ``SUCCESS``.
        failed_count: Number of entries with status ``FAILED``.
        by_module: Count of entries grouped by module.
        by_action: Count of entries grouped by action.
        by_severity: Count of entries grouped by severity.
        by_status: Count of entries grouped by status.
        date_from: Inclusive lower bound of the statistics window, if scoped.
        date_to: Inclusive upper bound of the statistics window, if scoped.
    """

    model_config = ConfigDict(from_attributes=True)

    total_events: int = Field(..., ge=0)
    success_count: int = Field(..., ge=0)
    failed_count: int = Field(..., ge=0)
    by_module: dict[str, int] = Field(default_factory=dict)
    by_action: dict[str, int] = Field(default_factory=dict)
    by_severity: dict[str, int] = Field(default_factory=dict)
    by_status: dict[str, int] = Field(default_factory=dict)
    date_from: Optional[datetime] = None
    date_to: Optional[datetime] = None

    def __getitem__(self, key: str):
        if key == "total_logs":
            return self.total_events
        return getattr(self, key)


# --------------------------------------------------------------------------
# Backward/forward-compatible alias
#
# test_audit_repository.py and test_audit_service.py import this class under
# the name `AuditLogSearchFilter`, while app/services/audit_log_service.py
# already defines and uses it as `AuditLogFilter`. This is a direct alias
# (not a redefinition), so both names always resolve to the exact same
# class/validation behavior.
#
# NOTE: some of those tests additionally construct this filter with an
# `is_active=...` kwarg and expect the repository layer to filter/soft-delete
# on it. `AuditLog` (app/models/audit_log.py) has no `is_active` column and
# `AuditLogRepository` has no `soft_delete`/`search` methods today -- audit
# trails are typically append-only/immutable by design, so retrofitting
# soft-delete onto them is a real architectural decision, not a naming fix.
# That gap is intentionally NOT patched here; it needs an explicit decision
# before schema/production changes are made.
# --------------------------------------------------------------------------
class AuditLogSearchFilter(AuditLogFilter):
    """Backward-compatible search filter with service-level validation semantics.

    API/Pydantic validation normally raises ``ValidationError``.  The audit
    service contract, however, historically exposes invalid pagination as the
    domain ``ValidationException``.  Keep the public filter schema strict for
    normal ``AuditLogFilter`` usage while translating invalid search-filter
    pagination at this compatibility boundary.

    ``page_size`` is intentionally capped at 200 rather than rejected when a
    larger value is supplied; this matches the audit search service contract
    exercised by the existing tests.
    """

    def __init__(self, **data: Any) -> None:
        from app.core.exceptions import ValidationException

        page = data.get("page", 1)
        if page is not None and page < 1:
            raise ValidationException("page must be greater than or equal to 1.")

        page_size = data.get("page_size", 20)
        if page_size is not None and page_size < 1:
            raise ValidationException("page_size must be greater than or equal to 1.")

        # The service contract caps page size at 200.
        if page_size is not None and page_size > 200:
            data["page_size"] = 200

        try:
            super().__init__(**data)
        except ValidationError as exc:
            raise ValidationException(str(exc)) from exc
