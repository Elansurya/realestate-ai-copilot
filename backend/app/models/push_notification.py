# backend/app/models/push_notification.py
"""Push notification channel detail model."""

import enum
import uuid
from typing import Optional, TYPE_CHECKING

from sqlalchemy import Boolean, CheckConstraint, ForeignKey, Index, Integer, String, Text
from sqlalchemy import Enum as SAEnum
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base
from app.models.notification import TimestampMixin

if TYPE_CHECKING:
    from app.models.notification import Notification


class DevicePlatform(str, enum.Enum):
    """Target device platform for a push notification."""

    IOS = "ios"
    ANDROID = "android"
    WEB = "web"


class PushProvider(str, enum.Enum):
    """Third-party provider used to dispatch a push notification."""

    FCM = "fcm"
    APNS = "apns"
    ONESIGNAL = "onesignal"


class PushNotification(Base, TimestampMixin):
    """Channel-specific payload and delivery metadata for a push notification.

    Attributes:
        id: Primary key UUID.
        notification_id: FK to the parent notification (one-to-one).
        device_token: Target device push token/registration id.
        platform: Target device platform.
        title: Push notification title.
        body: Push notification body text.
        data_payload: Optional custom data payload delivered with the push.
        provider: Push delivery provider used.
        provider_message_id: Provider-assigned message identifier.
        is_silent: Whether this is a silent/background push.
        badge_count: App icon badge count to set, if applicable.
    """

    __tablename__ = "push_notifications"
    __table_args__ = (
        CheckConstraint("device_token <> ''", name="ck_push_notifications_device_token_not_empty"),
        CheckConstraint(
            "badge_count IS NULL OR badge_count >= 0", name="ck_push_notifications_badge_count_non_negative"
        ),
        Index("ix_push_notifications_provider_message_id", "provider", "provider_message_id"),
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
    device_token: Mapped[str] = mapped_column(Text, nullable=False)
    platform: Mapped[DevicePlatform] = mapped_column(
        SAEnum(DevicePlatform, name="device_platform_enum"), nullable=False
    )
    title: Mapped[str] = mapped_column(String(255), nullable=False)
    body: Mapped[str] = mapped_column(Text, nullable=False)
    data_payload: Mapped[Optional[dict]] = mapped_column(JSONB, nullable=True)
    provider: Mapped[PushProvider] = mapped_column(
        SAEnum(PushProvider, name="push_provider_enum"), nullable=False
    )
    provider_message_id: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    is_silent: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    badge_count: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)

    notification: Mapped["Notification"] = relationship(back_populates="push_detail")