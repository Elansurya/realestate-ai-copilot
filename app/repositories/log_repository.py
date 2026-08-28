# backend/app/repositories/log_repository.py
"""Repository for notification audit logs.

Contains only database access operations. No business rules, validation,
or orchestration logic belongs in this module.
"""

import uuid
from typing import Optional, Sequence, Tuple

from sqlalchemy import and_, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.notification import NotificationStatus
from app.models.notification_log import NotificationEventType, NotificationLog


class LogRepository:
    """Data access layer for `NotificationLog` entries.

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

    async def create_log(self, log_entry: NotificationLog) -> NotificationLog:
        """Persist a single audit log entry.

        Args:
            log_entry: Log entity to insert.

        Returns:
            The persisted log entry with generated fields populated.
        """
        self.session.add(log_entry)
        await self.session.flush()
        await self.session.refresh(log_entry)
        return log_entry

    async def bulk_create_logs(
        self, log_entries: Sequence[NotificationLog]
    ) -> Sequence[NotificationLog]:
        """Persist multiple audit log entries in a single operation.

        Args:
            log_entries: Log entities to insert.

        Returns:
            The persisted log entries.
        """
        self.session.add_all(log_entries)
        await self.session.flush()
        return log_entries

    async def get_by_id(self, log_id: uuid.UUID) -> Optional[NotificationLog]:
        """Fetch a single log entry by primary key.

        Args:
            log_id: UUID of the log entry.

        Returns:
            The matching log entry, or None if not found.
        """
        stmt = select(NotificationLog).where(NotificationLog.id == log_id)
        result = await self.session.execute(stmt)
        return result.scalar_one_or_none()

    async def get_latest_for_notification(
        self, notification_id: uuid.UUID
    ) -> Optional[NotificationLog]:
        """Fetch the most recent log entry for a notification.

        Args:
            notification_id: UUID of the parent notification.

        Returns:
            The latest log entry ordered by occurrence time, or None if
            no logs exist.
        """
        stmt = (
            select(NotificationLog)
            .where(NotificationLog.notification_id == notification_id)
            .order_by(NotificationLog.occurred_at.desc())
            .limit(1)
        )
        result = await self.session.execute(stmt)
        return result.scalar_one_or_none()

    async def list_logs_for_notification(
        self,
        notification_id: uuid.UUID,
        event_type: Optional[NotificationEventType] = None,
        status: Optional[NotificationStatus] = None,
        sort_desc: bool = True,
        page: int = 1,
        page_size: int = 20,
    ) -> Tuple[Sequence[NotificationLog], int]:
        """Fetch a filtered, sorted, paginated list of logs for a notification.

        Args:
            notification_id: UUID of the parent notification.
            event_type: Filter by lifecycle event type.
            status: Filter by recorded status at the time of the event.
            sort_desc: Whether to sort by occurrence time descending.
            page: 1-indexed page number.
            page_size: Number of records per page.

        Returns:
            A tuple of (matching log entries for the page, total matching
            count across all pages).
        """
        conditions = [NotificationLog.notification_id == notification_id]
        if event_type is not None:
            conditions.append(NotificationLog.event_type == event_type)
        if status is not None:
            conditions.append(NotificationLog.status == status)

        count_stmt = select(func.count(NotificationLog.id)).where(and_(*conditions))
        total_result = await self.session.execute(count_stmt)
        total = total_result.scalar_one()

        order_clause = (
            NotificationLog.occurred_at.desc()
            if sort_desc
            else NotificationLog.occurred_at.asc()
        )
        list_stmt = (
            select(NotificationLog)
            .where(and_(*conditions))
            .order_by(order_clause)
            .offset((page - 1) * page_size)
            .limit(page_size)
        )
        list_result = await self.session.execute(list_stmt)
        return list_result.scalars().all(), total

    async def list_logs(
        self,
        event_type: Optional[NotificationEventType] = None,
        status: Optional[NotificationStatus] = None,
        sort_desc: bool = True,
        page: int = 1,
        page_size: int = 20,
    ) -> Tuple[Sequence[NotificationLog], int]:
        """Fetch a filtered, sorted, paginated list of logs across all notifications.

        Args:
            event_type: Filter by lifecycle event type.
            status: Filter by recorded status at the time of the event.
            sort_desc: Whether to sort by occurrence time descending.
            page: 1-indexed page number.
            page_size: Number of records per page.

        Returns:
            A tuple of (matching log entries for the page, total matching
            count across all pages).
        """
        conditions = []
        if event_type is not None:
            conditions.append(NotificationLog.event_type == event_type)
        if status is not None:
            conditions.append(NotificationLog.status == status)

        count_stmt = select(func.count(NotificationLog.id)).where(and_(*conditions))
        total_result = await self.session.execute(count_stmt)
        total = total_result.scalar_one()

        order_clause = (
            NotificationLog.occurred_at.desc()
            if sort_desc
            else NotificationLog.occurred_at.asc()
        )
        list_stmt = (
            select(NotificationLog)
            .where(and_(*conditions))
            .order_by(order_clause)
            .offset((page - 1) * page_size)
            .limit(page_size)
        )
        list_result = await self.session.execute(list_stmt)
        return list_result.scalars().all(), total