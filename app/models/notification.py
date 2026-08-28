
"""Core notification domain model.

This module defines the central `Notification` aggregate root along with
the shared enums and mixins (`TimestampMixin`, `SoftDeleteMixin`) that are
reused across every other model in the notification module.
"""

import enum
import uuid
from datetime import datetime
from typing import List, Optional, TYPE_CHECKING

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    func,
)
from sqlalchemy import Enum as SAEnum
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base

if TYPE_CHECKING:
    from app.models.email_notification import EmailNotification
    from app.models.in_app_notification import InAppNotification
    from app.models.notification_log import NotificationLog
    from app.models.notification_queue import NotificationQueue
    from app.models.notification_template import NotificationTemplate
    from app.models.push_notification import PushNotification
    from app.models.sms_notification import SMSNotification
    from app.models.whatsapp_notification import WhatsAppNotification


class NotificationChannel(str, enum.Enum):
    """Supported notification delivery channels."""

    EMAIL = "email"
    SMS = "sms"
    WHATSAPP = "whatsapp"
    PUSH = "push"
    IN_APP = "in_app"


class NotificationPriority(str, enum.Enum):
    """Delivery priority used for queue ordering."""

    LOW = "low"
    NORMAL = "normal"
    HIGH = "high"
    URGENT = "urgent"


class NotificationStatus(str, enum.Enum):
    """Lifecycle status of a notification."""

    PENDING = "pending"
    QUEUED = "queued"
    SENT = "sent"
    DELIVERED = "delivered"
    FAILED = "failed"
    RETRYING = "retrying"
    SCHEDULED = "scheduled"
    CANCELLED = "cancelled"
    READ = "read"


class NotificationCategory(str, enum.Enum):
    """Business domain category of the notification."""

    LEAD = "lead"
    DEAL = "deal"
    TASK = "task"
    PROPERTY = "property"
    PAYMENT = "payment"
    APPOINTMENT = "appointment"
    SYSTEM = "system"
    MARKETING = "marketing"


class TimestampMixin:
    """Mixin providing standard audit timestamp columns."""

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )


class SoftDeleteMixin:
    """Mixin providing soft delete support."""

    is_deleted: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    deleted_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )


class Notification(Base, TimestampMixin, SoftDeleteMixin):
    """Central aggregate root representing a single notification instance.

    Each notification is associated with exactly one delivery channel and
    may optionally reference a rendering template, a queue entry, an
    ordered set of audit logs, and a channel-specific detail record.

    Attributes:
        id: Primary key UUID.
        recipient_id: UUID of the recipient user or contact.
        sender_id: UUID of the user that triggered the notification, if any.
        channel: Delivery channel for this notification.
        category: Business category/domain of the notification.
        priority: Delivery priority.
        status: Current lifecycle status.
        subject: Optional short subject/title line.
        body: Rendered notification body content.
        template_id: Optional FK to the template used to render this notification.
        metadata_payload: Arbitrary structured metadata.
        scheduled_at: Timestamp for deferred delivery, if scheduled.
        sent_at: Timestamp when the notification was dispatched.
        delivered_at: Timestamp when delivery was confirmed by the provider.
        read_at: Timestamp when the recipient read the notification.
        is_read: Whether the recipient has read the notification.
        retry_count: Number of delivery attempts made so far.
        max_retries: Maximum number of allowed delivery attempts.
        failure_reason: Last known failure reason, if any.
    """

    __tablename__ = "notifications"
    __table_args__ = (
        CheckConstraint(
            "retry_count >= 0", name="ck_notifications_retry_count_non_negative"
        ),
        CheckConstraint(
            "max_retries >= 0", name="ck_notifications_max_retries_non_negative"
        ),
        CheckConstraint(
            "retry_count <= max_retries", name="ck_notifications_retry_within_max"
        ),
        Index("ix_notifications_recipient_status", "recipient_id", "status"),
        Index(
            "ix_notifications_channel_status_priority",
            "channel",
            "status",
            "priority",
        ),
        Index("ix_notifications_scheduled_status", "scheduled_at", "status"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    recipient_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), nullable=False, index=True
    )
    sender_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True), nullable=True
    )
    channel: Mapped[NotificationChannel] = mapped_column(
        SAEnum(NotificationChannel, name="notification_channel_enum"),
        nullable=False,
    )
    category: Mapped[NotificationCategory] = mapped_column(
        SAEnum(NotificationCategory, name="notification_category_enum"),
        nullable=False,
    )
    priority: Mapped[NotificationPriority] = mapped_column(
        SAEnum(NotificationPriority, name="notification_priority_enum"),
        default=NotificationPriority.NORMAL,
        nullable=False,
    )
    status: Mapped[NotificationStatus] = mapped_column(
        SAEnum(NotificationStatus, name="notification_status_enum"),
        default=NotificationStatus.PENDING,
        nullable=False,
    )
    subject: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    body: Mapped[str] = mapped_column(Text, nullable=False)
    template_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("notification_templates.id", ondelete="SET NULL"),
        nullable=True,
    )
    metadata_payload: Mapped[Optional[dict]] = mapped_column(JSONB, nullable=True)
    scheduled_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    sent_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    delivered_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    read_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    is_read: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    retry_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    max_retries: Mapped[int] = mapped_column(Integer, default=3, nullable=False)
    failure_reason: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    # Audit actor fields. These columns already exist in the notification
    # migration and are required by NotificationService when creating,
    # updating, and soft-deleting notifications.
    created_by: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True), nullable=True
    )
    updated_by: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True), nullable=True
    )
    deleted_by: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True), nullable=True
    )

    template: Mapped[Optional["NotificationTemplate"]] = relationship(
        back_populates="notifications"
    )
    logs: Mapped[List["NotificationLog"]] = relationship(
        back_populates="notification",
        cascade="all, delete-orphan",
        order_by="NotificationLog.occurred_at",
    )
    queue_entry: Mapped[Optional["NotificationQueue"]] = relationship(
        back_populates="notification", uselist=False, cascade="all, delete-orphan"
    )
    email_detail: Mapped[Optional["EmailNotification"]] = relationship(
        back_populates="notification", uselist=False, cascade="all, delete-orphan"
    )
    sms_detail: Mapped[Optional["SMSNotification"]] = relationship(
        back_populates="notification", uselist=False, cascade="all, delete-orphan"
    )
    whatsapp_detail: Mapped[Optional["WhatsAppNotification"]] = relationship(
        back_populates="notification", uselist=False, cascade="all, delete-orphan"
    )
    push_detail: Mapped[Optional["PushNotification"]] = relationship(
        back_populates="notification", uselist=False, cascade="all, delete-orphan"
    )
    in_app_detail: Mapped[Optional["InAppNotification"]] = relationship(
        back_populates="notification", uselist=False, cascade="all, delete-orphan"
    )


# ---------------------------------------------------------------------------
# Re-exports
# ---------------------------------------------------------------------------
# NotificationLog, NotificationQueue, and NotificationTemplate are defined in
# their own modules (app.models.notification_log, app.models.notification_queue,
# app.models.notification_template respectively) because each has its own
# table/relationships back to `Notification`. They are imported here — after
# `Notification` is fully defined — purely to give consumers a single stable
# import surface (`from app.models.notification import NotificationLog`, etc.)
# without duplicating the class definitions. The import is placed at the
# bottom of the file (rather than at the top) because those modules import
# `Notification` back for their own relationship configuration; importing
# them before `Notification` exists would create a circular import.
from app.models.notification_log import NotificationLog  # noqa: E402
from app.models.notification_queue import NotificationQueue  # noqa: E402
from app.models.notification_template import NotificationTemplate  # noqa: E402

__all__ = [
    "NotificationChannel",
    "NotificationPriority",
    "NotificationStatus",
    "NotificationCategory",
    "TimestampMixin",
    "SoftDeleteMixin",
    "Notification",
    "NotificationLog",
    "NotificationQueue",
    "NotificationTemplate",
]