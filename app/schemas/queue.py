# backend/app/schemas/queue.py
"""Pydantic schemas for the notification queue."""

import uuid
from datetime import datetime
from typing import Optional

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.models.notification import NotificationPriority
from app.models.notification_queue import QueueStatus


class NotificationQueueBase(BaseModel):
    """Shared fields for notification queue schemas.

    Attributes:
        priority: Dispatch priority for the queue entry.
        scheduled_at: Earliest timestamp the entry may be dispatched.
        max_retries: Maximum number of allowed processing attempts.
    """

    priority: NotificationPriority = NotificationPriority.NORMAL
    scheduled_at: Optional[datetime] = None
    max_retries: int = Field(default=3, ge=0)

    model_config = ConfigDict(from_attributes=True)


class NotificationQueueCreate(NotificationQueueBase):
    """Schema for creating a new notification queue entry."""

    notification_id: uuid.UUID


class NotificationQueueUpdate(BaseModel):
    """Schema for updating queue processing state.

    Attributes:
        status: New processing status for the queue entry.
        locked_at: Timestamp a worker acquired a processing lock.
        locked_by: Identifier of the worker holding the lock.
        retry_count: Number of processing attempts made so far.
        next_retry_at: Timestamp of the next scheduled retry attempt.
        last_error: Last error message recorded during processing.
    """

    status: Optional[QueueStatus] = None
    locked_at: Optional[datetime] = None
    locked_by: Optional[str] = Field(default=None, max_length=100)
    retry_count: Optional[int] = Field(default=None, ge=0)
    next_retry_at: Optional[datetime] = None
    last_error: Optional[str] = None

    model_config = ConfigDict(from_attributes=True)

    @model_validator(mode="after")
    def validate_processing_lock_consistency(self) -> "NotificationQueueUpdate":
        """Ensure locked_by is present whenever a processing lock is being acquired."""
        if self.locked_at is not None and not self.locked_by:
            raise ValueError("locked_by is required when locked_at is set")
        return self


class NotificationQueueRead(NotificationQueueBase):
    """Schema representing a fully populated notification queue entry."""

    id: uuid.UUID
    notification_id: uuid.UUID
    status: QueueStatus
    locked_at: Optional[datetime] = None
    locked_by: Optional[str] = None
    retry_count: int
    next_retry_at: Optional[datetime] = None
    last_error: Optional[str] = None
    created_at: datetime
    updated_at: datetime

    model_config = ConfigDict(from_attributes=True)