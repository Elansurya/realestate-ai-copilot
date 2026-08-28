# backend/app/services/push_service.py
"""Business logic for push notifications: creation, dispatch, and tracking."""

import uuid
from datetime import datetime
from typing import Any, Dict, List, Optional, Sequence

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.push_provider import PushMessage, PushProviderInterface
from app.models.notification import (
    Notification,
    NotificationCategory,
    NotificationChannel,
    NotificationPriority,
    NotificationStatus,
)
from app.models.notification_log import NotificationEventType, NotificationLog
from app.models.push_notification import DevicePlatform, PushNotification, PushProvider
from app.repositories.log_repository import LogRepository
from app.repositories.notification_repository import NotificationRepository
from app.services.notification_service import (
    DispatchResult,
    InvalidNotificationStateError,
    NotificationNotFoundError,
    NoOpRateLimiter,
    RateLimitExceededError,
    RateLimiter,
)


class PushService:
    """Business logic layer for creating and dispatching push notifications.

    Attributes:
        session: Async SQLAlchemy session used to persist channel details.
        notification_repo: Data access layer for notifications.
        log_repo: Data access layer for audit logs.
        provider: Push delivery provider implementation.
        provider_name: Enum identifier of the configured provider.
        rate_limiter: Pluggable rate limiting hook.
    """

    def __init__(
        self,
        session: AsyncSession,
        notification_repo: NotificationRepository,
        log_repo: LogRepository,
        provider: PushProviderInterface,
        provider_name: PushProvider,
        rate_limiter: Optional[RateLimiter] = None,
    ) -> None:
        """Initialize the service.

        Args:
            session: Async SQLAlchemy session for persisting push details.
            notification_repo: Repository for `Notification` entities.
            log_repo: Repository for `NotificationLog` entries.
            provider: Configured push delivery provider.
            provider_name: Enum identifier matching the configured provider.
            rate_limiter: Optional rate limiting hook; defaults to a no-op.
        """
        self.session = session
        self.notification_repo = notification_repo
        self.log_repo = log_repo
        self.provider = provider
        self.provider_name = provider_name
        self.rate_limiter = rate_limiter or NoOpRateLimiter()

    async def send_single(
        self,
        recipient_id: uuid.UUID,
        device_token: str,
        platform: DevicePlatform,
        title: str,
        body: str,
        category: NotificationCategory,
        data_payload: Optional[Dict[str, Any]] = None,
        priority: NotificationPriority = NotificationPriority.NORMAL,
        sender_id: Optional[uuid.UUID] = None,
        template_id: Optional[uuid.UUID] = None,
        is_silent: bool = False,
        badge_count: Optional[int] = None,
        scheduled_at: Optional[datetime] = None,
        max_retries: int = 3,
    ) -> Notification:
        """Create a notification and its push detail record.

        Args:
            recipient_id: UUID of the recipient.
            device_token: Target device push token/registration id.
            platform: Target device platform.
            title: Push notification title.
            body: Push notification body text.
            category: Business category of the notification.
            data_payload: Optional custom data payload delivered with push.
            priority: Delivery priority.
            sender_id: UUID of the user that triggered the notification.
            template_id: UUID of the template used to render this message.
            is_silent: Whether this is a silent/background push.
            badge_count: App icon badge count to set, if applicable.
            scheduled_at: Optional timestamp for deferred delivery.
            max_retries: Maximum allowed delivery attempts.

        Returns:
            The persisted notification, with its push detail record created.

        Raises:
            InvalidNotificationStateError: If `device_token` is blank.
        """
        if not device_token.strip():
            raise InvalidNotificationStateError("device_token must not be blank")

        notification = Notification(
            recipient_id=recipient_id,
            sender_id=sender_id,
            channel=NotificationChannel.PUSH,
            category=category,
            priority=priority,
            status=NotificationStatus.PENDING,
            subject=title,
            body=body,
            template_id=template_id,
            scheduled_at=scheduled_at,
            max_retries=max_retries,
        )
        notification = await self.notification_repo.create(notification)

        push_detail = PushNotification(
            notification_id=notification.id,
            device_token=device_token,
            platform=platform,
            title=title,
            body=body,
            data_payload=data_payload,
            provider=self.provider_name,
            is_silent=is_silent,
            badge_count=badge_count,
        )
        self.session.add(push_detail)
        await self.session.flush()

        await self.log_repo.create_log(
            NotificationLog(
                notification_id=notification.id,
                event_type=NotificationEventType.CREATED,
                status=NotificationStatus.PENDING,
                attempt_number=1,
            )
        )
        return notification

    async def send_bulk(self, requests: Sequence[dict]) -> List[Notification]:
        """Create notifications and push details for a batch of recipients.

        Args:
            requests: Sequence of keyword-argument dictionaries, each
                matching the parameters accepted by `send_single`.

        Returns:
            The persisted notifications, in the same order as `requests`.
        """
        results: List[Notification] = []
        for request in requests:
            results.append(await self.send_single(**request))
        return results

    async def dispatch(self, notification: Notification) -> DispatchResult:
        """Attempt delivery of a persisted push notification.

        Args:
            notification: Notification entity with its `push_detail`
                relationship loaded.

        Returns:
            The outcome of the dispatch attempt.

        Raises:
            InvalidNotificationStateError: If no push detail record exists.
            RateLimitExceededError: If the rate limiting hook rejects the
                attempt.
        """
        detail = notification.push_detail
        if detail is None:
            raise InvalidNotificationStateError(
                f"notification {notification.id} has no push detail record"
            )

        if not await self.rate_limiter.check(f"push:{detail.device_token}"):
            raise RateLimitExceededError(
                f"rate limit exceeded for device {detail.device_token}"
            )

        message = PushMessage(
            device_token=detail.device_token,
            platform=detail.platform,
            title=detail.title,
            body=detail.body,
            data_payload=detail.data_payload or {},
            is_silent=detail.is_silent,
            badge_count=detail.badge_count,
        )
        result = await self.provider.send(message)

        if result.success:
            detail.provider_message_id = result.provider_message_id
        await self.session.flush()

        return DispatchResult(
            success=result.success,
            provider_message_id=result.provider_message_id,
            error_message=result.error_message,
        )

    async def update_badge_count(
        self, notification_id: uuid.UUID, badge_count: int
    ) -> PushNotification:
        """Update the badge count associated with a push notification.

        Args:
            notification_id: UUID of the parent notification.
            badge_count: New app icon badge count.

        Returns:
            The updated push detail record.

        Raises:
            InvalidNotificationStateError: If `badge_count` is negative.
            NotificationNotFoundError: If no push detail record exists.
        """
        if badge_count < 0:
            raise InvalidNotificationStateError("badge_count must be non-negative")

        notification = await self.notification_repo.get_by_id(
            notification_id, include_relations=True
        )
        if notification is None or notification.push_detail is None:
            raise NotificationNotFoundError(notification_id)

        notification.push_detail.badge_count = badge_count
        await self.session.flush()
        return notification.push_detail