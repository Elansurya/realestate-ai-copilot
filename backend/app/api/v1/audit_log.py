"""API router for the Audit Log module.

Exposes read, search, statistics, and administrative endpoints over the
audit trail. All endpoints require an authenticated principal; mutating
and destructive endpoints are further restricted by role via RBAC
dependencies. This layer performs no business logic itself: it maps
HTTP requests to :class:`~app.services.audit_log_service.AuditLogService`
calls and translates domain exceptions into HTTP responses.
"""

import uuid
from datetime import datetime
from typing import Optional

from fastapi import APIRouter, Depends, Query, status
from fastapi.responses import JSONResponse
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_user, get_db, require_roles
from app.core.exceptions import (
    BusinessRuleException,
    NotFoundException,
    ValidationException,
)
from app.models.audit_log import AuditAction, AuditSeverity, AuditStatus
from app.models.user import User, UserRole
from app.repositories.audit_log_repository import AuditLogRepository
from app.schemas.audit_log import (
    AuditLogFilter,
    AuditLogListResponse,
    AuditLogResponse,
    AuditStatisticsResponse,
)
from app.services.audit_log_service import AuditLogService

router = APIRouter(prefix="/audit-logs", tags=["Audit Logs"])

# Roles permitted to read audit data. `UserRole` (app.models.user) only
# defines ADMIN, SALES_MANAGER, and SALES_AGENT -- there is no separate
# SYSTEM_ADMIN/AUDITOR role, so read access is granted to the two
# elevated roles that actually exist.
_READ_ROLES = (UserRole.ADMIN, UserRole.SALES_MANAGER)

# Only administrators may delete or purge audit history.
_DELETE_ROLES = (UserRole.ADMIN,)


def get_audit_log_service(db: AsyncSession = Depends(get_db)) -> AuditLogService:
    """Builds an :class:`AuditLogService` wired to a request-scoped session.

    Args:
        db: The request-scoped asynchronous database session.

    Returns:
        AuditLogService: A fully constructed service instance.
    """
    repository = AuditLogRepository(db)
    return AuditLogService(repository)


@router.get(
    "",
    response_model=AuditLogListResponse,
    status_code=status.HTTP_200_OK,
    summary="List audit log entries",
    description=(
        "Retrieves a paginated, filterable, sortable list of audit log "
        "entries. Supports filtering by user, module, entity, action, "
        "severity, status, free-text search, and date range. "
        "Accessible to Admin, System Admin, and Auditor roles."
    ),
    responses={
        200: {"description": "Page of audit log entries returned successfully."},
        401: {"description": "Missing or invalid authentication credentials."},
        403: {"description": "Caller lacks a permitted role."},
        422: {"description": "Invalid filter, sort, or pagination parameters."},
    },
)
async def list_audit_logs(
    user_id: Optional[int] = Query(default=None, description="Filter by acting user id."),
    module: Optional[str] = Query(default=None, description="Filter by owning module."),
    entity_type: Optional[str] = Query(default=None, description="Filter by affected entity type."),
    entity_id: Optional[str] = Query(default=None, description="Filter by affected entity id."),
    action: Optional[AuditAction] = Query(default=None, description="Filter by action."),
    status_: Optional[AuditStatus] = Query(default=None, alias="status", description="Filter by outcome status."),
    severity: Optional[AuditSeverity] = Query(default=None, description="Filter by severity."),
    request_id: Optional[str] = Query(default=None, description="Filter by correlation id."),
    search: Optional[str] = Query(default=None, description="Free-text search on description."),
    date_from: Optional[datetime] = Query(default=None, description="Inclusive lower bound on created_at."),
    date_to: Optional[datetime] = Query(default=None, description="Inclusive upper bound on created_at."),
    page: int = Query(default=1, ge=1, description="1-indexed page number."),
    page_size: int = Query(default=20, ge=1, le=200, description="Entries per page."),
    sort_by: str = Query(default="created_at", description="Column to sort by."),
    sort_order: str = Query(default="desc", description="Sort direction: asc or desc."),
    service: AuditLogService = Depends(get_audit_log_service),
    current_user: User = Depends(get_current_user),
    _: None = Depends(require_roles(*_READ_ROLES)),
) -> AuditLogListResponse:
    """Lists audit log entries matching the supplied filters.

    Args:
        user_id: Optional acting-user filter.
        module: Optional module filter.
        entity_type: Optional entity type filter.
        entity_id: Optional entity id filter.
        action: Optional action filter.
        status_: Optional outcome status filter (bound to the ``status`` query param).
        severity: Optional severity filter.
        request_id: Optional correlation id filter.
        search: Optional free-text search on description.
        date_from: Optional inclusive lower bound on ``created_at``.
        date_to: Optional inclusive upper bound on ``created_at``.
        page: 1-indexed page number.
        page_size: Number of entries per page.
        sort_by: Column name to sort by.
        sort_order: Sort direction, ``"asc"`` or ``"desc"``.
        service: Injected audit log service.
        current_user: The authenticated caller.

    Returns:
        AuditLogListResponse: The matching page of entries plus pagination metadata.
    """
    filters = AuditLogFilter(
        user_id=user_id,
        module=module,
        entity_type=entity_type,
        entity_id=entity_id,
        action=action,
        status=status_,
        severity=severity,
        request_id=request_id,
        search=search,
        date_from=date_from,
        date_to=date_to,
        page=page,
        page_size=page_size,
        sort_by=sort_by,
        sort_order=sort_order,
    )
    return await service.list_logs(filters)


@router.get(
    "/search",
    response_model=AuditLogListResponse,
    status_code=status.HTTP_200_OK,
    summary="Search audit log entries",
    description=(
        "Performs a case-insensitive free-text search over audit log "
        "descriptions, with pagination and sorting. Accessible to Admin, "
        "System Admin, and Auditor roles."
    ),
    responses={
        200: {"description": "Matching audit log entries returned successfully."},
        401: {"description": "Missing or invalid authentication credentials."},
        403: {"description": "Caller lacks a permitted role."},
        422: {"description": "Search term or pagination parameters invalid."},
    },
)
async def search_audit_logs(
    q: str = Query(..., min_length=1, description="Free-text search term."),
    page: int = Query(default=1, ge=1, description="1-indexed page number."),
    page_size: int = Query(default=20, ge=1, le=200, description="Entries per page."),
    sort_by: str = Query(default="created_at", description="Column to sort by."),
    sort_order: str = Query(default="desc", description="Sort direction: asc or desc."),
    service: AuditLogService = Depends(get_audit_log_service),
    current_user: User = Depends(get_current_user),
    _: None = Depends(require_roles(*_READ_ROLES)),
) -> AuditLogListResponse:
    """Searches audit log entries by free-text term.

    Args:
        q: The search term to match against descriptions.
        page: 1-indexed page number.
        page_size: Number of entries per page.
        sort_by: Column name to sort by.
        sort_order: Sort direction, ``"asc"`` or ``"desc"``.
        service: Injected audit log service.
        current_user: The authenticated caller.

    Returns:
        AuditLogListResponse: The matching page of entries plus pagination metadata.
    """
    return await service.search_logs(
        q, page=page, page_size=page_size, sort_by=sort_by, sort_order=sort_order
    )


@router.get(
    "/statistics",
    response_model=AuditStatisticsResponse,
    status_code=status.HTTP_200_OK,
    summary="Get aggregate audit statistics",
    description=(
        "Returns aggregate counts of audit events grouped by module, "
        "action, severity, and status, optionally scoped to a date "
        "range. Accessible to Admin, System Admin, and Auditor roles."
    ),
    responses={
        200: {"description": "Aggregate statistics computed successfully."},
        401: {"description": "Missing or invalid authentication credentials."},
        403: {"description": "Caller lacks a permitted role."},
        422: {"description": "date_from is after date_to."},
    },
)
async def get_audit_statistics(
    date_from: Optional[datetime] = Query(default=None, description="Inclusive lower bound on created_at."),
    date_to: Optional[datetime] = Query(default=None, description="Inclusive upper bound on created_at."),
    service: AuditLogService = Depends(get_audit_log_service),
    current_user: User = Depends(get_current_user),
    _: None = Depends(require_roles(*_READ_ROLES)),
) -> AuditStatisticsResponse:
    """Computes aggregate audit statistics over an optional date range.

    Args:
        date_from: Optional inclusive lower bound on ``created_at``.
        date_to: Optional inclusive upper bound on ``created_at``.
        service: Injected audit log service.
        current_user: The authenticated caller.

    Returns:
        AuditStatisticsResponse: The computed aggregate statistics.
    """
    return await service.get_statistics(date_from=date_from, date_to=date_to)


@router.get(
    "/recent",
    response_model=list[AuditLogResponse],
    status_code=status.HTTP_200_OK,
    summary="Get most recent audit activity",
    description=(
        "Returns the most recently created audit log entries system-wide. "
        "Accessible to Admin, System Admin, and Auditor roles."
    ),
    responses={
        200: {"description": "Recent audit log entries returned successfully."},
        401: {"description": "Missing or invalid authentication credentials."},
        403: {"description": "Caller lacks a permitted role."},
    },
)
async def get_recent_audit_logs(
    limit: int = Query(default=20, ge=1, le=200, description="Maximum number of entries to return."),
    service: AuditLogService = Depends(get_audit_log_service),
    current_user: User = Depends(get_current_user),
    _: None = Depends(require_roles(*_READ_ROLES)),
) -> list[AuditLogResponse]:
    """Retrieves the most recent audit log entries.

    Args:
        limit: Maximum number of entries to return.
        service: Injected audit log service.
        current_user: The authenticated caller.

    Returns:
        list[AuditLogResponse]: The most recent entries, newest first.
    """
    return await service.get_recent_activities(limit)


@router.get(
    "/failed",
    response_model=list[AuditLogResponse],
    status_code=status.HTTP_200_OK,
    summary="Get recent failed operations",
    description=(
        "Returns the most recent audit log entries with a FAILED outcome "
        "status. Accessible to Admin, System Admin, and Auditor roles."
    ),
    responses={
        200: {"description": "Recent failed audit log entries returned successfully."},
        401: {"description": "Missing or invalid authentication credentials."},
        403: {"description": "Caller lacks a permitted role."},
    },
)
async def get_failed_audit_logs(
    limit: int = Query(default=20, ge=1, le=200, description="Maximum number of entries to return."),
    service: AuditLogService = Depends(get_audit_log_service),
    current_user: User = Depends(get_current_user),
    _: None = Depends(require_roles(*_READ_ROLES)),
) -> list[AuditLogResponse]:
    """Retrieves the most recent failed audit log entries.

    Args:
        limit: Maximum number of entries to return.
        service: Injected audit log service.
        current_user: The authenticated caller.

    Returns:
        list[AuditLogResponse]: The most recent failed entries, newest first.
    """
    result = await service.get_dashboard_summary(recent_limit=limit)
    return result["recent_failed_logs"]


@router.get(
    "/critical",
    response_model=list[AuditLogResponse],
    status_code=status.HTTP_200_OK,
    summary="Get recent critical-severity events",
    description=(
        "Returns the most recent audit log entries with CRITICAL severity. "
        "Accessible to Admin, System Admin, and Auditor roles."
    ),
    responses={
        200: {"description": "Recent critical audit log entries returned successfully."},
        401: {"description": "Missing or invalid authentication credentials."},
        403: {"description": "Caller lacks a permitted role."},
    },
)
async def get_critical_audit_logs(
    limit: int = Query(default=20, ge=1, le=200, description="Maximum number of entries to return."),
    service: AuditLogService = Depends(get_audit_log_service),
    current_user: User = Depends(get_current_user),
    _: None = Depends(require_roles(*_READ_ROLES)),
) -> list[AuditLogResponse]:
    """Retrieves the most recent critical-severity audit log entries.

    Args:
        limit: Maximum number of entries to return.
        service: Injected audit log service.
        current_user: The authenticated caller.

    Returns:
        list[AuditLogResponse]: The most recent critical entries, newest first.
    """
    result = await service.get_dashboard_summary(recent_limit=limit)
    return result["recent_critical_logs"]


@router.get(
    "/user/{user_id}",
    response_model=AuditLogListResponse,
    status_code=status.HTTP_200_OK,
    summary="List audit log entries for a specific user",
    description=(
        "Retrieves a paginated list of audit log entries authored by the "
        "given user. Accessible to Admin, System Admin, and Auditor roles."
    ),
    responses={
        200: {"description": "Audit log entries for the user returned successfully."},
        401: {"description": "Missing or invalid authentication credentials."},
        403: {"description": "Caller lacks a permitted role."},
    },
)
async def get_audit_logs_by_user(
    user_id: int,
    page: int = Query(default=1, ge=1, description="1-indexed page number."),
    page_size: int = Query(default=20, ge=1, le=200, description="Entries per page."),
    sort_by: str = Query(default="created_at", description="Column to sort by."),
    sort_order: str = Query(default="desc", description="Sort direction: asc or desc."),
    service: AuditLogService = Depends(get_audit_log_service),
    current_user: User = Depends(get_current_user),
    _: None = Depends(require_roles(*_READ_ROLES)),
) -> AuditLogListResponse:
    """Lists audit log entries authored by a specific user.

    Args:
        user_id: The acting user's identifier.
        page: 1-indexed page number.
        page_size: Number of entries per page.
        sort_by: Column name to sort by.
        sort_order: Sort direction, ``"asc"`` or ``"desc"``.
        service: Injected audit log service.
        current_user: The authenticated caller.

    Returns:
        AuditLogListResponse: The matching page of entries plus pagination metadata.
    """
    filters = AuditLogFilter(
        user_id=user_id,
        page=page,
        page_size=page_size,
        sort_by=sort_by,
        sort_order=sort_order,
    )
    return await service.list_logs(filters)


@router.get(
    "/module/{module}",
    response_model=AuditLogListResponse,
    status_code=status.HTTP_200_OK,
    summary="List audit log entries for a specific module",
    description=(
        "Retrieves a paginated list of audit log entries recorded by the "
        "given module. Accessible to Admin, System Admin, and Auditor roles."
    ),
    responses={
        200: {"description": "Audit log entries for the module returned successfully."},
        401: {"description": "Missing or invalid authentication credentials."},
        403: {"description": "Caller lacks a permitted role."},
    },
)
async def get_audit_logs_by_module(
    module: str,
    page: int = Query(default=1, ge=1, description="1-indexed page number."),
    page_size: int = Query(default=20, ge=1, le=200, description="Entries per page."),
    sort_by: str = Query(default="created_at", description="Column to sort by."),
    sort_order: str = Query(default="desc", description="Sort direction: asc or desc."),
    service: AuditLogService = Depends(get_audit_log_service),
    current_user: User = Depends(get_current_user),
    _: None = Depends(require_roles(*_READ_ROLES)),
) -> AuditLogListResponse:
    """Lists audit log entries recorded by a specific module.

    Args:
        module: The owning module name (e.g. ``"customer"``, ``"booking"``).
        page: 1-indexed page number.
        page_size: Number of entries per page.
        sort_by: Column name to sort by.
        sort_order: Sort direction, ``"asc"`` or ``"desc"``.
        service: Injected audit log service.
        current_user: The authenticated caller.

    Returns:
        AuditLogListResponse: The matching page of entries plus pagination metadata.
    """
    filters = AuditLogFilter(
        module=module,
        page=page,
        page_size=page_size,
        sort_by=sort_by,
        sort_order=sort_order,
    )
    return await service.list_logs(filters)


@router.get(
    "/entity/{entity_type}/{entity_id}",
    response_model=list[AuditLogResponse],
    status_code=status.HTTP_200_OK,
    summary="Get the full activity timeline for an entity",
    description=(
        "Retrieves the complete chronological audit history for a single "
        "entity instance, ordered oldest to newest. Accessible to Admin, "
        "System Admin, and Auditor roles."
    ),
    responses={
        200: {"description": "Entity activity timeline returned successfully."},
        401: {"description": "Missing or invalid authentication credentials."},
        403: {"description": "Caller lacks a permitted role."},
        422: {"description": "entity_type or entity_id was empty."},
    },
)
async def get_entity_audit_timeline(
    entity_type: str,
    entity_id: str,
    service: AuditLogService = Depends(get_audit_log_service),
    current_user: User = Depends(get_current_user),
    _: None = Depends(require_roles(*_READ_ROLES)),
) -> list[AuditLogResponse]:
    """Retrieves the full audit timeline for a specific entity instance.

    Args:
        entity_type: Name of the entity type (e.g. ``"Customer"``).
        entity_id: Primary key of the entity instance.
        service: Injected audit log service.
        current_user: The authenticated caller.

    Returns:
        list[AuditLogResponse]: All audit entries for the entity, oldest first.
    """
    return await service.get_activity_timeline(
        entity_type=entity_type, entity_id=entity_id
    )


@router.get(
    "/{log_id}",
    response_model=AuditLogResponse,
    status_code=status.HTTP_200_OK,
    summary="Get a single audit log entry",
    description=(
        "Retrieves a single audit log entry by its unique identifier. "
        "Accessible to Admin, System Admin, and Auditor roles."
    ),
    responses={
        200: {"description": "Audit log entry returned successfully."},
        401: {"description": "Missing or invalid authentication credentials."},
        403: {"description": "Caller lacks a permitted role."},
        404: {"description": "No audit log entry exists with the given id."},
    },
)
async def get_audit_log(
    log_id: uuid.UUID,
    service: AuditLogService = Depends(get_audit_log_service),
    current_user: User = Depends(get_current_user),
    _: None = Depends(require_roles(*_READ_ROLES)),
) -> AuditLogResponse:
    """Retrieves a single audit log entry by id.

    Args:
        log_id: The UUID primary key of the entry.
        service: Injected audit log service.
        current_user: The authenticated caller.

    Returns:
        AuditLogResponse: The matching audit log entry.

    Raises:
        NotFoundException: If no entry with the given id exists; translated
            to a 404 response by the global exception handler.
    """
    return await service.get_log(log_id)


@router.post(
    "/export",
    response_model=list[dict],
    status_code=status.HTTP_200_OK,
    summary="Export audit log entries",
    description=(
        "Produces a flat, JSON-serializable export of audit log entries "
        "matching the supplied filters, suitable for downstream CSV/XLSX "
        "generation. Accessible to Admin, System Admin, and Auditor roles."
    ),
    responses={
        200: {"description": "Export-ready audit log rows returned successfully."},
        401: {"description": "Missing or invalid authentication credentials."},
        403: {"description": "Caller lacks a permitted role."},
        422: {"description": "Invalid filter parameters."},
    },
)
async def export_audit_logs(
    filters: AuditLogFilter,
    service: AuditLogService = Depends(get_audit_log_service),
    current_user: User = Depends(get_current_user),
    _: None = Depends(require_roles(*_READ_ROLES)),
) -> list[dict]:
    """Exports audit log entries matching the supplied filters.

    Args:
        filters: The combined filter, sort, and pagination parameters
            scoping the export, supplied as the request body.
        service: Injected audit log service.
        current_user: The authenticated caller.

    Returns:
        list[dict]: JSON-serializable rows ready for downstream export
        formatting (e.g. CSV or XLSX generation).
    """
    return await service.export_ready_data(filters)


@router.delete(
    "/cleanup",
    status_code=status.HTTP_200_OK,
    summary="Purge audit log entries older than a retention window",
    description=(
        "Permanently deletes audit log entries older than the given "
        "retention window (in days). Enforces a minimum retention floor "
        "to prevent accidental loss of recent audit history. "
        "Restricted to Admin role only."
    ),
    responses={
        200: {"description": "Old audit log entries purged successfully."},
        401: {"description": "Missing or invalid authentication credentials."},
        403: {"description": "Caller is not an Admin."},
        422: {
            "description": (
                "retention_days is not positive or is below the enforced "
                "minimum retention window."
            )
        },
    },
)
async def cleanup_old_audit_logs(
    retention_days: int = Query(..., ge=1, description="Days of history to retain."),
    service: AuditLogService = Depends(get_audit_log_service),
    current_user: User = Depends(get_current_user),
    _: None = Depends(require_roles(*_DELETE_ROLES)),
) -> JSONResponse:
    """Purges audit log entries older than the given retention window.

    Args:
        retention_days: Number of days of history to retain.
        service: Injected audit log service.
        current_user: The authenticated caller, must hold the Admin role.

    Returns:
        JSONResponse: A payload reporting the number of entries deleted.

    Raises:
        BusinessRuleException: If ``retention_days`` is below the
            enforced minimum retention window; translated to a 422/409
            response by the global exception handler.
    """
    deleted_count = await service.cleanup_old_logs(retention_days)
    return JSONResponse(
        status_code=status.HTTP_200_OK,
        content={"deleted_count": deleted_count, "retention_days": retention_days},
    )


@router.delete(
    "/{log_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Delete a single audit log entry",
    description=(
        "Permanently deletes a single audit log entry by id. This is a "
        "destructive, irreversible operation restricted to Admin role only."
    ),
    responses={
        204: {"description": "Audit log entry deleted successfully."},
        401: {"description": "Missing or invalid authentication credentials."},
        403: {"description": "Caller is not an Admin."},
        404: {"description": "No audit log entry exists with the given id."},
    },
)
async def delete_audit_log(
    log_id: uuid.UUID,
    service: AuditLogService = Depends(get_audit_log_service),
    current_user: User = Depends(get_current_user),
    _: None = Depends(require_roles(*_DELETE_ROLES)),
) -> None:
    """Deletes a single audit log entry by id.

    Args:
        log_id: The UUID primary key of the entry to delete.
        service: Injected audit log service.
        current_user: The authenticated caller, must hold the Admin role.

    Raises:
        NotFoundException: If no entry with the given id exists; translated
            to a 404 response by the global exception handler.
    """
    # Confirms existence first so a 404 is raised for unknown ids rather
    # than silently reporting success for a no-op bulk delete.
    await service.get_log(log_id)
    await service.bulk_delete_logs([log_id])