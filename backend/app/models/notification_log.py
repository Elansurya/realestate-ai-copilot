# backend/app/models/notification_log.py
"""Notification audit log model.

Stores an immutable, append-only history of lifecycle events for each
notification, used for auditing, debugging, and retry analysis.
"""

import enum
import uuid
from datetime import datetime
from typing import Optional, TYPE_CHECKING

from sqlalchemy import DateTime, ForeignKey, Index, Integer, Text, func
from sqlalchemy import Enum as SAEnum
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base
from app.models.notification import NotificationStatus

if TYPE_CHECKING:
    from app.models.notification import Notification


class NotificationEventType(str, enum.Enum):
    """Type of lifecycle event recorded in a notification log entry."""

    CREATED = "created"
    QUEUED = "queued"
    DISPATCHED = "dispatched"
    SENT = "sent"
    DELIVERED = "delivered"
    FAILED = "failed"
    RETRIED = "retried"
    READ = "read"
    CANCELLED = "cancelled"


class NotificationLog(Base):
    """Immutable audit log entry for a single notification lifecycle event.

    Attributes:
        id: Primary key UUID.
        notification_id: FK to the parent notification.
        event_type: Type of lifecycle event being recorded.
        status: Notification status at the time of this event.
        attempt_number: Delivery attempt number associated with this event.
        provider_response: Raw provider response payload, if applicable.
        error_message: Error detail captured for failed events.
        occurred_at: Timestamp the event actually occurred.
        created_at: Timestamp the log row was persisted.
    """

    __tablename__ = "notification_logs"
    __table_args__ = (
        Index(
            "ix_notification_logs_notification_occurred",
            "notification_id",
            "occurred_at",
        ),
        Index("ix_notification_logs_event_status", "event_type", "status"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    notification_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("notifications.id", ondelete="CASCADE"),
        nullable=False,
    )
    event_type: Mapped[NotificationEventType] = mapped_column(
        SAEnum(NotificationEventType, name="notification_event_type_enum"),
        nullable=False,
    )
    status: Mapped[NotificationStatus] = mapped_column(
        SAEnum(NotificationStatus, name="notification_status_enum"), nullable=False
    )
    attempt_number: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    provider_response: Mapped[Optional[dict]] = mapped_column(JSONB, nullable=True)
    error_message: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    occurred_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    notification: Mapped["Notification"] = relationship(back_populates="logs")