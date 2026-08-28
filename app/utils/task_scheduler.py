"""
backend/app/utils/task_scheduler.py

Task Scheduler / Reminder Engine for the Task Management module.

Two independent periodic jobs run in-process as asyncio background
tasks, each with its own short-lived database session per tick (never
one long-lived session held across the process lifetime):

    * **Reminder sweep** -- polls
      ``TaskRepository.get_due_reminders`` for tasks whose
      `reminder_time` has elapsed and dispatches a reminder
      notification for each, exactly once, via a pluggable
      :class:`ReminderDispatcher`.
    * **Overdue sweep** -- polls tasks that are newly overdue
      (`due_date` passed, non-terminal status) and dispatches an
      overdue alert, also exactly once per task, via the same
      dispatcher.

Design notes:
    - This module has no hard dependency on any concrete notification
      implementation. `ReminderDispatcher` is a small `Protocol`; the
      default implementation just logs, so the scheduler is safe to
      start even before a real Notification module is wired in. Pass
      a real dispatcher via `TaskScheduler(..., dispatcher=...)` when
      one is available.
    - "Exactly once" is approximated with a bounded in-memory
      dedup cache (`_dispatched_reminder_ids` /
      `_dispatched_overdue_ids`) keyed by task id, not by persisting
      dispatch state on the row itself. Persisted, crash-safe
      dedup (e.g. a `reminder_dispatched_at` column) is a
      `TaskRepository`/`Task` model concern and is intentionally out
      of scope here -- this module only orchestrates *when* to poll
      and *what* to do with what it finds. If a
      `reminder_dispatched_at`-style column is added later, replace
      the in-memory sets below with a repository-level filter and
      this class needs no other changes.
    - Uses only the stdlib (`asyncio`) -- no APScheduler/Celery
      dependency is assumed, since the CRM's task queue setup is
      outside this module's scope. Swap `TaskScheduler.start()` for a
      Celery beat / APScheduler job calling `run_reminder_sweep_once`
      and `run_overdue_sweep_once` directly if the project already
      standardizes on one of those.

Wiring (FastAPI lifespan), e.g. in `app/main.py`::

    from app.utils.task_scheduler import TaskScheduler

    scheduler = TaskScheduler(session_factory=AsyncSessionLocal)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        await scheduler.start()
        yield
        await scheduler.stop()

Mirrors: app/utils/activity_scheduler.py (naming/style conventions).
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from datetime import datetime, timezone
from typing import Awaitable, Callable, Optional, Protocol

from sqlalchemy.ext.asyncio import AsyncSession

from app.models.task import Task
from app.repositories.task_repository import TaskRepository

__all__ = [
    "ReminderDispatcher",
    "LoggingReminderDispatcher",
    "TaskScheduler",
]

logger = logging.getLogger("app.tasks.scheduler")

#: Factory producing a new `AsyncSession` as an async context manager,
#: e.g. `AsyncSessionLocal` from `app.db.session`. Typed loosely here
#: to avoid importing the concrete session module (kept out of this
#: file's "assumed to exist" surface per the module docstring).
SessionFactory = Callable[[], "asyncio.AbstractAsyncContextManager[AsyncSession]"]


# ---------------------------------------------------------------------------
# Dispatch abstraction
# ---------------------------------------------------------------------------
class ReminderDispatcher(Protocol):
    """Protocol for delivering reminder/overdue notifications for a task.

    Concrete implementations might push to the Notification module, an
    email/SMS provider, a websocket channel, etc. The scheduler only
    depends on this narrow interface so it can be started safely
    before any real notification backend is wired in.
    """

    async def dispatch_reminder(self, task: Task) -> None:
        """Delivers a "reminder due" notification for `task`.

        Args:
            task: The task whose `reminder_time` has elapsed.
        """
        ...

    async def dispatch_overdue_alert(self, task: Task) -> None:
        """Delivers an "overdue" notification for `task`.

        Args:
            task: The task that is currently overdue.
        """
        ...


class LoggingReminderDispatcher:
    """Default :class:`ReminderDispatcher` that only logs.

    Used when no real notification backend has been supplied, so the
    scheduler remains fully functional (and observable via logs) in
    isolation, and so its behavior is trivially deterministic in
    tests.
    """

    async def dispatch_reminder(self, task: Task) -> None:
        """Logs a reminder-due event for `task`.

        Args:
            task: The task whose `reminder_time` has elapsed.
        """
        logger.info(
            "task.reminder_due task_id=%s title=%r assigned_to_id=%s "
            "reminder_time=%s",
            task.id,
            task.title,
            task.assigned_to_id,
            task.reminder_time,
        )

    async def dispatch_overdue_alert(self, task: Task) -> None:
        """Logs an overdue event for `task`.

        Args:
            task: The task that is currently overdue.
        """
        logger.warning(
            "task.overdue task_id=%s title=%r assigned_to_id=%s due_date=%s",
            task.id,
            task.title,
            task.assigned_to_id,
            task.due_date,
        )


# ---------------------------------------------------------------------------
# Scheduler
# ---------------------------------------------------------------------------
class TaskScheduler:
    """Runs periodic reminder and overdue sweeps for the Task module.

    Attributes:
        session_factory: Async-context-manager factory producing a
            fresh `AsyncSession` per sweep tick.
        dispatcher: The notification dispatcher used to deliver
            reminder/overdue events.
        reminder_interval_seconds: Delay between reminder sweeps.
        overdue_interval_seconds: Delay between overdue sweeps.
        reminder_batch_size: Max tasks fetched per reminder sweep.
        overdue_batch_size: Max tasks fetched per overdue sweep.
    """

    def __init__(
        self,
        session_factory: SessionFactory,
        *,
        dispatcher: Optional[ReminderDispatcher] = None,
        reminder_interval_seconds: int = 60,
        overdue_interval_seconds: int = 300,
        reminder_batch_size: int = 100,
        overdue_batch_size: int = 100,
        on_error: Optional[Callable[[Exception], Awaitable[None]]] = None,
    ) -> None:
        """Initializes the scheduler without starting any background work.

        Args:
            session_factory: Async-context-manager factory producing a
                fresh `AsyncSession` per sweep tick (e.g.
                `AsyncSessionLocal` from `app.db.session`).
            dispatcher: Notification dispatcher to use; defaults to
                :class:`LoggingReminderDispatcher` if omitted.
            reminder_interval_seconds: Seconds between reminder sweeps.
            overdue_interval_seconds: Seconds between overdue sweeps.
            reminder_batch_size: Max tasks fetched per reminder sweep.
            overdue_batch_size: Max tasks fetched per overdue sweep.
            on_error: Optional async callback invoked with any
                exception raised during a sweep tick, after it has
                been logged. Use this to forward failures to error
                tracking (e.g. Sentry) without this module depending
                on it directly. Sweep loops always continue after an
                error; a single failed tick never stops the scheduler.
        """
        self.session_factory = session_factory
        self.dispatcher: ReminderDispatcher = dispatcher or LoggingReminderDispatcher()
        self.reminder_interval_seconds = reminder_interval_seconds
        self.overdue_interval_seconds = overdue_interval_seconds
        self.reminder_batch_size = reminder_batch_size
        self.overdue_batch_size = overdue_batch_size
        self._on_error = on_error

        self._reminder_task: Optional[asyncio.Task] = None
        self._overdue_task: Optional[asyncio.Task] = None
        self._stopping = asyncio.Event()

        # Bounded in-memory dedup caches; see module docstring for why
        # this is in-memory rather than persisted, and the tradeoff
        # that implies (a process restart may re-dispatch once).
        self._dispatched_reminder_ids: set[uuid.UUID] = set()
        self._dispatched_overdue_ids: set[uuid.UUID] = set()
        self._max_dedup_cache_size: int = 10_000

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------
    async def start(self) -> None:
        """Starts the reminder and overdue background sweep loops.

        Idempotent: calling `start()` while already running is a
        no-op rather than spawning duplicate loops.
        """
        self._stopping.clear()
        if self._reminder_task is None or self._reminder_task.done():
            self._reminder_task = asyncio.create_task(
                self._run_loop(
                    self._reminder_interval_tick, self.reminder_interval_seconds
                ),
                name="task-scheduler-reminders",
            )
        if self._overdue_task is None or self._overdue_task.done():
            self._overdue_task = asyncio.create_task(
                self._run_loop(
                    self._overdue_interval_tick, self.overdue_interval_seconds
                ),
                name="task-scheduler-overdue",
            )
        logger.info("TaskScheduler started.")

    async def stop(self) -> None:
        """Signals both sweep loops to stop and awaits their completion.

        Safe to call even if `start()` was never called.
        """
        self._stopping.set()
        for task in (self._reminder_task, self._overdue_task):
            if task is not None:
                task.cancel()
        for task in (self._reminder_task, self._overdue_task):
            if task is not None:
                try:
                    await task
                except asyncio.CancelledError:
                    pass
        self._reminder_task = None
        self._overdue_task = None
        logger.info("TaskScheduler stopped.")

    # ------------------------------------------------------------------
    # Loop machinery
    # ------------------------------------------------------------------
    async def _run_loop(
        self, tick: Callable[[], Awaitable[None]], interval_seconds: int
    ) -> None:
        """Runs `tick` repeatedly on a fixed interval until stopped.

        Args:
            tick: The async callable to invoke on each iteration.
            interval_seconds: Delay, in seconds, between iterations.
        """
        while not self._stopping.is_set():
            try:
                await tick()
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - sweep must never die
                logger.exception("Task scheduler tick failed: %s", exc)
                if self._on_error is not None:
                    await self._on_error(exc)
            try:
                await asyncio.wait_for(
                    self._stopping.wait(), timeout=interval_seconds
                )
            except asyncio.TimeoutError:
                pass

    async def _reminder_interval_tick(self) -> None:
        """Opens a session and runs a single reminder sweep."""
        async with self.session_factory() as session:
            await self.run_reminder_sweep_once(session)

    async def _overdue_interval_tick(self) -> None:
        """Opens a session and runs a single overdue sweep."""
        async with self.session_factory() as session:
            await self.run_overdue_sweep_once(session)

    # ------------------------------------------------------------------
    # Sweep implementations (also callable directly, e.g. from Celery
    # beat / APScheduler / a management command / tests).
    # ------------------------------------------------------------------
    async def run_reminder_sweep_once(self, session: AsyncSession) -> int:
        """Dispatches reminders for all currently-due, not-yet-dispatched tasks.

        Args:
            session: An active `AsyncSession` scoped to this sweep.

        Returns:
            int: The number of reminders dispatched in this sweep.
        """
        repository = TaskRepository(session)
        due_tasks = await repository.get_due_reminders(
            as_of=datetime.now(timezone.utc), limit=self.reminder_batch_size
        )
        dispatched = 0
        for task in due_tasks:
            if task.id in self._dispatched_reminder_ids:
                continue
            await self.dispatcher.dispatch_reminder(task)
            self._remember_dispatched(self._dispatched_reminder_ids, task.id)
            dispatched += 1
        if dispatched:
            logger.info("Reminder sweep dispatched %d reminder(s).", dispatched)
        return dispatched

    async def run_overdue_sweep_once(self, session: AsyncSession) -> int:
        """Dispatches overdue alerts for all currently-overdue, non-terminal tasks.

        Args:
            session: An active `AsyncSession` scoped to this sweep.

        Returns:
            int: The number of overdue alerts dispatched in this sweep.
        """
        repository = TaskRepository(session)
        overdue_tasks, _total = await repository.list_tasks(
            only_overdue=True,
            page=1,
            page_size=self.overdue_batch_size,
            sort_by="due_date",
            sort_order="asc",
        )
        dispatched = 0
        for task in overdue_tasks:
            if task.id in self._dispatched_overdue_ids:
                continue
            await self.dispatcher.dispatch_overdue_alert(task)
            self._remember_dispatched(self._dispatched_overdue_ids, task.id)
            dispatched += 1
        if dispatched:
            logger.info("Overdue sweep dispatched %d alert(s).", dispatched)
        return dispatched

    # ------------------------------------------------------------------
    # Dedup cache bookkeeping
    # ------------------------------------------------------------------
    def _remember_dispatched(
        self, cache: set[uuid.UUID], task_id: uuid.UUID
    ) -> None:
        """Records `task_id` as dispatched, trimming `cache` if it overflows.

        Args:
            cache: The dedup cache to update (one of
                `_dispatched_reminder_ids` / `_dispatched_overdue_ids`).
            task_id: The task id to record.
        """
        if len(cache) >= self._max_dedup_cache_size:
            # Cheap unbounded-growth guard: drop the whole cache rather
            # than tracking insertion order. Worst case this causes a
            # tiny number of duplicate dispatches right after a trim,
            # which is an acceptable tradeoff for a best-effort,
            # in-memory, at-least-once notification path.
            cache.clear()
            logger.warning(
                "Task scheduler dedup cache exceeded %d entries; cleared.",
                self._max_dedup_cache_size,
            )
        cache.add(task_id)