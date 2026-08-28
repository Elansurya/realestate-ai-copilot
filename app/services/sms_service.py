# backend/app/services/sms_service.py
"""Business logic for SMS notifications: creation, dispatch, and tracking."""

import uuid
from datetime import datetime
from typing import List, Optional, Sequence

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.sms_provider import SMSMessage, SMSProviderInterface
from app.models.notification import (
    Notification,
    NotificationCategory,
    NotificationChannel,
    NotificationPriority,
    NotificationStatus,
)
from app.models.notification_log import NotificationEventType, NotificationLog
from app.models.sms_notification import SMSDeliveryStatus, SMSNotification, SMSProvider
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

_MAX_SEGMENT_LENGTH = 160


class SMSService:
    """Business logic layer for creating and dispatching SMS notifications.

    Attributes:
        session: Async SQLAlchemy session used to persist channel details.
        notification_repo: Data access layer for notifications.
        log_repo: Data access layer for audit logs.
        provider: SMS delivery provider implementation.
        provider_name: Enum identifier of the configured provider.
        default_sender_number: Default from-number used for outbound SMS.
        rate_limiter: Pluggable rate limiting hook.
    """

    def __init__(
        self,
        session: AsyncSession,
        notification_repo: NotificationRepository,
        log_repo: LogRepository,
        provider: SMSProviderInterface,
        provider_name: SMSProvider,
        default_sender_number: str,
        rate_limiter: Optional[RateLimiter] = None,
    ) -> None:
        """Initialize the service.

        Args:
            session: Async SQLAlchemy session for persisting SMS details.
            notification_repo: Repository for `Notification` entities.
            log_repo: Repository for `NotificationLog` entries.
            provider: Configured SMS delivery provider.
            provider_name: Enum identifier matching the configured provider.
            default_sender_number: Default from-number in E.164 format.
            rate_limiter: Optional rate limiting hook; defaults to a no-op.
        """
        self.session = session
        self.notification_repo = notification_repo
        self.log_repo = log_repo
        self.provider = provider
        self.provider_name = provider_name
        self.default_sender_number = default_sender_number
        self.rate_limiter = rate_limiter or NoOpRateLimiter()

    async def send_single(
        self,
        recipient_id: uuid.UUID,
        to_number: str,
        message_body: str,
        category: NotificationCategory,
        priority: NotificationPriority = NotificationPriority.NORMAL,
        sender_id: Optional[uuid.UUID] = None,
        template_id: Optional[uuid.UUID] = None,
        from_number: Optional[str] = None,
        scheduled_at: Optional[datetime] = None,
        max_retries: int = 3,
    ) -> Notification:
        """Create a notification and its SMS detail record.

        Args:
            recipient_id: UUID of the recipient.
            to_number: Recipient phone number in E.164 format.
            message_body: Rendered SMS text content.
            category: Business category of the notification.
            priority: Delivery priority.
            sender_id: UUID of the user that triggered the notification.
            template_id: UUID of the template used to render this message.
            from_number: Sender number; defaults to `default_sender_number`.
            scheduled_at: Optional timestamp for deferred delivery.
            max_retries: Maximum allowed delivery attempts.

        Returns:
            The persisted notification, with its SMS detail record created.

        Raises:
            InvalidNotificationStateError: If `message_body` is blank or
                exceeds the maximum supported length.
        """
        stripped_body = message_body.strip()
        if not stripped_body:
            raise InvalidNotificationStateError("message_body must not be blank")
        if len(stripped_body) > 1600:
            raise InvalidNotificationStateError(
                "message_body exceeds the maximum supported length of 1600 characters"
            )

        notification = Notification(
            recipient_id=recipient_id,
            sender_id=sender_id,
            channel=NotificationChannel.SMS,
            category=category,
            priority=priority,
            status=NotificationStatus.PENDING,
            subject=None,
            body=stripped_body,
            template_id=template_id,
            scheduled_at=scheduled_at,
            max_retries=max_retries,
        )
        notification = await self.notification_repo.create(notification)

        segments_count = max(1, -(-len(stripped_body) // _MAX_SEGMENT_LENGTH))
        sms_detail = SMSNotification(
            notification_id=notification.id,
            from_number=from_number or self.default_sender_number,
            to_number=to_number,
            message_body=stripped_body,
            provider=self.provider_name,
            delivery_status=SMSDeliveryStatus.QUEUED,
            segments_count=segments_count,
        )
        self.session.add(sms_detail)
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
        """Create notifications and SMS details for a batch of recipients.

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
        """Attempt delivery of a persisted SMS notification.

        Args:
            notification: Notification entity with its `sms_detail`
                relationship loaded.

        Returns:
            The outcome of the dispatch attempt.

        Raises:
            InvalidNotificationStateError: If no SMS detail record exists.
            RateLimitExceededError: If the rate limiting hook rejects the
                attempt.
        """
        detail = notification.sms_detail
        if detail is None:
            raise InvalidNotificationStateError(
                f"notification {notification.id} has no sms detail record"
            )

        if not await self.rate_limiter.check(f"sms:{detail.to_number}"):
            raise RateLimitExceededError(
                f"rate limit exceeded for recipient {detail.to_number}"
            )

        message = SMSMessage(
            from_number=detail.from_number,
            to_number=detail.to_number,
            body=detail.message_body,
        )
        result = await self.provider.send(message)

        if result.success:
            detail.provider_message_id = result.provider_message_id
            detail.delivery_status = SMSDeliveryStatus.SENT
        else:
            detail.delivery_status = SMSDeliveryStatus.FAILED
        await self.session.flush()

        return DispatchResult(
            success=result.success,
            provider_message_id=result.provider_message_id,
            error_message=result.error_message,
        )

    async def update_delivery_status(
        self, notification_id: uuid.UUID, delivery_status: SMSDeliveryStatus
    ) -> SMSNotification:
        """Update the provider-reported delivery status of an SMS notification.

        Args:
            notification_id: UUID of the parent notification.
            delivery_status: New provider-reported delivery status.

        Returns:
            The updated SMS detail record.

        Raises:
            NotificationNotFoundError: If no SMS detail record exists.
        """
        notification = await self.notification_repo.get_by_id(
            notification_id, include_relations=True
        )
        if notification is None or notification.sms_detail is None:
            raise NotificationNotFoundError(notification_id)

        notification.sms_detail.delivery_status = delivery_status
        await self.session.flush()
        return notification.sms_detail