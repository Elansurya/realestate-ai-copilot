# backend/app/services/whatsapp_service.py
"""Business logic for WhatsApp notifications: creation, dispatch, and tracking."""

import uuid
from datetime import datetime
from typing import List, Optional, Sequence

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.whatsapp_provider import WhatsAppMessage, WhatsAppProviderInterface
from app.models.notification import (
    Notification,
    NotificationCategory,
    NotificationChannel,
    NotificationPriority,
    NotificationStatus,
)
from app.models.notification_log import NotificationEventType, NotificationLog
from app.models.whatsapp_notification import (
    WhatsAppMessageType,
    WhatsAppNotification,
    WhatsAppProvider,
)
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


class WhatsAppService:
    """Business logic layer for creating and dispatching WhatsApp notifications.

    Attributes:
        session: Async SQLAlchemy session used to persist channel details.
        notification_repo: Data access layer for notifications.
        log_repo: Data access layer for audit logs.
        provider: WhatsApp delivery provider implementation.
        provider_name: Enum identifier of the configured provider.
        default_business_number: Default from-number used for outbound
            messages.
        rate_limiter: Pluggable rate limiting hook.
    """

    def __init__(
        self,
        session: AsyncSession,
        notification_repo: NotificationRepository,
        log_repo: LogRepository,
        provider: WhatsAppProviderInterface,
        provider_name: WhatsAppProvider,
        default_business_number: str,
        rate_limiter: Optional[RateLimiter] = None,
    ) -> None:
        """Initialize the service.

        Args:
            session: Async SQLAlchemy session for persisting WhatsApp details.
            notification_repo: Repository for `Notification` entities.
            log_repo: Repository for `NotificationLog` entries.
            provider: Configured WhatsApp delivery provider.
            provider_name: Enum identifier matching the configured provider.
            default_business_number: Default sender WhatsApp business number.
            rate_limiter: Optional rate limiting hook; defaults to a no-op.
        """
        self.session = session
        self.notification_repo = notification_repo
        self.log_repo = log_repo
        self.provider = provider
        self.provider_name = provider_name
        self.default_business_number = default_business_number
        self.rate_limiter = rate_limiter or NoOpRateLimiter()

    async def send_single(
        self,
        recipient_id: uuid.UUID,
        to_number: str,
        message_type: WhatsAppMessageType,
        category: NotificationCategory,
        body: str,
        template_name: Optional[str] = None,
        template_language: Optional[str] = None,
        media_url: Optional[str] = None,
        priority: NotificationPriority = NotificationPriority.NORMAL,
        sender_id: Optional[uuid.UUID] = None,
        template_id: Optional[uuid.UUID] = None,
        from_number: Optional[str] = None,
        scheduled_at: Optional[datetime] = None,
        max_retries: int = 3,
    ) -> Notification:
        """Create a notification and its WhatsApp detail record.

        Args:
            recipient_id: UUID of the recipient.
            to_number: Recipient WhatsApp number in E.164 format.
            message_type: Type of WhatsApp message payload.
            category: Business category of the notification.
            body: Rendered notification body for the parent `Notification`
                record; distinct from provider-specific template payloads.
            template_name: Approved template name, required for template
                messages.
            template_language: Language code of the approved template.
            media_url: Media asset URL, required for media messages.
            priority: Delivery priority.
            sender_id: UUID of the user that triggered the notification.
            template_id: UUID of the internal rendering template used.
            from_number: Sender number; defaults to `default_business_number`.
            scheduled_at: Optional timestamp for deferred delivery.
            max_retries: Maximum allowed delivery attempts.

        Returns:
            The persisted notification, with its WhatsApp detail record
            created.

        Raises:
            InvalidNotificationStateError: If required fields for the
                selected message type are missing.
        """
        if message_type == WhatsAppMessageType.TEMPLATE and not template_name:
            raise InvalidNotificationStateError(
                "template_name is required for template messages"
            )
        if message_type == WhatsAppMessageType.MEDIA and not media_url:
            raise InvalidNotificationStateError(
                "media_url is required for media messages"
            )

        notification = Notification(
            recipient_id=recipient_id,
            sender_id=sender_id,
            channel=NotificationChannel.WHATSAPP,
            category=category,
            priority=priority,
            status=NotificationStatus.PENDING,
            subject=None,
            body=body,
            template_id=template_id,
            scheduled_at=scheduled_at,
            max_retries=max_retries,
        )
        notification = await self.notification_repo.create(notification)

        whatsapp_detail = WhatsAppNotification(
            notification_id=notification.id,
            from_number=from_number or self.default_business_number,
            to_number=to_number,
            message_type=message_type,
            template_name=template_name,
            template_language=template_language,
            media_url=media_url,
            provider=self.provider_name,
        )
        self.session.add(whatsapp_detail)
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
        """Create notifications and WhatsApp details for a batch of recipients.

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
        """Attempt delivery of a persisted WhatsApp notification.

        Args:
            notification: Notification entity with its `whatsapp_detail`
                relationship loaded.

        Returns:
            The outcome of the dispatch attempt.

        Raises:
            InvalidNotificationStateError: If no WhatsApp detail record
                exists.
            RateLimitExceededError: If the rate limiting hook rejects the
                attempt.
        """
        detail = notification.whatsapp_detail
        if detail is None:
            raise InvalidNotificationStateError(
                f"notification {notification.id} has no whatsapp detail record"
            )

        if not await self.rate_limiter.check(f"whatsapp:{detail.to_number}"):
            raise RateLimitExceededError(
                f"rate limit exceeded for recipient {detail.to_number}"
            )

        message = WhatsAppMessage(
            from_number=detail.from_number,
            to_number=detail.to_number,
            message_type=detail.message_type,
            text_body=notification.body if detail.message_type == WhatsAppMessageType.TEXT else None,
            template_name=detail.template_name,
            template_language=detail.template_language,
            media_url=detail.media_url,
        )
        result = await self.provider.send(message)

        if result.success:
            detail.provider_message_id = result.provider_message_id
            detail.whatsapp_message_status = "sent"
        else:
            detail.whatsapp_message_status = "failed"
        await self.session.flush()

        return DispatchResult(
            success=result.success,
            provider_message_id=result.provider_message_id,
            error_message=result.error_message,
        )

    async def update_message_status(
        self, notification_id: uuid.UUID, status: str
    ) -> WhatsAppNotification:
        """Update the raw provider-reported status of a WhatsApp notification.

        Args:
            notification_id: UUID of the parent notification.
            status: Raw provider-reported message status.

        Returns:
            The updated WhatsApp detail record.

        Raises:
            NotificationNotFoundError: If no WhatsApp detail record exists.
        """
        notification = await self.notification_repo.get_by_id(
            notification_id, include_relations=True
        )
        if notification is None or notification.whatsapp_detail is None:
            raise NotificationNotFoundError(notification_id)

        notification.whatsapp_detail.whatsapp_message_status = status
        await self.session.flush()
        return notification.whatsapp_detail