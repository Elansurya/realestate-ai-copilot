# backend/app/models/email_notification.py
"""Email notification channel detail model."""

import enum
import uuid
from datetime import datetime
from typing import List, Optional, TYPE_CHECKING

from sqlalchemy import Boolean, CheckConstraint, DateTime, ForeignKey, Index, String, Text
from sqlalchemy import Enum as SAEnum
from sqlalchemy.dialects.postgresql import ARRAY, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base
from app.models.notification import TimestampMixin

if TYPE_CHECKING:
    from app.models.notification import Notification


class EmailProvider(str, enum.Enum):
    """Third-party provider used to dispatch an email notification."""

    SES = "ses"
    SENDGRID = "sendgrid"
    SMTP = "smtp"
    MAILGUN = "mailgun"


class EmailNotification(Base, TimestampMixin):
    """Channel-specific payload and delivery metadata for an email notification.

    Attributes:
        id: Primary key UUID.
        notification_id: FK to the parent notification (one-to-one).
        from_email: Sender email address.
        to_email: Primary recipient email address.
        cc: Optional list of carbon-copy recipient addresses.
        bcc: Optional list of blind carbon-copy recipient addresses.
        reply_to: Optional reply-to address.
        subject: Rendered email subject line.
        html_body: Rendered HTML body.
        text_body: Rendered plain text body.
        provider: Email delivery provider used.
        provider_message_id: Provider-assigned message identifier.
        is_bounced: Whether the message bounced.
        bounce_type: Provider-reported bounce classification.
        opened_at: Timestamp the recipient opened the email.
        clicked_at: Timestamp the recipient clicked a tracked link.
    """

    __tablename__ = "email_notifications"
    __table_args__ = (
        CheckConstraint("from_email <> ''", name="ck_email_notifications_from_email_not_empty"),
        CheckConstraint("to_email <> ''", name="ck_email_notifications_to_email_not_empty"),
        Index(
            "ix_email_notifications_provider_message_id", "provider", "provider_message_id"
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
    from_email: Mapped[str] = mapped_column(String(255), nullable=False)
    to_email: Mapped[str] = mapped_column(String(255), nullable=False)
    cc: Mapped[Optional[List[str]]] = mapped_column(ARRAY(String(255)), nullable=True)
    bcc: Mapped[Optional[List[str]]] = mapped_column(ARRAY(String(255)), nullable=True)
    reply_to: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    subject: Mapped[str] = mapped_column(String(500), nullable=False)
    html_body: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    text_body: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    provider: Mapped[EmailProvider] = mapped_column(
        SAEnum(EmailProvider, name="email_provider_enum"), nullable=False
    )
    provider_message_id: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    is_bounced: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    bounce_type: Mapped[Optional[str]] = mapped_column(String(50), nullable=True)
    opened_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    clicked_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    notification: Mapped["Notification"] = relationship(back_populates="email_detail")