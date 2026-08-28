# backend/app/schemas/email.py
"""Pydantic schemas for email notification detail records."""

import uuid
from datetime import datetime
from typing import List, Optional

from pydantic import BaseModel, ConfigDict, EmailStr, Field, model_validator

from app.models.email_notification import EmailProvider


class EmailNotificationBase(BaseModel):
    """Shared fields for email notification schemas.

    Attributes:
        from_email: Sender email address.
        to_email: Primary recipient email address.
        cc: Optional list of carbon-copy recipient addresses.
        bcc: Optional list of blind carbon-copy recipient addresses.
        reply_to: Optional reply-to address.
        subject: Rendered email subject line.
        html_body: Rendered HTML body.
        text_body: Rendered plain text body.
        provider: Email delivery provider to use.
    """

    from_email: EmailStr
    to_email: EmailStr
    cc: Optional[List[EmailStr]] = None
    bcc: Optional[List[EmailStr]] = None
    reply_to: Optional[EmailStr] = None
    subject: str = Field(min_length=1, max_length=500)
    html_body: Optional[str] = None
    text_body: Optional[str] = None
    provider: EmailProvider

    model_config = ConfigDict(from_attributes=True)

    @model_validator(mode="after")
    def validate_body_present(self) -> "EmailNotificationBase":
        """Ensure at least one of html_body or text_body is provided."""
        if not self.html_body and not self.text_body:
            raise ValueError("at least one of html_body or text_body is required")
        return self


class EmailNotificationCreate(EmailNotificationBase):
    """Schema for creating a new email notification detail record."""

    notification_id: uuid.UUID


class EmailNotificationUpdate(BaseModel):
    """Schema for updating delivery tracking fields on an email notification."""

    provider_message_id: Optional[str] = Field(default=None, max_length=255)
    is_bounced: Optional[bool] = None
    bounce_type: Optional[str] = Field(default=None, max_length=50)
    opened_at: Optional[datetime] = None
    clicked_at: Optional[datetime] = None

    model_config = ConfigDict(from_attributes=True)


class EmailNotificationRead(EmailNotificationBase):
    """Schema representing a fully populated email notification detail record."""

    id: uuid.UUID
    notification_id: uuid.UUID
    provider_message_id: Optional[str] = None
    is_bounced: bool
    bounce_type: Optional[str] = None
    opened_at: Optional[datetime] = None
    clicked_at: Optional[datetime] = None
    created_at: datetime
    updated_at: datetime

    model_config = ConfigDict(from_attributes=True)