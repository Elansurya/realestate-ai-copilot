# backend/app/models/in_app_notification.py
"""In-app notification channel detail model."""

import enum
import uuid
from datetime import datetime
from typing import Optional, TYPE_CHECKING

from sqlalchemy import Boolean, CheckConstraint, DateTime, ForeignKey, Index, Integer, String, Text
from sqlalchemy import Enum as SAEnum
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base
from app.models.notification import TimestampMixin

if TYPE_CHECKING:
    from app.models.notification import Notification


class InAppDisplayType(str, enum.Enum):
    """Visual presentation style for an in-app notification."""

    TOAST = "toast"
    BANNER = "banner"
    MODAL = "modal"
    BADGE = "badge"


class InAppNotification(Base, TimestampMixin):
    """Channel-specific payload and read/dismiss state for an in-app notification.

    Attributes:
        id: Primary key UUID.
        notification_id: FK to the parent notification (one-to-one).
        user_id: FK to the target user.
        icon: Optional icon identifier or URL to render alongside the notification.
        action_url: Optional deep link/URL triggered when the notification is tapped.
        display_type: Visual presentation style.
        is_read: Whether the recipient has read the notification.
        read_at: Timestamp the recipient read the notification.
        is_dismissed: Whether the recipient dismissed the notification.
        dismissed_at: Timestamp the recipient dismissed the notification.
        expires_at: Timestamp after which the notification should no longer display.
    """

    __tablename__ = "in_app_notifications"
    __table_args__ = (
        CheckConstraint(
            "dismissed_at IS NULL OR is_dismissed IS TRUE",
            name="ck_in_app_notifications_dismissed_consistency",
        ),
        Index("ix_in_app_notifications_user_read", "user_id", "is_read"),
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
    # NOTE: users.id is INTEGER in this database (see app/models/user.py --
    # the user's separate `uuid` column is a string, not the PK). This
    # column was incorrectly typed as UUID, which is inconsistent with
    # every other users.id FK in this codebase and made the FK constraint
    # to `users` impossible to create ("Key columns ... are of incompatible
    # types: uuid and integer").
    user_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    icon: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    action_url: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    display_type: Mapped[InAppDisplayType] = mapped_column(
        SAEnum(InAppDisplayType, name="in_app_display_type_enum"),
        default=InAppDisplayType.TOAST,
        nullable=False,
    )
    is_read: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    read_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    is_dismissed: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    dismissed_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    expires_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    notification: Mapped["Notification"] = relationship(back_populates="in_app_detail")