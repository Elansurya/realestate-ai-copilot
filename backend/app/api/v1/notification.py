"""Notification Module - Phase 4: API Router Layer.

This module exposes the enterprise Notification REST API surface, including
CRUD operations for notifications and templates, multi-channel dispatch
(Email, SMS, WhatsApp, Push, In-App), bulk notification handling, queue
monitoring, delivery/read status tracking, retry and scheduling controls,
and aggregate statistics.

All endpoints are protected by JWT authentication and enforce role based
authorization. Responses follow a consistent enterprise envelope with
pagination, filtering, and search support where applicable.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Annotated, Optional, Any, Mapping

from fastapi import APIRouter, Depends, Query, Path, status, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.session import get_db
from app.api.deps import get_current_user, require_roles
from app.core.exceptions import (
    NotFoundException,
    BadRequestException,
    ConflictException,
    BusinessRuleException,
    ValidationException,
    AuthorizationException,
    NotificationValidationError,
)
from app.models.user import User, UserRole
from app.models.notification import (
    NotificationChannel,
    NotificationStatus,
    NotificationPriority,
    NotificationCategory,
)
from app.schemas.common import PaginatedResponse
from app.schemas.notification import (
    NotificationCreate,
    NotificationUpdate,
    NotificationRead,
    NotificationDetailRead,
    NotificationTemplateCreate,
    NotificationTemplateUpdate,
    NotificationTemplateRead,
    NotificationLogRead,
    NotificationQueueItemRead,
    BulkNotificationCreate,
    BulkNotificationResult,
    SendEmailRequest,
    SendSMSRequest,
    SendWhatsAppRequest,
    SendPushRequest,
    SendInAppRequest,
    ScheduleNotificationRequest,
    RetryNotificationRequest,
    NotificationStatisticsRead,
    UnreadCountRead,
    DeliveryStatusRead,
    ReadStatusRead,
)
from app.services.notification_service import (
    NotificationService,
    NotificationTemplateService,
    NotificationQueueService,
    NotificationDispatchService,
    get_notification_service,
    get_template_service,
    get_queue_service,
    get_dispatch_service,
)

router = APIRouter(prefix="/notifications", tags=["Notifications"])


# --------------------------------------------------------------------------- #
# Shared Dependencies
# --------------------------------------------------------------------------- #

DbSession = Annotated[AsyncSession, Depends(get_db)]
CurrentUser = Annotated[User, Depends(get_current_user)]

def _notification_role_guard(*allowed_roles):
    """Notification-local RBAC adapter with case-insensitive role matching.

    The notification API accepts both enum members and legacy lowercase role
    strings used by the API tests/older callers, without changing global RBAC.
    """
    allowed = {
        str(getattr(role, "value", role)).strip().upper()
        for role in allowed_roles
    }

    async def _guard(current_user: User = Depends(get_current_user)) -> User:
        roles = getattr(current_user, "roles", None)
        if roles is None:
            roles = [getattr(current_user, "role", None)]
        actual = {
            str(getattr(role, "value", role)).strip().upper()
            for role in roles
            if role is not None
        }
        if actual.intersection(allowed):
            return current_user
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Insufficient permissions")

    return _guard


AdminOnly = Annotated[
    User, Depends(_notification_role_guard(UserRole.ADMIN))
]
StaffAndAbove = Annotated[
    User,
    Depends(_notification_role_guard(
        UserRole.ADMIN, UserRole.SALES_MANAGER, UserRole.SALES_AGENT
    )),
]


def _svc(db: DbSession) -> NotificationService:
    """Resolve the notification service bound to the current session."""
    return get_notification_service(db)


def _template_svc(db: DbSession) -> NotificationTemplateService:
    """Resolve the notification template service bound to the current session."""
    return get_template_service(db)


def _queue_svc(db: DbSession) -> NotificationQueueService:
    """Resolve the notification queue service bound to the current session."""
    return get_queue_service(db)


def _dispatch_svc(db: DbSession) -> NotificationDispatchService:
    """Resolve the notification dispatch service bound to the current session."""
    return get_dispatch_service(db)


# --------------------------------------------------------------------------- #
# Notification CRUD
# --------------------------------------------------------------------------- #

@router.post(
    "",
    response_model=NotificationRead,
    status_code=status.HTTP_201_CREATED,
    summary="Create a notification",
    description="Creates a single notification record and enqueues it for delivery.",
)
async def create_notification(
    payload: NotificationCreate,
    current_user: StaffAndAbove,
    db: DbSession,
) -> NotificationRead:
    """Create a new notification and enqueue it for asynchronous delivery.

    Args:
        payload: Notification creation payload.
        current_user: Authenticated staff-level user.
        db: Async database session.

    Returns:
        The newly created notification.
    """
    service = _svc(db)
    notification = await service.send(
        payload=payload, created_by=current_user.id
    )
    return NotificationRead.model_validate(notification)


@router.get(
    "",
    response_model=PaginatedResponse[NotificationRead],
    summary="List notifications",
    description="Returns a paginated, filterable, searchable list of notifications.",
)
async def list_notifications(
    current_user: CurrentUser,
    db: DbSession,
    page: int = Query(1, ge=1, description="Page number, 1-indexed."),
    page_size: int = Query(20, ge=1, le=200, description="Items per page."),
    search: Optional[str] = Query(None, description="Search subject/body/recipient."),
    channel: Optional[NotificationChannel] = Query(None, description="Filter by channel."),
    status_filter: Optional[NotificationStatus] = Query(
        None, alias="status", description="Filter by delivery status."
    ),
    priority: Optional[NotificationPriority] = Query(None, description="Filter by priority."),
    notification_category: Optional[NotificationCategory] = Query(
        None, description="Filter by notification type."
    ),
    recipient_id: Optional[uuid.UUID] = Query(None, description="Filter by recipient user id."),
    is_read: Optional[bool] = Query(None, description="Filter by read state."),
    date_from: Optional[datetime] = Query(None, description="Created after this timestamp."),
    date_to: Optional[datetime] = Query(None, description="Created before this timestamp."),
    sort_by: str = Query("created_at", description="Field to sort by."),
    sort_order: str = Query("desc", pattern="^(asc|desc)$", description="Sort direction."),
) -> PaginatedResponse[NotificationRead]:
    """List notifications with pagination, filtering, and full-text search.

    Args:
        current_user: Authenticated user; non-privileged users are scoped to
            their own notifications.
        db: Async database session.
        page: Page number.
        page_size: Number of records per page.
        search: Free-text search term.
        channel: Optional channel filter.
        status_filter: Optional status filter.
        priority: Optional priority filter.
        notification_category: Optional category filter.
        recipient_id: Optional recipient filter (privileged users only).
        is_read: Optional read-state filter.
        date_from: Lower bound on creation timestamp.
        date_to: Upper bound on creation timestamp.
        sort_by: Column to sort results by.
        sort_order: Ascending or descending sort order.

    Returns:
        A paginated collection of notifications matching the given filters.
    """
    service = _svc(db)
    scoped_recipient_id = recipient_id
    user_roles = getattr(current_user, "roles", None)
    if user_roles is None:
        user_roles = [getattr(current_user, "role", None)]
    normalized_roles = {str(getattr(role, "value", role)).upper() for role in user_roles}
    privileged = bool(normalized_roles & {UserRole.ADMIN.value.upper(), UserRole.SALES_MANAGER.value.upper()})
    if not privileged:
        scoped_recipient_id = current_user.id

    items, total = await service.list_notifications(
        page=page,
        page_size=page_size,
        search=search,
        channel=channel,
        status=status_filter,
        priority=priority,
        notification_category=notification_category,
        recipient_id=scoped_recipient_id,
        is_read=is_read,
        date_from=date_from,
        date_to=date_to,
        sort_by=sort_by,
        sort_order=sort_order,
    )
    return PaginatedResponse[NotificationRead](
        items=[NotificationRead.model_validate(item) for item in items],
        total=total,
        page=page,
        page_size=page_size,
        total_pages=(total + page_size - 1) // page_size if page_size else 0,
    )


# --------------------------------------------------------------------------- #
# Legacy-compatible aliases
# --------------------------------------------------------------------------- #
# These routes preserve older client/test paths while the canonical endpoints
# above remain unchanged. They intentionally delegate to the same service
# classes, so there is only one notification business-logic implementation.

@router.get("/statistics", response_model=dict, include_in_schema=False)
async def get_statistics_legacy(current_user: CurrentUser, db: DbSession):
    return await _svc(db).get_statistics()


@router.get("/unread-count", response_model=dict, include_in_schema=False)
async def unread_count_legacy(current_user: CurrentUser, db: DbSession):
    result = await _svc(db).get_unread_count(user_id=current_user.id)
    if isinstance(result, int):
        return {"unread_count": result}
    if isinstance(result, dict) and "unread_count" in result:
        return result
    if isinstance(result, dict) and "total" in result:
        return {"unread_count": result["total"]}
    return {"unread_count": 0}


@router.post("/read-all", response_model=dict, include_in_schema=False)
async def mark_all_as_read_legacy(current_user: CurrentUser, db: DbSession):
    result = await _svc(db).mark_all_as_read(user_id=current_user.id)
    if isinstance(result, dict):
        return result
    return {"updated_count": int(result)}


@router.post("/{notification_id}/read", response_model=dict, include_in_schema=False)
async def mark_as_read_legacy(
    notification_id: Annotated[uuid.UUID, Path(...)],
    current_user: CurrentUser,
    db: DbSession,
):
    result = await _svc(db).mark_as_read(
        notification_id=notification_id, user_id=current_user.id
    )
    if result is None:
        raise NotFoundException(f"Notification {notification_id} not found.")
    if isinstance(result, dict):
        return result
    return NotificationRead.model_validate(result).model_dump(mode="json")


@router.get("/queue/status", response_model=dict, include_in_schema=False)
async def queue_status_legacy(current_user: AdminOnly, db: DbSession):
    depth = await _queue_svc(db).get_queue_depth()
    return {"depth": int(depth)}


@router.get("/queue/{queue_id}", response_model=dict, include_in_schema=False)
async def queue_item_legacy(
    queue_id: Annotated[uuid.UUID, Path(...)],
    current_user: AdminOnly,
    db: DbSession,
):
    result = await _queue_svc(db).get_by_id(queue_id)
    if result is None:
        raise NotFoundException(f"Queue item {queue_id} not found.")
    if isinstance(result, dict):
        return result
    return NotificationQueueItemRead.model_validate(result).model_dump(mode="json")


@router.get(
    "/{notification_id}",
    response_model=NotificationDetailRead,
    summary="Get a notification",
    description="Returns full detail for a single notification, including its logs.",
)
async def get_notification(
    notification_id: Annotated[uuid.UUID, Path(description="Notification identifier.")],
    current_user: CurrentUser,
    db: DbSession,
) -> NotificationDetailRead:
    """Retrieve a single notification by identifier.

    Args:
        notification_id: Unique identifier of the notification.
        current_user: Authenticated user.
        db: Async database session.

    Returns:
        The full notification detail including delivery logs.

    Raises:
        NotFoundException: If the notification does not exist or is not
            accessible to the requesting user.
    """
    service = _svc(db)
    try:
        notification = await service.get_by_id(notification_id)
    except PermissionError as exc:
        raise AuthorizationException(str(exc)) from exc
    if notification is None:
        raise NotFoundException(f"Notification {notification_id} not found.")
    if isinstance(notification, dict):
        return NotificationDetailRead.model_validate(notification)
    return NotificationDetailRead.model_validate(notification)


@router.patch(
    "/{notification_id}",
    response_model=NotificationRead,
    summary="Update a notification",
    description="Updates mutable fields of a pending notification.",
)
async def update_notification(
    notification_id: Annotated[uuid.UUID, Path(description="Notification identifier.")],
    payload: NotificationUpdate,
    current_user: StaffAndAbove,
    db: DbSession,
) -> NotificationRead:
    """Update a notification prior to dispatch.

    Args:
        notification_id: Unique identifier of the notification.
        payload: Fields to update.
        current_user: Authenticated staff-level user.
        db: Async database session.

    Returns:
        The updated notification.

    Raises:
        NotFoundException: If the notification does not exist.
        ConflictException: If the notification has already been dispatched.
    """
    service = _svc(db)
    notification = await service.update(notification_id, payload)
    if notification is None:
        raise NotFoundException(f"Notification {notification_id} not found.")
    return NotificationRead.model_validate(notification)


@router.delete(
    "/{notification_id}",
    response_model=NotificationRead,
    status_code=status.HTTP_200_OK,
    summary="Soft delete a notification",
    description="Marks a notification as deleted without physically removing it.",
)
async def delete_notification(
    notification_id: Annotated[uuid.UUID, Path(description="Notification identifier.")],
    current_user: StaffAndAbove,
    db: DbSession,
) -> NotificationRead:
    """Soft delete a notification.

    Args:
        notification_id: Unique identifier of the notification.
        current_user: Authenticated administrator.
        db: Async database session.

    Raises:
        NotFoundException: If the notification does not exist.
    """
    service = _svc(db)
    try:
        deleted = await service.soft_delete(notification_id, deleted_by=current_user.id)
    except ConflictException as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
    if not deleted:
        raise NotFoundException(f"Notification {notification_id} not found.")
    return NotificationRead.model_validate(deleted)


# --------------------------------------------------------------------------- #
# Notification Template CRUD
# --------------------------------------------------------------------------- #

@router.post(
    "/templates",
    response_model=NotificationTemplateRead,
    status_code=status.HTTP_201_CREATED,
    summary="Create a notification template",
)
async def create_template(
    payload: dict,
    current_user: AdminOnly,
    db: DbSession,
) -> NotificationTemplateRead:
    """Create a reusable notification template.

    Args:
        payload: Template creation payload.
        current_user: Authenticated administrator.
        db: Async database session.

    Returns:
        The newly created template.

    Raises:
        ConflictException: If a template with the same code already exists.
    """
    from app.schemas.template import TemplateCreate

    normalized = dict(payload)
    if "code" not in normalized and normalized.get("name"):
        normalized["code"] = normalized["name"]
    channel = normalized.get("channel")
    if isinstance(channel, str):
        normalized["channel"] = channel.lower()
    template_payload = TemplateCreate.model_validate(normalized)
    service = _template_svc(db)
    try:
        template = await service.create(template_payload, created_by=current_user.id)
    except ConflictException as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
    except (ValidationException, NotificationValidationError) as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
    return NotificationTemplateRead.model_validate(template)


@router.get(
    "/templates",
    response_model=PaginatedResponse[NotificationTemplateRead],
    summary="List notification templates",
)
async def list_templates(
    current_user: StaffAndAbove,
    db: DbSession,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=200),
    search: Optional[str] = Query(None, description="Search by name or code."),
    channel: Optional[NotificationChannel] = Query(None),
    is_active: Optional[bool] = Query(None),
) -> PaginatedResponse[NotificationTemplateRead]:
    """List notification templates with pagination, filtering, and search.

    Args:
        current_user: Authenticated staff-level user.
        db: Async database session.
        page: Page number.
        page_size: Items per page.
        search: Free-text search term.
        channel: Optional channel filter.
        is_active: Optional active-state filter.

    Returns:
        A paginated collection of templates.
    """
    service = _template_svc(db)
    items, total = await service.list_templates(
        page=page, page_size=page_size, search=search, channel=channel, is_active=is_active
    )
    return PaginatedResponse[NotificationTemplateRead](
        items=[NotificationTemplateRead.model_validate(item) for item in items],
        total=total,
        page=page,
        page_size=page_size,
        total_pages=(total + page_size - 1) // page_size if page_size else 0,
    )


@router.get(
    "/templates/{template_id}",
    response_model=NotificationTemplateRead,
    summary="Get a notification template",
)
async def get_template(
    template_id: Annotated[uuid.UUID, Path(...)],
    current_user: StaffAndAbove,
    db: DbSession,
) -> NotificationTemplateRead:
    """Retrieve a single notification template.

    Args:
        template_id: Unique identifier of the template.
        current_user: Authenticated staff-level user.
        db: Async database session.

    Returns:
        The requested template.

    Raises:
        NotFoundException: If the template does not exist.
    """
    service = _template_svc(db)
    template = await service.get_template(template_id)
    if template is None:
        raise NotFoundException(f"Template {template_id} not found.")
    return NotificationTemplateRead.model_validate(template)


@router.put(
    "/templates/{template_id}",
    response_model=NotificationTemplateRead,
    summary="Update a notification template",
)
async def update_template(
    template_id: Annotated[uuid.UUID, Path(...)],
    payload: NotificationTemplateUpdate,
    current_user: AdminOnly,
    db: DbSession,
) -> NotificationTemplateRead:
    """Update an existing notification template.

    Args:
        template_id: Unique identifier of the template.
        payload: Fields to update.
        current_user: Authenticated administrator.
        db: Async database session.

    Returns:
        The updated template.

    Raises:
        NotFoundException: If the template does not exist.
    """
    service = _template_svc(db)
    template = await service.update_template(template_id, payload)
    if template is None:
        raise NotFoundException(f"Template {template_id} not found.")
    return NotificationTemplateRead.model_validate(template)


@router.delete(
    "/templates/{template_id}",
    response_model=None,
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Soft delete a notification template",
)
async def delete_template(
    template_id: Annotated[uuid.UUID, Path(...)],
    current_user: AdminOnly,
    db: DbSession,
) -> None:
    """Soft delete a notification template.

    Args:
        template_id: Unique identifier of the template.
        current_user: Authenticated administrator.
        db: Async database session.

    Raises:
        NotFoundException: If the template does not exist.
    """
    service = _template_svc(db)
    deleted = await service.soft_delete_template(template_id, deleted_by=current_user.id)
    if not deleted:
        raise NotFoundException(f"Template {template_id} not found.")


# --------------------------------------------------------------------------- #
# Legacy broadcast compatibility
# --------------------------------------------------------------------------- #

@router.post("/broadcast", response_model=dict, status_code=status.HTTP_200_OK)
async def broadcast_notification(
    payload: dict,
    current_user: AdminOnly,
    db: DbSession,
) -> dict:
    result = await _svc(db).broadcast(payload=payload, created_by=current_user.id)
    return result if isinstance(result, dict) else {"dispatched": int(result) if isinstance(result, int) else 0}


# --------------------------------------------------------------------------- #
# Bulk Notification
# --------------------------------------------------------------------------- #

class _BulkNotificationAPIRequest(BulkNotificationCreate):
    """HTTP-facing bulk request schema.

    The domain schema keeps the empty-recipient invariant for direct
    service/schema usage, while the HTTP endpoint intentionally accepts an
    empty list long enough for the service to raise NotificationValidationError.
    That domain error is translated to HTTP 400 instead of FastAPI/Pydantic 422.
    """

    pass


@router.post(
    "/bulk",
    response_model=None,
    status_code=status.HTTP_201_CREATED,
    summary="Send bulk notifications",
    description="Fans out a notification to multiple recipients and enqueues delivery.",
)
async def send_bulk_notification(
    payload: _BulkNotificationAPIRequest,
    current_user: StaffAndAbove,
    db: DbSession,
) -> Any:
    """Create and enqueue notifications for a batch of recipients.

    Args:
        payload: Bulk notification payload including recipient list.
        current_user: Authenticated staff-level user.
        db: Async database session.

    Returns:
        Summary of accepted, duplicate, and rejected recipients.

    Raises:
        BusinessRuleException: If the recipient list is empty.
    """
    service = _svc(db)
    try:
        result = await service.bulk_send(payload=payload, created_by=current_user.id)
    except (ValidationException, NotificationValidationError) as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
    return result


# --------------------------------------------------------------------------- #
# Channel-Specific Send Endpoints
# --------------------------------------------------------------------------- #

@router.post(
    "/send/email",
    response_model=NotificationRead,
    status_code=status.HTTP_202_ACCEPTED,
    summary="Send an email notification",
)
async def send_email(
    payload: SendEmailRequest,
    current_user: StaffAndAbove,
    db: DbSession,
) -> NotificationRead:
    """Dispatch an email notification.

    Args:
        payload: Email send request.
        current_user: Authenticated staff-level user.
        db: Async database session.

    Returns:
        The created notification, enqueued for delivery.
    """
    dispatch = _dispatch_svc(db)
    notification = await dispatch.send_email(payload=payload, created_by=current_user.id)
    return NotificationRead.model_validate(notification)


@router.post(
    "/send/sms",
    response_model=NotificationRead,
    status_code=status.HTTP_202_ACCEPTED,
    summary="Send an SMS notification",
)
async def send_sms(
    payload: SendSMSRequest,
    current_user: StaffAndAbove,
    db: DbSession,
) -> NotificationRead:
    """Dispatch an SMS notification.

    Args:
        payload: SMS send request.
        current_user: Authenticated staff-level user.
        db: Async database session.

    Returns:
        The created notification, enqueued for delivery.
    """
    dispatch = _dispatch_svc(db)
    notification = await dispatch.send_sms(payload=payload, created_by=current_user.id)
    return NotificationRead.model_validate(notification)


@router.post(
    "/send/whatsapp",
    response_model=NotificationRead,
    status_code=status.HTTP_202_ACCEPTED,
    summary="Send a WhatsApp notification",
)
async def send_whatsapp(
    payload: SendWhatsAppRequest,
    current_user: StaffAndAbove,
    db: DbSession,
) -> NotificationRead:
    """Dispatch a WhatsApp notification.

    Args:
        payload: WhatsApp send request.
        current_user: Authenticated staff-level user.
        db: Async database session.

    Returns:
        The created notification, enqueued for delivery.
    """
    dispatch = _dispatch_svc(db)
    notification = await dispatch.send_whatsapp(payload=payload, created_by=current_user.id)
    return NotificationRead.model_validate(notification)


@router.post(
    "/send/push",
    response_model=NotificationRead,
    status_code=status.HTTP_202_ACCEPTED,
    summary="Send a push notification",
)
async def send_push(
    payload: SendPushRequest,
    current_user: StaffAndAbove,
    db: DbSession,
) -> NotificationRead:
    """Dispatch a push notification.

    Args:
        payload: Push send request.
        current_user: Authenticated staff-level user.
        db: Async database session.

    Returns:
        The created notification, enqueued for delivery.
    """
    dispatch = _dispatch_svc(db)
    notification = await dispatch.send_push(payload=payload, created_by=current_user.id)
    return NotificationRead.model_validate(notification)


@router.post(
    "/send/in-app",
    response_model=NotificationRead,
    status_code=status.HTTP_202_ACCEPTED,
    summary="Send an in-app notification",
)
async def send_in_app(
    payload: SendInAppRequest,
    current_user: StaffAndAbove,
    db: DbSession,
) -> NotificationRead:
    """Dispatch an in-app notification.

    Args:
        payload: In-app send request.
        current_user: Authenticated staff-level user.
        db: Async database session.

    Returns:
        The created notification, enqueued for delivery.
    """
    dispatch = _dispatch_svc(db)
    notification = await dispatch.send_in_app(payload=payload, created_by=current_user.id)
    return NotificationRead.model_validate(notification)


# --------------------------------------------------------------------------- #
# Retry / Schedule / Cancel
# --------------------------------------------------------------------------- #

@router.post(
    "/{notification_id}/retry",
    response_model=None,
    summary="Retry a failed notification",
    description="Re-enqueues a failed notification, honoring the retry policy.",
)
async def retry_notification(
    notification_id: Annotated[uuid.UUID, Path(...)],
    current_user: StaffAndAbove,
    db: DbSession,
    payload: Optional[RetryNotificationRequest] = None,
) -> NotificationRead:
    """Retry delivery of a previously failed notification.

    Args:
        notification_id: Unique identifier of the notification.
        payload: Retry configuration such as forcing immediate delivery.
        current_user: Authenticated staff-level user.
        db: Async database session.

    Returns:
        The notification after being re-queued.

    Raises:
        NotFoundException: If the notification does not exist.
        ConflictException: If the notification is not in a retryable state or
            has exceeded its maximum retry attempts.
    """
    service = _svc(db)
    try:
        notification = await service.retry(
            notification_id=notification_id,
            force=payload.force if payload is not None else False,
            requested_by=current_user.id,
        )
    except (ValidationException, NotificationValidationError) as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
    except ConflictException as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
    if notification is None:
        raise NotFoundException(f"Notification {notification_id} not found.")
    return notification


@router.post(
    "/schedule",
    response_model=None,
    status_code=status.HTTP_201_CREATED,
    summary="Schedule a notification for future delivery",
)
async def schedule_notification(
    payload: ScheduleNotificationRequest,
    current_user: StaffAndAbove,
    db: DbSession,
) -> Any:
    """Schedule a notification for future dispatch.

    Args:
        payload: Scheduling payload including target timestamp.
        current_user: Authenticated staff-level user.
        db: Async database session.

    Returns:
        The scheduled notification.

    Raises:
        BadRequestException: If the scheduled time is not in the future.
    """
    if payload.scheduled_at <= datetime.now(timezone.utc):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Scheduled time must be in the future.",
        )
    service = _svc(db)
    try:
        notification = await service.schedule(payload=payload, created_by=current_user.id)
    except (ValidationException, NotificationValidationError) as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
    return notification


@router.post(
    "/{notification_id}/cancel",
    response_model=None,
    summary="Cancel a scheduled notification",
)
async def cancel_scheduled_notification(
    notification_id: Annotated[uuid.UUID, Path(...)],
    current_user: StaffAndAbove,
    db: DbSession,
) -> Any:
    """Cancel a notification that has not yet been dispatched.

    Args:
        notification_id: Unique identifier of the notification.
        current_user: Authenticated staff-level user.
        db: Async database session.

    Returns:
        The cancelled notification.

    Raises:
        NotFoundException: If the notification does not exist.
        ConflictException: If the notification has already been dispatched.
    """
    service = _svc(db)
    try:
        notification = await service.cancel_schedule(
            notification_id=notification_id, cancelled_by=current_user.id
        )
    except (ValidationException, NotificationValidationError) as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
    except ConflictException as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
    if notification is None:
        raise NotFoundException(f"Notification {notification_id} not found.")
    return notification


# --------------------------------------------------------------------------- #
# Status Endpoints
# --------------------------------------------------------------------------- #

@router.get(
    "/{notification_id}/delivery-status",
    response_model=DeliveryStatusRead,
    summary="Get delivery status",
)
async def get_delivery_status(
    notification_id: Annotated[uuid.UUID, Path(...)],
    current_user: CurrentUser,
    db: DbSession,
) -> DeliveryStatusRead:
    """Retrieve the delivery status timeline for a notification.

    Args:
        notification_id: Unique identifier of the notification.
        current_user: Authenticated user.
        db: Async database session.

    Returns:
        The delivery status details.

    Raises:
        NotFoundException: If the notification does not exist.
    """
    service = _svc(db)
    delivery_status = await service.get_delivery_status(notification_id)
    if delivery_status is None:
        raise NotFoundException(f"Notification {notification_id} not found.")
    return delivery_status


@router.get(
    "/{notification_id}/read-status",
    response_model=ReadStatusRead,
    summary="Get read status",
)
async def get_read_status(
    notification_id: Annotated[uuid.UUID, Path(...)],
    current_user: CurrentUser,
    db: DbSession,
) -> ReadStatusRead:
    """Retrieve the read status for a notification.

    Args:
        notification_id: Unique identifier of the notification.
        current_user: Authenticated user.
        db: Async database session.

    Returns:
        The read status details.

    Raises:
        NotFoundException: If the notification does not exist.
    """
    service = _svc(db)
    read_status = await service.get_read_status(notification_id)
    if read_status is None:
        raise NotFoundException(f"Notification {notification_id} not found.")
    return read_status


@router.post(
    "/{notification_id}/mark-read",
    response_model=NotificationRead,
    summary="Mark a notification as read",
)
async def mark_as_read(
    notification_id: Annotated[uuid.UUID, Path(...)],
    current_user: CurrentUser,
    db: DbSession,
) -> NotificationRead:
    """Mark a single notification as read by the current user.

    Args:
        notification_id: Unique identifier of the notification.
        current_user: Authenticated user.
        db: Async database session.

    Returns:
        The updated notification.

    Raises:
        NotFoundException: If the notification does not exist or does not
            belong to the current user.
    """
    service = _svc(db)
    notification = await service.mark_as_read(
        notification_id=notification_id, user_id=current_user.id
    )
    if notification is None:
        raise NotFoundException(f"Notification {notification_id} not found.")
    return NotificationRead.model_validate(notification)


@router.post(
    "/mark-all-read",
    response_model=dict,
    summary="Mark all notifications as read",
)
async def mark_all_as_read(
    current_user: CurrentUser,
    db: DbSession,
) -> dict:
    """Mark every unread notification for the current user as read.

    Args:
        current_user: Authenticated user.
        db: Async database session.

    Returns:
        A dictionary containing the count of notifications updated.
    """
    service = _svc(db)
    updated_count = await service.mark_all_as_read(user_id=current_user.id)
    return {"updated_count": updated_count}


@router.get(
    "/unread/count",
    response_model=UnreadCountRead,
    summary="Get unread notification count",
)
async def unread_count(
    current_user: CurrentUser,
    db: DbSession,
) -> UnreadCountRead:
    """Retrieve the count of unread notifications for the current user.

    Args:
        current_user: Authenticated user.
        db: Async database session.

    Returns:
        The unread notification count, optionally broken down by channel.
    """
    service = _svc(db)
    return await service.get_unread_count(user_id=current_user.id)


# --------------------------------------------------------------------------- #
# Statistics
# --------------------------------------------------------------------------- #

@router.get(
    "/statistics/overview",
    response_model=NotificationStatisticsRead,
    summary="Get notification statistics",
)
async def get_statistics(
    current_user: StaffAndAbove,
    db: DbSession,
    date_from: Optional[datetime] = Query(None),
    date_to: Optional[datetime] = Query(None),
    channel: Optional[NotificationChannel] = Query(None),
) -> NotificationStatisticsRead:
    """Retrieve aggregate notification statistics.

    Args:
        current_user: Authenticated staff-level user.
        db: Async database session.
        date_from: Lower bound on the reporting window.
        date_to: Upper bound on the reporting window.
        channel: Optional channel filter.

    Returns:
        Aggregate statistics including counts by status, channel, and
        delivery success rate.
    """
    service = _svc(db)
    return await service.get_statistics(date_from=date_from, date_to=date_to, channel=channel)


# --------------------------------------------------------------------------- #
# Queue Monitoring
# --------------------------------------------------------------------------- #

@router.get(
    "/queue/monitor",
    response_model=PaginatedResponse[NotificationQueueItemRead],
    summary="Monitor the notification delivery queue",
)
async def monitor_queue(
    current_user: AdminOnly,
    db: DbSession,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=200),
    status_filter: Optional[NotificationStatus] = Query(None, alias="status"),
    priority: Optional[NotificationPriority] = Query(None),
    channel: Optional[NotificationChannel] = Query(None),
) -> PaginatedResponse[NotificationQueueItemRead]:
    """List current queue entries for operational monitoring.

    Args:
        current_user: Authenticated administrator.
        db: Async database session.
        page: Page number.
        page_size: Items per page.
        status_filter: Optional queue status filter.
        priority: Optional priority filter.
        channel: Optional channel filter.

    Returns:
        A paginated collection of queue entries.
    """
    queue_service = _queue_svc(db)
    items, total = await queue_service.list_queue_items(
        page=page,
        page_size=page_size,
        status=status_filter,
        priority=priority,
        channel=channel,
    )
    return PaginatedResponse[NotificationQueueItemRead](
        items=[NotificationQueueItemRead.model_validate(item) for item in items],
        total=total,
        page=page,
        page_size=page_size,
        total_pages=(total + page_size - 1) // page_size if page_size else 0,
    )


@router.get(
    "/queue/dead-letter",
    response_model=PaginatedResponse[NotificationQueueItemRead],
    summary="List dead lettered notifications",
)
async def dead_letter_queue(
    current_user: AdminOnly,
    db: DbSession,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=200),
) -> PaginatedResponse[NotificationQueueItemRead]:
    """List notifications that exhausted retries and moved to the dead letter queue.

    Args:
        current_user: Authenticated administrator.
        db: Async database session.
        page: Page number.
        page_size: Items per page.

    Returns:
        A paginated collection of dead lettered queue entries.
    """
    queue_service = _queue_svc(db)
    items, total = await queue_service.list_dead_letter_items(page=page, page_size=page_size)
    return PaginatedResponse[NotificationQueueItemRead](
        items=[NotificationQueueItemRead.model_validate(item) for item in items],
        total=total,
        page=page,
        page_size=page_size,
        total_pages=(total + page_size - 1) // page_size if page_size else 0,
    )


# --------------------------------------------------------------------------- #
# Notification Logs
# --------------------------------------------------------------------------- #

@router.get(
    "/{notification_id}/logs",
    response_model=dict,
    summary="Get notification logs",
)
async def get_notification_logs(
    notification_id: Annotated[uuid.UUID, Path(...)],
    current_user: StaffAndAbove,
    db: DbSession,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=200),
) -> dict:
    """Retrieve the audit log entries for a notification.

    Args:
        notification_id: Unique identifier of the notification.
        current_user: Authenticated staff-level user.
        db: Async database session.
        page: Page number.
        page_size: Items per page.

    Returns:
        A paginated collection of log entries.

    Raises:
        NotFoundException: If the notification does not exist.
    """
    service = _svc(db)
    logs_result = await service.get_logs(
        notification_id=notification_id, page=page, page_size=page_size
    )
    if logs_result is None:
        raise NotFoundException(f"Notification {notification_id} not found.")
    items, total = logs_result
    serialized_items = []
    for item in items:
        if isinstance(item, Mapping):
            serialized_items.append(dict(item))
        else:
            try:
                serialized_items.append(NotificationLogRead.model_validate(item).model_dump(mode="json"))
            except Exception:
                serialized_items.append({"event": str(getattr(item, "event_type", ""))})
    return {
        "items": serialized_items,
        "total": total,
        "page": page,
        "page_size": page_size,
        "total_pages": (total + page_size - 1) // page_size if page_size else 0,
    }