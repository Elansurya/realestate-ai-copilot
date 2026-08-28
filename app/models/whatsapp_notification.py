# backend/app/models/whatsapp_notification.py
"""WhatsApp notification channel detail model."""

import enum
import uuid
from typing import Optional, TYPE_CHECKING

from sqlalchemy import CheckConstraint, ForeignKey, Index, String, Text
from sqlalchemy import Enum as SAEnum
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base
from app.models.notification import TimestampMixin

if TYPE_CHECKING:
    from app.models.notification import Notification


class WhatsAppProvider(str, enum.Enum):
    """Third-party provider used to dispatch a WhatsApp notification."""

    META_CLOUD_API = "meta_cloud_api"
    TWILIO = "twilio"
    GUPSHUP = "gupshup"


class WhatsAppMessageType(str, enum.Enum):
    """Type of WhatsApp message payload being sent."""

    TEXT = "text"
    TEMPLATE = "template"
    MEDIA = "media"
    INTERACTIVE = "interactive"


class WhatsAppNotification(Base, TimestampMixin):
    """Channel-specific payload and delivery metadata for a WhatsApp notification.

    Attributes:
        id: Primary key UUID.
        notification_id: FK to the parent notification (one-to-one).
        from_number: Sender WhatsApp business number in E.164 format.
        to_number: Recipient WhatsApp number in E.164 format.
        message_type: Type of WhatsApp message payload.
        template_name: Approved template name, required for template messages.
        template_language: Language code of the approved template.
        media_url: Media asset URL, required for media messages.
        provider: WhatsApp delivery provider used.
        provider_message_id: Provider-assigned message identifier.
        whatsapp_message_status: Raw provider-reported message status.
    """

    __tablename__ = "whatsapp_notifications"
    __table_args__ = (
        CheckConstraint("from_number <> ''", name="ck_whatsapp_notifications_from_number_not_empty"),
        CheckConstraint("to_number <> ''", name="ck_whatsapp_notifications_to_number_not_empty"),
        Index(
            "ix_whatsapp_notifications_provider_message_id", "provider", "provider_message_id"
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
    message_type: Mapped[WhatsAppMessageType] = mapped_column(
        SAEnum(WhatsAppMessageType, name="whatsapp_message_type_enum"), nullable=False
    )
    template_name: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    template_language: Mapped[Optional[str]] = mapped_column(String(20), nullable=True)
    media_url: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    provider: Mapped[WhatsAppProvider] = mapped_column(
        SAEnum(WhatsAppProvider, name="whatsapp_provider_enum"), nullable=False
    )
    provider_message_id: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    whatsapp_message_status: Mapped[Optional[str]] = mapped_column(String(50), nullable=True)

    notification: Mapped["Notification"] = relationship(back_populates="whatsapp_detail")