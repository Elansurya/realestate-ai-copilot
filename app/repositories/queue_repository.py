# backend/app/repositories/queue_repository.py
"""Repository for the notification dispatch queue.

Contains only database access operations. No business rules, validation,
or orchestration logic belongs in this module.
"""

import uuid
from datetime import datetime
from typing import Optional, Sequence, Tuple

from sqlalchemy import and_, case, func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.notification import NotificationPriority
from app.models.notification_queue import NotificationQueue, QueueStatus


class QueueRepository:
    """Data access layer for `NotificationQueue` entries.

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

    async def enqueue(self, queue_entry: NotificationQueue) -> NotificationQueue:
        """Insert a new queue entry.

        Args:
            queue_entry: Queue entity to insert.

        Returns:
            The persisted queue entry with generated fields populated.
        """
        self.session.add(queue_entry)
        await self.session.flush()
        await self.session.refresh(queue_entry)
        return queue_entry

    async def bulk_enqueue(
        self, queue_entries: Sequence[NotificationQueue]
    ) -> Sequence[NotificationQueue]:
        """Insert multiple queue entries in a single operation.

        Args:
            queue_entries: Queue entities to insert.

        Returns:
            The persisted queue entries.
        """
        self.session.add_all(queue_entries)
        await self.session.flush()
        return queue_entries

    async def get_by_id(self, queue_id: uuid.UUID) -> Optional[NotificationQueue]:
        """Fetch a single queue entry by primary key.

        Args:
            queue_id: UUID of the queue entry.

        Returns:
            The matching queue entry, or None if not found.
        """
        stmt = select(NotificationQueue).where(NotificationQueue.id == queue_id)
        result = await self.session.execute(stmt)
        return result.scalar_one_or_none()

    async def get_by_notification_id(
        self, notification_id: uuid.UUID
    ) -> Optional[NotificationQueue]:
        """Fetch the queue entry associated with a notification.

        Args:
            notification_id: UUID of the parent notification.

        Returns:
            The matching queue entry, or None if not found.
        """
        stmt = select(NotificationQueue).where(
            NotificationQueue.notification_id == notification_id
        )
        result = await self.session.execute(stmt)
        return result.scalar_one_or_none()

    async def fetch_next_batch(
        self,
        worker_id: str,
        locked_at: datetime,
        batch_size: int = 50,
        as_of: Optional[datetime] = None,
    ) -> Sequence[NotificationQueue]:
        """Atomically claim the next batch of dispatchable queue entries.

        Selects waiting entries whose scheduled time has elapsed, ordered
        by priority then scheduling time, locking rows with
        `FOR UPDATE SKIP LOCKED` and marking them as processing under the
        given worker identifier.

        Args:
            worker_id: Identifier of the worker claiming the batch.
            locked_at: Timestamp to record as the lock acquisition time.
            batch_size: Maximum number of entries to claim.
            as_of: Reference timestamp used to evaluate `scheduled_at`.
                Defaults to the database's current timestamp when omitted.

        Returns:
            The queue entries claimed for processing by this worker.
        """
        reference_time = as_of or func.now()
        # NOTE: previously built via
        # `func.array_position(list(priority_order.keys()), NotificationQueue.priority)`,
        # passing raw `NotificationPriority` enum members as an untyped
        # array literal parameter. Without the column's own enum bind
        # processor involved, psycopg serialized each member using its
        # `str(str_enum_member)` value (e.g. "urgent", lowercase) instead
        # of the `.name` label the native `notification_priority_enum`
        # type actually contains (e.g. "URGENT"), so Postgres rejected
        # every call with `invalid input value for enum
        # notification_priority_enum`. A `CASE` expression compares each
        # branch directly against the properly-typed `priority` column
        # instead, so normal enum bind-parameter handling applies.
        priority_rank = case(
            (NotificationQueue.priority == NotificationPriority.URGENT, 0),
            (NotificationQueue.priority == NotificationPriority.HIGH, 1),
            (NotificationQueue.priority == NotificationPriority.NORMAL, 2),
            (NotificationQueue.priority == NotificationPriority.LOW, 3),
            else_=4,
        )
        candidate_stmt = (
            select(NotificationQueue.id)
            .where(
                NotificationQueue.status == QueueStatus.WAITING,
                and_(
                    NotificationQueue.scheduled_at.is_(None)
                    | (NotificationQueue.scheduled_at <= reference_time)
                ),
            )
            .order_by(
                priority_rank,
                NotificationQueue.created_at.asc(),
            )
            .limit(batch_size)
            .with_for_update(skip_locked=True)
        )
        candidate_result = await self.session.execute(candidate_stmt)
        candidate_ids = candidate_result.scalars().all()

        if not candidate_ids:
            return []

        claim_stmt = (
            update(NotificationQueue)
            .where(NotificationQueue.id.in_(candidate_ids))
            .values(
                status=QueueStatus.PROCESSING,
                locked_at=locked_at,
                locked_by=worker_id,
            )
            .returning(NotificationQueue)
        )
        claim_result = await self.session.execute(claim_stmt)
        await self.session.flush()
        return claim_result.scalars().all()

    async def mark_completed(self, queue_id: uuid.UUID) -> Optional[NotificationQueue]:
        """Mark a queue entry as completed.

        Args:
            queue_id: UUID of the queue entry.

        Returns:
            The updated queue entry, or None if not found.
        """
        stmt = (
            update(NotificationQueue)
            .where(NotificationQueue.id == queue_id)
            .values(status=QueueStatus.COMPLETED, locked_at=None, locked_by=None)
            .returning(NotificationQueue)
        )
        result = await self.session.execute(stmt)
        await self.session.flush()
        return result.scalar_one_or_none()

    async def mark_failed(
        self, queue_id: uuid.UUID, error_message: str
    ) -> Optional[NotificationQueue]:
        """Mark a queue entry as failed and record the error.

        Args:
            queue_id: UUID of the queue entry.
            error_message: Error detail describing the failure.

        Returns:
            The updated queue entry, or None if not found.
        """
        stmt = (
            update(NotificationQueue)
            .where(NotificationQueue.id == queue_id)
            .values(
                status=QueueStatus.FAILED,
                last_error=error_message,
                locked_at=None,
                locked_by=None,
            )
            .returning(NotificationQueue)
        )
        result = await self.session.execute(stmt)
        await self.session.flush()
        return result.scalar_one_or_none()

    async def schedule_retry(
        self, queue_id: uuid.UUID, next_retry_at: datetime
    ) -> Optional[NotificationQueue]:
        """Increment the retry counter and reschedule a queue entry.

        Args:
            queue_id: UUID of the queue entry.
            next_retry_at: Timestamp of the next scheduled retry attempt.

        Returns:
            The updated queue entry, or None if not found.
        """
        stmt = (
            update(NotificationQueue)
            .where(NotificationQueue.id == queue_id)
            .values(
                status=QueueStatus.WAITING,
                retry_count=NotificationQueue.retry_count + 1,
                next_retry_at=next_retry_at,
                scheduled_at=next_retry_at,
                locked_at=None,
                locked_by=None,
            )
            .returning(NotificationQueue)
        )
        result = await self.session.execute(stmt)
        await self.session.flush()
        return result.scalar_one_or_none()

    async def release_stale_locks(
        self, stale_before: datetime
    ) -> int:
        """Release processing locks held past a staleness threshold.

        Args:
            stale_before: Entries locked before this timestamp are
                considered stale and returned to the waiting state.

        Returns:
            Number of rows released.
        """
        stmt = (
            update(NotificationQueue)
            .where(
                NotificationQueue.status == QueueStatus.PROCESSING,
                NotificationQueue.locked_at < stale_before,
            )
            .values(status=QueueStatus.WAITING, locked_at=None, locked_by=None)
        )
        result = await self.session.execute(stmt)
        await self.session.flush()
        return result.rowcount or 0

    async def list_queue_entries(
        self,
        status: Optional[QueueStatus] = None,
        priority: Optional[NotificationPriority] = None,
        sort_by: str = "created_at",
        sort_desc: bool = True,
        page: int = 1,
        page_size: int = 20,
    ) -> Tuple[Sequence[NotificationQueue], int]:
        """Fetch a filtered, sorted, paginated list of queue entries.

        Args:
            status: Filter by processing status.
            priority: Filter by dispatch priority.
            sort_by: Column name to sort by. Must be an attribute of
                `NotificationQueue`.
            sort_desc: Whether to sort in descending order.
            page: 1-indexed page number.
            page_size: Number of records per page.

        Returns:
            A tuple of (matching entries for the page, total matching
            count across all pages).
        """
        conditions = []
        if status is not None:
            conditions.append(NotificationQueue.status == status)
        if priority is not None:
            conditions.append(NotificationQueue.priority == priority)

        count_stmt = select(func.count(NotificationQueue.id)).where(and_(*conditions))
        total_result = await self.session.execute(count_stmt)
        total = total_result.scalar_one()

        sort_column = getattr(NotificationQueue, sort_by, NotificationQueue.created_at)
        order_clause = sort_column.desc() if sort_desc else sort_column.asc()

        list_stmt = (
            select(NotificationQueue)
            .where(and_(*conditions))
            .order_by(order_clause)
            .offset((page - 1) * page_size)
            .limit(page_size)
        )
        list_result = await self.session.execute(list_stmt)
        return list_result.scalars().all(), total