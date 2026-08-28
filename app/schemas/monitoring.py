"""
backend/app/schemas/monitoring.py

Pydantic v2 schemas for the Enterprise Monitoring & Health module of the
Enterprise Real Estate AI Copilot CRM.

Generated exclusively from the approved `app.models.monitoring.SystemHealth`
ORM model. No fields, types, or defaults are assumed or carried over from
any other module's schemas.

Naming convention (mirrors `app/schemas/task.py` / `app/schemas/document.py`):
    - `*Create`             -> payload accepted on creation.
    - `*Update`              -> payload accepted on partial update
      (PATCH-style, all fields optional).
    - `*Response`            -> full representation returned by the API,
      including server-generated/audit fields.
    - `*ListResponse`        -> paginated collection wrapper.
    - `*Filter`              -> query/filter/sort/pagination parameters.
    - `*StatisticsResponse`  -> aggregate counts/metrics over a set of
      entries.

Additional module-specific response contracts:
    - `HealthCheckResponse`  -> result of running a single, ad-hoc health
      probe against a component.
    - `HealthStatusResponse` -> aggregated, whole-system health overview.
    - `MetricsResponse`      -> a single point-in-time metric sample for
      a component.
    - `ComponentResponse`    -> lightweight, dashboard-friendly view of a
      single monitored component.

Design Notes:
    - `ComponentType`, `HealthStatus`, and `MetricType` are imported
      directly from `app.models.monitoring` and reused as-is, so
      API-facing enum values always stay in lockstep with the ORM/DB
      enum definitions with zero duplication (mirrors
      `app/schemas/document.py`'s reuse of `DocumentCategory` /
      `DocumentFileType` / `DocumentStorageProvider`).
    - `ConfigDict(from_attributes=True)` is used on all response schemas
      so they can be constructed directly from `SystemHealth` ORM
      instances without manual dict conversion.
    - Validation constraints (`0 <= cpu_usage_percent <= 100`,
      `response_time_ms >= 0`, non-negative counters, non-blank
      `component_name`) mirror the `CheckConstraint`s declared in
      `SystemHealth.__table_args__` exactly, so a payload that passes
      schema validation will never be rejected by the database's own
      constraints.
    - Whitespace-only or empty-string input on optional free-text
      fields is normalized to `None` at the schema boundary, so the
      service/repository layers never have to special-case empty
      strings vs. NULL (mirrors `app/schemas/document.py`).
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Optional

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.models.monitoring import ComponentType, HealthStatus, MetricType

__all__ = [
    "SystemHealthCreate",
    "SystemHealthUpdate",
    "SystemHealthResponse",
    "SystemHealthListResponse",
    "HealthFilter",
    "HealthCheckResponse",
    "HealthStatusResponse",
    "MetricsResponse",
    "ComponentResponse",
    "MonitoringStatisticsResponse",
]

# --------------------------------------------------------------------------
# Shared Validation Constants
# --------------------------------------------------------------------------
_ALLOWED_SORT_FIELDS = frozenset(
    {
        "component_name",
        "component_type",
        "status",
        "cpu_usage_percent",
        "memory_usage_percent",
        "disk_usage_percent",
        "response_time_ms",
        "error_count",
        "warning_count",
        "last_health_check_at",
        "last_success_at",
        "last_failure_at",
        "created_at",
        "updated_at",
    }
)

_ALLOWED_SORT_ORDERS = frozenset({"asc", "desc"})


def _blank_to_none(value: Optional[str]) -> Optional[str]:
    """Strips surrounding whitespace and converts a blank string to `None`."""
    if value is None:
        return None
    stripped = value.strip()
    return stripped or None


# --------------------------------------------------------------------------
# SystemHealth Base Schema
# --------------------------------------------------------------------------
class SystemHealthBase(BaseModel):
    """
    Common fields shared across health-record creation, update, and
    response schemas. Contains no identity or audit fields.
    """

    model_config = ConfigDict(str_strip_whitespace=True)

    parent_component_id: Optional[uuid.UUID] = Field(
        default=None,
        description="Optional reference to a parent SystemHealth row this component rolls up into.",
        examples=["3fa85f64-5717-4562-b3fc-2c963f66afa6"],
    )
    component_name: str = Field(
        ...,
        min_length=1,
        max_length=150,
        description="Human-readable, unique-per-type name of the monitored component.",
        examples=["primary-postgres"],
    )
    component_type: ComponentType = Field(
        ...,
        description="Category of system component being monitored.",
        examples=["DATABASE"],
    )
    status: HealthStatus = Field(
        default=HealthStatus.UNKNOWN,
        description="Current operational health state of the component.",
        examples=["HEALTHY"],
    )
    cpu_usage_percent: Optional[float] = Field(
        default=None,
        ge=0,
        le=100,
        description="CPU utilization of the component, as a percentage (0-100).",
        examples=[42.5],
    )
    memory_usage_percent: Optional[float] = Field(
        default=None,
        ge=0,
        le=100,
        description="Memory utilization of the component, as a percentage (0-100).",
        examples=[68.2],
    )
    disk_usage_percent: Optional[float] = Field(
        default=None,
        ge=0,
        le=100,
        description="Disk utilization of the component, as a percentage (0-100).",
        examples=[35.0],
    )
    response_time_ms: Optional[float] = Field(
        default=None,
        ge=0,
        description="Latency of the most recent health check, in milliseconds.",
        examples=[124.75],
    )
    error_count: int = Field(
        default=0,
        ge=0,
        description="Count of errors observed since the last counter reset.",
        examples=[0],
    )
    warning_count: int = Field(
        default=0,
        ge=0,
        description="Count of warnings observed since the last counter reset.",
        examples=[0],
    )
    last_health_check_at: datetime = Field(
        ...,
        description="UTC timestamp of the most recent health check attempt.",
        examples=["2026-08-03T09:00:00Z"],
    )
    last_success_at: Optional[datetime] = Field(
        default=None,
        description="UTC timestamp of the most recent successful health check.",
        examples=["2026-08-03T09:00:00Z"],
    )
    last_failure_at: Optional[datetime] = Field(
        default=None,
        description="UTC timestamp of the most recent failed health check.",
        examples=[None],
    )
    status_message: Optional[str] = Field(
        default=None,
        description="Free-form human-readable detail about the current status.",
        examples=["Connection pool at 80% capacity"],
    )
    meta_data: Optional[dict] = Field(
        default=None,
        description="Arbitrary JSON payload for provider-specific diagnostics.",
        examples=[{"pool_size": 20, "active_connections": 16}],
    )
    is_active: bool = Field(
        default=True,
        description="Soft-disable flag; inactive components are excluded from active monitoring views.",
    )

    @field_validator("component_name", "status_message")
    @classmethod
    def normalize_blank_optional_strings(cls, value: Optional[str]) -> Optional[str]:
        """Normalizes blank optional free-text fields to `None`."""
        return _blank_to_none(value)

    @model_validator(mode="after")
    def validate_failure_success_not_both_future(self) -> "SystemHealthBase":
        """
        Validates that, when both are supplied, `last_success_at` and
        `last_failure_at` are not identical instants representing
        contradictory simultaneous outcomes.

        Returns:
            The validated model instance.

        Raises:
            ValueError: If both timestamps are supplied and equal.
        """
        if (
            self.last_success_at is not None
            and self.last_failure_at is not None
            and self.last_success_at == self.last_failure_at
        ):
            raise ValueError(
                "last_success_at and last_failure_at must not be identical timestamps."
            )
        return self


# --------------------------------------------------------------------------
# SystemHealth Create Schema
# --------------------------------------------------------------------------
class SystemHealthCreate(SystemHealthBase):
    """Request payload for creating a new health snapshot record."""

    model_config = ConfigDict(
        str_strip_whitespace=True,
        json_schema_extra={
            "example": {
                "parent_component_id": None,
                "component_name": "primary-postgres",
                "component_type": "DATABASE",
                "status": "HEALTHY",
                "cpu_usage_percent": 42.5,
                "memory_usage_percent": 68.2,
                "disk_usage_percent": 35.0,
                "response_time_ms": 12.4,
                "error_count": 0,
                "warning_count": 0,
                "last_health_check_at": "2026-08-03T09:00:00Z",
                "last_success_at": "2026-08-03T09:00:00Z",
                "last_failure_at": None,
                "status_message": None,
                "meta_data": {"pool_size": 20, "active_connections": 16},
                "is_active": True,
            }
        },
    )


# --------------------------------------------------------------------------
# SystemHealth Update Schema
# --------------------------------------------------------------------------
class SystemHealthUpdate(BaseModel):
    """
    Request payload for partially updating an existing health snapshot
    record (PATCH semantics). Every field is optional; only supplied
    fields are intended to be applied by the service layer.
    """

    model_config = ConfigDict(str_strip_whitespace=True)

    parent_component_id: Optional[uuid.UUID] = Field(default=None)
    component_name: Optional[str] = Field(default=None, min_length=1, max_length=150)
    component_type: Optional[ComponentType] = Field(default=None)
    status: Optional[HealthStatus] = Field(default=None)
    cpu_usage_percent: Optional[float] = Field(default=None, ge=0, le=100)
    memory_usage_percent: Optional[float] = Field(default=None, ge=0, le=100)
    disk_usage_percent: Optional[float] = Field(default=None, ge=0, le=100)
    response_time_ms: Optional[float] = Field(default=None, ge=0)
    error_count: Optional[int] = Field(default=None, ge=0)
    warning_count: Optional[int] = Field(default=None, ge=0)
    last_health_check_at: Optional[datetime] = Field(default=None)
    last_success_at: Optional[datetime] = Field(default=None)
    last_failure_at: Optional[datetime] = Field(default=None)
    status_message: Optional[str] = Field(default=None)
    meta_data: Optional[dict] = Field(default=None)
    is_active: Optional[bool] = Field(
        default=None,
        description="Soft-disable flag; inactive components are excluded from active monitoring views.",
    )
    is_deleted: Optional[bool] = Field(
        default=None,
        description="Soft delete flag; deleted health records are excluded everywhere.",
    )
    deleted_by_id: Optional[int] = Field(default=None)

    @field_validator("component_name", "status_message")
    @classmethod
    def normalize_blank_optional_strings(cls, value: Optional[str]) -> Optional[str]:
        """Normalizes blank optional free-text fields to `None`."""
        return _blank_to_none(value)


# --------------------------------------------------------------------------
# SystemHealth Response Schema
# --------------------------------------------------------------------------
class SystemHealthResponse(SystemHealthBase):
    """
    Outward-facing representation of a health snapshot record, returned
    by monitoring retrieval and mutation endpoints. Extends
    `SystemHealthBase` with identity, soft-delete, and audit fields
    populated by the persistence layer.
    """

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID = Field(
        ...,
        description="Globally unique identifier of the health snapshot record.",
        examples=["3fa85f64-5717-4562-b3fc-2c963f66afa6"],
    )
    is_deleted: bool = Field(
        default=False,
        description="Soft delete flag; deleted health records are excluded everywhere.",
    )
    deleted_at: Optional[datetime] = Field(
        default=None,
        description="UTC timestamp when the health record was soft-deleted, if deleted.",
    )
    deleted_by_id: Optional[int] = Field(
        default=None,
        description="Internal ID of the User who soft-deleted this health record, if deleted.",
    )
    created_by_id: Optional[int] = Field(
        default=None,
        description="Internal ID of the User who created this health record, if created interactively.",
        examples=[5],
    )
    updated_by_id: Optional[int] = Field(
        default=None,
        description="Internal ID of the User who last modified this health record, if modified interactively.",
        examples=[5],
    )
    created_at: datetime = Field(
        ...,
        description="UTC timestamp when the health record was created.",
        examples=["2026-08-03T09:00:00Z"],
    )
    updated_at: datetime = Field(
        ...,
        description="UTC timestamp when the health record was last updated.",
        examples=["2026-08-03T09:00:00Z"],
    )


# --------------------------------------------------------------------------
# SystemHealth List Response Schema
# --------------------------------------------------------------------------
class SystemHealthListResponse(BaseModel):
    """Paginated collection response for health-record listing/search endpoints."""

    model_config = ConfigDict(from_attributes=True)

    items: list[SystemHealthResponse] = Field(
        ...,
        description="The page of health snapshot records matching the query.",
    )
    total: int = Field(
        ...,
        ge=0,
        description="Total number of health records matching the query, across all pages.",
        examples=[9],
    )
    page: int = Field(
        ...,
        ge=1,
        description="Current page number (1-indexed).",
        examples=[1],
    )
    page_size: int = Field(
        ...,
        ge=1,
        description="Number of records requested per page.",
        examples=[20],
    )
    total_pages: int = Field(
        ...,
        ge=0,
        description="Total number of pages available for the given page_size.",
        examples=[1],
    )


# --------------------------------------------------------------------------
# Health Filter Schema (Filtering + Pagination + Sorting)
# --------------------------------------------------------------------------
class HealthFilter(BaseModel):
    """
    Query parameters for searching and filtering health records,
    including pagination and sorting controls.
    """

    model_config = ConfigDict(str_strip_whitespace=True)

    component_name: Optional[str] = Field(
        default=None,
        description="Filter by exact component name.",
        examples=["primary-postgres"],
    )
    component_type: Optional[ComponentType] = Field(
        default=None,
        description="Filter by component category.",
    )
    status: Optional[HealthStatus] = Field(
        default=None,
        description="Filter by current health status.",
    )
    parent_component_id: Optional[uuid.UUID] = Field(
        default=None,
        description="Filter by parent component ID (returns direct children).",
    )
    is_active: Optional[bool] = Field(
        default=None,
        description="Filter by soft-disable status.",
    )
    is_deleted: Optional[bool] = Field(
        default=False,
        description="Filter by soft-delete status. Defaults to excluding deleted records.",
    )
    search: Optional[str] = Field(
        default=None,
        min_length=1,
        max_length=150,
        description="Free-text search term matched against component_name.",
        examples=["postgres"],
    )
    page: int = Field(
        default=1,
        ge=1,
        description="Page number to retrieve (1-indexed).",
        examples=[1],
    )
    page_size: int = Field(
        default=20,
        ge=1,
        le=100,
        description="Number of records to return per page (max 100).",
        examples=[20],
    )
    sort_by: str = Field(
        default="last_health_check_at",
        description=(
            "Field to sort results by. Allowed values: component_name, "
            "component_type, status, cpu_usage_percent, memory_usage_percent, "
            "disk_usage_percent, response_time_ms, error_count, warning_count, "
            "last_health_check_at, last_success_at, last_failure_at, "
            "created_at, updated_at."
        ),
        examples=["last_health_check_at"],
    )
    sort_order: str = Field(
        default="desc",
        description="Sort direction: 'asc' or 'desc'.",
        examples=["desc"],
    )

    @field_validator("search", "component_name")
    @classmethod
    def validate_non_blank(cls, value: Optional[str]) -> Optional[str]:
        """
        Rejects a search/name term that is empty after whitespace
        stripping, since an effectively-blank string would otherwise
        defeat the purpose of filtering.

        Args:
            value: The raw string, or None if not supplied.

        Returns:
            The validated, stripped string, or None.

        Raises:
            ValueError: If the string is empty after stripping.
        """
        if value is not None and not value.strip():
            raise ValueError("Value must not be empty or whitespace only.")
        return value

    @field_validator("sort_by")
    @classmethod
    def validate_sort_by(cls, value: str) -> str:
        """
        Validates that `sort_by` references an allowed, indexable
        column to prevent arbitrary/unsafe field references reaching
        the query layer.

        Args:
            value: The requested sort field name.

        Returns:
            The validated sort field name.

        Raises:
            ValueError: If the field is not in the allowed set.
        """
        if value not in _ALLOWED_SORT_FIELDS:
            raise ValueError(
                f"sort_by must be one of: {', '.join(sorted(_ALLOWED_SORT_FIELDS))}."
            )
        return value

    @field_validator("sort_order")
    @classmethod
    def validate_sort_order(cls, value: str) -> str:
        """
        Validates that `sort_order` is either 'asc' or 'desc'.

        Args:
            value: The requested sort direction.

        Returns:
            The validated, lower-cased sort direction.

        Raises:
            ValueError: If the value is not 'asc' or 'desc'.
        """
        normalized = value.strip().lower()
        if normalized not in _ALLOWED_SORT_ORDERS:
            raise ValueError("sort_order must be 'asc' or 'desc'.")
        return normalized


# --------------------------------------------------------------------------
# Health Check Response Schema
# --------------------------------------------------------------------------
class HealthCheckResponse(BaseModel):
    """
    Result of running a single, ad-hoc health probe against one
    component, as returned by an on-demand "check now" endpoint.
    """

    model_config = ConfigDict(from_attributes=True)

    component_name: str = Field(
        ...,
        description="Name of the component that was probed.",
        examples=["primary-postgres"],
    )
    component_type: ComponentType = Field(
        ...,
        description="Category of the component that was probed.",
        examples=["DATABASE"],
    )
    status: HealthStatus = Field(
        ...,
        description="Health status determined by this probe.",
        examples=["HEALTHY"],
    )
    is_healthy: bool = Field(
        ...,
        description="Convenience boolean; True if status is HEALTHY.",
        examples=[True],
    )
    response_time_ms: Optional[float] = Field(
        default=None,
        ge=0,
        description="Latency of this probe, in milliseconds.",
        examples=[12.4],
    )
    message: Optional[str] = Field(
        default=None,
        description="Human-readable detail about the probe result.",
        examples=["Connection established and query executed successfully."],
    )
    checked_at: datetime = Field(
        ...,
        description="UTC timestamp when this probe was executed.",
        examples=["2026-08-03T09:00:00Z"],
    )


# --------------------------------------------------------------------------
# Health Status Response Schema (Aggregated System Overview)
# --------------------------------------------------------------------------
class HealthStatusResponse(BaseModel):
    """
    Aggregated, whole-system health overview, combining an overall
    status with a per-component breakdown. Intended for a top-level
    "/health" or monitoring-dashboard endpoint.
    """

    model_config = ConfigDict(from_attributes=True)

    overall_status: HealthStatus = Field(
        ...,
        description="Worst-case aggregated status across all monitored components.",
        examples=["DEGRADED"],
    )
    components: list[ComponentResponse] = Field(
        default_factory=list,
        description="Per-component health breakdown.",
    )
    healthy_count: int = Field(
        ...,
        ge=0,
        description="Number of components currently HEALTHY.",
        examples=[7],
    )
    degraded_count: int = Field(
        ...,
        ge=0,
        description="Number of components currently DEGRADED.",
        examples=[1],
    )
    unhealthy_count: int = Field(
        ...,
        ge=0,
        description="Number of components currently UNHEALTHY.",
        examples=[0],
    )
    down_count: int = Field(
        ...,
        ge=0,
        description="Number of components currently DOWN.",
        examples=[0],
    )
    checked_at: datetime = Field(
        ...,
        description="UTC timestamp when this aggregated view was computed.",
        examples=["2026-08-03T09:00:00Z"],
    )


# --------------------------------------------------------------------------
# Metrics Response Schema
# --------------------------------------------------------------------------
class MetricsResponse(BaseModel):
    """A single point-in-time metric sample for a monitored component."""

    model_config = ConfigDict(from_attributes=True)

    component_name: str = Field(
        ...,
        description="Name of the component this metric was recorded for.",
        examples=["primary-postgres"],
    )
    component_type: ComponentType = Field(
        ...,
        description="Category of the component this metric was recorded for.",
        examples=["DATABASE"],
    )
    metric_type: MetricType = Field(
        ...,
        description="Kind of metric being reported.",
        examples=["CPU_USAGE"],
    )
    value: float = Field(
        ...,
        description="Numeric value of the metric sample.",
        examples=[42.5],
    )
    unit: Optional[str] = Field(
        default=None,
        description="Unit of measurement for the value (e.g. 'percent', 'ms', 'count').",
        examples=["percent"],
    )
    recorded_at: datetime = Field(
        ...,
        description="UTC timestamp when this metric sample was recorded.",
        examples=["2026-08-03T09:00:00Z"],
    )


# --------------------------------------------------------------------------
# Component Response Schema (Lightweight Dashboard View)
# --------------------------------------------------------------------------
class ComponentResponse(BaseModel):
    """
    Lightweight, dashboard-friendly representation of a single
    monitored component's current state.
    """

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID = Field(
        ...,
        description="Globally unique identifier of the health snapshot record.",
        examples=["3fa85f64-5717-4562-b3fc-2c963f66afa6"],
    )
    component_name: str = Field(
        ...,
        description="Human-readable name of the monitored component.",
        examples=["primary-postgres"],
    )
    component_type: ComponentType = Field(
        ...,
        description="Category of system component being monitored.",
        examples=["DATABASE"],
    )
    status: HealthStatus = Field(
        ...,
        description="Current operational health state of the component.",
        examples=["HEALTHY"],
    )
    cpu_usage_percent: Optional[float] = Field(
        default=None,
        description="CPU utilization of the component, as a percentage (0-100).",
        examples=[42.5],
    )
    memory_usage_percent: Optional[float] = Field(
        default=None,
        description="Memory utilization of the component, as a percentage (0-100).",
        examples=[68.2],
    )
    disk_usage_percent: Optional[float] = Field(
        default=None,
        description="Disk utilization of the component, as a percentage (0-100).",
        examples=[35.0],
    )
    response_time_ms: Optional[float] = Field(
        default=None,
        description="Latency of the most recent health check, in milliseconds.",
        examples=[12.4],
    )
    error_count: int = Field(
        default=0,
        description="Count of errors observed since the last counter reset.",
        examples=[0],
    )
    warning_count: int = Field(
        default=0,
        description="Count of warnings observed since the last counter reset.",
        examples=[0],
    )
    last_health_check_at: datetime = Field(
        ...,
        description="UTC timestamp of the most recent health check attempt.",
        examples=["2026-08-03T09:00:00Z"],
    )


# --------------------------------------------------------------------------
# Monitoring Statistics Response Schema
# --------------------------------------------------------------------------
class MonitoringStatisticsResponse(BaseModel):
    """
    Aggregate monitoring statistics over the full set of monitored
    components, for a summary/reporting dashboard.
    """

    model_config = ConfigDict(from_attributes=True)

    total_components: int = Field(
        ...,
        ge=0,
        description="Total number of monitored components (excluding soft-deleted).",
        examples=[9],
    )
    healthy_count: int = Field(
        ...,
        ge=0,
        description="Number of components currently HEALTHY.",
        examples=[7],
    )
    degraded_count: int = Field(
        ...,
        ge=0,
        description="Number of components currently DEGRADED.",
        examples=[1],
    )
    unhealthy_count: int = Field(
        ...,
        ge=0,
        description="Number of components currently UNHEALTHY.",
        examples=[0],
    )
    down_count: int = Field(
        ...,
        ge=0,
        description="Number of components currently DOWN.",
        examples=[0],
    )
    maintenance_count: int = Field(
        ...,
        ge=0,
        description="Number of components currently in MAINTENANCE.",
        examples=[1],
    )
    unknown_count: int = Field(
        ...,
        ge=0,
        description="Number of components with UNKNOWN status.",
        examples=[0],
    )
    average_response_time_ms: Optional[float] = Field(
        default=None,
        ge=0,
        description="Average response time across all components, in milliseconds.",
        examples=[87.3],
    )
    total_error_count: int = Field(
        ...,
        ge=0,
        description="Sum of error_count across all components.",
        examples=[3],
    )
    total_warning_count: int = Field(
        ...,
        ge=0,
        description="Sum of warning_count across all components.",
        examples=[12],
    )
    uptime_percentage: Optional[float] = Field(
        default=None,
        ge=0,
        le=100,
        description="Percentage of components currently HEALTHY or DEGRADED (i.e. not DOWN/UNHEALTHY).",
        examples=[97.8],
    )
    by_component_type: dict[str, int] = Field(
        default_factory=dict,
        description="Count of monitored components grouped by component_type.",
        examples=[{"DATABASE": 1, "AI_PROVIDER": 2}],
    )
    generated_at: datetime = Field(
        ...,
        description="UTC timestamp when these statistics were computed.",
        examples=["2026-08-03T09:00:00Z"],
    )