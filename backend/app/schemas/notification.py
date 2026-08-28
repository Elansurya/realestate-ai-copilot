# backend/app/schemas/notification.py
"""Pydantic schemas for the core Notification entity.

In addition to the core `Notification` CRUD schemas, this module also
defines (or re-exports) every schema consumed by
`app/api/v1/notification.py`: notification templates, the delivery
queue, bulk/channel-specific send requests, retry/schedule controls,
delivery/read status, and aggregate statistics.

Notification templates and delivery-queue entries have their own
dedicated modules (`app/schemas/template.py` and
`app/schemas/queue.py`, respectively). Rather than duplicating those
definitions, this module re-exports them under the `Notification*`
names the router expects (`NotificationTemplateCreate`,
`NotificationTemplateUpdate`, `NotificationTemplateRead`,
`NotificationQueueItemRead`), so there is a single source of truth for
each schema's fields and validation.
"""

import uuid
from datetime import datetime, timezone
from typing import Any, Optional

from pydantic import BaseModel, ConfigDict, EmailStr, Field, field_validator, model_validator

from app.models.notification import (
    NotificationCategory,
    NotificationChannel,
    NotificationPriority,
    NotificationStatus,
)
from app.models.notification_queue import QueueStatus
from app.schemas.queue import NotificationQueueRead as NotificationQueueItemRead
from app.schemas.template import (
    TemplateCreate as NotificationTemplateCreate,
    TemplateRead as NotificationTemplateRead,
    TemplateUpdate as NotificationTemplateUpdate,
)

__all__ = [
    # Core notification CRUD
    "NotificationBase",
    "NotificationCreate",
    "NotificationUpdate",
    "NotificationRead",
    "NotificationDetailRead",
    # Templates (re-exported from app.schemas.template)
    "NotificationTemplateCreate",
    "NotificationTemplateUpdate",
    "NotificationTemplateRead",
    # Delivery logs
    "NotificationLogRead",
    # Queue (re-exported from app.schemas.queue)
    "NotificationQueueItemRead",
    # Bulk send
    "BulkNotificationCreate",
    "BulkNotificationResult",
    # Channel-specific send requests
    "SendEmailRequest",
    "SendSMSRequest",
    "SendWhatsAppRequest",
    "SendPushRequest",
    "SendInAppRequest",
    # Retry / schedule
    "ScheduleNotificationRequest",
    "RetryNotificationRequest",
    # Statistics / counts
    "NotificationStatisticsRead",
    "UnreadCountRead",
    # Status
    "DeliveryStatusRead",
    "ReadStatusRead",
]


class NotificationBase(BaseModel):
    """Shared fields for notification schemas.

    Attributes:
        recipient_id: UUID of the recipient user or contact.
        sender_id: UUID of the user that triggered the notification, if any.
        channel: Delivery channel for this notification.
        category: Business category/domain of the notification.
        priority: Delivery priority.
        subject: Optional short subject/title line.
        body: Rendered notification body content.
        template_id: Optional FK to the rendering template.
        metadata_payload: Arbitrary structured metadata.
        scheduled_at: Optional timestamp for deferred delivery.
    """

    recipient_id: uuid.UUID
    sender_id: Optional[uuid.UUID] = None
    channel: NotificationChannel
    category: NotificationCategory = NotificationCategory.SYSTEM
    priority: NotificationPriority = NotificationPriority.NORMAL
    subject: Optional[str] = Field(default=None, max_length=255)
    body: str = Field(min_length=1)
    template_id: Optional[uuid.UUID] = None
    metadata_payload: Optional[dict[str, Any]] = None
    scheduled_at: Optional[datetime] = None

    @model_validator(mode="before")
    @classmethod
    def normalize_legacy_payload(cls, values):
        if not isinstance(values, dict):
            return values
        values = dict(values)
        if "body" not in values and "message" in values:
            values["body"] = values["message"]
        for key, enum_type in (("channel", NotificationChannel), ("category", NotificationCategory), ("priority", NotificationPriority)):
            value = values.get(key)
            if isinstance(value, str):
                raw = value.strip()
                if key == "priority" and raw.upper() == "MEDIUM":
                    raw = "NORMAL"
                try:
                    values[key] = enum_type[raw.upper()]
                except KeyError:
                    try: values[key] = enum_type(raw.lower())
                    except ValueError: pass
        return values

    @field_validator("scheduled_at")
    @classmethod
    def validate_scheduled_at(cls, value: Optional[datetime]) -> Optional[datetime]:
        """Validate timezone awareness for all notification creates.

        Normal notification creation rejects timestamps in the past.
        ScheduleNotificationRequest intentionally defers the past-time
        business rule to the API/service layer so the API can return 400
        instead of Pydantic 422.
        """
        if value is None:
            return value
        if value.tzinfo is None:
            raise ValueError("scheduled_at must be timezone-aware")
        if cls.__name__ != "ScheduleNotificationRequest" and value < datetime.now(timezone.utc):
            raise ValueError("scheduled_at must not be in the past")
        return value

    model_config = ConfigDict(from_attributes=True, populate_by_name=True, extra="ignore")

    @field_validator("body")
    @classmethod
    def validate_body_not_blank(cls, value: str) -> str:
        """Ensure the notification body is not blank after trimming whitespace."""
        stripped = value.strip()
        if not stripped:
            raise ValueError("body must not be blank")
        return stripped



class NotificationCreate(NotificationBase):
    """Schema for creating a new notification."""

    max_retries: int = Field(default=3, ge=0)


class NotificationUpdate(BaseModel):
    """Schema for updating mutable notification lifecycle fields."""

    status: Optional[NotificationStatus] = None
    is_read: Optional[bool] = None
    read_at: Optional[datetime] = None
    sent_at: Optional[datetime] = None
    delivered_at: Optional[datetime] = None
    retry_count: Optional[int] = Field(default=None, ge=0)
    failure_reason: Optional[str] = None
    subject: Optional[str] = Field(default=None, max_length=255)

    model_config = ConfigDict(from_attributes=True)

    @model_validator(mode="after")
    def validate_read_consistency(self) -> "NotificationUpdate":
        """Ensure read_at is present whenever is_read is being set to True."""
        if self.is_read is True and self.read_at is None:
            raise ValueError("read_at is required when is_read is set to True")
        return self


class NotificationRead(NotificationBase):
    """Schema representing a fully populated notification for read operations."""

    id: uuid.UUID
    status: NotificationStatus
    sent_at: Optional[datetime] = None
    delivered_at: Optional[datetime] = None
    read_at: Optional[datetime] = None
    is_read: bool
    retry_count: int
    max_retries: int = 3
    failure_reason: Optional[str] = None
    created_at: datetime
    updated_at: datetime

    @field_validator("status", mode="before")
    @classmethod
    def normalize_status(cls, value):
        if isinstance(value, NotificationStatus):
            return value
        if isinstance(value, str):
            raw = value.strip()
            try:
                return NotificationStatus[raw.upper()]
            except KeyError:
                try:
                    return NotificationStatus(raw.lower())
                except ValueError:
                    return value
        return value

    model_config = ConfigDict(from_attributes=True)


# --------------------------------------------------------------------------- #
# Notification Delivery Logs
# --------------------------------------------------------------------------- #
class NotificationLogRead(BaseModel):
    """API representation of an immutable notification lifecycle log entry."""

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    notification_id: uuid.UUID
    event_type: str
    status: NotificationStatus
    attempt_number: int = Field(default=1, ge=1)
    provider_response: Optional[dict[str, Any]] = None
    error_message: Optional[str] = None
    occurred_at: datetime
    created_at: datetime


class NotificationLogListResponse(BaseModel):
    """Schema representing a paginated collection of NotificationLogRead records."""

    model_config = ConfigDict(from_attributes=True)

    items: list[NotificationLogRead] = Field(default_factory=list)
    total: int = Field(..., ge=0)
    page: int = Field(..., ge=1)
    page_size: int = Field(..., ge=1)
    total_pages: int = Field(..., ge=0)


# --------------------------------------------------------------------------- #
# Notification Detail (Notification + delivery logs)
# --------------------------------------------------------------------------- #
class NotificationDetailRead(NotificationRead):
    """Schema representing a fully populated notification along with its
    delivery-attempt logs, used for single-record detail views.

    Attributes:
        logs: Delivery-attempt log entries recorded for this notification,
            most recent first.
    """

    logs: list[NotificationLogRead] = Field(default_factory=list)


# --------------------------------------------------------------------------- #
# Bulk Notification
# --------------------------------------------------------------------------- #
class BulkNotificationCreate(BaseModel):
    """Schema for fanning a notification out to multiple recipients.

    Attributes:
        recipient_ids: UUIDs of the recipients to notify.
        sender_id: UUID of the user that triggered the notification, if any.
        channel: Delivery channel for this notification batch.
        category: Business category/domain of the notification.
        priority: Delivery priority.
        subject: Optional short subject/title line.
        body: Rendered notification body content.
        template_id: Optional FK to the rendering template.
        metadata_payload: Arbitrary structured metadata.
        scheduled_at: Optional timestamp for deferred delivery.
    """

    recipient_ids: list[uuid.UUID] = Field(default_factory=list)
    sender_id: Optional[uuid.UUID] = None
    channel: NotificationChannel
    category: NotificationCategory = NotificationCategory.SYSTEM
    priority: NotificationPriority = NotificationPriority.NORMAL
    subject: Optional[str] = Field(default=None, max_length=255)
    body: str = Field(min_length=1)
    template_id: Optional[uuid.UUID] = None
    metadata_payload: Optional[dict[str, Any]] = None
    scheduled_at: Optional[datetime] = None

    @model_validator(mode="before")
    @classmethod
    def normalize_legacy_payload(cls, values):
        if not isinstance(values, dict):
            return values
        values = dict(values)
        if "body" not in values and "message" in values:
            values["body"] = values["message"]
        for key, enum_type in (("channel", NotificationChannel), ("category", NotificationCategory), ("priority", NotificationPriority)):
            value = values.get(key)
            if isinstance(value, str):
                raw = value.strip()
                if key == "priority" and raw.upper() == "MEDIUM":
                    raw = "NORMAL"
                try:
                    values[key] = enum_type[raw.upper()]
                except KeyError:
                    try: values[key] = enum_type(raw.lower())
                    except ValueError: pass
        return values

    model_config = ConfigDict(from_attributes=True, populate_by_name=True, extra="ignore")

    @field_validator("recipient_ids")
    @classmethod
    def normalize_recipient_ids(cls, value: list[uuid.UUID]) -> list[uuid.UUID]:
        """Preserve order while removing duplicate recipient identifiers."""
        seen: set[uuid.UUID] = set()
        deduped: list[uuid.UUID] = []
        for recipient_id in value:
            if recipient_id not in seen:
                seen.add(recipient_id)
                deduped.append(recipient_id)
        if cls.__name__ == "BulkNotificationCreate" and not deduped:
            raise ValueError("recipient_ids cannot be empty")
        return deduped


class BulkNotificationResult(BaseModel):
    """Schema summarizing the outcome of a bulk notification request.

    Attributes:
        accepted_count: Number of recipients successfully enqueued.
        duplicate_count: Number of recipients skipped as duplicates.
        rejected_count: Number of recipients rejected (e.g. invalid or
            opted-out).
        notification_ids: Identifiers of the created notifications.
        rejected_recipient_ids: Recipient identifiers that were rejected.
    """

    model_config = ConfigDict(from_attributes=True)

    accepted_count: int = Field(default=0, ge=0)
    duplicate_count: int = Field(default=0, ge=0)
    rejected_count: int = Field(default=0, ge=0)
    notification_ids: list[uuid.UUID] = Field(default_factory=list)
    rejected_recipient_ids: list[uuid.UUID] = Field(default_factory=list)


# --------------------------------------------------------------------------- #
# Channel-Specific Send Requests
# --------------------------------------------------------------------------- #
class SendEmailRequest(BaseModel):
    """Schema for dispatching a one-off email notification.

    Attributes:
        recipient_id: UUID of the recipient user or contact.
        sender_id: UUID of the user that triggered the notification, if any.
        to_email: Destination email address.
        cc: Optional list of carbon-copy recipient addresses.
        bcc: Optional list of blind carbon-copy recipient addresses.
        reply_to: Optional reply-to address.
        subject: Rendered email subject line.
        html_body: Rendered HTML body; at least one of html_body/text_body
            is required.
        text_body: Rendered plain text body.
        category: Business category/domain of the notification.
        priority: Delivery priority.
        template_id: Optional FK to the rendering template.
        metadata_payload: Arbitrary structured metadata.
    """

    recipient_id: uuid.UUID
    sender_id: Optional[uuid.UUID] = None
    to_email: EmailStr
    cc: Optional[list[EmailStr]] = None
    bcc: Optional[list[EmailStr]] = None
    reply_to: Optional[EmailStr] = None
    subject: str = Field(min_length=1, max_length=500)
    html_body: Optional[str] = None
    text_body: Optional[str] = None
    category: NotificationCategory
    priority: NotificationPriority = NotificationPriority.NORMAL
    template_id: Optional[uuid.UUID] = None
    metadata_payload: Optional[dict[str, Any]] = None

    model_config = ConfigDict(from_attributes=True)

    @model_validator(mode="after")
    def validate_body_present(self) -> "SendEmailRequest":
        """Ensure at least one of html_body or text_body is provided."""
        if not self.html_body and not self.text_body:
            raise ValueError("at least one of html_body or text_body is required")
        return self


class SendSMSRequest(BaseModel):
    """Schema for dispatching a one-off SMS notification.

    Attributes:
        recipient_id: UUID of the recipient user or contact.
        sender_id: UUID of the user that triggered the notification, if any.
        to_number: Recipient phone number in E.164 format.
        message_body: Rendered SMS text content.
        category: Business category/domain of the notification.
        priority: Delivery priority.
        template_id: Optional FK to the rendering template.
        metadata_payload: Arbitrary structured metadata.
    """

    recipient_id: uuid.UUID
    sender_id: Optional[uuid.UUID] = None
    to_number: str = Field(min_length=8, max_length=20)
    message_body: str = Field(min_length=1, max_length=1600)
    category: NotificationCategory
    priority: NotificationPriority = NotificationPriority.NORMAL
    template_id: Optional[uuid.UUID] = None
    metadata_payload: Optional[dict[str, Any]] = None

    model_config = ConfigDict(from_attributes=True)


class SendWhatsAppRequest(BaseModel):
    """Schema for dispatching a one-off WhatsApp notification.

    Attributes:
        recipient_id: UUID of the recipient user or contact.
        sender_id: UUID of the user that triggered the notification, if any.
        to_number: Recipient WhatsApp number in E.164 format.
        message_type: Type of WhatsApp message payload (e.g. text, template,
            media).
        template_name: Approved template name, required for template
            messages.
        template_language: Language code of the approved template.
        media_url: Media asset URL, required for media messages.
        body: Rendered free-text message body, for text messages.
        category: Business category/domain of the notification.
        priority: Delivery priority.
        metadata_payload: Arbitrary structured metadata.
    """

    recipient_id: uuid.UUID
    sender_id: Optional[uuid.UUID] = None
    to_number: str = Field(min_length=8, max_length=20)
    message_type: str = Field(default="text", max_length=20)
    template_name: Optional[str] = Field(default=None, max_length=255)
    template_language: Optional[str] = Field(default=None, max_length=20)
    media_url: Optional[str] = None
    body: Optional[str] = None
    category: NotificationCategory
    priority: NotificationPriority = NotificationPriority.NORMAL
    metadata_payload: Optional[dict[str, Any]] = None

    model_config = ConfigDict(from_attributes=True)


class SendPushRequest(BaseModel):
    """Schema for dispatching a one-off push notification.

    Attributes:
        recipient_id: UUID of the recipient user or contact.
        sender_id: UUID of the user that triggered the notification, if any.
        device_token: Target device push token/registration id. Optional
            when the service layer resolves tokens from the recipient's
            registered devices.
        title: Push notification title.
        body: Push notification body text.
        data_payload: Optional custom data payload delivered with the push.
        is_silent: Whether this is a silent/background push.
        badge_count: App icon badge count to set, if applicable.
        category: Business category/domain of the notification.
        priority: Delivery priority.
        metadata_payload: Arbitrary structured metadata.
    """

    recipient_id: uuid.UUID
    sender_id: Optional[uuid.UUID] = None
    device_token: Optional[str] = None
    title: str = Field(min_length=1, max_length=255)
    body: str = Field(min_length=1)
    data_payload: Optional[dict[str, Any]] = None
    is_silent: bool = False
    badge_count: Optional[int] = Field(default=None, ge=0)
    category: NotificationCategory
    priority: NotificationPriority = NotificationPriority.NORMAL
    metadata_payload: Optional[dict[str, Any]] = None

    model_config = ConfigDict(from_attributes=True)


class SendInAppRequest(BaseModel):
    """Schema for dispatching a one-off in-app notification.

    Attributes:
        recipient_id: UUID of the recipient user or contact.
        sender_id: UUID of the user that triggered the notification, if any.
        subject: Optional short subject/title line.
        body: Rendered notification body content.
        category: Business category/domain of the notification.
        priority: Delivery priority.
        metadata_payload: Arbitrary structured metadata.
    """

    recipient_id: uuid.UUID
    sender_id: Optional[uuid.UUID] = None
    subject: Optional[str] = Field(default=None, max_length=255)
    body: str = Field(min_length=1)
    category: NotificationCategory
    priority: NotificationPriority = NotificationPriority.NORMAL
    metadata_payload: Optional[dict[str, Any]] = None

    model_config = ConfigDict(from_attributes=True)


# --------------------------------------------------------------------------- #
# Retry / Schedule
# --------------------------------------------------------------------------- #
class ScheduleNotificationRequest(NotificationCreate):
    """Schema for scheduling a notification for future delivery.

    The schema validates only that the timestamp is timezone-aware.
    Whether it is in the past is an HTTP/business-rule concern handled by
    the API route so clients receive HTTP 400 rather than Pydantic 422.
    """

    scheduled_at: datetime

    @field_validator("scheduled_at")
    @classmethod
    def validate_scheduled_at_timezone(cls, value: datetime) -> datetime:
        if value.tzinfo is None:
            raise ValueError("scheduled_at must be timezone-aware")
        return value


class RetryNotificationRequest(BaseModel):
    """Schema for retrying a previously failed notification.

    Attributes:
        force: When true, bypasses the max_retries cap and retry-window
            checks and re-enqueues immediately.
        reason: Optional operator-supplied reason for the manual retry.
    """

    force: bool = False
    reason: Optional[str] = Field(default=None, max_length=500)

    model_config = ConfigDict(from_attributes=True)


# --------------------------------------------------------------------------- #
# Statistics / Counts
# --------------------------------------------------------------------------- #
class NotificationStatisticsRead(BaseModel):
    """Schema representing aggregate notification statistics.

    Attributes:
        total_notifications: Total number of notifications in scope.
        by_status: Count of notifications grouped by delivery status.
        by_channel: Count of notifications grouped by channel.
        by_category: Count of notifications grouped by category.
        by_priority: Count of notifications grouped by priority.
        sent_count: Number of notifications successfully sent.
        failed_count: Number of notifications that failed delivery.
        pending_count: Number of notifications awaiting dispatch.
        success_rate_percentage: Percentage of notifications successfully
            delivered, out of total notifications in scope.
        average_delivery_time_seconds: Average time from creation to
            delivery, in seconds.
        generated_at: Timestamp when these statistics were computed.
    """

    model_config = ConfigDict(from_attributes=True)

    total_notifications: int = Field(default=0, ge=0)
    by_status: dict[str, int] = Field(default_factory=dict)
    by_channel: dict[str, int] = Field(default_factory=dict)
    by_category: dict[str, int] = Field(default_factory=dict)
    by_priority: dict[str, int] = Field(default_factory=dict)
    sent_count: int = Field(default=0, ge=0)
    failed_count: int = Field(default=0, ge=0)
    pending_count: int = Field(default=0, ge=0)
    success_rate_percentage: Optional[float] = Field(default=None, ge=0, le=100)
    average_delivery_time_seconds: Optional[float] = Field(default=None, ge=0)
    generated_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


class UnreadCountRead(BaseModel):
    """Schema representing the unread notification count for a user.

    Attributes:
        total: Total number of unread notifications.
        by_channel: Unread count broken down by channel.
    """

    model_config = ConfigDict(from_attributes=True)

    total: int = Field(default=0, ge=0)
    by_channel: dict[str, int] = Field(default_factory=dict)


# --------------------------------------------------------------------------- #
# Status
# --------------------------------------------------------------------------- #
class DeliveryStatusRead(BaseModel):
    """Schema representing the delivery status timeline for a notification.

    Attributes:
        notification_id: Identifier of the notification.
        status: Current delivery status.
        channel: Delivery channel used.
        sent_at: Timestamp the notification was sent, if any.
        delivered_at: Timestamp the notification was confirmed delivered,
            if any.
        failed_at: Timestamp the notification most recently failed, if any.
        failure_reason: Human-readable failure detail, if the notification
            failed.
        retry_count: Number of delivery attempts made so far.
        max_retries: Maximum number of allowed delivery attempts.
    """

    model_config = ConfigDict(from_attributes=True)

    notification_id: uuid.UUID
    status: NotificationStatus
    channel: NotificationChannel
    sent_at: Optional[datetime] = None
    delivered_at: Optional[datetime] = None
    failed_at: Optional[datetime] = None
    failure_reason: Optional[str] = None
    retry_count: int = Field(default=0, ge=0)
    max_retries: int = Field(default=3, ge=0)


class ReadStatusRead(BaseModel):
    """Schema representing the read status for a notification.

    Attributes:
        notification_id: Identifier of the notification.
        is_read: Whether the notification has been read.
        read_at: Timestamp the notification was read, if applicable.
    """

    model_config = ConfigDict(from_attributes=True)

    notification_id: uuid.UUID
    is_read: bool
    read_at: Optional[datetime] = None