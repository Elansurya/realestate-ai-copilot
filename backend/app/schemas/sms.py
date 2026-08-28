# backend/app/schemas/sms.py
"""Pydantic schemas for SMS notification detail records."""

import re
import uuid
from datetime import datetime
from typing import Optional

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.models.sms_notification import SMSDeliveryStatus, SMSProvider

_E164_PATTERN = re.compile(r"^\+[1-9]\d{6,14}$")


class SMSNotificationBase(BaseModel):
    """Shared fields for SMS notification schemas.

    Attributes:
        from_number: Sender phone number in E.164 format.
        to_number: Recipient phone number in E.164 format.
        message_body: Rendered SMS text content.
        provider: SMS delivery provider to use.
    """

    from_number: str = Field(min_length=8, max_length=20)
    to_number: str = Field(min_length=8, max_length=20)
    message_body: str = Field(min_length=1, max_length=1600)
    provider: SMSProvider

    model_config = ConfigDict(from_attributes=True)

    @field_validator("from_number", "to_number")
    @classmethod
    def validate_e164_format(cls, value: str) -> str:
        """Ensure phone numbers conform to the E.164 format."""
        if not _E164_PATTERN.match(value):
            raise ValueError("phone number must be in E.164 format, e.g. +14155552671")
        return value


class SMSNotificationCreate(SMSNotificationBase):
    """Schema for creating a new SMS notification detail record."""

    notification_id: uuid.UUID


class SMSNotificationUpdate(BaseModel):
    """Schema for updating delivery tracking fields on an SMS notification."""

    provider_message_id: Optional[str] = Field(default=None, max_length=255)
    delivery_status: Optional[SMSDeliveryStatus] = None
    segments_count: Optional[int] = Field(default=None, gt=0)
    cost: Optional[float] = Field(default=None, ge=0)

    model_config = ConfigDict(from_attributes=True)


class SMSNotificationRead(SMSNotificationBase):
    """Schema representing a fully populated SMS notification detail record."""

    id: uuid.UUID
    notification_id: uuid.UUID
    provider_message_id: Optional[str] = None
    delivery_status: SMSDeliveryStatus
    segments_count: int
    cost: Optional[float] = None
    created_at: datetime
    updated_at: datetime

    model_config = ConfigDict(from_attributes=True)