# backend/app/services/queue_service.py
"""Business logic for notification queue enqueueing and batch processing.

Designed to be transport-agnostic: `process_next_batch` performs no
FastAPI-specific work and can be invoked from a Celery task, an RQ job,
or a standalone asyncio worker loop without modification.
"""

import logging
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Dict, Optional, Sequence

from app.core.notification_settings import QueueConfig, RetryConfig
from app.models.notification import Notification, NotificationChannel, NotificationStatus
from app.models.notification_log import NotificationEventType, NotificationLog
from app.models.notification_queue import NotificationQueue
from app.repositories.log_repository import LogRepository
from app.repositories.notification_repository import NotificationRepository
from app.repositories.queue_repository import QueueRepository
from app.services.notification_service import (
    ChannelDispatcher,
    DispatchResult,
    NotificationError,
    NotificationNotFoundError,
    NotificationService,
    RetryLimitExceededError,
)

logger = logging.getLogger(__name__)


@dataclass
class BatchProcessResult:
    """Summary of a single queue processing batch.

    Attributes:
        claimed: Number of queue entries claimed for this batch.
        succeeded: Number of entries that were dispatched successfully.
        failed: Number of entries that exhausted retries or failed
            permanently.
        retried: Number of entries scheduled for a future retry attempt.
    """

    claimed: int
    succeeded: int
    failed: int
    retried: int


class QueueService:
    """Business logic layer for enqueueing and processing queued notifications.

    Attributes:
        queue_repo: Data access layer for the dispatch queue.
        notification_repo: Data access layer for notifications.
        log_repo: Data access layer for audit logs.
        notification_service: Cross-channel notification orchestrator.
        dispatchers: Mapping of channel to its concrete dispatcher.
        retry_config: Retry backoff policy configuration.
        queue_config: Queue batch size and lock timeout configuration.
    """

    def __init__(
        self,
        queue_repo: QueueRepository,
        notification_repo: NotificationRepository,
        log_repo: LogRepository,
        notification_service: NotificationService,
        dispatchers: Dict[NotificationChannel, ChannelDispatcher],
        retry_config: RetryConfig,
        queue_config: QueueConfig,
    ) -> None:
        """Initialize the service.

        Args:
            queue_repo: Repository for `NotificationQueue` entries.
            notification_repo: Repository for `Notification` entities.
            log_repo: Repository for `NotificationLog` entries.
            notification_service: Orchestrator used to record delivery
                outcomes on the parent notification.
            dispatchers: Mapping of `NotificationChannel` to the channel
                service responsible for dispatching that channel.
            retry_config: Retry backoff policy configuration.
            queue_config: Queue batch size and lock timeout configuration.
        """
        self.queue_repo = queue_repo
        self.notification_repo = notification_repo
        self.log_repo = log_repo
        self.notification_service = notification_service
        self.dispatchers = dispatchers
        self.retry_config = retry_config
        self.queue_config = queue_config

    async def enqueue_notification(
        self, notification: Notification, scheduled_at: Optional[datetime] = None
    ) -> NotificationQueue:
        """Enqueue a persisted notification for dispatch.

        Args:
            notification: Notification to enqueue.
            scheduled_at: Optional deferred dispatch time; defaults to the
                notification's own `scheduled_at` value.

        Returns:
            The created queue entry.
        """
        queue_entry = NotificationQueue(
            notification_id=notification.id,
            priority=notification.priority,
            scheduled_at=scheduled_at if scheduled_at is not None else notification.scheduled_at,
            max_retries=notification.max_retries,
        )
        created = await self.queue_repo.enqueue(queue_entry)
        await self.notification_repo.update_fields(
            notification.id, {"status": NotificationStatus.QUEUED}
        )
        await self.log_repo.create_log(
            NotificationLog(
                notification_id=notification.id,
                event_type=NotificationEventType.QUEUED,
                status=NotificationStatus.QUEUED,
                attempt_number=1,
            )
        )
        return created

    async def bulk_enqueue(
        self, notifications: Sequence[Notification]
    ) -> Sequence[NotificationQueue]:
        """Enqueue multiple persisted notifications for dispatch.

        Args:
            notifications: Notifications to enqueue.

        Returns:
            The created queue entries, in the same order as `notifications`.
        """
        entries = [
            NotificationQueue(
                notification_id=notification.id,
                priority=notification.priority,
                scheduled_at=notification.scheduled_at,
                max_retries=notification.max_retries,
            )
            for notification in notifications
        ]
        created = await self.queue_repo.bulk_enqueue(entries)
        await self.notification_repo.bulk_update_status(
            [notification.id for notification in notifications],
            NotificationStatus.QUEUED,
        )
        return created

    async def process_next_batch(self, worker_id: str) -> BatchProcessResult:
        """Claim and process the next batch of dispatchable queue entries.

        Intended to be called repeatedly by a worker process (Celery beat,
        RQ scheduler, or a bare asyncio loop). Each entry is claimed with a
        row lock, dispatched through the channel-appropriate service, and
        transitioned to completed, retrying, or permanently failed based
        on the outcome and its remaining retry budget.

        Args:
            worker_id: Identifier of the worker claiming this batch, used
                for lock attribution and observability.

        Returns:
            A summary of how many entries were claimed and their outcomes.
        """
        claimed_entries = await self.queue_repo.fetch_next_batch(
            worker_id=worker_id,
            locked_at=datetime.now(timezone.utc),
            batch_size=self.queue_config.batch_size,
        )

        succeeded = failed = retried = 0
        for queue_entry in claimed_entries:
            outcome = await self._process_single_entry(queue_entry)
            if outcome == "succeeded":
                succeeded += 1
            elif outcome == "retried":
                retried += 1
            else:
                failed += 1

        return BatchProcessResult(
            claimed=len(claimed_entries),
            succeeded=succeeded,
            failed=failed,
            retried=retried,
        )

    async def _process_single_entry(self, queue_entry: NotificationQueue) -> str:
        """Dispatch a single claimed queue entry and update its state.

        Args:
            queue_entry: Queue entry that has already been claimed.

        Returns:
            One of "succeeded", "retried", or "failed", describing the
            resulting transition.
        """
        notification = await self.notification_repo.get_by_id(
            queue_entry.notification_id, include_relations=True
        )
        if notification is None:
            await self.queue_repo.mark_failed(
                queue_entry.id, "notification record no longer exists"
            )
            return "failed"

        dispatcher = self.dispatchers.get(notification.channel)
        if dispatcher is None:
            await self.queue_repo.mark_failed(
                queue_entry.id,
                f"no dispatcher registered for channel {notification.channel.value}",
            )
            await self.notification_service.record_delivery_failure(
                notification.id,
                f"no dispatcher registered for channel {notification.channel.value}",
                queue_entry.retry_count + 1,
            )
            return "failed"

        try:
            result: DispatchResult = await dispatcher.dispatch(notification)
        except NotificationError as exc:
            result = DispatchResult(success=False, error_message=str(exc))
        except Exception as exc:  # noqa: BLE001 - provider failures must not crash the worker
            logger.exception("unexpected dispatch failure for notification %s", notification.id)
            result = DispatchResult(success=False, error_message=str(exc))

        if result.success:
            await self.queue_repo.mark_completed(queue_entry.id)
            await self.notification_service.record_delivery_success(
                notification.id, result.provider_message_id
            )
            return "succeeded"

        return await self._handle_failed_dispatch(queue_entry, notification, result)

    async def _handle_failed_dispatch(
        self,
        queue_entry: NotificationQueue,
        notification: Notification,
        result: DispatchResult,
    ) -> str:
        """Handle a failed dispatch attempt by retrying or terminating it.

        Args:
            queue_entry: The claimed queue entry that failed dispatch.
            notification: The parent notification.
            result: The failed dispatch outcome.

        Returns:
            Either "retried" or "failed", describing the resulting
            transition.
        """
        error_message = result.error_message or "delivery failed"

        if queue_entry.retry_count >= queue_entry.max_retries:
            await self.queue_repo.mark_failed(queue_entry.id, error_message)
            await self.notification_service.record_delivery_failure(
                notification.id, error_message, queue_entry.retry_count + 1
            )
            return "failed"

        next_attempt = queue_entry.retry_count + 1
        backoff_seconds = self.retry_config.compute_backoff_seconds(next_attempt)
        next_retry_at = datetime.now(timezone.utc) + timedelta(seconds=backoff_seconds)

        await self.queue_repo.schedule_retry(queue_entry.id, next_retry_at)
        await self.notification_repo.increment_retry_count(notification.id)
        await self.notification_repo.update_fields(
            notification.id, {"status": NotificationStatus.RETRYING}
        )
        await self.log_repo.create_log(
            NotificationLog(
                notification_id=notification.id,
                event_type=NotificationEventType.RETRIED,
                status=NotificationStatus.RETRYING,
                attempt_number=next_attempt,
                error_message=error_message,
            )
        )
        return "retried"

    async def release_stale_locks(self) -> int:
        """Release processing locks held past the configured timeout.

        Returns:
            Number of queue entries released back to the waiting state.
        """
        stale_before = datetime.now(timezone.utc) - timedelta(
            seconds=self.queue_config.lock_timeout_seconds
        )
        return await self.queue_repo.release_stale_locks(stale_before)

    async def get_queue_status(
        self, notification_id: uuid.UUID
    ) -> Optional[NotificationQueue]:
        """Fetch the queue entry associated with a notification.

        Args:
            notification_id: UUID of the parent notification.

        Returns:
            The matching queue entry, or None if not queued.
        """
        return await self.queue_repo.get_by_notification_id(notification_id)

    async def retry_now(self, notification_id: uuid.UUID) -> NotificationQueue:
        """Force an immediate retry of a queued notification.

        Args:
            notification_id: UUID of the parent notification.

        Returns:
            The updated queue entry, rescheduled for immediate dispatch.

        Raises:
            NotificationNotFoundError: If no queue entry exists.
            RetryLimitExceededError: If the retry budget is exhausted.
        """
        queue_entry = await self.queue_repo.get_by_notification_id(notification_id)
        if queue_entry is None:
            raise NotificationNotFoundError(notification_id)
        if queue_entry.retry_count >= queue_entry.max_retries:
            raise RetryLimitExceededError(
                f"retry limit reached for notification {notification_id}"
            )
        return await self.queue_repo.schedule_retry(
            queue_entry.id, datetime.now(timezone.utc)
        )

    async def reschedule(
        self, notification_id: uuid.UUID, new_scheduled_at: datetime
    ) -> NotificationQueue:
        """Reschedule a queued notification to a new future dispatch time.

        Args:
            notification_id: UUID of the parent notification.
            new_scheduled_at: New timestamp for deferred dispatch.

        Returns:
            The updated queue entry.

        Raises:
            NotificationNotFoundError: If no queue entry exists.
        """
        queue_entry = await self.queue_repo.get_by_notification_id(notification_id)
        if queue_entry is None:
            raise NotificationNotFoundError(notification_id)

        updated = await self.queue_repo.schedule_retry(queue_entry.id, new_scheduled_at)
        await self.notification_repo.update_fields(
            notification_id,
            {"scheduled_at": new_scheduled_at, "status": NotificationStatus.QUEUED},
        )
        return updated

    async def cancel(self, notification_id: uuid.UUID) -> None:
        """Cancel a queued notification's pending dispatch.

        Args:
            notification_id: UUID of the parent notification.

        Raises:
            NotificationNotFoundError: If the notification does not exist.
        """
        queue_entry = await self.queue_repo.get_by_notification_id(notification_id)
        if queue_entry is not None:
            await self.queue_repo.mark_failed(queue_entry.id, "cancelled")

        updated = await self.notification_repo.update_fields(
            notification_id, {"status": NotificationStatus.CANCELLED}
        )
        if updated is None:
            raise NotificationNotFoundError(notification_id)