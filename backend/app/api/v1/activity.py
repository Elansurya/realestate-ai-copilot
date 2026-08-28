"""
backend/app/api/v1/activity.py

FastAPI router for the Activity Timeline module of the Enterprise
Real Estate AI Copilot CRM.

Mirrors the routing, dependency-injection, JWT authentication, RBAC,
pagination, filtering, and exception-handling conventions already
established by the other ``app/api/v1/*`` routers in this project.
Business logic and validation live entirely in
``app.services.activity_service.ActivityService``; this module is
limited to request/response wiring.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Optional

from fastapi import APIRouter, Depends, Query, status

from app.api.deps import get_current_user, get_db, require_roles
from app.models.user import User, UserRole
from app.repositories.activity_repository import ActivityRepository
from app.schemas.activity import (
    ActivityCreate,
    ActivityFilter,
    ActivityListResponse,
    ActivityResponse,
    ActivityUpdate,
    StatisticsResponse,
    TimelineResponse,
)
from app.services.activity_service import ActivityService
from sqlalchemy.ext.asyncio import AsyncSession

router = APIRouter(prefix="/activities", tags=["Activity Timeline"])


# ---------------------------------------------------------------------------
# Dependency wiring
# ---------------------------------------------------------------------------
def get_activity_service(db: AsyncSession = Depends(get_db)) -> ActivityService:
    """Builds an :class:`ActivityService` bound to the current DB session.

    Args:
        db: The request-scoped asynchronous SQLAlchemy session.

    Returns:
        ActivityService: A service instance backed by an
        :class:`ActivityRepository` for ``db``.
    """
    return ActivityService(ActivityRepository(db))


# Roles permitted to write/mutate activity entries. Read access is granted
# to any authenticated user via `get_current_user`; mutation is restricted
# to elevated roles, consistent with this project's RBAC conventions.
# `require_roles` takes `UserRole` members, not raw strings; `app.models.
# user.UserRole` only defines ADMIN, SALES_MANAGER, and SALES_AGENT.
WRITE_ROLES = (UserRole.ADMIN, UserRole.SALES_MANAGER, UserRole.SALES_AGENT)
DELETE_ROLES = (UserRole.ADMIN, UserRole.SALES_MANAGER)


# ---------------------------------------------------------------------------
# CRUD
# ---------------------------------------------------------------------------
@router.post(
    "",
    response_model=ActivityResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Create a new activity timeline entry",
    dependencies=[Depends(require_roles(*WRITE_ROLES))],
)
async def create_activity(
    payload: ActivityCreate,
    current_user: User = Depends(get_current_user),
    service: ActivityService = Depends(get_activity_service),
) -> ActivityResponse:
    """Creates a new activity timeline entry.

    Args:
        payload: The activity fields to persist.
        current_user: The authenticated caller, used as the default actor
            when ``performed_by_id`` is not explicitly supplied.
        service: The injected activity service.

    Returns:
        ActivityResponse: The newly created activity entry.
    """
    activity = await service.create_activity(payload, actor_id=current_user.id)
    return ActivityResponse.model_validate(activity)


@router.get(
    "",
    response_model=ActivityListResponse,
    summary="List activity timeline entries with filtering, search, sorting, and pagination",
)
async def list_activities(
    filters: ActivityFilter = Depends(),
    current_user: User = Depends(get_current_user),
    service: ActivityService = Depends(get_activity_service),
) -> ActivityListResponse:
    """Lists activity entries matching the supplied filters.

    Args:
        filters: Filter, search, sort, and pagination parameters.
        current_user: The authenticated caller.
        service: The injected activity service.

    Returns:
        ActivityListResponse: The matching page of activity entries.
    """
    return await service.list_activities(filters)


@router.get(
    "/statistics",
    response_model=StatisticsResponse,
    summary="Get aggregate activity statistics",
)
async def get_statistics(
    date_from: Optional[datetime] = Query(
        default=None, description="Inclusive lower bound (ISO 8601) on created_at."
    ),
    date_to: Optional[datetime] = Query(
        default=None, description="Inclusive upper bound (ISO 8601) on created_at."
    ),
    current_user: User = Depends(get_current_user),
    service: ActivityService = Depends(get_activity_service),
) -> StatisticsResponse:
    """Returns aggregate activity counts grouped by module/action/priority/status.

    Args:
        date_from: Optional inclusive lower bound on ``created_at``. Parsed
            by FastAPI directly into a timezone-aware ``datetime`` from an
            ISO 8601 query string, matching
            ``ActivityService.get_statistics``'s ``Optional[datetime]``
            parameter type (previously declared as ``Optional[str]`` here,
            which passed a raw string through to the repository's
            ``created_at >= date_from`` / ``created_at <= date_to``
            comparisons instead of a proper bound value).
        date_to: Optional inclusive upper bound on ``created_at``. See
            ``date_from``.
        current_user: The authenticated caller.
        service: The injected activity service.

    Returns:
        StatisticsResponse: The aggregate statistics payload.
    """
    return await service.get_statistics(date_from=date_from, date_to=date_to)


@router.get(
    "/timeline/{entity_type}/{entity_id}",
    response_model=TimelineResponse,
    summary="Get the chronological activity timeline for a single entity",
)
async def get_entity_timeline(
    entity_type: str,
    entity_id: str,
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=200, ge=1, le=500),
    sort_order: str = Query(default="asc", pattern="^(asc|desc)$"),
    current_user: User = Depends(get_current_user),
    service: ActivityService = Depends(get_activity_service),
) -> TimelineResponse:
    """Retrieves the full activity history for a single entity.

    Args:
        entity_type: The entity/table the timeline belongs to (e.g.
            ``"Booking"``).
        entity_id: The primary key of the entity.
        page: 1-indexed page number.
        page_size: Number of entries per page.
        sort_order: ``"asc"`` (chronological) or ``"desc"`` (newest first).
        current_user: The authenticated caller.
        service: The injected activity service.

    Returns:
        TimelineResponse: The entity's activity timeline.
    """
    return await service.get_entity_timeline(
        entity_type=entity_type,
        entity_id=entity_id,
        page=page,
        page_size=page_size,
        sort_order=sort_order,
    )


@router.get(
    "/module/{module}",
    response_model=ActivityListResponse,
    summary="Get the activity feed for an entire owning module",
)
async def get_module_timeline(
    module: str,
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=50, ge=1, le=200),
    sort_order: str = Query(default="desc", pattern="^(asc|desc)$"),
    current_user: User = Depends(get_current_user),
    service: ActivityService = Depends(get_activity_service),
) -> ActivityListResponse:
    """Retrieves the activity feed for an entire owning module.

    Args:
        module: The owning module to scope the feed to (e.g.
            ``"booking"``, ``"payment"``).
        page: 1-indexed page number.
        page_size: Number of entries per page.
        sort_order: ``"asc"`` or ``"desc"``.
        current_user: The authenticated caller.
        service: The injected activity service.

    Returns:
        ActivityListResponse: The module's activity feed.
    """
    items, total = await service.get_module_timeline(
        module=module, page=page, page_size=page_size, sort_order=sort_order
    )
    return ActivityListResponse(
        items=[ActivityResponse.model_validate(item) for item in items],
        total=total,
        page=page,
        page_size=page_size,
        total_pages=(total + page_size - 1) // page_size if page_size else 0,
    )


@router.get(
    "/user/{user_id}",
    response_model=ActivityListResponse,
    summary="Get the activity feed involving a specific user",
)
async def get_user_timeline(
    user_id: int,
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=50, ge=1, le=200),
    sort_order: str = Query(default="desc", pattern="^(asc|desc)$"),
    current_user: User = Depends(get_current_user),
    service: ActivityService = Depends(get_activity_service),
) -> ActivityListResponse:
    """Retrieves the activity feed involving a specific user.

    Includes activity entries where the user is either the performer of
    the action or the assignee.

    Args:
        user_id: The user id to scope the feed to.
        page: 1-indexed page number.
        page_size: Number of entries per page.
        sort_order: ``"asc"`` or ``"desc"``.
        current_user: The authenticated caller.
        service: The injected activity service.

    Returns:
        ActivityListResponse: The user's activity feed.
    """
    items, total = await service.get_user_timeline(
        user_id=user_id, page=page, page_size=page_size, sort_order=sort_order
    )
    return ActivityListResponse(
        items=[ActivityResponse.model_validate(item) for item in items],
        total=total,
        page=page,
        page_size=page_size,
        total_pages=(total + page_size - 1) // page_size if page_size else 0,
    )


@router.get(
    "/{activity_id}",
    response_model=ActivityResponse,
    summary="Get a single activity timeline entry by id",
)
async def get_activity(
    activity_id: uuid.UUID,
    current_user: User = Depends(get_current_user),
    service: ActivityService = Depends(get_activity_service),
) -> ActivityResponse:
    """Retrieves a single activity entry by its primary key.

    Args:
        activity_id: The UUID primary key of the entry.
        current_user: The authenticated caller.
        service: The injected activity service.

    Returns:
        ActivityResponse: The matching activity entry.

    Raises:
        HTTPException: 404 if no matching, non-deleted entry exists.
    """
    activity = await service.get_activity(activity_id)
    return ActivityResponse.model_validate(activity)


@router.put(
    "/{activity_id}",
    response_model=ActivityResponse,
    summary="Update an existing activity timeline entry",
    dependencies=[Depends(require_roles(*WRITE_ROLES))],
)
async def update_activity(
    activity_id: uuid.UUID,
    payload: ActivityUpdate,
    current_user: User = Depends(get_current_user),
    service: ActivityService = Depends(get_activity_service),
) -> ActivityResponse:
    """Applies a partial update to an existing activity entry.

    Args:
        activity_id: The UUID primary key of the entry to update.
        payload: The fields to update.
        current_user: The authenticated caller.
        service: The injected activity service.

    Returns:
        ActivityResponse: The updated activity entry.

    Raises:
        HTTPException: 404 if no matching, non-deleted entry exists.
    """
    activity = await service.update_activity(activity_id, payload)
    return ActivityResponse.model_validate(activity)


@router.delete(
    "/{activity_id}",
    response_model=ActivityResponse,
    summary="Soft-delete an activity timeline entry",
    dependencies=[Depends(require_roles(*DELETE_ROLES))],
)
async def delete_activity(
    activity_id: uuid.UUID,
    current_user: User = Depends(get_current_user),
    service: ActivityService = Depends(get_activity_service),
) -> ActivityResponse:
    """Soft-deletes an activity entry.

    Args:
        activity_id: The UUID primary key of the entry to delete.
        current_user: The authenticated caller.
        service: The injected activity service.

    Returns:
        ActivityResponse: The soft-deleted activity entry.

    Raises:
        HTTPException: 404 if no matching, non-deleted entry exists.
    """
    activity = await service.delete_activity(activity_id)
    return ActivityResponse.model_validate(activity)


@router.patch(
    "/{activity_id}/restore",
    response_model=ActivityResponse,
    summary="Restore a soft-deleted activity timeline entry",
    dependencies=[Depends(require_roles(*DELETE_ROLES))],
)
async def restore_activity(
    activity_id: uuid.UUID,
    current_user: User = Depends(get_current_user),
    service: ActivityService = Depends(get_activity_service),
) -> ActivityResponse:
    """Reverses a soft-delete on an activity entry.

    Args:
        activity_id: The UUID primary key of the entry to restore.
        current_user: The authenticated caller.
        service: The injected activity service.

    Returns:
        ActivityResponse: The restored activity entry.

    Raises:
        HTTPException: 404 if no matching, soft-deleted entry exists.
    """
    activity = await service.restore_activity(activity_id)
    return ActivityResponse.model_validate(activity)