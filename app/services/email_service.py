# backend/app/services/email_service.py
"""Business logic for email notifications: creation, dispatch, and tracking."""

import uuid
from datetime import datetime, timezone
from typing import List, Optional, Sequence

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.email_provider import EmailMessage, EmailProviderInterface
from app.models.email_notification import EmailNotification, EmailProvider
from app.models.notification import (
    Notification,
    NotificationCategory,
    NotificationChannel,
    NotificationPriority,
    NotificationStatus,
)
from app.models.notification_log import NotificationEventType, NotificationLog
from app.repositories.log_repository import LogRepository
from app.repositories.notification_repository import NotificationRepository
from app.services.notification_types import DispatchResult
from typing import Protocol


class RateLimiter(Protocol):
    async def check(self, key: str) -> bool:
        ...


class NoOpRateLimiter:
    """Default rate limiter that never blocks delivery."""

    async def check(self, key: str) -> bool:
        return True


class EmailService:
    """Business logic layer for creating and dispatching email notifications.

    Attributes:
        session: Async SQLAlchemy session used to persist channel details.
        notification_repo: Data access layer for notifications.
        log_repo: Data access layer for audit logs.
        provider: Email delivery provider implementation.
        provider_name: Enum identifier of the configured provider.
        default_sender_email: Default from-address used for outbound email.
        rate_limiter: Pluggable rate limiting hook.
    """

    def __init__(
        self,
        session: AsyncSession,
        notification_repo: NotificationRepository,
        log_repo: LogRepository,
        provider: EmailProviderInterface,
        provider_name: EmailProvider,
        default_sender_email: str,
        rate_limiter: Optional[RateLimiter] = None,
    ) -> None:
        """Initialize the service.

        Args:
            session: Async SQLAlchemy session for persisting email details.
            notification_repo: Repository for `Notification` entities.
            log_repo: Repository for `NotificationLog` entries.
            provider: Configured email delivery provider.
            provider_name: Enum identifier matching the configured provider.
            default_sender_email: Default from-address used when none is
                explicitly supplied.
            rate_limiter: Optional rate limiting hook; defaults to a no-op.
        """
        self.session = session
        self.notification_repo = notification_repo
        self.log_repo = log_repo
        self.provider = provider
        self.provider_name = provider_name
        self.default_sender_email = default_sender_email
        self.rate_limiter = rate_limiter or NoOpRateLimiter()

    async def send_single(
        self,
        recipient_id: uuid.UUID,
        to_email: str,
        subject: str,
        category: NotificationCategory,
        html_body: Optional[str] = None,
        text_body: Optional[str] = None,
        priority: NotificationPriority = NotificationPriority.NORMAL,
        sender_id: Optional[uuid.UUID] = None,
        template_id: Optional[uuid.UUID] = None,
        cc: Optional[List[str]] = None,
        bcc: Optional[List[str]] = None,
        reply_to: Optional[str] = None,
        from_email: Optional[str] = None,
        scheduled_at: Optional[datetime] = None,
        max_retries: int = 3,
    ) -> Notification:
        """Create a notification and its email detail record.

        Args:
            recipient_id: UUID of the recipient.
            to_email: Recipient email address.
            subject: Rendered subject line.
            category: Business category of the notification.
            html_body: Rendered HTML body content.
            text_body: Rendered plain text body content.
            priority: Delivery priority.
            sender_id: UUID of the user that triggered the notification.
            template_id: UUID of the template used to render this message.
            cc: Optional carbon-copy recipients.
            bcc: Optional blind carbon-copy recipients.
            reply_to: Optional reply-to address.
            from_email: Sender address; defaults to `default_sender_email`.
            scheduled_at: Optional timestamp for deferred delivery.
            max_retries: Maximum allowed delivery attempts.

        Returns:
            The persisted notification, with its email detail record created.

        Raises:
            InvalidNotificationStateError: If neither `html_body` nor
                `text_body` is provided.
        """
        if not html_body and not text_body:
            from app.services.notification_service import InvalidNotificationStateError

            raise InvalidNotificationStateError(
                "at least one of html_body or text_body is required"
            )

        notification = Notification(
            recipient_id=recipient_id,
            sender_id=sender_id,
            channel=NotificationChannel.EMAIL,
            category=category,
            priority=priority,
            status=NotificationStatus.PENDING,
            subject=subject,
            body=text_body or html_body or "",
            template_id=template_id,
            scheduled_at=scheduled_at,
            max_retries=max_retries,
        )
        notification = await self.notification_repo.create(notification)

        email_detail = EmailNotification(
            notification_id=notification.id,
            from_email=from_email or self.default_sender_email,
            to_email=to_email,
            cc=cc,
            bcc=bcc,
            reply_to=reply_to,
            subject=subject,
            html_body=html_body,
            text_body=text_body,
            provider=self.provider_name,
        )
        self.session.add(email_detail)
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

    async def send_bulk(
        self, requests: Sequence[dict]
    ) -> List[Notification]:
        """Create notifications and email details for a batch of recipients.

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
        """Attempt delivery of a persisted email notification.

        Args:
            notification: Notification entity with its `email_detail`
                relationship loaded.

        Returns:
            The outcome of the dispatch attempt.

        Raises:
            InvalidNotificationStateError: If no email detail record exists.
            RateLimitExceededError: If the rate limiting hook rejects the
                attempt.
        """
        detail = notification.email_detail
        if detail is None:
            from app.services.notification_service import InvalidNotificationStateError

            raise InvalidNotificationStateError(
                f"notification {notification.id} has no email detail record"
            )

        if not await self.rate_limiter.check(f"email:{detail.to_email}"):
            from app.services.notification_service import RateLimitExceededError

            raise RateLimitExceededError(
                f"rate limit exceeded for recipient {detail.to_email}"
            )

        message = EmailMessage(
            from_email=detail.from_email,
            to_email=detail.to_email,
            subject=detail.subject,
            html_body=detail.html_body,
            text_body=detail.text_body,
            cc=detail.cc or [],
            bcc=detail.bcc or [],
            reply_to=detail.reply_to,
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

    async def record_open(self, notification_id: uuid.UUID) -> EmailNotification:
        """Record that a recipient opened an email notification.

        Args:
            notification_id: UUID of the parent notification.

        Returns:
            The updated email detail record.

        Raises:
            NotificationNotFoundError: If no email detail record exists.
        """
        detail = await self._get_detail(notification_id)
        detail.opened_at = datetime.now(timezone.utc)
        await self.session.flush()
        return detail

    async def record_click(self, notification_id: uuid.UUID) -> EmailNotification:
        """Record that a recipient clicked a tracked link in an email.

        Args:
            notification_id: UUID of the parent notification.

        Returns:
            The updated email detail record.

        Raises:
            NotificationNotFoundError: If no email detail record exists.
        """
        detail = await self._get_detail(notification_id)
        detail.clicked_at = datetime.now(timezone.utc)
        await self.session.flush()
        return detail

    async def record_bounce(
        self, notification_id: uuid.UUID, bounce_type: str
    ) -> EmailNotification:
        """Record that an email notification bounced.

        Args:
            notification_id: UUID of the parent notification.
            bounce_type: Provider-reported bounce classification.

        Returns:
            The updated email detail record.

        Raises:
            NotificationNotFoundError: If no email detail record exists.
        """
        detail = await self._get_detail(notification_id)
        detail.is_bounced = True
        detail.bounce_type = bounce_type
        await self.session.flush()
        return detail

    async def _get_detail(self, notification_id: uuid.UUID) -> EmailNotification:
        """Load the email detail record for a notification via the session.

        Args:
            notification_id: UUID of the parent notification.

        Returns:
            The matching email detail record.

        Raises:
            NotificationNotFoundError: If no matching record exists.
        """
        notification = await self.notification_repo.get_by_id(
            notification_id, include_relations=True
        )
        if notification is None or notification.email_detail is None:
            from app.services.notification_service import NotificationNotFoundError

            raise NotificationNotFoundError(notification_id)
        return notification.email_detail