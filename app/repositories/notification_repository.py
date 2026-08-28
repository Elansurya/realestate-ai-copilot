# backend/app/repositories/notification_repository.py
"""Repository for the core Notification aggregate.

Contains only database access operations. No business rules, validation,
or orchestration logic belongs in this module.

Fix Note (2026-08-20):
    - `NotificationService.list_notifications()` has always accepted and
      forwarded `date_from` and `date_to` as creation-timestamp bounds
      (mirroring the `date_from`/`date_to` query params on
      `GET /api/v1/notifications` in the router), but
      `NotificationRepository.list_notifications()` never declared
      matching parameters. Any call to `GET /api/v1/notifications`
      therefore raised `TypeError: NotificationRepository.
      list_notifications() got an unexpected keyword argument
      'date_from'` inside the service call, surfacing as an uncaught
      HTTP 500. `date_from` and `date_to` are now accepted and applied
      as inclusive lower/upper bounds against `Notification.created_at`,
      restoring signature parity between the service and repository
      layers.
"""

import uuid
from datetime import datetime, timezone
from typing import List, Optional, Sequence, Tuple

from sqlalchemy import Select, and_, delete, func, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.models.notification import (
    Notification,
    NotificationChannel,
    NotificationCategory,
    NotificationPriority,
    NotificationStatus,
)


class NotificationRepository:
    """Data access layer for `Notification` entities.

    Attributes:
        session: Active async SQLAlchemy session bound to the current
            unit of work.
    """

    def __init__(self, session: AsyncSession) -> None:
        """Initialize the repository.

        Args:
            session: Async SQLAlchemy session used for all operations.
        """
        self.session = session

    async def create(self, notification: Notification) -> Notification:
        """Persist a new notification.

        Args:
            notification: Notification entity to insert.

        Returns:
            The persisted notification with generated fields populated.
        """
        self.session.add(notification)
        await self.session.flush()
        await self.session.refresh(notification)
        return notification

    async def bulk_create(
        self, notifications: Sequence[Notification]
    ) -> Sequence[Notification]:
        """Persist multiple notifications in a single operation.

        Args:
            notifications: Notification entities to insert.

        Returns:
            The persisted notifications.
        """
        self.session.add_all(notifications)
        await self.session.flush()
        return notifications

    async def get_by_id(
        self, notification_id: uuid.UUID, include_relations: bool = False
    ) -> Optional[Notification]:
        """Fetch a single notification by primary key.

        Args:
            notification_id: UUID of the notification.
            include_relations: Whether to eagerly load channel detail,
                template, queue, and log relationships.

        Returns:
            The matching notification, or None if not found or soft deleted.
        """
        stmt: Select = select(Notification).where(
            Notification.id == notification_id,
            Notification.is_deleted.is_(False),
        )
        if include_relations:
            stmt = stmt.options(
                selectinload(Notification.template),
                selectinload(Notification.logs),
                selectinload(Notification.queue_entry),
                selectinload(Notification.email_detail),
                selectinload(Notification.sms_detail),
                selectinload(Notification.whatsapp_detail),
                selectinload(Notification.push_detail),
                selectinload(Notification.in_app_detail),
            )
        result = await self.session.execute(stmt)
        return result.scalar_one_or_none()

    async def list_notifications(
        self,
        recipient_id: Optional[uuid.UUID] = None,
        channel: Optional[NotificationChannel] = None,
        category: Optional[NotificationCategory] = None,
        priority: Optional[NotificationPriority] = None,
        status: Optional[NotificationStatus] = None,
        is_read: Optional[bool] = None,
        search_term: Optional[str] = None,
        date_from: Optional[datetime] = None,
        date_to: Optional[datetime] = None,
        sort_by: str = "created_at",
        sort_desc: bool = True,
        page: int = 1,
        page_size: int = 20,
        include_deleted: bool = False,
    ) -> Tuple[Sequence[Notification], int]:
        """Fetch a filtered, sorted, paginated list of notifications.

        Args:
            recipient_id: Filter by recipient UUID.
            channel: Filter by delivery channel.
            category: Filter by business category.
            priority: Filter by delivery priority.
            status: Filter by lifecycle status.
            is_read: Filter by read state.
            search_term: Free text match against subject and body.
            date_from: Inclusive lower bound on `created_at`.
            date_to: Inclusive upper bound on `created_at`.
            sort_by: Column name to sort by. Must be an attribute of
                `Notification`.
            sort_desc: Whether to sort in descending order.
            page: 1-indexed page number.
            page_size: Number of records per page.
            include_deleted: When True, soft-deleted notifications are
                included in the results instead of being filtered out.

        Returns:
            A tuple of (matching notifications for the page, total matching
            count across all pages).
        """
        conditions = [] if include_deleted else [Notification.is_deleted.is_(False)]
        if recipient_id is not None:
            conditions.append(Notification.recipient_id == recipient_id)
        if channel is not None:
            conditions.append(Notification.channel == channel)
        if category is not None:
            conditions.append(Notification.category == category)
        if priority is not None:
            conditions.append(Notification.priority == priority)
        if status is not None:
            conditions.append(Notification.status == status)
        if is_read is not None:
            conditions.append(Notification.is_read.is_(is_read))
        if date_from is not None:
            conditions.append(Notification.created_at >= date_from)
        if date_to is not None:
            conditions.append(Notification.created_at <= date_to)
        if search_term:
            like_pattern = f"%{search_term}%"
            conditions.append(
                or_(
                    Notification.subject.ilike(like_pattern),
                    Notification.body.ilike(like_pattern),
                )
            )

        count_stmt = select(func.count(Notification.id)).where(and_(True, *conditions))
        total_result = await self.session.execute(count_stmt)
        total = total_result.scalar_one()

        sort_column = getattr(Notification, sort_by, Notification.created_at)
        order_clause = sort_column.desc() if sort_desc else sort_column.asc()

        list_stmt = (
            select(Notification)
            .where(and_(True, *conditions))
            .order_by(order_clause)
            .offset((page - 1) * page_size)
            .limit(page_size)
        )
        list_result = await self.session.execute(list_stmt)
        return list_result.scalars().all(), total

    async def update_fields(
        self, notification_id: uuid.UUID, values: dict
    ) -> Optional[Notification]:
        """Update arbitrary column values on a notification.

        Args:
            notification_id: UUID of the notification to update.
            values: Mapping of column name to new value.

        Returns:
            The updated notification, or None if not found.
        """
        stmt = (
            update(Notification)
            .where(Notification.id == notification_id, Notification.is_deleted.is_(False))
            .values(**values)
            .returning(Notification)
        )
        result = await self.session.execute(stmt)
        await self.session.flush()
        return result.scalar_one_or_none()

    async def bulk_update_status(
        self, notification_ids: Sequence[uuid.UUID], status: NotificationStatus
    ) -> int:
        """Update the status of multiple notifications at once.

        Args:
            notification_ids: UUIDs of the notifications to update.
            status: New lifecycle status to apply.

        Returns:
            Number of rows affected.
        """
        stmt = (
            update(Notification)
            .where(
                Notification.id.in_(notification_ids),
                Notification.is_deleted.is_(False),
            )
            .values(status=status)
        )
        result = await self.session.execute(stmt)
        await self.session.flush()
        return result.rowcount or 0

    async def update_delivery_status(
        self,
        notification_id: uuid.UUID,
        status: NotificationStatus,
        sent_at: Optional[datetime] = None,
        delivered_at: Optional[datetime] = None,
        failure_reason: Optional[str] = None,
    ) -> Optional[Notification]:
        """Update the delivery status and related timestamps of a notification.

        Args:
            notification_id: UUID of the notification.
            status: New lifecycle status.
            sent_at: Timestamp the notification was dispatched.
            delivered_at: Timestamp delivery was confirmed.
            failure_reason: Failure detail if the status indicates failure.

        Returns:
            The updated notification, or None if not found.
        """
        values = {"status": status}
        if sent_at is not None:
            values["sent_at"] = sent_at
        if delivered_at is not None:
            values["delivered_at"] = delivered_at
        if failure_reason is not None:
            values["failure_reason"] = failure_reason
        return await self.update_fields(notification_id, values)

    async def mark_as_read(
        self, notification_id: uuid.UUID, read_at: datetime
    ) -> Optional[Notification]:
        """Mark a notification as read.

        Args:
            notification_id: UUID of the notification.
            read_at: Timestamp the notification was read.

        Returns:
            The updated notification, or None if not found.
        """
        return await self.update_fields(
            notification_id,
            {"is_read": True, "read_at": read_at, "status": NotificationStatus.READ},
        )

    async def increment_retry_count(
        self, notification_id: uuid.UUID
    ) -> Optional[Notification]:
        """Increment the retry counter of a notification by one.

        Args:
            notification_id: UUID of the notification.

        Returns:
            The updated notification, or None if not found.
        """
        stmt = (
            update(Notification)
            .where(Notification.id == notification_id, Notification.is_deleted.is_(False))
            .values(retry_count=Notification.retry_count + 1)
            .returning(Notification)
        )
        result = await self.session.execute(stmt)
        await self.session.flush()
        return result.scalar_one_or_none()

    async def soft_delete(self, notification_id: uuid.UUID, deleted_at: datetime) -> bool:
        """Soft delete a notification.

        Args:
            notification_id: UUID of the notification to delete.
            deleted_at: Timestamp of the deletion.

        Returns:
            True if a row was updated, False otherwise.
        """
        stmt = (
            update(Notification)
            .where(Notification.id == notification_id, Notification.is_deleted.is_(False))
            .values(is_deleted=True, deleted_at=deleted_at)
        )
        result = await self.session.execute(stmt)
        await self.session.flush()
        return (result.rowcount or 0) > 0

    async def restore(self, notification_id: uuid.UUID) -> Optional[Notification]:
        """Reverse a soft delete, making the notification visible again.

        Args:
            notification_id: UUID of the notification to restore.

        Returns:
            The restored notification, or None if no soft-deleted
            notification with that id exists.
        """
        stmt = (
            update(Notification)
            .where(Notification.id == notification_id, Notification.is_deleted.is_(True))
            .values(is_deleted=False, deleted_at=None)
            .returning(Notification)
        )
        result = await self.session.execute(stmt)
        await self.session.flush()
        return result.scalar_one_or_none()

    async def hard_delete(self, notification_id: uuid.UUID) -> bool:
        """Permanently remove a notification row from the database.

        Unlike `soft_delete`, this is irreversible and physically deletes
        the row (e.g. for data-erasure requests). Prefer `soft_delete` for
        normal lifecycle deletion.

        Args:
            notification_id: UUID of the notification to delete.

        Returns:
            True if a row was deleted, False otherwise.
        """
        stmt = delete(Notification).where(Notification.id == notification_id)
        result = await self.session.execute(stmt)
        await self.session.flush()
        return (result.rowcount or 0) > 0

    async def get_unread_count(self, recipient_id: uuid.UUID) -> int:
        """Count unread notifications for a recipient.

        Args:
            recipient_id: UUID of the recipient.

        Returns:
            Number of unread, non-deleted notifications.
        """
        stmt = select(func.count(Notification.id)).where(
            Notification.recipient_id == recipient_id,
            Notification.is_read.is_(False),
            Notification.is_deleted.is_(False),
        )
        result = await self.session.execute(stmt)
        return result.scalar_one()

    async def mark_failed(
        self, notification_id: uuid.UUID, error_message: str
    ) -> Optional[Notification]:
        """Record a failed delivery attempt.

        Atomically increments `retry_count`, sets `status` to `FAILED`,
        and records `failure_reason` in a single update.

        Args:
            notification_id: UUID of the notification.
            error_message: Description of the failure.

        Returns:
            The updated notification, or None if not found.
        """
        stmt = (
            update(Notification)
            .where(Notification.id == notification_id, Notification.is_deleted.is_(False))
            .values(
                status=NotificationStatus.FAILED,
                retry_count=Notification.retry_count + 1,
                failure_reason=error_message,
            )
            .returning(Notification)
        )
        result = await self.session.execute(stmt)
        await self.session.flush()
        return result.scalar_one_or_none()

    async def mark_sent(self, notification_id: uuid.UUID) -> Optional[Notification]:
        """Mark a notification as sent, recording the current timestamp.

        Args:
            notification_id: UUID of the notification.

        Returns:
            The updated notification, or None if not found.
        """
        return await self.update_delivery_status(
            notification_id,
            status=NotificationStatus.SENT,
            sent_at=datetime.now(timezone.utc),
        )

    async def mark_delivered(self, notification_id: uuid.UUID) -> Optional[Notification]:
        """Mark a notification as delivered, recording the current timestamp.

        Args:
            notification_id: UUID of the notification.

        Returns:
            The updated notification, or None if not found.
        """
        return await self.update_delivery_status(
            notification_id,
            status=NotificationStatus.DELIVERED,
            delivered_at=datetime.now(timezone.utc),
        )

    async def get_retryable(self, max_retries: int) -> Sequence[Notification]:
        """Fetch failed notifications that have not exhausted their retries.

        Args:
            max_retries: Retry-count ceiling; notifications at or above
                this count are excluded.

        Returns:
            Failed, non-deleted notifications with `retry_count` below
            `max_retries`.
        """
        stmt = select(Notification).where(
            Notification.status == NotificationStatus.FAILED,
            Notification.retry_count < max_retries,
            Notification.is_deleted.is_(False),
        )
        result = await self.session.execute(stmt)
        return result.scalars().all()

    async def reset_retry(self, notification_id: uuid.UUID) -> Optional[Notification]:
        """Reset a notification's retry counter for a fresh manual retry.

        Args:
            notification_id: UUID of the notification.

        Returns:
            The updated notification, or None if not found.
        """
        return await self.update_fields(
            notification_id,
            {"retry_count": 0, "status": NotificationStatus.PENDING, "failure_reason": None},
        )

    async def mark_all_as_read(self, recipient_id: uuid.UUID) -> int:
        """Mark every unread notification for a recipient as read.

        Args:
            recipient_id: UUID of the recipient.

        Returns:
            Number of rows updated.
        """
        stmt = (
            update(Notification)
            .where(
                Notification.recipient_id == recipient_id,
                Notification.is_read.is_(False),
                Notification.is_deleted.is_(False),
            )
            .values(
                is_read=True,
                read_at=datetime.now(timezone.utc),
                status=NotificationStatus.READ,
            )
        )
        result = await self.session.execute(stmt)
        await self.session.flush()
        return result.rowcount or 0

    async def get_delivery_stats(self) -> dict:
        """Aggregate delivery outcome counts across all notifications.

        Returns:
            A dict with `total`, `delivered`, `failed`, and `pending`
            counts across all non-deleted notifications.
        """
        stmt = select(Notification.status, func.count(Notification.id)).where(
            Notification.is_deleted.is_(False)
        ).group_by(Notification.status)
        result = await self.session.execute(stmt)
        counts = {status: count for status, count in result.all()}

        total = sum(counts.values())
        delivered = counts.get(NotificationStatus.DELIVERED, 0)
        failed = counts.get(NotificationStatus.FAILED, 0)
        pending = total - delivered - failed

        return {
            "total": total,
            "delivered": delivered,
            "failed": failed,
            "pending": pending,
        }

    async def get_statistics(
        self,
        date_from: Optional[datetime] = None,
        date_to: Optional[datetime] = None,
        channel: Optional[NotificationChannel] = None,
    ) -> dict:
        """Return aggregate notification counts for an optional time/channel filter."""
        conditions = [Notification.is_deleted.is_(False)]
        if date_from is not None:
            conditions.append(Notification.created_at >= date_from)
        if date_to is not None:
            conditions.append(Notification.created_at <= date_to)
        if channel is not None:
            conditions.append(Notification.channel == channel)

        total_stmt = select(func.count(Notification.id)).where(and_(*conditions))
        status_stmt = (
            select(Notification.status, func.count(Notification.id))
            .where(and_(*conditions))
            .group_by(Notification.status)
        )
        channel_stmt = (
            select(Notification.channel, func.count(Notification.id))
            .where(and_(*conditions))
            .group_by(Notification.channel)
        )

        total = int((await self.session.execute(total_stmt)).scalar_one())
        status_rows = (await self.session.execute(status_stmt)).all()
        channel_rows = (await self.session.execute(channel_stmt)).all()

        by_status = {getattr(k, "name", str(k)): int(v) for k, v in status_rows}
        by_channel = {getattr(k, "name", str(k)): int(v) for k, v in channel_rows}
        delivered = by_status.get(NotificationStatus.DELIVERED.name, 0)
        failed = by_status.get(NotificationStatus.FAILED.name, 0)
        pending = sum(
            count
            for name, count in by_status.items()
            if name not in {NotificationStatus.DELIVERED.name, NotificationStatus.FAILED.name}
        )
        return {
            "total": total,
            "delivered": delivered,
            "failed": failed,
            "pending": pending,
            "by_status": by_status,
            "by_channel": by_channel,
        }