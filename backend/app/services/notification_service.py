# backend/app/services/notification_service.py
"""Core notification orchestration service.

Provides business logic for cross-channel notification lifecycle
management: creation, retrieval, read/unread tracking, cancellation,
delivery status recording, and audit history. Channel-specific dispatch
logic lives in the individual channel services; this module defines the
shared exception hierarchy and the `ChannelDispatcher` / `RateLimiter`
contracts that those services implement or consume.

Also exposes the service classes and factory functions consumed by the
API router layer: `NotificationService`, `NotificationTemplateService`,
`NotificationQueueService`, and `NotificationDispatchService`.
"""

import logging
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Dict, Optional, Protocol, Sequence, Tuple

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.exceptions import (
    BadRequestException,
    BusinessRuleException,
    ExternalServiceException,
    NotFoundException,
    RateLimitException,
    ValidationException,
)
from app.models.notification_template import NotificationTemplate
from app.models.notification import (
    Notification,
    NotificationCategory,
    NotificationChannel,
    NotificationPriority,
    NotificationStatus,
    NotificationTemplate,
)
from app.models.notification_log import NotificationEventType, NotificationLog
from app.models.notification_queue import NotificationQueue
from app.repositories.log_repository import LogRepository
from app.repositories.notification_repository import NotificationRepository
from app.repositories.queue_repository import QueueRepository

try:
    # Template persistence lives in its own repository, following the same
    # convention as the other notification repositories.
    from app.repositories.template_repository import TemplateRepository
except ImportError:  # pragma: no cover - defensive fallback
    TemplateRepository = None  # type: ignore[assignment,misc]

logger = logging.getLogger(__name__)


# --------------------------------------------------------------------------- #
# Exceptions
# --------------------------------------------------------------------------- #

class NotificationError(Exception):
    """Base exception for all notification module business errors.

    Kept as a plain `Exception` (not `AppException`) because non-HTTP
    callers -- e.g. `queue_service.py`'s background dispatch loop --
    catch this base class to build an internal `DispatchResult` and
    must keep working outside of any FastAPI request context.

    Every concrete subclass below additionally inherits from the
    matching `app.core.exceptions.AppException` subclass, so when one
    of these escapes into an HTTP request it is still translated by
    `app_exception_handler` into the correct 4xx envelope instead of
    falling through to the generic 500 handler.
    """


class NotificationNotFoundError(NotFoundException, NotificationError):
    """Raised when a notification cannot be located (404)."""

    default_error_code = "NOTIFICATION_NOT_FOUND"

    def __init__(self, notification_id: uuid.UUID) -> None:
        """Initialize the exception.

        Args:
            notification_id: UUID of the notification that was not found.
        """
        NotFoundException.__init__(
            self, f"Notification {notification_id} was not found"
        )
        self.notification_id = notification_id


class TemplateNotFoundError(NotFoundException, NotificationError):
    """Raised when no active template matches the requested key (404)."""

    default_error_code = "NOTIFICATION_TEMPLATE_NOT_FOUND"


class TemplateRenderError(ValidationException, NotificationError):
    """Raised when a template cannot be rendered from supplied variables (400)."""

    default_error_code = "TEMPLATE_RENDER_ERROR"


class InvalidNotificationStateError(BusinessRuleException, NotificationError):
    """Raised when an operation is invalid for a notification's current
    state, or when request parameters violate a domain rule (422)."""

    default_error_code = "INVALID_NOTIFICATION_STATE"


class ProviderDispatchError(ExternalServiceException, NotificationError):
    """Raised when a channel provider fails to accept a message (502)."""

    default_error_code = "NOTIFICATION_PROVIDER_DISPATCH_FAILED"


class RateLimitExceededError(RateLimitException, NotificationError):
    """Raised when a rate limiting hook rejects a dispatch attempt (429)."""

    default_error_code = "NOTIFICATION_RATE_LIMIT_EXCEEDED"


class RetryLimitExceededError(BusinessRuleException, NotificationError):
    """Raised when a notification has exhausted its allowed retry attempts (422)."""

    default_error_code = "RETRY_LIMIT_EXCEEDED"


class SchedulingError(BadRequestException, NotificationError):
    """Raised when a scheduling operation is invalid (400)."""

    default_error_code = "INVALID_SCHEDULING_REQUEST"


# --------------------------------------------------------------------------- #
# Dispatch contracts
# --------------------------------------------------------------------------- #

@dataclass
class DispatchResult:
    """Outcome of a single channel dispatch attempt.

    Attributes:
        success: Whether the underlying provider accepted the message.
        provider_message_id: Provider-assigned message identifier, if any.
        error_message: Error detail when `success` is False.
    """

    success: bool
    provider_message_id: Optional[str] = None
    error_message: Optional[str] = None


class ChannelDispatcher(Protocol):
    """Contract implemented by every channel service for queue dispatch."""

    async def dispatch(self, notification: Notification) -> DispatchResult:
        """Attempt delivery of a persisted notification for a specific channel.

        Args:
            notification: Notification entity to dispatch, expected to have
                its channel-specific detail relationship already loaded.

        Returns:
            The outcome of the dispatch attempt.
        """
        ...


class RateLimiter(Protocol):
    """Contract for pluggable rate limiting hooks."""

    async def check(self, key: str) -> bool:
        """Determine whether a dispatch attempt is currently permitted.

        Args:
            key: Rate limiting bucket key (e.g. channel + recipient).

        Returns:
            True if the attempt is permitted, False if it should be
            rejected.
        """
        ...


class NoOpRateLimiter:
    """Default rate limiter that permits every request.

    Used when no external rate limiting backend (e.g. Redis token bucket)
    has been wired in, keeping channel services fully functional without
    a hard dependency on a specific rate limiting implementation.
    """

    async def check(self, key: str) -> bool:
        """Always permit the dispatch attempt.

        Args:
            key: Rate limiting bucket key, unused.

        Returns:
            Always True.
        """
        return True


# --------------------------------------------------------------------------- #
# NotificationService
# --------------------------------------------------------------------------- #

class NotificationService:
    """Business logic orchestrator for cross-channel notification operations.

    Attributes:
        notification_repo: Data access layer for notifications.
        queue_repo: Data access layer for the dispatch queue.
        log_repo: Data access layer for audit logs.
    """

    def __init__(
        self,
        notification_repo: NotificationRepository,
        queue_repo: QueueRepository,
        log_repo: LogRepository,
    ) -> None:
        """Initialize the service with its required repositories.

        Args:
            notification_repo: Repository for `Notification` entities.
            queue_repo: Repository for `NotificationQueue` entries.
            log_repo: Repository for `NotificationLog` entries.
        """
        self.notification_repo = notification_repo
        self.queue_repo = queue_repo
        self.log_repo = log_repo

    # -- Retrieval ---------------------------------------------------------- #

    async def get_notification(
        self, notification_id: uuid.UUID, include_relations: bool = False
    ) -> Notification:
        """Fetch a notification, raising if it does not exist.

        Args:
            notification_id: UUID of the notification.
            include_relations: Whether to eagerly load channel details,
                template, queue, and log relationships.

        Returns:
            The matching notification.

        Raises:
            NotificationNotFoundError: If no matching, non-deleted
                notification exists.
        """
        notification = await self.notification_repo.get_by_id(
            notification_id, include_relations=include_relations
        )
        if notification is None:
            raise NotificationNotFoundError(notification_id)
        return notification

    async def get_notification_detail(
        self, notification_id: uuid.UUID
    ) -> Optional[Notification]:
        """Fetch full notification detail including logs, for the router layer.

        Unlike `get_notification`, this returns `None` instead of raising so
        the router can translate a miss into an HTTP 404.

        Args:
            notification_id: UUID of the notification.

        Returns:
            The notification with relations loaded, or None if not found.
        """
        return await self.notification_repo.get_by_id(
            notification_id, include_relations=True
        )

    async def notification_exists(self, notification_id: uuid.UUID) -> bool:
        """Check whether a notification exists.

        Args:
            notification_id: UUID of the notification.

        Returns:
            True if the notification exists and is not deleted.
        """
        notification = await self.notification_repo.get_by_id(notification_id)
        return notification is not None

    async def list_notifications(
        self,
        recipient_id: Optional[uuid.UUID] = None,
        channel: Optional[NotificationChannel] = None,
        notification_category: Optional[NotificationCategory] = None,
        priority: Optional[NotificationPriority] = None,
        status: Optional[NotificationStatus] = None,
        is_read: Optional[bool] = None,
        search: Optional[str] = None,
        date_from: Optional[datetime] = None,
        date_to: Optional[datetime] = None,
        sort_by: str = "created_at",
        sort_order: str = "desc",
        page: int = 1,
        page_size: int = 20,
    ) -> Tuple[Sequence[Notification], int]:
        """Fetch a filtered, sorted, paginated list of notifications.

        Args:
            recipient_id: Filter by recipient UUID.
            channel: Filter by delivery channel.
            notification_category: Filter by business category.
            priority: Filter by delivery priority.
            status: Filter by lifecycle status.
            is_read: Filter by read state.
            search: Free text match against subject and body.
            date_from: Lower bound on creation timestamp.
            date_to: Upper bound on creation timestamp.
            sort_by: Column name to sort by.
            sort_order: "asc" or "desc".
            page: 1-indexed page number.
            page_size: Number of records per page.

        Returns:
            A tuple of (matching notifications for the page, total count).
        """
        if page < 1 or page_size < 1:
            raise InvalidNotificationStateError("page and page_size must be positive integers")
        return await self.notification_repo.list_notifications(
            recipient_id=recipient_id,
            channel=channel,
            category=notification_category,
            priority=priority,
            status=status,
            is_read=is_read,
            search_term=search,
            date_from=date_from,
            date_to=date_to,
            sort_by=sort_by,
            sort_desc=(sort_order == "desc"),
            page=page,
            page_size=page_size,
        )

    # -- Creation / mutation -------------------------------------------------- #

    async def create_notification(
        self, payload: Any, created_by: uuid.UUID
    ) -> Notification:
        """Create a single notification and enqueue it for delivery.

        Args:
            payload: Notification creation payload (schema object).
            created_by: UUID of the user creating the notification.

        Returns:
            The newly created, persisted notification.
        """
        data: Dict[str, Any] = payload.model_dump()
        notification = Notification(**data)
        created = await self.notification_repo.create(notification)

        await self.queue_repo.enqueue(created)

        await self.log_repo.create_log(
            NotificationLog(
                notification_id=created.id,
                event_type=NotificationEventType.CREATED,
                status=created.status,
                attempt_number=1,
            )
        )
        return created

    async def update_notification(
        self, notification_id: uuid.UUID, payload: Any
    ) -> Optional[Notification]:
        """Update mutable fields of a pending notification.

        Args:
            notification_id: UUID of the notification.
            payload: Update payload (schema object) with only the fields
                to change set.

        Returns:
            The updated notification, or None if it does not exist.

        Raises:
            InvalidNotificationStateError: If the notification has already
                been dispatched.
        """
        notification = await self.notification_repo.get_by_id(notification_id)
        if notification is None:
            return None
        if notification.status not in (NotificationStatus.PENDING, NotificationStatus.QUEUED):
            raise InvalidNotificationStateError(
                f"cannot update notification {notification_id} in status {notification.status.value}"
            )
        fields = payload.model_dump(exclude_unset=True)
        return await self.notification_repo.update_fields(notification_id, fields)

    async def send_bulk(self, payload: Any, created_by: uuid.UUID) -> Any:
        """Create and enqueue notifications for a batch of recipients.

        Args:
            payload: Bulk notification payload including recipient list.
            created_by: UUID of the user initiating the bulk send.

        Returns:
            A `BulkNotificationResult`-shaped object summarizing the outcome.
        """
        accepted: list = []
        rejected: list = []
        base_data = payload.model_dump(exclude={"recipient_ids"})

        for recipient_id in payload.recipient_ids:
            try:
                notification = Notification(
                    **base_data, recipient_id=recipient_id
                )
                created = await self.notification_repo.create(notification)
                await self.queue_repo.enqueue(created)
                await self.log_repo.create_log(
                    NotificationLog(
                        notification_id=created.id,
                        event_type=NotificationEventType.CREATED,
                        status=created.status,
                        attempt_number=1,
                    )
                )
                accepted.append(created.id)
            except Exception as exc:  # noqa: BLE001 - collect per-recipient failures
                logger.warning(
                    "bulk notification failed for recipient %s: %s", recipient_id, exc
                )
                rejected.append({"recipient_id": recipient_id, "error": str(exc)})

        return {
            "accepted_count": len(accepted),
            "rejected_count": len(rejected),
            "accepted_ids": accepted,
            "rejected": rejected,
        }

    async def retry_notification(
        self,
        notification_id: uuid.UUID,
        force: bool = False,
        requested_by: Optional[uuid.UUID] = None,
    ) -> Optional[Notification]:
        """Retry delivery of a previously failed notification.

        Args:
            notification_id: UUID of the notification.
            force: Whether to bypass the standard retry backoff/limit checks.
            requested_by: UUID of the user requesting the retry.

        Returns:
            The re-queued notification, or None if it does not exist.

        Raises:
            InvalidNotificationStateError: If the notification is not in a
                retryable state.
            RetryLimitExceededError: If retries are exhausted and `force`
                is False.
        """
        notification = await self.notification_repo.get_by_id(notification_id)
        if notification is None:
            return None

        if notification.status != NotificationStatus.FAILED:
            raise InvalidNotificationStateError(
                f"cannot retry notification {notification_id} in status {notification.status.value}"
            )

        max_retries = getattr(notification, "max_retries", 3)
        if not force and notification.retry_count >= max_retries:
            raise RetryLimitExceededError(
                f"notification {notification_id} has exhausted its retry attempts"
            )

        updated = await self.notification_repo.update_fields(
            notification_id,
            {"status": NotificationStatus.QUEUED, "retry_count": notification.retry_count + 1},
        )
        if updated is None:
            return None

        await self.queue_repo.enqueue(updated)

        await self.log_repo.create_log(
            NotificationLog(
                notification_id=notification_id,
                event_type=NotificationEventType.RETRIED,
                status=NotificationStatus.QUEUED,
                attempt_number=updated.retry_count,
            )
        )
        return updated

    async def schedule_notification(self, payload: Any, created_by: uuid.UUID) -> Notification:
        """Schedule a notification for future dispatch.

        Args:
            payload: Scheduling payload including target timestamp.
            created_by: UUID of the user scheduling the notification.

        Returns:
            The scheduled notification.

        Raises:
            SchedulingError: If the scheduled timestamp is invalid.
        """
        if payload.scheduled_at <= datetime.now(timezone.utc):
            raise SchedulingError("scheduled_at must be in the future")

        data = payload.model_dump()
        notification = Notification(
            **data, status=NotificationStatus.SCHEDULED
        )
        created = await self.notification_repo.create(notification)

        await self.log_repo.create_log(
            NotificationLog(
                notification_id=created.id,
                event_type=NotificationEventType.SCHEDULED,
                status=NotificationStatus.SCHEDULED,
                attempt_number=1,
            )
        )
        return created

    async def cancel_notification(
        self, notification_id: uuid.UUID, cancelled_by: Optional[uuid.UUID] = None
    ) -> Optional[Notification]:
        """Cancel a notification that has not yet completed delivery.

        Args:
            notification_id: UUID of the notification.
            cancelled_by: UUID of the user requesting the cancellation.

        Returns:
            The updated, cancelled notification, or None if it does not exist.

        Raises:
            InvalidNotificationStateError: If the notification has already
                reached a terminal delivered/read state.
        """
        notification = await self.notification_repo.get_by_id(notification_id)
        if notification is None:
            return None

        if notification.status in (
            NotificationStatus.SENT,
            NotificationStatus.DELIVERED,
            NotificationStatus.READ,
        ):
            raise InvalidNotificationStateError(
                f"cannot cancel notification {notification_id} in status {notification.status.value}"
            )

        queue_entry = await self.queue_repo.get_by_notification_id(notification_id)
        if queue_entry is not None:
            await self.queue_repo.mark_failed(queue_entry.id, "cancelled by user request")

        updated = await self.notification_repo.update_fields(
            notification_id, {"status": NotificationStatus.CANCELLED}
        )
        if updated is None:
            return None

        await self.log_repo.create_log(
            NotificationLog(
                notification_id=notification_id,
                event_type=NotificationEventType.CANCELLED,
                status=NotificationStatus.CANCELLED,
                attempt_number=updated.retry_count + 1,
            )
        )
        return updated

    async def soft_delete_notification(
        self, notification_id: uuid.UUID, deleted_by: Optional[uuid.UUID] = None
    ) -> bool:
        """Soft delete a notification.

        Args:
            notification_id: UUID of the notification to delete.
            deleted_by: UUID of the user performing the deletion.

        Returns:
            True once the notification has been soft deleted, False if it
            did not exist.
        """
        deleted = await self.notification_repo.soft_delete(
            notification_id, datetime.now(timezone.utc)
        )
        return bool(deleted)

    # -- Read / unread -------------------------------------------------------- #

    async def mark_as_read(
        self, notification_id: uuid.UUID, user_id: Optional[uuid.UUID] = None
    ) -> Optional[Notification]:
        """Mark a notification as read by its recipient.

        Args:
            notification_id: UUID of the notification.
            user_id: UUID of the user marking the notification read; used to
                scope the operation to the notification's owner when provided.

        Returns:
            The updated notification, or None if not found (or not owned by
            `user_id`, when provided). Idempotent if already read.
        """
        notification = await self.notification_repo.get_by_id(notification_id)
        if notification is None:
            return None
        if user_id is not None and getattr(notification, "recipient_id", None) != user_id:
            return None
        if notification.is_read:
            return notification

        updated = await self.notification_repo.mark_as_read(
            notification_id, datetime.now(timezone.utc)
        )
        if updated is None:
            return None

        await self.log_repo.create_log(
            NotificationLog(
                notification_id=notification_id,
                event_type=NotificationEventType.READ,
                status=NotificationStatus.READ,
                attempt_number=updated.retry_count + 1,
            )
        )
        return updated

    async def mark_all_as_read(self, user_id: uuid.UUID) -> int:
        """Mark every unread notification for a recipient as read.

        Args:
            user_id: UUID of the recipient.

        Returns:
            The number of notifications updated.
        """
        items, _ = await self.notification_repo.list_notifications(
            recipient_id=user_id,
            is_read=False,
            page=1,
            page_size=10_000,
        )
        updated_count = 0
        for item in items:
            result = await self.notification_repo.mark_as_read(
                item.id, datetime.now(timezone.utc)
            )
            if result is not None:
                await self.log_repo.create_log(
                    NotificationLog(
                        notification_id=item.id,
                        event_type=NotificationEventType.READ,
                        status=NotificationStatus.READ,
                        attempt_number=result.retry_count + 1,
                    )
                )
                updated_count += 1
        return updated_count

    async def get_unread_count(self, user_id: uuid.UUID) -> Any:
        """Count unread notifications for a recipient.

        Args:
            user_id: UUID of the recipient.

        Returns:
            An object/dict exposing the unread notification count.
        """
        count = await self.notification_repo.get_unread_count(user_id)
        return {"unread_count": count}

    # -- Status --------------------------------------------------------------- #

    async def get_delivery_status(self, notification_id: uuid.UUID) -> Optional[Any]:
        """Retrieve the delivery status timeline for a notification.

        Args:
            notification_id: UUID of the notification.

        Returns:
            A delivery status payload, or None if the notification does not
            exist.
        """
        notification = await self.notification_repo.get_by_id(notification_id)
        if notification is None:
            return None
        return {
            "notification_id": notification.id,
            "status": notification.status,
            "sent_at": getattr(notification, "sent_at", None),
            "delivered_at": getattr(notification, "delivered_at", None),
            "failure_reason": getattr(notification, "failure_reason", None),
            "retry_count": notification.retry_count,
        }

    async def get_read_status(self, notification_id: uuid.UUID) -> Optional[Any]:
        """Retrieve the read status for a notification.

        Args:
            notification_id: UUID of the notification.

        Returns:
            A read status payload, or None if the notification does not
            exist.
        """
        notification = await self.notification_repo.get_by_id(notification_id)
        if notification is None:
            return None
        return {
            "notification_id": notification.id,
            "is_read": notification.is_read,
            "read_at": getattr(notification, "read_at", None),
        }

    # -- History / logs --------------------------------------------------------- #

    async def get_history(
        self, notification_id: uuid.UUID, page: int = 1, page_size: int = 50
    ) -> Tuple[Sequence[NotificationLog], int]:
        """Fetch the paginated audit history for a notification.

        Args:
            notification_id: UUID of the notification.
            page: 1-indexed page number.
            page_size: Number of records per page.

        Returns:
            A tuple of (log entries for the page, total matching count).
        """
        await self.get_notification(notification_id)
        return await self.log_repo.list_logs_for_notification(
            notification_id, page=page, page_size=page_size
        )

    async def list_logs(
        self, notification_id: uuid.UUID, page: int = 1, page_size: int = 20
    ) -> Tuple[Sequence[NotificationLog], int]:
        """Router-facing alias for `get_history`.

        Args:
            notification_id: UUID of the notification.
            page: 1-indexed page number.
            page_size: Number of records per page.

        Returns:
            A tuple of (log entries for the page, total matching count).
        """
        return await self.log_repo.list_logs_for_notification(
            notification_id, page=page, page_size=page_size
        )

    # -- Statistics --------------------------------------------------------- #

    async def get_statistics(
        self,
        date_from: Optional[datetime] = None,
        date_to: Optional[datetime] = None,
        channel: Optional[NotificationChannel] = None,
    ) -> Any:
        """Retrieve aggregate notification statistics.

        Args:
            date_from: Lower bound on the reporting window.
            date_to: Upper bound on the reporting window.
            channel: Optional channel filter.

        Returns:
            Aggregate statistics as returned by the repository layer.
        """
        return await self.notification_repo.get_statistics(
            date_from=date_from, date_to=date_to, channel=channel
        )

    # -- Delivery recording --------------------------------------------------- #

    async def record_delivery_success(
        self, notification_id: uuid.UUID, provider_message_id: Optional[str] = None
    ) -> Notification:
        """Record a successful dispatch for a notification.

        Args:
            notification_id: UUID of the notification.
            provider_message_id: Provider-assigned message identifier.

        Returns:
            The updated notification.

        Raises:
            NotificationNotFoundError: If the notification does not exist.
        """
        now = datetime.now(timezone.utc)
        updated = await self.notification_repo.update_delivery_status(
            notification_id, NotificationStatus.SENT, sent_at=now
        )
        if updated is None:
            raise NotificationNotFoundError(notification_id)

        await self.log_repo.create_log(
            NotificationLog(
                notification_id=notification_id,
                event_type=NotificationEventType.SENT,
                status=NotificationStatus.SENT,
                attempt_number=updated.retry_count + 1,
                provider_response=(
                    {"provider_message_id": provider_message_id}
                    if provider_message_id
                    else None
                ),
            )
        )
        return updated

    async def record_delivery_confirmation(self, notification_id: uuid.UUID) -> Notification:
        """Record provider-confirmed delivery for a notification.

        Args:
            notification_id: UUID of the notification.

        Returns:
            The updated notification.

        Raises:
            NotificationNotFoundError: If the notification does not exist.
        """
        now = datetime.now(timezone.utc)
        updated = await self.notification_repo.update_delivery_status(
            notification_id, NotificationStatus.DELIVERED, delivered_at=now
        )
        if updated is None:
            raise NotificationNotFoundError(notification_id)

        await self.log_repo.create_log(
            NotificationLog(
                notification_id=notification_id,
                event_type=NotificationEventType.DELIVERED,
                status=NotificationStatus.DELIVERED,
                attempt_number=updated.retry_count + 1,
            )
        )
        return updated

    async def record_delivery_failure(
        self, notification_id: uuid.UUID, error_message: str, attempt_number: int
    ) -> Notification:
        """Record a failed dispatch attempt for a notification.

        Args:
            notification_id: UUID of the notification.
            error_message: Human readable failure detail.
            attempt_number: Attempt number associated with this failure.

        Returns:
            The updated notification.

        Raises:
            NotificationNotFoundError: If the notification does not exist.
        """
        updated = await self.notification_repo.update_delivery_status(
            notification_id, NotificationStatus.FAILED, failure_reason=error_message
        )
        if updated is None:
            raise NotificationNotFoundError(notification_id)

        await self.log_repo.create_log(
            NotificationLog(
                notification_id=notification_id,
                event_type=NotificationEventType.FAILED,
                status=NotificationStatus.FAILED,
                attempt_number=attempt_number,
                error_message=error_message,
            )
        )
        logger.warning(
            "notification %s failed on attempt %s: %s",
            notification_id,
            attempt_number,
            error_message,
        )
        return updated


# --------------------------------------------------------------------------- #
# NotificationTemplateService
# --------------------------------------------------------------------------- #

    # ------------------------------------------------------------------
    # Backward-compatible service method names
    # ------------------------------------------------------------------
    async def get_by_id(self, notification_id: uuid.UUID) -> Optional[Notification]:
        return await self.get_notification(notification_id)

    async def update(self, notification_id: uuid.UUID, payload: Any) -> Optional[Notification]:
        return await self.update_notification(notification_id, payload)

    async def soft_delete(
        self, notification_id: uuid.UUID, deleted_by: Optional[uuid.UUID] = None
    ) -> Optional[Notification]:
        return await self.soft_delete_notification(notification_id, deleted_by=deleted_by)

    async def bulk_send(self, payload: Any, created_by: uuid.UUID) -> Any:
        return await self.send_bulk(payload, created_by=created_by)

    async def schedule(self, payload: Any, created_by: uuid.UUID) -> Notification:
        return await self.schedule_notification(payload, created_by=created_by)

    async def cancel_schedule(
        self, notification_id: uuid.UUID, cancelled_by: Optional[uuid.UUID] = None
    ) -> Optional[Notification]:
        return await self.cancel_notification(notification_id, cancelled_by=cancelled_by)

    async def retry(
        self,
        notification_id: uuid.UUID,
        force: bool = False,
        requested_by: Optional[uuid.UUID] = None,
    ) -> Optional[Notification]:
        return await self.retry_notification(
            notification_id, force=force, requested_by=requested_by
        )

    async def get_logs(
        self, notification_id: uuid.UUID, page: int = 1, page_size: int = 20
    ) -> Optional[Tuple[Sequence[Any], int]]:
        return await self.list_logs(notification_id, page=page, page_size=page_size)

    async def send(self, payload: Any, created_by: uuid.UUID) -> Notification:
        """Compatibility dispatcher for callers using the generic send API."""
        channel = getattr(payload, "channel", None)
        if channel == NotificationChannel.EMAIL:
            return await self.send_email(payload, created_by)
        if channel == NotificationChannel.SMS:
            return await self.send_sms(payload, created_by)
        if channel == NotificationChannel.WHATSAPP:
            return await self.send_whatsapp(payload, created_by)
        if channel == NotificationChannel.PUSH:
            return await self.send_push(payload, created_by)
        if channel == NotificationChannel.IN_APP:
            return await self.send_in_app(payload, created_by)
        raise ValidationException("Unsupported notification channel")

    async def broadcast(self, payload: Any, created_by: uuid.UUID) -> Any:
        return await self.send_bulk(payload, created_by=created_by)

class NotificationTemplateService:
    """Business logic orchestrator for notification template management.

    Attributes:
        template_repo: Data access layer for notification templates.
    """

    def __init__(self, template_repo: "TemplateRepository") -> None:
        """Initialize the service with its required repository.

        Args:
            template_repo: Repository for `NotificationTemplate` entities.
        """
        self.template_repo = template_repo

    async def get_by_code(self, code: str) -> Optional[Any]:
        """Fetch a template by its unique code.

        Args:
            code: Unique template code.

        Returns:
            The matching template, or None if no active template exists.
        """
        return await self.template_repo.get_by_code(code)

    async def get_template(self, template_id: uuid.UUID) -> Optional[Any]:
        """Fetch a template by identifier.

        Args:
            template_id: UUID of the template.

        Returns:
            The matching template, or None if not found.
        """
        return await self.template_repo.get_by_id(template_id)

    async def list_templates(
        self,
        page: int = 1,
        page_size: int = 20,
        search: Optional[str] = None,
        channel: Optional[NotificationChannel] = None,
        is_active: Optional[bool] = None,
    ) -> Tuple[Sequence[Any], int]:
        """Fetch a filtered, paginated list of templates.

        Args:
            page: 1-indexed page number.
            page_size: Number of records per page.
            search: Free text match against name and code.
            channel: Filter by delivery channel.
            is_active: Filter by active state.

        Returns:
            A tuple of (matching templates for the page, total count).
        """
        if page < 1 or page_size < 1:
            raise InvalidNotificationStateError("page and page_size must be positive integers")
        return await self.template_repo.list_templates(
            page=page,
            page_size=page_size,
            search_term=search,
            channel=channel,
            is_active=is_active,
        )

    async def create_template(self, payload: Any, created_by: uuid.UUID) -> Any:
        """Create a new notification template.

        Args:
            payload: Template creation payload.
            created_by: UUID of the user creating the template.

        Returns:
            The newly created template.
        """
        data = payload.model_dump()
        return await self.template_repo.create(data, created_by=created_by)

    async def update_template(self, template_id: uuid.UUID, payload: Any) -> Optional[Any]:
        """Update an existing notification template.

        Args:
            template_id: UUID of the template.
            payload: Update payload with only the fields to change set.

        Returns:
            The updated template, or None if it does not exist.
        """
        fields = payload.model_dump(exclude_unset=True)
        return await self.template_repo.update_fields(template_id, fields)

    async def soft_delete_template(
        self, template_id: uuid.UUID, deleted_by: Optional[uuid.UUID] = None
    ) -> bool:
        """Soft delete a notification template.

        Args:
            template_id: UUID of the template to delete.
            deleted_by: UUID of the user performing the deletion.

        Returns:
            True once the template has been soft deleted, False if it did
            not exist.
        """
        deleted = await self.template_repo.soft_delete(
            template_id, datetime.now(timezone.utc)
        )
        return bool(deleted)

    async def render(self, code: str, variables: Dict[str, Any]) -> Any:
        """Render a template's content using the supplied variables.

        Args:
            code: Unique template code.
            variables: Variables to interpolate into the template.

        Returns:
            The rendered template content.

        Raises:
            TemplateNotFoundError: If no active template matches `code`.
            TemplateRenderError: If rendering fails.
        """
        template = await self.template_repo.get_by_code(code)
        if template is None:
            raise TemplateNotFoundError(f"Template with code '{code}' was not found")
        try:
            return await self.template_repo.render(template, variables)
        except Exception as exc:  # noqa: BLE001
            raise TemplateRenderError(str(exc)) from exc


# --------------------------------------------------------------------------- #
# NotificationQueueService
# --------------------------------------------------------------------------- #

    async def create(self, data: Any) -> NotificationTemplate:
        """Compatibility alias for ``create_template``."""
        return await self.create_template(data)

class NotificationQueueService:
    """Business logic orchestrator for queue monitoring and dead lettering.

    Attributes:
        queue_repo: Data access layer for the dispatch queue.
    """

    def __init__(self, queue_repo: QueueRepository) -> None:
        """Initialize the service with its required repository.

        Args:
            queue_repo: Repository for `NotificationQueue` entries.
        """
        self.queue_repo = queue_repo

    async def list_queue_items(
        self,
        page: int = 1,
        page_size: int = 20,
        status: Optional[NotificationStatus] = None,
        priority: Optional[NotificationPriority] = None,
        channel: Optional[NotificationChannel] = None,
    ) -> Tuple[Sequence[Any], int]:
        """Fetch a filtered, paginated list of queue entries.

        Args:
            page: 1-indexed page number.
            page_size: Number of records per page.
            status: Filter by queue entry status.
            priority: Filter by priority.
            channel: Filter by delivery channel.

        Returns:
            A tuple of (matching queue entries for the page, total count).
        """
        if page < 1 or page_size < 1:
            raise InvalidNotificationStateError("page and page_size must be positive integers")
        return await self.queue_repo.list_queue_items(
            page=page,
            page_size=page_size,
            status=status,
            priority=priority,
            channel=channel,
        )

    async def list_dead_letter_items(
        self, page: int = 1, page_size: int = 20
    ) -> Tuple[Sequence[Any], int]:
        """Fetch notifications that exhausted retries and were dead lettered.

        Args:
            page: 1-indexed page number.
            page_size: Number of records per page.

        Returns:
            A tuple of (matching dead letter entries for the page, total
            count).
        """
        if page < 1 or page_size < 1:
            raise InvalidNotificationStateError("page and page_size must be positive integers")
        return await self.queue_repo.list_dead_letter_items(page=page, page_size=page_size)


# --------------------------------------------------------------------------- #
# NotificationDispatchService
# --------------------------------------------------------------------------- #

    async def get_queue_depth(self) -> int:
        """Return the number of currently queued entries."""
        if hasattr(self.queue_repo, "get_queue_depth"):
            return await self.queue_repo.get_queue_depth()
        items, total = await self.queue_repo.list_queue_entries(page=1, page_size=1)
        return int(total)

    async def get_by_id(self, queue_id: uuid.UUID) -> Optional[NotificationQueue]:
        """Fetch one queue entry by id."""
        return await self.queue_repo.get_by_id(queue_id)

class NotificationDispatchService:
    """Business logic orchestrator for channel-specific send endpoints.

    Builds and persists a `Notification` for the requested channel, then
    enqueues it for asynchronous delivery. Actual provider communication is
    performed by the queue worker via the `ChannelDispatcher` contract.

    Attributes:
        notification_repo: Data access layer for notifications.
        queue_repo: Data access layer for the dispatch queue.
        log_repo: Data access layer for audit logs.
    """

    def __init__(
        self,
        notification_repo: NotificationRepository,
        queue_repo: QueueRepository,
        log_repo: LogRepository,
    ) -> None:
        """Initialize the service with its required repositories.

        Args:
            notification_repo: Repository for `Notification` entities.
            queue_repo: Repository for `NotificationQueue` entries.
            log_repo: Repository for `NotificationLog` entries.
        """
        self.notification_repo = notification_repo
        self.queue_repo = queue_repo
        self.log_repo = log_repo

    async def _create_and_enqueue(
        self, channel: NotificationChannel, payload: Any, created_by: uuid.UUID
    ) -> Notification:
        """Persist a channel-specific send request and enqueue it.

        Args:
            channel: Delivery channel for this notification.
            payload: Channel-specific send request payload.
            created_by: UUID of the user initiating the send.

        Returns:
            The newly created, enqueued notification.
        """
        data = payload.model_dump()
        notification = Notification(
            **data,
            channel=channel,
            status=NotificationStatus.QUEUED,
            )
        created = await self.notification_repo.create(notification)
        await self.queue_repo.enqueue(created)

        await self.log_repo.create_log(
            NotificationLog(
                notification_id=created.id,
                event_type=NotificationEventType.CREATED,
                status=NotificationStatus.QUEUED,
                attempt_number=1,
            )
        )
        return created

    async def send_email(self, payload: Any, created_by: uuid.UUID) -> Notification:
        """Dispatch an email notification.

        Args:
            payload: Email send request.
            created_by: UUID of the user initiating the send.

        Returns:
            The created notification, enqueued for delivery.
        """
        return await self._create_and_enqueue(NotificationChannel.EMAIL, payload, created_by)

    async def send_sms(self, payload: Any, created_by: uuid.UUID) -> Notification:
        """Dispatch an SMS notification.

        Args:
            payload: SMS send request.
            created_by: UUID of the user initiating the send.

        Returns:
            The created notification, enqueued for delivery.
        """
        return await self._create_and_enqueue(NotificationChannel.SMS, payload, created_by)

    async def send_whatsapp(self, payload: Any, created_by: uuid.UUID) -> Notification:
        """Dispatch a WhatsApp notification.

        Args:
            payload: WhatsApp send request.
            created_by: UUID of the user initiating the send.

        Returns:
            The created notification, enqueued for delivery.
        """
        return await self._create_and_enqueue(NotificationChannel.WHATSAPP, payload, created_by)

    async def send_push(self, payload: Any, created_by: uuid.UUID) -> Notification:
        """Dispatch a push notification.

        Args:
            payload: Push send request.
            created_by: UUID of the user initiating the send.

        Returns:
            The created notification, enqueued for delivery.
        """
        return await self._create_and_enqueue(NotificationChannel.PUSH, payload, created_by)

    async def send_in_app(self, payload: Any, created_by: uuid.UUID) -> Notification:
        """Dispatch an in-app notification.

        Args:
            payload: In-app send request.
            created_by: UUID of the user initiating the send.

        Returns:
            The created notification, enqueued for delivery.
        """
        return await self._create_and_enqueue(NotificationChannel.IN_APP, payload, created_by)


# --------------------------------------------------------------------------- #
# Factory functions (consumed by the router layer)
# --------------------------------------------------------------------------- #

def get_notification_service(db: AsyncSession) -> NotificationService:
    """Build a `NotificationService` bound to the given session.

    Args:
        db: Async database session.

    Returns:
        A ready-to-use `NotificationService`.
    """
    return NotificationService(
        notification_repo=NotificationRepository(db),
        queue_repo=QueueRepository(db),
        log_repo=LogRepository(db),
    )


def get_template_service(db: AsyncSession) -> NotificationTemplateService:
    """Build a `NotificationTemplateService` bound to the given session.

    Args:
        db: Async database session.

    Returns:
        A ready-to-use `NotificationTemplateService`.

    Raises:
        RuntimeError: If the template repository module is unavailable.
    """
    if TemplateRepository is None:
        raise RuntimeError(
            "TemplateRepository is not available; "
            "app.repositories.template_repository could not be imported"
        )
    return NotificationTemplateService(template_repo=TemplateRepository(db))


def get_queue_service(db: AsyncSession) -> NotificationQueueService:
    """Build a `NotificationQueueService` bound to the given session.

    Args:
        db: Async database session.

    Returns:
        A ready-to-use `NotificationQueueService`.
    """
    return NotificationQueueService(queue_repo=QueueRepository(db))


def get_dispatch_service(db: AsyncSession) -> NotificationDispatchService:
    """Build a `NotificationDispatchService` bound to the given session.

    Args:
        db: Async database session.

    Returns:
        A ready-to-use `NotificationDispatchService`.
    """
    return NotificationDispatchService(
        notification_repo=NotificationRepository(db),
        queue_repo=QueueRepository(db),
        log_repo=LogRepository(db),
    )