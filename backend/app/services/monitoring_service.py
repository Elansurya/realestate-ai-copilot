"""
backend/app/services/monitoring_service.py

Business/service layer for the Enterprise Monitoring & Health module of
the Enterprise Real Estate AI Copilot CRM.

This service owns every business rule for `SystemHealth` records:
component-identity validation, metric/threshold validation, derived
health-status computation, health-check execution, and system-wide
aggregation (whole-system status overview + statistics). It talks to
the database exclusively through `MonitoringRepository` and never
constructs SQLAlchemy statements itself.

Error handling:
    - This module raises ONLY project domain exceptions (from
      `app.core.exceptions`). It never raises or imports
      `fastapi.HTTPException` -- translating domain exceptions into
      HTTP responses is the router layer's responsibility, and routers
      are explicitly out of scope for this module.
    - Domain exceptions used here:
        * `NotFoundException`     -- referenced record does not exist
          (or is soft-deleted / otherwise ineligible).
        * `ConflictException`     -- a uniqueness/state conflict would
          be violated (e.g. duplicate component identity).
        * `ValidationException`   -- a business rule (beyond what the
          Pydantic schema layer already enforces) was violated.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Optional

from app.core.exceptions import ConflictException, NotFoundException, ValidationException
from app.models.monitoring import ComponentType, HealthStatus, SystemHealth
from app.repositories.monitoring_repository import MonitoringRepository
from app.schemas.monitoring import (
    ComponentResponse,
    HealthCheckResponse,
    HealthFilter,
    HealthStatusResponse,
    MonitoringStatisticsResponse,
    SystemHealthCreate,
    SystemHealthListResponse,
    SystemHealthResponse,
    SystemHealthUpdate,
)

__all__ = ["MonitoringService"]

# --------------------------------------------------------------------------
# Threshold Constants (Business Rules)
# --------------------------------------------------------------------------
# Resource-utilization thresholds (percent) used to derive a component's
# status from its raw metrics when the caller does not supply an explicit
# `status`. Mirrors typical enterprise monitoring SLOs: healthy below the
# warning line, degraded up to the critical line, unhealthy beyond it.
_RESOURCE_WARNING_THRESHOLD = 75.0
_RESOURCE_CRITICAL_THRESHOLD = 90.0

# Response-time thresholds (milliseconds).
_RESPONSE_TIME_WARNING_MS = 1000.0
_RESPONSE_TIME_CRITICAL_MS = 5000.0

# Error-count threshold (absolute count since last reset) above which a
# component cannot be considered HEALTHY regardless of resource metrics.
_ERROR_COUNT_UNHEALTHY_THRESHOLD = 10


class MonitoringService:
    """Business/service layer orchestrating `SystemHealth` operations."""

    def __init__(self, repository: MonitoringRepository) -> None:
        """
        Args:
            repository: The repository this service delegates all
                persistence operations to.
        """
        self._repository = repository

    # ------------------------------------------------------------------
    # Business Rule Helpers -- Component Validation
    # ------------------------------------------------------------------
    async def _ensure_component_identity_available(
        self,
        component_name: str,
        component_type: ComponentType,
        *,
        exclude_id: Optional[uuid.UUID] = None,
    ) -> None:
        """
        Validates that no other live (non-deleted) `SystemHealth` row
        already occupies the given `component_name` + `component_type`
        identity, enforcing the same uniqueness rule as
        `uq_system_health_component_name_component_type` at the
        business layer so callers get a clear domain exception instead
        of a raw database integrity error.

        Args:
            component_name: The component name to check.
            component_type: The component type to check.
            exclude_id: A record ID to exclude from the conflict check
                (used when updating a record's own identity fields).

        Raises:
            ConflictException: If another live record already uses
                this component identity.
        """
        existing = await self._repository.get_by_name_and_type(
            component_name, component_type
        )
        if existing is not None and existing.id != exclude_id:
            raise ConflictException(
                f"A health record for component '{component_name}' of type "
                f"'{component_type.value}' already exists."
            )

    async def _get_existing_or_raise(
        self,
        health_id: uuid.UUID,
        *,
        include_deleted: bool = False,
    ) -> SystemHealth:
        """
        Fetches a `SystemHealth` record by ID or raises `NotFoundException`.

        Args:
            health_id: The UUID of the record to fetch.
            include_deleted: If True, soft-deleted records are eligible.

        Returns:
            The matching `SystemHealth` instance.

        Raises:
            NotFoundException: If no matching record exists.
        """
        record = await self._repository.get_by_id(
            health_id, include_deleted=include_deleted
        )
        if record is None:
            raise NotFoundException(f"Health record '{health_id}' was not found.")
        return record

    # ------------------------------------------------------------------
    # Business Rule Helpers -- Health / Metric Validation
    # ------------------------------------------------------------------
    @staticmethod
    def _validate_parent_not_self(
        health_id: Optional[uuid.UUID],
        parent_component_id: Optional[uuid.UUID],
    ) -> None:
        """
        Validates that a component is not configured as its own parent,
        mirroring `ck_system_health_parent_not_self` at the business
        layer.

        Args:
            health_id: The ID of the record being created/updated, or
                `None` on creation before an ID is assigned.
            parent_component_id: The proposed parent component ID.

        Raises:
            ValidationException: If `parent_component_id` equals
                `health_id`.
        """
        if (
            health_id is not None
            and parent_component_id is not None
            and parent_component_id == health_id
        ):
            raise ValidationException(
                "A component cannot be configured as its own parent."
            )

    async def _validate_parent_exists(
        self, parent_component_id: Optional[uuid.UUID]
    ) -> None:
        """
        Validates that a referenced parent component actually exists
        and is not soft-deleted.

        Args:
            parent_component_id: The proposed parent component ID, or
                `None` to skip validation.

        Raises:
            NotFoundException: If `parent_component_id` is supplied but
                does not reference a live `SystemHealth` record.
        """
        if parent_component_id is None:
            return
        parent = await self._repository.get_by_id(parent_component_id)
        if parent is None:
            raise NotFoundException(
                f"Parent component '{parent_component_id}' was not found."
            )

    @staticmethod
    def _validate_failure_timestamp_not_before_success_gap(
        last_success_at: Optional[datetime], last_failure_at: Optional[datetime]
    ) -> None:
        """
        Business-layer sanity check: if both timestamps are present,
        neither may be set in the future relative to "now". Timezone-
        aware datetimes are required (enforced by the schema layer);
        this only adds the forward-looking-clock guard.

        Args:
            last_success_at: The last successful check timestamp.
            last_failure_at: The last failed check timestamp.

        Raises:
            ValidationException: If either timestamp is in the future.
        """
        now = datetime.now(timezone.utc)
        for label, value in (
            ("last_success_at", last_success_at),
            ("last_failure_at", last_failure_at),
        ):
            if value is not None and value > now:
                raise ValidationException(f"{label} cannot be set in the future.")

    @staticmethod
    def derive_status_from_metrics(
        *,
        cpu_usage_percent: Optional[float],
        memory_usage_percent: Optional[float],
        disk_usage_percent: Optional[float],
        response_time_ms: Optional[float],
        error_count: int,
    ) -> HealthStatus:
        """
        Derives a `HealthStatus` from raw resource/latency/error metrics
        using the module's threshold constants. This is the single
        source of truth for "what does this data mean" so status
        derivation is never duplicated or drifted between call sites.

        Precedence: any single metric breaching the CRITICAL threshold
        yields UNHEALTHY; any metric breaching the WARNING threshold
        (with none critical) yields DEGRADED; otherwise HEALTHY.

        Args:
            cpu_usage_percent: CPU utilization percentage, or `None`.
            memory_usage_percent: Memory utilization percentage, or `None`.
            disk_usage_percent: Disk utilization percentage, or `None`.
            response_time_ms: Health-check latency in milliseconds, or `None`.
            error_count: Count of errors observed since the last reset.

        Returns:
            The derived `HealthStatus`.
        """
        resource_values = [
            value
            for value in (cpu_usage_percent, memory_usage_percent, disk_usage_percent)
            if value is not None
        ]

        is_critical = (
            any(value >= _RESOURCE_CRITICAL_THRESHOLD for value in resource_values)
            or (
                response_time_ms is not None
                and response_time_ms >= _RESPONSE_TIME_CRITICAL_MS
            )
            or error_count >= _ERROR_COUNT_UNHEALTHY_THRESHOLD
        )
        if is_critical:
            return HealthStatus.UNHEALTHY

        is_degraded = (
            any(value >= _RESOURCE_WARNING_THRESHOLD for value in resource_values)
            or (
                response_time_ms is not None
                and response_time_ms >= _RESPONSE_TIME_WARNING_MS
            )
            or error_count > 0
        )
        if is_degraded:
            return HealthStatus.DEGRADED

        return HealthStatus.HEALTHY

    # ------------------------------------------------------------------
    # Create
    # ------------------------------------------------------------------
    async def create_health_record(
        self,
        data: SystemHealthCreate,
        actor_id: Optional[int] = None,
    ) -> SystemHealthResponse:
        """
        Creates a new health snapshot record after applying component-
        identity, parent-reference, and timestamp business-rule
        validation.

        Args:
            data: The validated creation payload.
            actor_id: The internal user ID creating this record
                interactively, if any.

        Returns:
            The created record, represented as `SystemHealthResponse`.

        Raises:
            ConflictException: If a live record already exists for this
                component identity.
            NotFoundException: If `parent_component_id` is supplied but
                does not reference a live record.
            ValidationException: If a business-rule timestamp/parent
                check fails.
        """
        await self._ensure_component_identity_available(
            data.component_name, data.component_type
        )
        await self._validate_parent_exists(data.parent_component_id)
        self._validate_parent_not_self(None, data.parent_component_id)
        self._validate_failure_timestamp_not_before_success_gap(
            data.last_success_at, data.last_failure_at
        )

        record = await self._repository.create(data, created_by_id=actor_id)
        return SystemHealthResponse.model_validate(record)

    # ------------------------------------------------------------------
    # Update
    # ------------------------------------------------------------------
    async def update_health_record(
        self,
        health_id: uuid.UUID,
        data: SystemHealthUpdate,
        actor_id: Optional[int] = None,
    ) -> SystemHealthResponse:
        """
        Applies a partial update to an existing health snapshot record
        after re-validating any identity/parent/timestamp fields that
        were supplied.

        Args:
            health_id: The UUID of the record to update.
            data: The validated partial update payload.
            actor_id: The internal user ID performing the update
                interactively, if any.

        Returns:
            The updated record, represented as `SystemHealthResponse`.

        Raises:
            NotFoundException: If no matching (non-deleted) record
                exists, or a referenced parent does not exist.
            ConflictException: If the update would create a duplicate
                component identity.
            ValidationException: If a business-rule check fails.
        """
        existing = await self._get_existing_or_raise(health_id)

        new_name = data.component_name if data.component_name is not None else existing.component_name
        new_type = data.component_type if data.component_type is not None else existing.component_type
        if data.component_name is not None or data.component_type is not None:
            await self._ensure_component_identity_available(
                new_name, new_type, exclude_id=existing.id
            )

        if "parent_component_id" in data.model_fields_set:
            self._validate_parent_not_self(existing.id, data.parent_component_id)
            await self._validate_parent_exists(data.parent_component_id)

        new_success = (
            data.last_success_at
            if "last_success_at" in data.model_fields_set
            else existing.last_success_at
        )
        new_failure = (
            data.last_failure_at
            if "last_failure_at" in data.model_fields_set
            else existing.last_failure_at
        )
        self._validate_failure_timestamp_not_before_success_gap(new_success, new_failure)

        updated = await self._repository.update(health_id, data, updated_by_id=actor_id)
        if updated is None:
            raise NotFoundException(f"Health record '{health_id}' was not found.")
        return SystemHealthResponse.model_validate(updated)

    # ------------------------------------------------------------------
    # Retrieve
    # ------------------------------------------------------------------
    async def get_component_status(
        self,
        component_name: str,
        component_type: ComponentType,
    ) -> SystemHealthResponse:
        """
        Retrieves the current status snapshot of a single monitored
        component.

        Args:
            component_name: The exact component name to look up.
            component_type: The component's category.

        Returns:
            The current snapshot, represented as `SystemHealthResponse`.

        Raises:
            NotFoundException: If no live record exists for this
                component identity.
        """
        record = await self._repository.get_component_status(
            component_name, component_type
        )
        if record is None:
            raise NotFoundException(
                f"No health record found for component '{component_name}' "
                f"of type '{component_type.value}'."
            )
        return SystemHealthResponse.model_validate(record)

    async def get_health_history(
        self,
        component_name: str,
        component_type: ComponentType,
        limit: int = 50,
    ) -> list[SystemHealthResponse]:
        """
        Retrieves the health history available for a component (its own
        live snapshot plus any child-rollup snapshots -- see
        `MonitoringRepository.get_health_history` for the documented
        semantics/limitation of "history" in an upserted-in-place model).

        Args:
            component_name: The exact component name to look up.
            component_type: The component's category.
            limit: Maximum number of rows to return.

        Returns:
            A list of `SystemHealthResponse` ordered most-recent-first.

        Raises:
            NotFoundException: If no live record exists for this
                component identity.
            ValidationException: If `limit` is not a positive integer.
        """
        if limit <= 0:
            raise ValidationException("limit must be a positive integer.")

        history = await self._repository.get_health_history(
            component_name, component_type, limit=limit
        )
        if not history:
            raise NotFoundException(
                f"No health record found for component '{component_name}' "
                f"of type '{component_type.value}'."
            )
        return [SystemHealthResponse.model_validate(record) for record in history]

    # ------------------------------------------------------------------
    # List / Search / Filter / Sort / Paginate
    # ------------------------------------------------------------------
    async def list_health_records(
        self, filters: HealthFilter
    ) -> SystemHealthListResponse:
        """
        Retrieves a filtered, searched, sorted, and paginated page of
        health snapshot records.

        Args:
            filters: The validated filter/search/sort/pagination
                parameters.

        Returns:
            A `SystemHealthListResponse` containing the page of records,
            the total matching count, and pagination metadata.
        """
        items, total = await self._repository.list_paginated(filters)
        total_pages = (
            (total + filters.page_size - 1) // filters.page_size if total > 0 else 0
        )
        return SystemHealthListResponse(
            items=[SystemHealthResponse.model_validate(item) for item in items],
            total=total,
            page=filters.page,
            page_size=filters.page_size,
            total_pages=total_pages,
        )

    # ------------------------------------------------------------------
    # Health Check Execution
    # ------------------------------------------------------------------
    async def execute_health_check(
        self,
        component_name: str,
        component_type: ComponentType,
        *,
        cpu_usage_percent: Optional[float] = None,
        memory_usage_percent: Optional[float] = None,
        disk_usage_percent: Optional[float] = None,
        response_time_ms: Optional[float] = None,
        error_count: int = 0,
        warning_count: int = 0,
        status_message: Optional[str] = None,
        meta_data: Optional[dict] = None,
        actor_id: Optional[int] = None,
    ) -> HealthCheckResponse:
        """
        Executes an ad-hoc health probe result against a component:
        derives its `HealthStatus` from the supplied metrics using
        `derive_status_from_metrics`, upserts the resulting snapshot in
        place, and returns the probe outcome.

        This is the single entry point business logic (schedulers,
        on-demand "check now" call sites, and webhook-driven updates)
        should use to record a health-check result, so status
        derivation and persistence semantics never diverge between
        call sites.

        Args:
            component_name: The exact component name being probed.
            component_type: The component's category.
            cpu_usage_percent: CPU utilization percentage observed, if any.
            memory_usage_percent: Memory utilization percentage observed, if any.
            disk_usage_percent: Disk utilization percentage observed, if any.
            response_time_ms: Latency of the probe, in milliseconds.
            error_count: Errors observed since the last counter reset.
            warning_count: Warnings observed since the last counter reset.
            status_message: Optional human-readable detail.
            meta_data: Optional provider-specific diagnostic payload.
            actor_id: The internal user ID triggering this probe
                interactively, or `None` for scheduler/worker-driven
                probes.

        Returns:
            A `HealthCheckResponse` describing the outcome of this probe.

        Raises:
            ValidationException: If a supplied metric is outside its
                valid 0-100 percent range or is negative where a
                non-negative value is required.
        """
        for label, value in (
            ("cpu_usage_percent", cpu_usage_percent),
            ("memory_usage_percent", memory_usage_percent),
            ("disk_usage_percent", disk_usage_percent),
        ):
            if value is not None and not (0 <= value <= 100):
                raise ValidationException(f"{label} must be between 0 and 100.")
        if response_time_ms is not None and response_time_ms < 0:
            raise ValidationException("response_time_ms must be non-negative.")
        if error_count < 0 or warning_count < 0:
            raise ValidationException("error_count and warning_count must be non-negative.")

        derived_status = self.derive_status_from_metrics(
            cpu_usage_percent=cpu_usage_percent,
            memory_usage_percent=memory_usage_percent,
            disk_usage_percent=disk_usage_percent,
            response_time_ms=response_time_ms,
            error_count=error_count,
        )
        is_healthy = derived_status == HealthStatus.HEALTHY
        now = datetime.now(timezone.utc)

        payload = SystemHealthCreate(
            component_name=component_name,
            component_type=component_type,
            status=derived_status,
            cpu_usage_percent=cpu_usage_percent,
            memory_usage_percent=memory_usage_percent,
            disk_usage_percent=disk_usage_percent,
            response_time_ms=response_time_ms,
            error_count=error_count,
            warning_count=warning_count,
            last_health_check_at=now,
            last_success_at=now if is_healthy else None,
            last_failure_at=None if is_healthy else now,
            status_message=status_message,
            meta_data=meta_data,
        )

        await self._repository.upsert_health_check_result(
            component_name, component_type, payload, actor_id=actor_id
        )

        return HealthCheckResponse(
            component_name=component_name,
            component_type=component_type,
            status=derived_status,
            is_healthy=is_healthy,
            response_time_ms=response_time_ms,
            message=status_message,
            checked_at=now,
        )

    # ------------------------------------------------------------------
    # Aggregated Whole-System Status
    # ------------------------------------------------------------------
    async def get_health_status_overview(self) -> HealthStatusResponse:
        """
        Computes an aggregated, whole-system health overview across all
        active, non-deleted components -- a worst-case `overall_status`
        plus a per-component breakdown, suitable for a top-level
        `/health` or monitoring-dashboard endpoint.

        Overall status precedence (worst wins): DOWN > UNHEALTHY >
        DEGRADED > MAINTENANCE > UNKNOWN > HEALTHY.

        Returns:
            A `HealthStatusResponse` describing the current whole-system
            health.
        """
        components = await self._repository.list_all_active()
        now = datetime.now(timezone.utc)

        status_precedence = [
            HealthStatus.DOWN,
            HealthStatus.UNHEALTHY,
            HealthStatus.DEGRADED,
            HealthStatus.MAINTENANCE,
            HealthStatus.UNKNOWN,
            HealthStatus.HEALTHY,
        ]
        present_statuses = {component.status for component in components}
        overall_status = next(
            (status for status in status_precedence if status in present_statuses),
            HealthStatus.UNKNOWN,
        )

        counts = {status: 0 for status in HealthStatus}
        for component in components:
            counts[component.status] += 1

        return HealthStatusResponse(
            overall_status=overall_status,
            components=[
                ComponentResponse.model_validate(component) for component in components
            ],
            healthy_count=counts[HealthStatus.HEALTHY],
            degraded_count=counts[HealthStatus.DEGRADED],
            unhealthy_count=counts[HealthStatus.UNHEALTHY],
            down_count=counts[HealthStatus.DOWN],
            checked_at=now,
        )

    # ------------------------------------------------------------------
    # Statistics
    # ------------------------------------------------------------------
    async def get_statistics(self) -> MonitoringStatisticsResponse:
        """
        Computes aggregate monitoring statistics across all active,
        non-deleted components: per-status counts, per-component-type
        counts, average response time, summed error/warning counters,
        and derived uptime percentage.

        Uptime percentage is defined as the share of components
        currently HEALTHY or DEGRADED (i.e. not DOWN/UNHEALTHY),
        matching `MonitoringStatisticsResponse.uptime_percentage`'s
        documented semantics.

        Returns:
            A `MonitoringStatisticsResponse` with the computed aggregates.
        """
        status_counts = await self._repository.count_by_status()
        type_counts = await self._repository.count_by_component_type()
        aggregates = await self._repository.get_aggregate_metrics()
        now = datetime.now(timezone.utc)

        total_components = aggregates["total_components"]
        healthy = status_counts.get(HealthStatus.HEALTHY, 0)
        degraded = status_counts.get(HealthStatus.DEGRADED, 0)
        unhealthy = status_counts.get(HealthStatus.UNHEALTHY, 0)
        down = status_counts.get(HealthStatus.DOWN, 0)
        maintenance = status_counts.get(HealthStatus.MAINTENANCE, 0)
        unknown = status_counts.get(HealthStatus.UNKNOWN, 0)

        uptime_percentage: Optional[float] = None
        if total_components > 0:
            uptime_percentage = round(((healthy + degraded) / total_components) * 100, 2)

        return MonitoringStatisticsResponse(
            total_components=total_components,
            healthy_count=healthy,
            degraded_count=degraded,
            unhealthy_count=unhealthy,
            down_count=down,
            maintenance_count=maintenance,
            unknown_count=unknown,
            average_response_time_ms=aggregates["average_response_time_ms"],
            total_error_count=aggregates["total_error_count"],
            total_warning_count=aggregates["total_warning_count"],
            uptime_percentage=uptime_percentage,
            by_component_type={
                component_type.value: count for component_type, count in type_counts.items()
            },
            generated_at=now,
        )

    # ------------------------------------------------------------------
    # Delete / Restore
    # ------------------------------------------------------------------
    async def delete_health_record(
        self,
        health_id: uuid.UUID,
        actor_id: Optional[int] = None,
    ) -> SystemHealthResponse:
        """
        Soft-deletes a health snapshot record.

        Args:
            health_id: The UUID of the record to soft-delete.
            actor_id: The internal user ID performing the soft-delete,
                if any.

        Returns:
            The soft-deleted record, represented as `SystemHealthResponse`.

        Raises:
            NotFoundException: If no matching (non-deleted) record exists.
        """
        await self._get_existing_or_raise(health_id)
        record = await self._repository.soft_delete(health_id, deleted_by_id=actor_id)
        if record is None:
            raise NotFoundException(f"Health record '{health_id}' was not found.")
        return SystemHealthResponse.model_validate(record)

    async def restore_health_record(
        self, health_id: uuid.UUID
    ) -> SystemHealthResponse:
        """
        Restores a previously soft-deleted health snapshot record.

        Args:
            health_id: The UUID of the record to restore.

        Returns:
            The restored record, represented as `SystemHealthResponse`.

        Raises:
            NotFoundException: If no record with that ID exists, or the
                record exists but was not soft-deleted.
        """
        record = await self._repository.restore(health_id)
        if record is None:
            raise NotFoundException(
                f"No soft-deleted health record '{health_id}' was found to restore."
            )
        return SystemHealthResponse.model_validate(record)