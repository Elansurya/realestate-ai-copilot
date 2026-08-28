# backend/app/models/notification_queue.py
"""Notification queue model.

Controls dispatch scheduling, worker locking, priority ordering, and
retry bookkeeping for notifications awaiting delivery.
"""

import enum
import uuid
from datetime import datetime
from typing import Optional, TYPE_CHECKING

from sqlalchemy import CheckConstraint, DateTime, ForeignKey, Index, Integer, String, Text
from sqlalchemy import Enum as SAEnum
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship, synonym

from app.db.base import Base
from app.models.notification import NotificationChannel, NotificationPriority, TimestampMixin

if TYPE_CHECKING:
    from app.models.notification import Notification


class QueueStatus(str, enum.Enum):
    """Processing status used by the ORM-facing queue API.

    The live migration uses a separate ``queue_status`` column. The ORM
    exposes ``status`` for backwards compatibility.
    """

    WAITING = "waiting"
    PROCESSING = "processing"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


class NotificationQueue(Base, TimestampMixin):
    """Queue entry controlling dispatch scheduling and retry of a notification.

    Attributes:
        id: Primary key UUID.
        notification_id: FK to the associated notification (one-to-one).
        status: Current queue processing status.
        priority: Dispatch priority, mirrors the parent notification.
        scheduled_at: Earliest timestamp the entry may be dispatched.
        locked_at: Timestamp a worker acquired a processing lock.
        locked_by: Identifier of the worker holding the lock.
        retry_count: Number of processing attempts made so far.
        max_retries: Maximum number of allowed processing attempts.
        next_retry_at: Timestamp of the next scheduled retry attempt.
        last_error: Last error message recorded during processing.
    """

    # NOTE: matches the table actually created by
    # alembic/versions/notification_module.py (singular "notification_queue"),
    # which is the live/migrated schema. This model previously declared the
    # plural "notification_queues", a name that was never created by any
    # migration -- under Base.metadata.create_all() that silently produced a
    # second, empty, un-migrated table, while normal (migration-driven)
    # database setups would fail with "relation does not exist" the moment
    # this model was used at all. Fixing the name here (not adding a new
    # migration) because the existing migration/table is correct and
    # untouched; no schema or data change is needed, only pointing the ORM
    # model at the table that has always actually existed.
    __tablename__ = "notification_queue"
    __table_args__ = (
        CheckConstraint(
            "retry_count >= 0", name="ck_notification_queues_retry_count_non_negative"
        ),
        CheckConstraint(
            "max_retries >= 0", name="ck_notification_queues_max_retries_non_negative"
        ),
        Index(
            "ix_notification_queues_status_priority_scheduled",
            "status",
            "priority",
            "scheduled_at",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    notification_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("notifications.id", ondelete="CASCADE"),
        unique=True,
        nullable=False,
    )
    # Legacy schema keeps a denormalized channel column. It is optional in
    # the ORM because the authoritative channel lives on notifications.channel.
    channel: Mapped[Optional[NotificationChannel]] = mapped_column(
        SAEnum(NotificationChannel, name="notification_channel_enum"),
        nullable=True,
    )
    status: Mapped[QueueStatus] = mapped_column(
        "status",
        String(30),
        default=QueueStatus.WAITING.value,
        nullable=False,
    )
    priority: Mapped[NotificationPriority] = mapped_column(
        SAEnum(NotificationPriority, name="notification_priority_enum"),
        default=NotificationPriority.NORMAL,
        nullable=False,
    )
    scheduled_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    locked_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    locked_by: Mapped[Optional[str]] = mapped_column(String(100), nullable=True)
    retry_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    max_retries: Mapped[int] = mapped_column(Integer, default=3, nullable=False)
    next_retry_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    last_error: Mapped[Optional[str]] = mapped_column(Text, nullable=True)

    notification: Mapped["Notification"] = relationship(back_populates="queue_entry")