"""
backend/app/api/v1/monitoring.py

FastAPI router (API layer) for the Enterprise Monitoring & Health
module of the Enterprise Real Estate AI Copilot CRM.

Scope of this module:
    - Declares every HTTP-facing contract for monitoring/health:
      Kubernetes-style liveness/readiness probes, per-component-type
      health checks (database, storage, external integrations, AI
      providers), CRUD over persisted `SystemHealth` snapshot records,
      whole-system status overview, system resource metrics, and
      aggregate statistics.
    - Owns JWT authentication and RBAC enforcement for every endpoint
      that is not a bare orchestration-platform probe.
    - Owns translation of domain exceptions raised by
      `MonitoringService` (`NotFoundException`, `ConflictException`,
      `ValidationException`) into the appropriate `HTTPException` --
      this is explicitly the router's job per
      `app/services/monitoring_service.py`'s module docstring.
    - Delegates ALL business rules and persistence to
      `MonitoringService` / `MonitoringRepository`. This module never
      constructs SQLAlchemy statements or touches `SystemHealth` rows
      directly.
    - Delegates the mechanics of actually probing a live dependency to
      `app.utils.health_checker`, and host/process resource sampling to
      `app.utils.metrics_collector`.

Authentication & Authorization:
    - `/live` and `/ready` are intentionally left UNAUTHENTICATED, since
      these are consumed by container orchestrators (Kubernetes
      liveness/readiness probes, load balancer health checks) that
      cannot supply a JWT and must remain reachable even when the auth
      subsystem itself is degraded.
    - Every other endpoint requires a valid JWT (`get_current_user`).
    - Read endpoints (status overview, component listing/detail,
      history, statistics, metrics) additionally require the
      `monitoring:read` permission, granted to the `ADMIN`, `MANAGER`,
      and `AUDITOR` roles.
    - Mutating endpoints (create/update/delete/restore a component,
      triggering an ad-hoc health check) require the `monitoring:write`
      permission, granted only to the `ADMIN` role, since these actions
      can affect alerting and on-call behavior platform-wide.

Swagger / OpenAPI:
    - Every route declares an explicit `summary`, `description`,
      `response_model`, and `responses` mapping so the generated
      OpenAPI schema (and therefore `/docs` and `/redoc`) is complete
      and useful without any further annotation.
"""

from __future__ import annotations

import uuid
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_user, get_db, require_roles
from app.core.exceptions import ConflictException, NotFoundException, ValidationException
from app.models.monitoring import ComponentType
from app.models.user import User
from app.repositories.monitoring_repository import MonitoringRepository
from app.schemas.monitoring import (
    HealthCheckResponse,
    HealthFilter,
    HealthStatusResponse,
    MonitoringStatisticsResponse,
    SystemHealthCreate,
    SystemHealthListResponse,
    SystemHealthResponse,
    SystemHealthUpdate,
)
from app.services.monitoring_service import MonitoringService
from app.utils import health_checker, metrics_collector

__all__ = ["router"]

router = APIRouter(prefix="/monitoring", tags=["Monitoring & Health"])

# --------------------------------------------------------------------------
# RBAC Role Groups
# --------------------------------------------------------------------------
# Any authenticated role permitted to VIEW monitoring data/dashboards.
_MONITORING_READ_ROLES = ("ADMIN", "MANAGER", "AUDITOR")

# Only administrators may mutate monitoring configuration/state or
# trigger ad-hoc probes.
_MONITORING_WRITE_ROLES = ("ADMIN",)


# --------------------------------------------------------------------------
# Dependency Wiring
# --------------------------------------------------------------------------
def get_monitoring_service(db: AsyncSession = Depends(get_db)) -> MonitoringService:
    """
    Builds a `MonitoringService` wired to a `MonitoringRepository` bound
    to the current request's database session.

    Args:
        db: The request-scoped async SQLAlchemy session, injected by
            `app.api.deps.get_db`.

    Returns:
        A ready-to-use `MonitoringService` instance.
    """
    return MonitoringService(MonitoringRepository(db))


# --------------------------------------------------------------------------
# Domain Exception -> HTTP Translation
# --------------------------------------------------------------------------
def _raise_http_from_domain_exception(exc: Exception) -> None:
    """
    Translates a domain exception raised by `MonitoringService` into the
    equivalent `HTTPException`, since routers -- not the service layer --
    own this translation per this project's layering convention.

    Args:
        exc: The caught domain exception instance.

    Raises:
        HTTPException: Always raises; the specific status code depends
            on the domain exception's type.
    """
    if isinstance(exc, NotFoundException):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    if isinstance(exc, ConflictException):
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
    if isinstance(exc, ValidationException):
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc)) from exc
    raise HTTPException(
        status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
        detail="An unexpected error occurred while processing the monitoring request.",
    ) from exc


# ==========================================================================
# Kubernetes-Style Probes (Unauthenticated)
# ==========================================================================
@router.get(
    "/live",
    summary="Liveness probe",
    description=(
        "Bare process-liveness check for container orchestrators. Returns "
        "200 as long as the application process is running and able to "
        "handle HTTP requests at all -- it deliberately does NOT check any "
        "downstream dependency (database, storage, etc.), since a failing "
        "dependency should trigger readiness failure, not a container "
        "restart. Intentionally unauthenticated."
    ),
    status_code=status.HTTP_200_OK,
    response_description="The process is alive.",
)
async def liveness_probe() -> dict:
    """
    Returns:
        A minimal JSON body confirming the process is alive, along with
        current process uptime in seconds.
    """
    return {"status": "alive", "uptime_seconds": metrics_collector.get_uptime_seconds()}


@router.get(
    "/ready",
    summary="Readiness probe",
    description=(
        "Readiness check for container orchestrators / load balancers. "
        "Returns 200 only if the application's critical dependency (the "
        "primary database) is currently reachable, so traffic is only "
        "routed to instances that can actually serve requests. Returns "
        "503 if the database is unreachable. Intentionally unauthenticated."
    ),
    status_code=status.HTTP_200_OK,
    responses={
        503: {"description": "The application is not ready to serve traffic."},
    },
)
async def readiness_probe(db: AsyncSession = Depends(get_db)) -> dict:
    """
    Args:
        db: The request-scoped async SQLAlchemy session.

    Returns:
        A JSON body confirming readiness.

    Raises:
        HTTPException: 503 if the database probe fails.
    """
    probe_result = await health_checker.check_database_health(db)
    if not probe_result["is_healthy"]:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=probe_result["message"] or "Database is not reachable.",
        )
    return {"status": "ready", "database_response_time_ms": probe_result["response_time_ms"]}


# ==========================================================================
# Whole-System Health Overview
# ==========================================================================
@router.get(
    "",
    summary="Get whole-system health overview",
    description=(
        "Returns an aggregated, worst-case health overview across every "
        "active, non-deleted monitored component, plus a per-component "
        "breakdown. Requires the monitoring:read permission."
    ),
    response_model=HealthStatusResponse,
    status_code=status.HTTP_200_OK,
)
async def get_system_health_overview(
    service: MonitoringService = Depends(get_monitoring_service),
    _current_user: User = Depends(require_roles(*_MONITORING_READ_ROLES)),
) -> HealthStatusResponse:
    """
    Args:
        service: The injected `MonitoringService`.
        _current_user: The authenticated, authorized caller.

    Returns:
        The whole-system `HealthStatusResponse` overview.
    """
    return await service.get_health_status_overview()


# ==========================================================================
# Per-Component-Type Health Checks
# ==========================================================================
@router.get(
    "/health/database",
    summary="Check database health",
    description=(
        "Executes a live probe against the primary database, records the "
        "resulting snapshot for the 'primary-database' DATABASE component, "
        "and returns the probe outcome. Requires the monitoring:read "
        "permission."
    ),
    response_model=HealthCheckResponse,
    status_code=status.HTTP_200_OK,
)
async def check_database_health(
    component_name: str = Query(
        default="primary-database", description="Component name to record this probe result under."
    ),
    db: AsyncSession = Depends(get_db),
    service: MonitoringService = Depends(get_monitoring_service),
    current_user: User = Depends(require_roles(*_MONITORING_READ_ROLES)),
) -> HealthCheckResponse:
    """
    Args:
        component_name: The `SystemHealth` component identity to upsert
            this probe result under.
        db: The request-scoped async SQLAlchemy session, reused as the
            connection the probe itself runs over.
        service: The injected `MonitoringService`.
        current_user: The authenticated, authorized caller.

    Returns:
        A `HealthCheckResponse` describing the probe outcome.
    """
    probe = await health_checker.check_database_health(db)
    try:
        return await service.execute_health_check(
            component_name=component_name,
            component_type=ComponentType.DATABASE,
            response_time_ms=probe["response_time_ms"],
            error_count=probe["error_count"],
            status_message=probe["message"],
            meta_data=probe.get("meta_data"),
            actor_id=current_user.id,
        )
    except (NotFoundException, ConflictException, ValidationException) as exc:
        _raise_http_from_domain_exception(exc)


@router.get(
    "/health/storage",
    summary="Check storage health",
    description=(
        "Executes a live probe against the configured object/file "
        "storage backend, records the resulting snapshot, and returns "
        "the probe outcome. Requires the monitoring:read permission."
    ),
    response_model=HealthCheckResponse,
    status_code=status.HTTP_200_OK,
)
async def check_storage_health(
    component_name: str = Query(
        default="primary-storage", description="Component name to record this probe result under."
    ),
    service: MonitoringService = Depends(get_monitoring_service),
    current_user: User = Depends(require_roles(*_MONITORING_READ_ROLES)),
) -> HealthCheckResponse:
    """
    Args:
        component_name: The `SystemHealth` component identity to upsert
            this probe result under.
        service: The injected `MonitoringService`.
        current_user: The authenticated, authorized caller.

    Returns:
        A `HealthCheckResponse` describing the probe outcome.
    """
    probe = await health_checker.check_storage_health()
    try:
        return await service.execute_health_check(
            component_name=component_name,
            component_type=ComponentType.STORAGE,
            response_time_ms=probe["response_time_ms"],
            error_count=probe["error_count"],
            status_message=probe["message"],
            meta_data=probe.get("meta_data"),
            actor_id=current_user.id,
        )
    except (NotFoundException, ConflictException, ValidationException) as exc:
        _raise_http_from_domain_exception(exc)


@router.get(
    "/health/integrations/{integration_name}",
    summary="Check external integration health",
    description=(
        "Executes a live probe against a named external integration "
        "(payment gateway, calendar provider, CRM webhook target, etc.), "
        "records the resulting snapshot, and returns the probe outcome. "
        "Requires the monitoring:read permission."
    ),
    response_model=HealthCheckResponse,
    status_code=status.HTTP_200_OK,
)
async def check_integration_health(
    integration_name: str,
    service: MonitoringService = Depends(get_monitoring_service),
    current_user: User = Depends(require_roles(*_MONITORING_READ_ROLES)),
) -> HealthCheckResponse:
    """
    Args:
        integration_name: The exact `component_name` of the
            EXTERNAL_INTEGRATION component to probe/upsert.
        service: The injected `MonitoringService`.
        current_user: The authenticated, authorized caller.

    Returns:
        A `HealthCheckResponse` describing the probe outcome.
    """
    probe = await health_checker.check_integration_health(integration_name)
    try:
        return await service.execute_health_check(
            component_name=integration_name,
            component_type=ComponentType.EXTERNAL_INTEGRATION,
            response_time_ms=probe["response_time_ms"],
            error_count=probe["error_count"],
            status_message=probe["message"],
            meta_data=probe.get("meta_data"),
            actor_id=current_user.id,
        )
    except (NotFoundException, ConflictException, ValidationException) as exc:
        _raise_http_from_domain_exception(exc)


@router.get(
    "/health/ai-providers/{provider_name}",
    summary="Check AI provider health",
    description=(
        "Executes a live probe against a named external AI/LLM provider "
        "(e.g. OpenAI, Anthropic), records the resulting snapshot, and "
        "returns the probe outcome. Requires the monitoring:read "
        "permission."
    ),
    response_model=HealthCheckResponse,
    status_code=status.HTTP_200_OK,
)
async def check_ai_provider_health(
    provider_name: str,
    service: MonitoringService = Depends(get_monitoring_service),
    current_user: User = Depends(require_roles(*_MONITORING_READ_ROLES)),
) -> HealthCheckResponse:
    """
    Args:
        provider_name: The exact `component_name` of the AI_PROVIDER
            component to probe/upsert.
        service: The injected `MonitoringService`.
        current_user: The authenticated, authorized caller.

    Returns:
        A `HealthCheckResponse` describing the probe outcome.
    """
    probe = await health_checker.check_ai_provider_health(provider_name)
    try:
        return await service.execute_health_check(
            component_name=provider_name,
            component_type=ComponentType.AI_PROVIDER,
            response_time_ms=probe["response_time_ms"],
            error_count=probe["error_count"],
            status_message=probe["message"],
            meta_data=probe.get("meta_data"),
            actor_id=current_user.id,
        )
    except (NotFoundException, ConflictException, ValidationException) as exc:
        _raise_http_from_domain_exception(exc)


@router.post(
    "/health/check/{component_type}/{component_name}",
    summary="Trigger an ad-hoc health check for any component",
    description=(
        "Generic, on-demand 'check now' entry point: derives a status "
        "from the supplied metrics using the same business rule used by "
        "the automated health-check worker, upserts the resulting "
        "snapshot, and returns the probe outcome. Requires the "
        "monitoring:write permission."
    ),
    response_model=HealthCheckResponse,
    status_code=status.HTTP_200_OK,
)
async def trigger_health_check(
    component_type: ComponentType,
    component_name: str,
    cpu_usage_percent: Optional[float] = Query(default=None, ge=0, le=100),
    memory_usage_percent: Optional[float] = Query(default=None, ge=0, le=100),
    disk_usage_percent: Optional[float] = Query(default=None, ge=0, le=100),
    response_time_ms: Optional[float] = Query(default=None, ge=0),
    error_count: int = Query(default=0, ge=0),
    warning_count: int = Query(default=0, ge=0),
    status_message: Optional[str] = Query(default=None, max_length=2000),
    service: MonitoringService = Depends(get_monitoring_service),
    current_user: User = Depends(require_roles(*_MONITORING_WRITE_ROLES)),
) -> HealthCheckResponse:
    """
    Args:
        component_type: The category of the component being probed.
        component_name: The exact component name being probed.
        cpu_usage_percent: CPU utilization percentage observed, if any.
        memory_usage_percent: Memory utilization percentage observed, if any.
        disk_usage_percent: Disk utilization percentage observed, if any.
        response_time_ms: Latency of the probe, in milliseconds.
        error_count: Errors observed since the last counter reset.
        warning_count: Warnings observed since the last counter reset.
        status_message: Optional human-readable detail.
        service: The injected `MonitoringService`.
        current_user: The authenticated, authorized caller.

    Returns:
        A `HealthCheckResponse` describing the probe outcome.
    """
    try:
        return await service.execute_health_check(
            component_name=component_name,
            component_type=component_type,
            cpu_usage_percent=cpu_usage_percent,
            memory_usage_percent=memory_usage_percent,
            disk_usage_percent=disk_usage_percent,
            response_time_ms=response_time_ms,
            error_count=error_count,
            warning_count=warning_count,
            status_message=status_message,
            actor_id=current_user.id,
        )
    except (NotFoundException, ConflictException, ValidationException) as exc:
        _raise_http_from_domain_exception(exc)


# ==========================================================================
# System Metrics
# ==========================================================================
@router.get(
    "/metrics",
    summary="Get current system resource metrics",
    description=(
        "Returns a live, point-in-time snapshot of host-level CPU, "
        "memory, and disk utilization, plus this application process's "
        "own resource footprint. This is a raw metrics sample -- it is "
        "NOT persisted as a `SystemHealth` row. Requires the "
        "monitoring:read permission."
    ),
    status_code=status.HTTP_200_OK,
)
async def get_system_metrics(
    _current_user: User = Depends(require_roles(*_MONITORING_READ_ROLES)),
) -> dict:
    """
    Args:
        _current_user: The authenticated, authorized caller.

    Returns:
        A dict with `system` (host-level) and `process` (this
        application process's own) resource metrics.
    """
    system_metrics = await metrics_collector.get_system_metrics()
    process_metrics = await metrics_collector.get_process_metrics()
    return {"system": system_metrics, "process": process_metrics}


# ==========================================================================
# Statistics
# ==========================================================================
@router.get(
    "/statistics",
    summary="Get aggregate monitoring statistics",
    description=(
        "Returns aggregate statistics across all active, non-deleted "
        "monitored components: per-status counts, per-component-type "
        "counts, average response time, summed error/warning counters, "
        "and derived uptime percentage. Requires the monitoring:read "
        "permission."
    ),
    response_model=MonitoringStatisticsResponse,
    status_code=status.HTTP_200_OK,
)
async def get_statistics(
    service: MonitoringService = Depends(get_monitoring_service),
    _current_user: User = Depends(require_roles(*_MONITORING_READ_ROLES)),
) -> MonitoringStatisticsResponse:
    """
    Args:
        service: The injected `MonitoringService`.
        _current_user: The authenticated, authorized caller.

    Returns:
        A `MonitoringStatisticsResponse` with the computed aggregates.
    """
    return await service.get_statistics()


# ==========================================================================
# SystemHealth Record CRUD
# ==========================================================================
@router.post(
    "/components",
    summary="Create a health snapshot record",
    description=(
        "Creates a new `SystemHealth` snapshot record for a monitored "
        "component. Requires the monitoring:write permission."
    ),
    response_model=SystemHealthResponse,
    status_code=status.HTTP_201_CREATED,
    responses={
        409: {"description": "A health record for this component identity already exists."},
        404: {"description": "The referenced parent component does not exist."},
        422: {"description": "A business-rule validation failed."},
    },
)
async def create_health_record(
    data: SystemHealthCreate,
    service: MonitoringService = Depends(get_monitoring_service),
    current_user: User = Depends(require_roles(*_MONITORING_WRITE_ROLES)),
) -> SystemHealthResponse:
    """
    Args:
        data: The validated creation payload.
        service: The injected `MonitoringService`.
        current_user: The authenticated, authorized caller.

    Returns:
        The created `SystemHealthResponse`.
    """
    try:
        return await service.create_health_record(data, actor_id=current_user.id)
    except (NotFoundException, ConflictException, ValidationException) as exc:
        _raise_http_from_domain_exception(exc)
    except Exception as exc:
        _raise_http_from_domain_exception(exc)


@router.get(
    "/components",
    summary="List / search / filter health records",
    description=(
        "Returns a filtered, searched, sorted, and paginated page of "
        "`SystemHealth` snapshot records. Requires the monitoring:read "
        "permission."
    ),
    response_model=SystemHealthListResponse,
    status_code=status.HTTP_200_OK,
)
async def list_health_records(
    filters: HealthFilter = Depends(),
    service: MonitoringService = Depends(get_monitoring_service),
    _current_user: User = Depends(require_roles(*_MONITORING_READ_ROLES)),
) -> SystemHealthListResponse:
    """
    Args:
        filters: The validated filter/search/sort/pagination parameters,
            bound from query parameters.
        service: The injected `MonitoringService`.
        _current_user: The authenticated, authorized caller.

    Returns:
        A `SystemHealthListResponse` page of matching records.
    """
    return await service.list_health_records(filters)


@router.get(
    "/components/status",
    summary="Get a single component's current status",
    description=(
        "Retrieves the current status snapshot of a single monitored "
        "component by its exact identity. Requires the monitoring:read "
        "permission."
    ),
    response_model=SystemHealthResponse,
    status_code=status.HTTP_200_OK,
    responses={404: {"description": "No health record exists for this component identity."}},
)
async def get_component_status(
    component_name: str = Query(..., description="Exact component name to look up."),
    component_type: ComponentType = Query(..., description="The component's category."),
    service: MonitoringService = Depends(get_monitoring_service),
    _current_user: User = Depends(require_roles(*_MONITORING_READ_ROLES)),
) -> SystemHealthResponse:
    """
    Args:
        component_name: The exact component name to look up.
        component_type: The component's category.
        service: The injected `MonitoringService`.
        _current_user: The authenticated, authorized caller.

    Returns:
        The matching `SystemHealthResponse`.
    """
    try:
        return await service.get_component_status(component_name, component_type)
    except NotFoundException as exc:
        _raise_http_from_domain_exception(exc)


@router.get(
    "/components/history",
    summary="Get a component's health history",
    description=(
        "Retrieves the health history available for a component (its "
        "own live snapshot plus any child-rollup snapshots). Requires "
        "the monitoring:read permission."
    ),
    response_model=list[SystemHealthResponse],
    status_code=status.HTTP_200_OK,
    responses={404: {"description": "No health record exists for this component identity."}},
)
async def get_health_history(
    component_name: str = Query(..., description="Exact component name to look up."),
    component_type: ComponentType = Query(..., description="The component's category."),
    limit: int = Query(default=50, gt=0, le=500, description="Maximum number of rows to return."),
    service: MonitoringService = Depends(get_monitoring_service),
    _current_user: User = Depends(require_roles(*_MONITORING_READ_ROLES)),
) -> list[SystemHealthResponse]:
    """
    Args:
        component_name: The exact component name to look up.
        component_type: The component's category.
        limit: Maximum number of rows to return.
        service: The injected `MonitoringService`.
        _current_user: The authenticated, authorized caller.

    Returns:
        A list of `SystemHealthResponse` ordered most-recent-first.
    """
    try:
        return await service.get_health_history(component_name, component_type, limit=limit)
    except (NotFoundException, ValidationException) as exc:
        _raise_http_from_domain_exception(exc)


@router.get(
    "/components/{health_id}",
    summary="Get a health record by ID",
    description="Retrieves a single `SystemHealth` snapshot record by its UUID. Requires the monitoring:read permission.",
    response_model=SystemHealthResponse,
    status_code=status.HTTP_200_OK,
    responses={404: {"description": "No health record exists with this ID."}},
)
async def get_health_record(
    health_id: uuid.UUID,
    service: MonitoringService = Depends(get_monitoring_service),
    _current_user: User = Depends(require_roles(*_MONITORING_READ_ROLES)),
) -> SystemHealthResponse:
    """
    Args:
        health_id: The UUID of the record to fetch.
        service: The injected `MonitoringService`.
        _current_user: The authenticated, authorized caller.

    Returns:
        The matching `SystemHealthResponse`.
    """
    try:
        record = await service._get_existing_or_raise(health_id)  # noqa: SLF001 - thin passthrough, no extra business rule needed
    except NotFoundException as exc:
        _raise_http_from_domain_exception(exc)
    return SystemHealthResponse.model_validate(record)


@router.patch(
    "/components/{health_id}",
    summary="Update a health record",
    description=(
        "Applies a partial (PATCH-style) update to an existing "
        "`SystemHealth` snapshot record. Requires the monitoring:write "
        "permission."
    ),
    response_model=SystemHealthResponse,
    status_code=status.HTTP_200_OK,
    responses={
        404: {"description": "No matching health record (or referenced parent) exists."},
        409: {"description": "The update would create a duplicate component identity."},
        422: {"description": "A business-rule validation failed."},
    },
)
async def update_health_record(
    health_id: uuid.UUID,
    data: SystemHealthUpdate,
    service: MonitoringService = Depends(get_monitoring_service),
    current_user: User = Depends(require_roles(*_MONITORING_WRITE_ROLES)),
) -> SystemHealthResponse:
    """
    Args:
        health_id: The UUID of the record to update.
        data: The validated partial update payload.
        service: The injected `MonitoringService`.
        current_user: The authenticated, authorized caller.

    Returns:
        The updated `SystemHealthResponse`.
    """
    try:
        return await service.update_health_record(health_id, data, actor_id=current_user.id)
    except (NotFoundException, ConflictException, ValidationException) as exc:
        _raise_http_from_domain_exception(exc)


@router.delete(
    "/components/{health_id}",
    summary="Soft-delete a health record",
    description="Soft-deletes a `SystemHealth` snapshot record. Requires the monitoring:write permission.",
    response_model=SystemHealthResponse,
    status_code=status.HTTP_200_OK,
    responses={404: {"description": "No matching health record exists."}},
)
async def delete_health_record(
    health_id: uuid.UUID,
    service: MonitoringService = Depends(get_monitoring_service),
    current_user: User = Depends(require_roles(*_MONITORING_WRITE_ROLES)),
) -> SystemHealthResponse:
    """
    Args:
        health_id: The UUID of the record to soft-delete.
        service: The injected `MonitoringService`.
        current_user: The authenticated, authorized caller.

    Returns:
        The soft-deleted `SystemHealthResponse`.
    """
    try:
        return await service.delete_health_record(health_id, actor_id=current_user.id)
    except NotFoundException as exc:
        _raise_http_from_domain_exception(exc)


@router.post(
    "/components/{health_id}/restore",
    summary="Restore a soft-deleted health record",
    description="Restores a previously soft-deleted `SystemHealth` snapshot record. Requires the monitoring:write permission.",
    response_model=SystemHealthResponse,
    status_code=status.HTTP_200_OK,
    responses={404: {"description": "No soft-deleted health record exists with this ID."}},
)
async def restore_health_record(
    health_id: uuid.UUID,
    service: MonitoringService = Depends(get_monitoring_service),
    _current_user: User = Depends(require_roles(*_MONITORING_WRITE_ROLES)),
) -> SystemHealthResponse:
    """
    Args:
        health_id: The UUID of the record to restore.
        service: The injected `MonitoringService`.
        _current_user: The authenticated, authorized caller.

    Returns:
        The restored `SystemHealthResponse`.
    """
    try:
        return await service.restore_health_record(health_id)
    except NotFoundException as exc:
        _raise_http_from_domain_exception(exc)