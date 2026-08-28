# backend/app/schemas/whatsapp.py
"""Pydantic schemas for WhatsApp notification detail records."""

import re
import uuid
from datetime import datetime
from typing import Optional

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.models.whatsapp_notification import WhatsAppMessageType, WhatsAppProvider

_E164_PATTERN = re.compile(r"^\+[1-9]\d{6,14}$")


class WhatsAppNotificationBase(BaseModel):
    """Shared fields for WhatsApp notification schemas.

    Attributes:
        from_number: Sender WhatsApp business number in E.164 format.
        to_number: Recipient WhatsApp number in E.164 format.
        message_type: Type of WhatsApp message payload.
        template_name: Approved template name, required for template messages.
        template_language: Language code of the approved template.
        media_url: Media asset URL, required for media messages.
        provider: WhatsApp delivery provider to use.
    """

    from_number: str = Field(min_length=8, max_length=20)
    to_number: str = Field(min_length=8, max_length=20)
    message_type: WhatsAppMessageType
    template_name: Optional[str] = Field(default=None, max_length=255)
    template_language: Optional[str] = Field(default=None, max_length=20)
    media_url: Optional[str] = None
    provider: WhatsAppProvider

    model_config = ConfigDict(from_attributes=True)

    @field_validator("from_number", "to_number")
    @classmethod
    def validate_e164_format(cls, value: str) -> str:
        """Ensure phone numbers conform to the E.164 format."""
        if not _E164_PATTERN.match(value):
            raise ValueError("phone number must be in E.164 format, e.g. +14155552671")
        return value

    @model_validator(mode="after")
    def validate_type_specific_fields(self) -> "WhatsAppNotificationBase":
        """Ensure required fields are present for the selected message type."""
        if self.message_type == WhatsAppMessageType.TEMPLATE and not self.template_name:
            raise ValueError("template_name is required for template messages")
        if self.message_type == WhatsAppMessageType.MEDIA and not self.media_url:
            raise ValueError("media_url is required for media messages")
        return self


class WhatsAppNotificationCreate(WhatsAppNotificationBase):
    """Schema for creating a new WhatsApp notification detail record."""

    notification_id: uuid.UUID


class WhatsAppNotificationUpdate(BaseModel):
    """Schema for updating delivery tracking fields on a WhatsApp notification."""

    provider_message_id: Optional[str] = Field(default=None, max_length=255)
    whatsapp_message_status: Optional[str] = Field(default=None, max_length=50)

    model_config = ConfigDict(from_attributes=True)


class WhatsAppNotificationRead(WhatsAppNotificationBase):
    """Schema representing a fully populated WhatsApp notification detail record."""

    id: uuid.UUID
    notification_id: uuid.UUID
    provider_message_id: Optional[str] = None
    whatsapp_message_status: Optional[str] = None
    created_at: datetime
    updated_at: datetime

    model_config = ConfigDict(from_attributes=True)