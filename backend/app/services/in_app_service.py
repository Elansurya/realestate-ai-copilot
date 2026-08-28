# backend/app/services/in_app_service.py
"""Business logic for in-app notifications: creation, read state, and expiry."""

import uuid
from datetime import datetime, timezone
from typing import List, Optional, Sequence, Tuple

from sqlalchemy.ext.asyncio import AsyncSession

from app.models.in_app_notification import InAppDisplayType, InAppNotification
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


class InAppService:
    """Business logic layer for creating and managing in-app notifications.

    Unlike external channels, in-app notifications are considered delivered
    as soon as they are persisted, since the recipient's client reads them
    directly from the CRM's own data store.
    """

    def __init__(
        self,
        session: AsyncSession,
        notification_repo: NotificationRepository,
        log_repo: LogRepository,
    ) -> None:
        self.session = session
        self.notification_repo = notification_repo
        self.log_repo = log_repo

    async def send_single(
        self,
        recipient_id: uuid.UUID,
        user_id: uuid.UUID,
        title: str,
        body: str,
        category: NotificationCategory,
        icon: Optional[str] = None,
        action_url: Optional[str] = None,
        display_type: InAppDisplayType = InAppDisplayType.TOAST,
        priority: NotificationPriority = NotificationPriority.NORMAL,
        sender_id: Optional[uuid.UUID] = None,
        template_id: Optional[uuid.UUID] = None,
        expires_at: Optional[datetime] = None,
        max_retries: int = 1,
    ) -> Notification:
        notification = Notification(
            recipient_id=recipient_id,
            sender_id=sender_id,
            channel=NotificationChannel.IN_APP,
            category=category,
            priority=priority,
            status=NotificationStatus.DELIVERED,
            subject=title,
            body=body,
            template_id=template_id,
            delivered_at=datetime.now(timezone.utc),
            max_retries=max_retries,
        )
        notification = await self.notification_repo.create(notification)

        in_app_detail = InAppNotification(
            notification_id=notification.id,
            user_id=user_id,
            icon=icon,
            action_url=action_url,
            display_type=display_type,
            expires_at=expires_at,
        )
        self.session.add(in_app_detail)
        await self.session.flush()

        await self.log_repo.create_log(
            NotificationLog(
                notification_id=notification.id,
                event_type=NotificationEventType.CREATED,
                status=NotificationStatus.PENDING,
                attempt_number=1,
            )
        )
        await self.log_repo.create_log(
            NotificationLog(
                notification_id=notification.id,
                event_type=NotificationEventType.DELIVERED,
                status=NotificationStatus.DELIVERED,
                attempt_number=1,
            )
        )
        return notification

    async def send_bulk(self, requests: Sequence[dict]) -> List[Notification]:
        results: List[Notification] = []
        for request in requests:
            results.append(await self.send_single(**request))
        return results

    async def dispatch(self, notification: Notification) -> DispatchResult:
        if notification.in_app_detail is None:
            from app.services.notification_service import InvalidNotificationStateError

            raise InvalidNotificationStateError(
                f"notification {notification.id} has no in_app detail record"
            )
        return DispatchResult(success=True)

    async def mark_read(self, notification_id: uuid.UUID) -> InAppNotification:
        detail = await self._get_detail(notification_id)
        detail.is_read = True
        detail.read_at = datetime.now(timezone.utc)
        await self.session.flush()

        await self.notification_repo.mark_as_read(notification_id, detail.read_at)
        await self.log_repo.create_log(
            NotificationLog(
                notification_id=notification_id,
                event_type=NotificationEventType.READ,
                status=NotificationStatus.READ,
                attempt_number=1,
            )
        )
        return detail

    async def mark_dismissed(self, notification_id: uuid.UUID) -> InAppNotification:
        detail = await self._get_detail(notification_id)
        detail.is_dismissed = True
        detail.dismissed_at = datetime.now(timezone.utc)
        await self.session.flush()
        return detail

    async def list_unread_for_user(
        self, user_id: uuid.UUID, page: int = 1, page_size: int = 20
    ) -> Tuple[Sequence[Notification], int]:
        return await self.notification_repo.list_notifications(
            recipient_id=user_id,
            channel=NotificationChannel.IN_APP,
            is_read=False,
            page=page,
            page_size=page_size,
        )

    async def _get_detail(self, notification_id: uuid.UUID) -> InAppNotification:
        notification = await self.notification_repo.get_by_id(
            notification_id, include_relations=True
        )
        if notification is None or notification.in_app_detail is None:
            from app.services.notification_service import NotificationNotFoundError

            raise NotificationNotFoundError(notification_id)
        return notification.in_app_detail