# backend/app/services/scheduler_service.py
"""Business logic for scheduling deferred notification delivery.

Exposes a small, transport-agnostic surface for scheduling, rescheduling,
and cancelling deferred notifications, and for triggering due-notification
processing. `process_due_notifications` performs no FastAPI-specific work
and is safe to invoke from a Celery beat task, an RQ scheduler, or a bare
asyncio loop.
"""

import uuid
from datetime import datetime, timezone
from typing import Optional

from app.core.notification_settings import RetryConfig
from app.models.notification import Notification
from app.repositories.notification_repository import NotificationRepository
from app.services.notification_service import NotificationNotFoundError, SchedulingError
from app.services.queue_service import BatchProcessResult, QueueService


class SchedulerService:
    """Business logic layer for scheduling and triggering deferred delivery.

    Attributes:
        notification_repo: Data access layer for notifications.
        queue_service: Business logic layer for queue enqueue/processing.
        retry_config: Retry backoff policy configuration.
    """

    def __init__(
        self,
        notification_repo: NotificationRepository,
        queue_service: QueueService,
        retry_config: RetryConfig,
    ) -> None:
        """Initialize the service.

        Args:
            notification_repo: Repository for `Notification` entities.
            queue_service: Service used to enqueue and reschedule dispatch.
            retry_config: Retry backoff policy configuration.
        """
        self.notification_repo = notification_repo
        self.queue_service = queue_service
        self.retry_config = retry_config

    async def schedule_notification(
        self, notification_id: uuid.UUID, scheduled_at: datetime
    ) -> Notification:
        """Schedule a persisted notification for deferred dispatch and enqueue it.

        Args:
            notification_id: UUID of the notification to schedule.
            scheduled_at: Timezone-aware timestamp for deferred delivery.

        Returns:
            The updated notification.

        Raises:
            SchedulingError: If `scheduled_at` is not timezone-aware or is
                not strictly in the future.
            NotificationNotFoundError: If the notification does not exist.
        """
        self._validate_future_timestamp(scheduled_at)

        notification = await self.notification_repo.get_by_id(notification_id)
        if notification is None:
            raise NotificationNotFoundError(notification_id)

        updated = await self.notification_repo.update_fields(
            notification_id, {"scheduled_at": scheduled_at}
        )
        if updated is None:
            raise NotificationNotFoundError(notification_id)

        await self.queue_service.enqueue_notification(updated, scheduled_at=scheduled_at)
        return updated

    async def reschedule_notification(
        self, notification_id: uuid.UUID, new_scheduled_at: datetime
    ) -> Notification:
        """Move an already-queued notification to a new future dispatch time.

        Args:
            notification_id: UUID of the notification to reschedule.
            new_scheduled_at: New timezone-aware timestamp for delivery.

        Returns:
            The updated notification.

        Raises:
            SchedulingError: If `new_scheduled_at` is not timezone-aware or
                is not strictly in the future.
            NotificationNotFoundError: If the notification or its queue
                entry does not exist.
        """
        self._validate_future_timestamp(new_scheduled_at)
        await self.queue_service.reschedule(notification_id, new_scheduled_at)

        notification = await self.notification_repo.get_by_id(notification_id)
        if notification is None:
            raise NotificationNotFoundError(notification_id)
        return notification

    async def cancel_schedule(self, notification_id: uuid.UUID) -> None:
        """Cancel a scheduled notification before it is dispatched.

        Args:
            notification_id: UUID of the notification to cancel.

        Raises:
            NotificationNotFoundError: If the notification does not exist.
        """
        await self.queue_service.cancel(notification_id)

    async def process_due_notifications(self, worker_id: str) -> BatchProcessResult:
        """Trigger processing of the next batch of due notifications.

        Args:
            worker_id: Identifier of the worker claiming this batch.

        Returns:
            A summary of the processed batch.
        """
        return await self.queue_service.process_next_batch(worker_id)

    async def release_expired_locks(self) -> int:
        """Release queue processing locks held past the configured timeout.

        Returns:
            Number of queue entries released back to the waiting state.
        """
        return await self.queue_service.release_stale_locks()

    def _validate_future_timestamp(self, candidate: datetime) -> None:
        """Validate that a scheduling timestamp is timezone-aware and future.

        Args:
            candidate: Timestamp to validate.

        Raises:
            SchedulingError: If the timestamp is naive or not in the future.
        """
        if candidate.tzinfo is None:
            raise SchedulingError("scheduled timestamp must be timezone-aware")
        if candidate <= datetime.now(timezone.utc):
            raise SchedulingError("scheduled timestamp must be in the future")