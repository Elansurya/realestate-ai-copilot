# backend/app/models/sms_notification.py
"""SMS notification channel detail model."""

import enum
import uuid
from typing import Optional, TYPE_CHECKING

from sqlalchemy import CheckConstraint, ForeignKey, Index, Integer, Numeric, String, Text
from sqlalchemy import Enum as SAEnum
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base
from app.models.notification import TimestampMixin

if TYPE_CHECKING:
    from app.models.notification import Notification


class SMSProvider(str, enum.Enum):
    """Third-party provider used to dispatch an SMS notification."""

    TWILIO = "twilio"
    MSG91 = "msg91"
    NEXMO = "nexmo"
    PLIVO = "plivo"


class SMSDeliveryStatus(str, enum.Enum):
    """Provider-reported delivery status of an SMS notification."""

    QUEUED = "queued"
    SENT = "sent"
    DELIVERED = "delivered"
    UNDELIVERED = "undelivered"
    FAILED = "failed"


class SMSNotification(Base, TimestampMixin):
    """Channel-specific payload and delivery metadata for an SMS notification.

    Attributes:
        id: Primary key UUID.
        notification_id: FK to the parent notification (one-to-one).
        from_number: Sender phone number in E.164 format.
        to_number: Recipient phone number in E.164 format.
        message_body: Rendered SMS text content.
        provider: SMS delivery provider used.
        provider_message_id: Provider-assigned message identifier.
        delivery_status: Provider-reported delivery status.
        segments_count: Number of SMS segments the message was split into.
        cost: Provider-reported delivery cost.
    """

    __tablename__ = "sms_notifications"
    __table_args__ = (
        CheckConstraint(
            "length(message_body) <= 1600", name="ck_sms_notifications_message_body_max_length"
        ),
        CheckConstraint(
            "segments_count > 0", name="ck_sms_notifications_segments_count_positive"
        ),
        Index(
            "ix_sms_notifications_provider_message_id", "provider", "provider_message_id"
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
    from_number: Mapped[str] = mapped_column(String(20), nullable=False)
    to_number: Mapped[str] = mapped_column(String(20), nullable=False)
    message_body: Mapped[str] = mapped_column(Text, nullable=False)
    provider: Mapped[SMSProvider] = mapped_column(
        SAEnum(SMSProvider, name="sms_provider_enum"), nullable=False
    )
    provider_message_id: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    delivery_status: Mapped[SMSDeliveryStatus] = mapped_column(
        SAEnum(SMSDeliveryStatus, name="sms_delivery_status_enum"),
        default=SMSDeliveryStatus.QUEUED,
        nullable=False,
    )
    segments_count: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    cost: Mapped[Optional[float]] = mapped_column(Numeric(10, 4), nullable=True)

    notification: Mapped["Notification"] = relationship(back_populates="sms_detail")