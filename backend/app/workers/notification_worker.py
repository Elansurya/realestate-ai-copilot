"""Notification Module - Phase 4: Background Worker Layer.

Implements a production-grade asynchronous worker responsible for:
    * Priority-based background queue processing.
    * Delivery across Email, SMS, WhatsApp, Push, and In-App channels.
    * Retry handling with exponential backoff for failed notifications.
    * Polling and dispatch of scheduled notifications.
    * Delivery tracking and audit log persistence.
    * Dead letter queue hand-off for notifications that exhaust retries.

The worker is designed to run as a standalone process (e.g. via
``python -m app.workers.notification_worker``) or to be orchestrated by a
process manager / container entrypoint. It shuts down gracefully on
SIGINT/SIGTERM, ensuring in-flight deliveries are not abandoned mid-write.
"""

from __future__ import annotations

import asyncio
import heapq
import itertools
import logging
import signal
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Optional
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import async_session_factory
from app.core.config import settings
from app.core.exceptions import NotificationDeliveryException
from app.models.notification import (
    NotificationChannel,
    NotificationPriority,
    NotificationStatus,
)
from app.repositories.notification_repository import (
    NotificationRepository,
    NotificationQueueRepository,
    NotificationLogRepository,
)
from app.services.notification_service import (
    EmailChannelSender,
    SMSChannelSender,
    WhatsAppChannelSender,
    PushChannelSender,
    InAppChannelSender,
)

logger = logging.getLogger("app.workers.notification_worker")

# Priority values: lower number == higher priority in the heap.
_PRIORITY_WEIGHT: dict[NotificationPriority, int] = {
    NotificationPriority.URGENT: 0,
    NotificationPriority.HIGH: 1,
    NotificationPriority.NORMAL: 2,
    NotificationPriority.LOW: 3,
}

_CHANNEL_SENDERS: dict[NotificationChannel, type] = {
    NotificationChannel.EMAIL: EmailChannelSender,
    NotificationChannel.SMS: SMSChannelSender,
    NotificationChannel.WHATSAPP: WhatsAppChannelSender,
    NotificationChannel.PUSH: PushChannelSender,
    NotificationChannel.IN_APP: InAppChannelSender,
}


@dataclass(order=True)
class _QueueTask:
    """Internal priority-ordered task wrapper for the in-memory heap.

    Attributes:
        sort_key: Tuple used by ``heapq`` to order tasks by priority and
            then by insertion sequence (FIFO within the same priority).
        notification_id: Identifier of the notification to process.
        channel: Delivery channel for the notification.
        attempt: Current attempt number, starting at 1.
    """

    sort_key: tuple[int, int] = field(compare=True)
    notification_id: UUID = field(compare=False)
    channel: NotificationChannel = field(compare=False)
    attempt: int = field(compare=False, default=1)


class RetryPolicy:
    """Encapsulates the exponential backoff retry policy for deliveries.

    Attributes:
        max_attempts: Maximum number of delivery attempts before the
            notification is routed to the dead letter queue.
        base_delay_seconds: Base delay used for exponential backoff.
        max_delay_seconds: Upper bound applied to the computed delay.
    """

    def __init__(
        self,
        max_attempts: int = 5,
        base_delay_seconds: float = 10.0,
        max_delay_seconds: float = 900.0,
    ) -> None:
        self.max_attempts = max_attempts
        self.base_delay_seconds = base_delay_seconds
        self.max_delay_seconds = max_delay_seconds

    def next_delay(self, attempt: int) -> float:
        """Compute the backoff delay, in seconds, before the next attempt.

        Args:
            attempt: The attempt number that just failed (1-indexed).

        Returns:
            The number of seconds to wait before retrying.
        """
        delay = self.base_delay_seconds * (2 ** (attempt - 1))
        return min(delay, self.max_delay_seconds)

    def is_exhausted(self, attempt: int) -> bool:
        """Determine whether the retry budget has been exhausted.

        Args:
            attempt: The attempt number that just failed (1-indexed).

        Returns:
            True if no further retries should be scheduled.
        """
        return attempt >= self.max_attempts


class DeadLetterQueueHandler:
    """Handles hand-off of permanently failed notifications.

    This is implemented as an extensible hook so that production
    deployments can plug in a message broker (SQS, RabbitMQ, Kafka) or a
    dedicated dead-letter table/alerting pipeline without modifying the
    core worker loop.
    """

    async def handle(
        self,
        session: AsyncSession,
        notification_id: UUID,
        channel: NotificationChannel,
        error: str,
    ) -> None:
        """Persist and surface a permanently failed notification.

        Args:
            session: Active async database session.
            notification_id: Identifier of the exhausted notification.
            channel: Delivery channel that failed.
            error: Last recorded error message.
        """
        queue_repo = NotificationQueueRepository(session)
        notification_repo = NotificationRepository(session)
        log_repo = NotificationLogRepository(session)

        await queue_repo.move_to_dead_letter(notification_id=notification_id, reason=error)
        await notification_repo.update_status(
            notification_id=notification_id,
            new_status=NotificationStatus.DEAD_LETTER,
        )
        await log_repo.create_log(
            notification_id=notification_id,
            event="DEAD_LETTER",
            message=f"Notification moved to dead letter queue on channel {channel.value}: {error}",
        )
        await session.commit()
        logger.error(
            "Notification %s permanently failed on channel %s and was dead-lettered: %s",
            notification_id,
            channel.value,
            error,
        )


class NotificationWorker:
    """Asynchronous, priority-aware notification delivery worker.

    The worker maintains an in-memory priority heap fed by a background
    poller that periodically pulls pending and due-scheduled notifications
    from PostgreSQL. A configurable pool of concurrent consumer coroutines
    drains the heap, dispatches deliveries through the appropriate channel
    sender, records delivery/audit logs, and applies retry or dead-letter
    handling on failure.

    Attributes:
        concurrency: Number of concurrent delivery consumers.
        poll_interval_seconds: Interval between database polling cycles.
        retry_policy: Retry/backoff policy applied to failed deliveries.
        dlq_handler: Handler invoked when retries are exhausted.
    """

    def __init__(
        self,
        concurrency: int = 10,
        poll_interval_seconds: float = 5.0,
        retry_policy: Optional[RetryPolicy] = None,
        dlq_handler: Optional[DeadLetterQueueHandler] = None,
    ) -> None:
        self.concurrency = concurrency
        self.poll_interval_seconds = poll_interval_seconds
        self.retry_policy = retry_policy or RetryPolicy()
        self.dlq_handler = dlq_handler or DeadLetterQueueHandler()

        self._heap: list[_QueueTask] = []
        self._heap_lock = asyncio.Lock()
        self._not_empty = asyncio.Condition(self._heap_lock)
        self._sequence = itertools.count()
        self._shutdown_event = asyncio.Event()
        self._delayed_tasks: dict[UUID, asyncio.Task] = {}

    # ------------------------------------------------------------------ #
    # Lifecycle
    # ------------------------------------------------------------------ #

    async def run(self) -> None:
        """Run the worker until a shutdown signal is received.

        Starts the scheduled-notification poller, the pending-notification
        poller, and the configured pool of consumer coroutines, then waits
        for a graceful shutdown signal before draining in-flight work.
        """
        self._install_signal_handlers()
        logger.info(
            "Starting NotificationWorker (concurrency=%d, poll_interval=%.1fs)",
            self.concurrency,
            self.poll_interval_seconds,
        )

        consumers = [
            asyncio.create_task(self._consumer_loop(worker_id=i))
            for i in range(self.concurrency)
        ]
        pending_poller = asyncio.create_task(self._pending_poller_loop())
        scheduled_poller = asyncio.create_task(self._scheduled_poller_loop())

        await self._shutdown_event.wait()
        logger.info("Shutdown signal received, draining NotificationWorker...")

        pending_poller.cancel()
        scheduled_poller.cancel()
        for task in self._delayed_tasks.values():
            task.cancel()

        for consumer in consumers:
            consumer.cancel()
        await asyncio.gather(*consumers, return_exceptions=True)
        logger.info("NotificationWorker shut down cleanly.")

    def _install_signal_handlers(self) -> None:
        """Register SIGINT/SIGTERM handlers for graceful shutdown."""
        loop = asyncio.get_event_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            try:
                loop.add_signal_handler(sig, self._shutdown_event.set)
            except NotImplementedError:
                # Signal handlers are unavailable on some platforms (e.g. Windows).
                logger.warning("Signal handler for %s could not be installed.", sig)

    # ------------------------------------------------------------------ #
    # Pollers
    # ------------------------------------------------------------------ #

    async def _pending_poller_loop(self) -> None:
        """Continuously poll the database for pending notifications to enqueue."""
        while not self._shutdown_event.is_set():
            try:
                async with async_session_factory() as session:
                    queue_repo = NotificationQueueRepository(session)
                    pending = await queue_repo.fetch_pending_batch(limit=self.concurrency * 5)
                    for item in pending:
                        await self.enqueue(
                            notification_id=item.notification_id,
                            channel=NotificationChannel(item.channel),
                            priority=NotificationPriority(item.priority),
                            attempt=item.attempt_count + 1,
                        )
                        await queue_repo.mark_dispatching(item.id)
                    await session.commit()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("Error while polling pending notifications.")
            await asyncio.sleep(self.poll_interval_seconds)

    async def _scheduled_poller_loop(self) -> None:
        """Continuously poll for scheduled notifications that are now due."""
        while not self._shutdown_event.is_set():
            try:
                async with async_session_factory() as session:
                    notification_repo = NotificationRepository(session)
                    due = await notification_repo.fetch_due_scheduled(
                        as_of=datetime.utcnow(), limit=self.concurrency * 5
                    )
                    queue_repo = NotificationQueueRepository(session)
                    for notification in due:
                        await queue_repo.enqueue(
                            notification_id=notification.id,
                            channel=notification.channel,
                            priority=notification.priority,
                        )
                        await notification_repo.update_status(
                            notification_id=notification.id,
                            new_status=NotificationStatus.QUEUED,
                        )
                    await session.commit()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("Error while polling scheduled notifications.")
            await asyncio.sleep(self.poll_interval_seconds)

    # ------------------------------------------------------------------ #
    # Priority Heap Management
    # ------------------------------------------------------------------ #

    async def enqueue(
        self,
        notification_id: UUID,
        channel: NotificationChannel,
        priority: NotificationPriority,
        attempt: int = 1,
    ) -> None:
        """Add a notification delivery task to the in-memory priority heap.

        Args:
            notification_id: Identifier of the notification to deliver.
            channel: Target delivery channel.
            priority: Delivery priority used to order the heap.
            attempt: Current attempt number, starting at 1.
        """
        weight = _PRIORITY_WEIGHT.get(priority, 2)
        task = _QueueTask(
            sort_key=(weight, next(self._sequence)),
            notification_id=notification_id,
            channel=channel,
            attempt=attempt,
        )
        async with self._not_empty:
            heapq.heappush(self._heap, task)
            self._not_empty.notify()

    async def _schedule_retry(self, task: _QueueTask, delay_seconds: float) -> None:
        """Schedule a delayed re-enqueue of a failed task.

        Args:
            task: The task that failed and should be retried.
            delay_seconds: Number of seconds to wait before re-enqueueing.
        """

        async def _delayed_requeue() -> None:
            try:
                await asyncio.sleep(delay_seconds)
                await self.enqueue(
                    notification_id=task.notification_id,
                    channel=task.channel,
                    priority=NotificationPriority.HIGH,
                    attempt=task.attempt + 1,
                )
            except asyncio.CancelledError:
                pass
            finally:
                self._delayed_tasks.pop(task.notification_id, None)

        self._delayed_tasks[task.notification_id] = asyncio.create_task(_delayed_requeue())

    # ------------------------------------------------------------------ #
    # Consumers
    # ------------------------------------------------------------------ #

    async def _consumer_loop(self, worker_id: int) -> None:
        """Continuously pop tasks from the heap and process them.

        Args:
            worker_id: Numeric identifier of this consumer, used for logging.
        """
        logger.info("Consumer %d started.", worker_id)
        try:
            while True:
                task = await self._pop_task()
                await self._process_task(task, worker_id)
        except asyncio.CancelledError:
            logger.info("Consumer %d stopped.", worker_id)
            raise

    async def _pop_task(self) -> _QueueTask:
        """Block until a task is available and pop the highest priority one.

        Returns:
            The next task to process.
        """
        async with self._not_empty:
            while not self._heap:
                await self._not_empty.wait()
            return heapq.heappop(self._heap)

    async def _process_task(self, task: _QueueTask, worker_id: int) -> None:
        """Process a single delivery task end-to-end.

        Handles channel dispatch, delivery-status persistence, retry
        scheduling on failure, and dead-letter hand-off once the retry
        budget is exhausted.

        Args:
            task: The task to process.
            worker_id: Numeric identifier of the consumer processing this task.
        """
        async with async_session_factory() as session:
            notification_repo = NotificationRepository(session)
            log_repo = NotificationLogRepository(session)
            queue_repo = NotificationQueueRepository(session)

            notification = await notification_repo.get_by_id(task.notification_id)
            if notification is None:
                logger.warning(
                    "Consumer %d: notification %s no longer exists, skipping.",
                    worker_id,
                    task.notification_id,
                )
                return

            if notification.status == NotificationStatus.CANCELLED:
                logger.info(
                    "Consumer %d: notification %s was cancelled, skipping delivery.",
                    worker_id,
                    task.notification_id,
                )
                return

            sender_cls = _CHANNEL_SENDERS.get(task.channel)
            if sender_cls is None:
                logger.error("No sender registered for channel %s", task.channel.value)
                return

            sender = sender_cls()
            try:
                await notification_repo.update_status(
                    notification_id=task.notification_id,
                    new_status=NotificationStatus.SENDING,
                )
                await session.commit()

                delivery_result: dict[str, Any] = await sender.send(notification)

                await notification_repo.update_status(
                    notification_id=task.notification_id,
                    new_status=NotificationStatus.SENT,
                    delivered_at=datetime.utcnow(),
                )
                await log_repo.create_log(
                    notification_id=task.notification_id,
                    event="DELIVERED",
                    message=f"Delivered via {task.channel.value} on attempt {task.attempt}.",
                    metadata=delivery_result,
                )
                await queue_repo.mark_completed(notification_id=task.notification_id)
                await session.commit()
                logger.info(
                    "Consumer %d: notification %s delivered via %s (attempt %d).",
                    worker_id,
                    task.notification_id,
                    task.channel.value,
                    task.attempt,
                )

            except NotificationDeliveryException as exc:
                await self._handle_failure(
                    session=session,
                    task=task,
                    worker_id=worker_id,
                    error=str(exc),
                )
            except Exception as exc:  # noqa: BLE001 - worker boundary, must not crash the loop
                await self._handle_failure(
                    session=session,
                    task=task,
                    worker_id=worker_id,
                    error=f"Unexpected error: {exc}",
                )

    async def _handle_failure(
        self,
        session: AsyncSession,
        task: _QueueTask,
        worker_id: int,
        error: str,
    ) -> None:
        """Record a failed delivery attempt and apply retry/DLQ logic.

        Args:
            session: Active async database session.
            task: The task that failed.
            worker_id: Numeric identifier of the consumer that failed.
            error: Human readable error description.
        """
        notification_repo = NotificationRepository(session)
        log_repo = NotificationLogRepository(session)
        queue_repo = NotificationQueueRepository(session)

        await log_repo.create_log(
            notification_id=task.notification_id,
            event="DELIVERY_FAILED",
            message=f"Attempt {task.attempt} on {task.channel.value} failed: {error}",
        )
        await queue_repo.increment_attempt(
            notification_id=task.notification_id, last_error=error
        )

        if self.retry_policy.is_exhausted(task.attempt):
            await session.commit()
            await self.dlq_handler.handle(
                session=session,
                notification_id=task.notification_id,
                channel=task.channel,
                error=error,
            )
            logger.error(
                "Consumer %d: notification %s exhausted retries on %s.",
                worker_id,
                task.notification_id,
                task.channel.value,
            )
            return

        await notification_repo.update_status(
            notification_id=task.notification_id,
            new_status=NotificationStatus.RETRY_SCHEDULED,
        )
        await session.commit()

        delay = self.retry_policy.next_delay(task.attempt)
        await self._schedule_retry(task, delay)
        logger.warning(
            "Consumer %d: notification %s failed (attempt %d), retrying in %.1fs.",
            worker_id,
            task.notification_id,
            task.attempt,
            delay,
        )


async def main() -> None:
    """Entry point for running the notification worker as a standalone process."""
    logging.basicConfig(
        level=getattr(settings, "LOG_LEVEL", "INFO"),
        format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    )
    worker = NotificationWorker(
        concurrency=getattr(settings, "NOTIFICATION_WORKER_CONCURRENCY", 10),
        poll_interval_seconds=getattr(settings, "NOTIFICATION_WORKER_POLL_INTERVAL", 5.0),
        retry_policy=RetryPolicy(
            max_attempts=getattr(settings, "NOTIFICATION_MAX_RETRY_ATTEMPTS", 5),
            base_delay_seconds=getattr(settings, "NOTIFICATION_RETRY_BASE_DELAY", 10.0),
            max_delay_seconds=getattr(settings, "NOTIFICATION_RETRY_MAX_DELAY", 900.0),
        ),
    )
    await worker.run()


if __name__ == "__main__":
    asyncio.run(main())