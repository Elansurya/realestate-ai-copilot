# backend/app/schemas/push.py
"""Pydantic schemas for push notification detail records."""

import uuid
from datetime import datetime
from typing import Any, Optional

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.models.push_notification import DevicePlatform, PushProvider


class PushNotificationBase(BaseModel):
    """Shared fields for push notification schemas.

    Attributes:
        device_token: Target device push token/registration id.
        platform: Target device platform.
        title: Push notification title.
        body: Push notification body text.
        data_payload: Optional custom data payload delivered with the push.
        provider: Push delivery provider to use.
        is_silent: Whether this is a silent/background push.
        badge_count: App icon badge count to set, if applicable.
    """

    device_token: str = Field(min_length=1)
    platform: DevicePlatform
    title: str = Field(min_length=1, max_length=255)
    body: str = Field(min_length=1)
    data_payload: Optional[dict[str, Any]] = None
    provider: PushProvider
    is_silent: bool = False
    badge_count: Optional[int] = Field(default=None, ge=0)

    model_config = ConfigDict(from_attributes=True)

    @field_validator("device_token")
    @classmethod
    def validate_device_token_not_blank(cls, value: str) -> str:
        """Ensure the device token is not blank after trimming whitespace."""
        stripped = value.strip()
        if not stripped:
            raise ValueError("device_token must not be blank")
        return stripped


class PushNotificationCreate(PushNotificationBase):
    """Schema for creating a new push notification detail record."""

    notification_id: uuid.UUID


class PushNotificationUpdate(BaseModel):
    """Schema for updating delivery tracking fields on a push notification."""

    provider_message_id: Optional[str] = Field(default=None, max_length=255)
    badge_count: Optional[int] = Field(default=None, ge=0)

    model_config = ConfigDict(from_attributes=True)


class PushNotificationRead(PushNotificationBase):
    """Schema representing a fully populated push notification detail record."""

    id: uuid.UUID
    notification_id: uuid.UUID
    provider_message_id: Optional[str] = None
    created_at: datetime
    updated_at: datetime

    model_config = ConfigDict(from_attributes=True)