# backend/app/schemas/template.py
"""Pydantic schemas for notification templates."""

import uuid
from datetime import datetime
from typing import Any, Optional

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.models.notification import NotificationChannel
from app.models.notification_template import TemplateLocale


class TemplateBase(BaseModel):
    """Shared fields for notification template schemas.

    Attributes:
        code: Stable business identifier for the template family.
        name: Human readable display name.
        channel: Delivery channel this template renders for.
        locale: Locale of the template content.
        subject_template: Optional subject line template.
        body_template: Body content template with placeholder variables.
        variables: JSON schema describing the expected template variables.
        is_active: Whether this template version is currently usable.
    """

    code: str = Field(min_length=1, max_length=100)
    name: str = Field(min_length=1, max_length=255)
    channel: NotificationChannel
    locale: TemplateLocale = TemplateLocale.EN_US
    subject_template: Optional[str] = Field(default=None, max_length=500)
    body_template: str = Field(min_length=1)
    variables: Optional[dict[str, Any]] = None
    is_active: bool = True

    model_config = ConfigDict(from_attributes=True)

    @field_validator("code")
    @classmethod
    def validate_code_format(cls, value: str) -> str:
        """Normalize the template code to a lowercase slug identifier."""
        normalized = value.strip().lower().replace(" ", "_")
        if not normalized.replace("_", "").isalnum():
            raise ValueError("code must be alphanumeric with optional underscores")
        return normalized

    @model_validator(mode="after")
    def validate_subject_required_for_email(self) -> "TemplateBase":
        """Ensure a subject template is present for the email channel."""
        if self.channel == NotificationChannel.EMAIL and not self.subject_template:
            raise ValueError("subject_template is required for the email channel")
        return self


class TemplateCreate(TemplateBase):
    """Schema for creating a new notification template."""

    version: int = Field(default=1, gt=0)


class TemplateUpdate(BaseModel):
    """Schema for updating an existing notification template."""

    name: Optional[str] = Field(default=None, min_length=1, max_length=255)
    subject_template: Optional[str] = Field(default=None, max_length=500)
    body_template: Optional[str] = Field(default=None, min_length=1)
    variables: Optional[dict[str, Any]] = None
    is_active: Optional[bool] = None

    model_config = ConfigDict(from_attributes=True)


class TemplateRead(TemplateBase):
    """Schema representing a fully populated template for read operations."""

    id: uuid.UUID
    version: int
    created_at: datetime
    updated_at: datetime

    model_config = ConfigDict(from_attributes=True)