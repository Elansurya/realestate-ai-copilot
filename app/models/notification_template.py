# backend/app/models/notification_template.py
"""Notification template model.

Defines reusable, versioned rendering templates that can be attached to
notifications for a given delivery channel and locale.
"""

import enum
import uuid
from typing import List, Optional, TYPE_CHECKING

from sqlalchemy import Boolean, CheckConstraint, Index, Integer, String, Text, UniqueConstraint
from sqlalchemy import Enum as SAEnum
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base
from app.models.notification import NotificationCategory, NotificationChannel, SoftDeleteMixin, TimestampMixin

if TYPE_CHECKING:
    from app.models.notification import Notification


class TemplateLocale(str, enum.Enum):
    """Supported locales for template content."""

    EN_US = "en_US"
    EN_IN = "en_IN"
    HI_IN = "hi_IN"
    AR_AE = "ar_AE"


class NotificationTemplate(Base, TimestampMixin, SoftDeleteMixin):
    """Reusable, versioned rendering template for a notification channel.

    Attributes:
        id: Primary key UUID.
        code: Stable business identifier for the template family.
        name: Human readable display name.
        channel: Delivery channel this template renders for.
        locale: Locale of the template content.
        version: Monotonically increasing version number for the template.
        subject_template: Optional subject line template (email/push/in-app).
        body_template: Body content template with placeholder variables.
        variables: JSON schema describing the expected template variables.
        is_active: Whether this template version is currently usable.
    """

    __tablename__ = "notification_templates"
    __table_args__ = (
        UniqueConstraint(
            "code", "locale", "version", name="uq_notification_templates_code_locale_version"
        ),
        CheckConstraint("version > 0", name="ck_notification_templates_version_positive"),
        Index("ix_notification_templates_channel_active", "channel", "is_active"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    code: Mapped[str] = mapped_column(String(100), nullable=False, index=True)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    channel: Mapped[NotificationChannel] = mapped_column(
        SAEnum(NotificationChannel, name="notification_channel_enum"), nullable=False
    )
    notification_type: Mapped[NotificationCategory] = mapped_column(
        SAEnum(
            NotificationCategory,
            name="notification_type_enum",
            values_callable=lambda enum_cls: [member.name.upper() for member in enum_cls],
        ),
        default=NotificationCategory.SYSTEM,
        nullable=False,
    )
    locale: Mapped[TemplateLocale] = mapped_column(
        SAEnum(TemplateLocale, name="template_locale_enum"),
        default=TemplateLocale.EN_US,
        nullable=False,
    )
    version: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    subject_template: Mapped[Optional[str]] = mapped_column(String(500), nullable=True)
    body_template: Mapped[str] = mapped_column(Text, nullable=False)
    variables: Mapped[Optional[dict]] = mapped_column(JSONB, nullable=True)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    created_by: Mapped[Optional[uuid.UUID]] = mapped_column(UUID(as_uuid=True), nullable=True)
    updated_by: Mapped[Optional[uuid.UUID]] = mapped_column(UUID(as_uuid=True), nullable=True)
    deleted_by: Mapped[Optional[uuid.UUID]] = mapped_column(UUID(as_uuid=True), nullable=True)

    notifications: Mapped[List["Notification"]] = relationship(back_populates="template")