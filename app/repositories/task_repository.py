"""
backend/app/repositories/task_repository.py

Data access layer for the Task Management module.

This repository is intentionally free of business logic and domain
validation. It is responsible solely for translating well-formed
requests into SQLAlchemy 2.x async queries against the ``tasks`` table
and returning ORM instances or primitive aggregation results. All
domain validation and exception raising lives in
``app.services.task_service.TaskService``.

Mirrors: app/repositories/activity_repository.py
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Any, Optional, Sequence

from sqlalchemy import Select, and_, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.task import Task, TaskPriority, TaskStatus, TaskType

__all__ = ["TaskRepository"]


class TaskRepository:
    """Provides raw persistence operations for :class:`Task` entities.

    Attributes:
        session: The active asynchronous SQLAlchemy session used for all
            database operations issued by this repository.
    """

    #: Terminal statuses excluded from "currently overdue" calculations.
    _TERMINAL_STATUSES: tuple[TaskStatus, ...] = (
        TaskStatus.COMPLETED,
        TaskStatus.CANCELLED,
    )

    def __init__(self, session: AsyncSession) -> None:
        """Initializes the repository with an active database session.

        Args:
            session: The asynchronous SQLAlchemy session to use for queries.
        """
        self.session = session

    # ------------------------------------------------------------------
    # CRUD
    # ------------------------------------------------------------------

    async def create(self, data: dict[str, Any]) -> Task:
        """Persists a new task.

        Args:
            data: Mapping of column names to values for the new row.

        Returns:
            Task: The newly created, refreshed ORM instance.
        """
        payload = dict(data)

        # The database constraint requires completed tasks to carry a
        # completion timestamp. Repository callers may legitimately create
        # a completed historical task without supplying one, so establish
        # the timestamp at the persistence boundary.
        if payload.get("status") in (TaskStatus.COMPLETED, TaskStatus.COMPLETED.value):
            payload.setdefault("completed_at", datetime.now(timezone.utc))

        # Give sequential repository-created rows distinct application-time
        # creation timestamps. PostgreSQL's now() is transaction-stable, so
        # relying solely on the server default can make two inserts in one
        # transaction tie and defeat "newest first" ordering.
        created_now = datetime.now(timezone.utc)
        payload.setdefault("created_at", created_now)
        payload.setdefault("updated_at", created_now)

        entry = Task(**payload)
        self.session.add(entry)
        await self.session.flush()
        await self.session.refresh(entry)
        return entry

    async def get_by_id(
        self, task_id: uuid.UUID, *, include_deleted: bool = False
    ) -> Optional[Task]:
        """Fetches a single task by its primary key.

        Args:
            task_id: The UUID primary key of the task.
            include_deleted: Whether soft-deleted rows should be considered.

        Returns:
            Optional[Task]: The matching task, or ``None`` if not found.
        """
        stmt = select(Task).where(Task.id == task_id)
        if not include_deleted:
            stmt = stmt.where(Task.is_deleted.is_(False))
        result = await self.session.execute(stmt)
        return result.scalar_one_or_none()

    async def update(self, task: Task, data: dict[str, Any]) -> Task:
        """Applies a partial set of column updates to an existing task.

        Args:
            task: The ORM instance to mutate, previously loaded via
                :meth:`get_by_id`.
            data: Mapping of column names to their new values.

        Returns:
            Task: The updated, refreshed ORM instance.
        """
        for key, value in data.items():
            setattr(task, key, value)
        await self.session.flush()
        await self.session.refresh(task)
        return task

    # ------------------------------------------------------------------
    # Assignment
    # ------------------------------------------------------------------

    async def assign(self, task: Task, assigned_to_id: Optional[int]) -> Task:
        """Sets (or clears) the assignee of a task.

        Used for both the initial assignment and any subsequent
        reassignment; the distinction between the two is a business
        rule enforced by ``TaskService``, not by this method.

        Args:
            task: The ORM instance to mutate.
            assigned_to_id: The new assignee's user id, or ``None`` to
                unassign the task.

        Returns:
            Task: The updated, refreshed ORM instance.
        """
        task.assigned_to_id = assigned_to_id
        await self.session.flush()
        await self.session.refresh(task)
        return task

    # ------------------------------------------------------------------
    # Lifecycle transitions
    # ------------------------------------------------------------------

    async def set_status(self, task: Task, status: TaskStatus) -> Task:
        """Applies a non-terminal status transition (e.g. start/hold/resume).

        Args:
            task: The ORM instance to mutate.
            status: The target lifecycle status.

        Returns:
            Task: The updated, refreshed ORM instance.
        """
        task.status = status
        await self.session.flush()
        await self.session.refresh(task)
        return task

    async def complete(
        self, task: Task, *, completed_by_id: Optional[int]
    ) -> Task:
        """Marks a task as completed.

        Args:
            task: The ORM instance to mutate.
            completed_by_id: The user id who completed the task, if any.

        Returns:
            Task: The updated, refreshed ORM instance.
        """
        task.status = TaskStatus.COMPLETED
        task.completed_at = datetime.now(timezone.utc)
        task.completed_by_id = completed_by_id
        await self.session.flush()
        await self.session.refresh(task)
        return task

    async def cancel(self, task: Task) -> Task:
        """Marks a task as cancelled.

        Args:
            task: The ORM instance to mutate.

        Returns:
            Task: The updated, refreshed ORM instance.
        """
        task.status = TaskStatus.CANCELLED
        task.completed_at = None
        task.completed_by_id = None
        await self.session.flush()
        await self.session.refresh(task)
        return task

    # ------------------------------------------------------------------
    # Soft delete / restore
    # ------------------------------------------------------------------

    async def soft_delete(self, task: Task) -> Task:
        """Marks a task as soft-deleted.

        Args:
            task: The ORM instance to soft-delete.

        Returns:
            Task: The updated, refreshed ORM instance.
        """
        task.is_deleted = True
        task.deleted_at = datetime.now(timezone.utc)
        await self.session.flush()
        await self.session.refresh(task)
        return task

    async def restore(self, task: Task) -> Task:
        """Reverses a soft-delete on a task.

        Args:
            task: The ORM instance to restore.

        Returns:
            Task: The updated, refreshed ORM instance.
        """
        task.is_deleted = False
        task.deleted_at = None
        await self.session.flush()
        await self.session.refresh(task)
        return task

    # ------------------------------------------------------------------
    # Bulk operations
    # ------------------------------------------------------------------

    async def bulk_update(
        self, ids: Sequence[uuid.UUID], data: dict[str, Any]
    ) -> int:
        """Applies the same set of column updates to a bounded set of tasks.

        Args:
            ids: The primary keys of the tasks to update.
            data: Mapping of column names to their new values, applied
                identically to every matched row.

        Returns:
            int: The number of rows affected.
        """
        if not ids or not data:
            return 0
        stmt = select(Task).where(Task.id.in_(ids), Task.is_deleted.is_(False))
        result = await self.session.execute(stmt)
        entries = list(result.scalars().all())
        for entry in entries:
            for key, value in data.items():
                setattr(entry, key, value)
        await self.session.flush()
        return len(entries)

    async def bulk_soft_delete(self, ids: Sequence[uuid.UUID]) -> int:
        """Soft-deletes a specific set of tasks by id.

        Args:
            ids: The primary keys of the tasks to soft-delete.

        Returns:
            int: The number of rows affected.
        """
        if not ids:
            return 0
        stmt = select(Task).where(Task.id.in_(ids), Task.is_deleted.is_(False))
        result = await self.session.execute(stmt)
        entries = list(result.scalars().all())
        now = datetime.now(timezone.utc)
        for entry in entries:
            entry.is_deleted = True
            entry.deleted_at = now
        await self.session.flush()
        return len(entries)

    # ------------------------------------------------------------------
    # Query building helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _apply_filters(
        stmt: Select,
        *,
        status: Optional[str] = None,
        priority: Optional[str] = None,
        task_type: Optional[str] = None,
        assigned_to_id: Optional[int] = None,
        created_by_id: Optional[int] = None,
        related_module: Optional[str] = None,
        related_entity_id: Optional[str] = None,
        search: Optional[str] = None,
        due_from: Optional[datetime] = None,
        due_to: Optional[datetime] = None,
        only_overdue: bool = False,
        include_deleted: bool = False,
    ) -> Select:
        """Applies the supplied filter predicates to a base select statement.

        Args:
            stmt: The base SQLAlchemy select statement to constrain.
            status: Restrict to tasks with this lifecycle status.
            priority: Restrict to tasks with this priority.
            task_type: Restrict to tasks with this task type.
            assigned_to_id: Restrict to tasks assigned to this user.
            created_by_id: Restrict to tasks created by this user.
            related_module: Restrict to tasks concerning this owning module.
            related_entity_id: Restrict to tasks concerning this related entity.
            search: Case-insensitive substring match against title/description.
            due_from: Inclusive lower bound on ``due_date``.
            due_to: Inclusive upper bound on ``due_date``.
            only_overdue: Restrict to tasks currently overdue (past due_date,
                not in a terminal status).
            include_deleted: Whether soft-deleted rows should be considered.

        Returns:
            Select: The statement with all applicable predicates applied.
        """
        conditions = []

        if not include_deleted:
            conditions.append(Task.is_deleted.is_(False))
        if status is not None:
            conditions.append(Task.status == status)
        if priority is not None:
            conditions.append(Task.priority == priority)
        if task_type is not None:
            conditions.append(Task.task_type == task_type)
        if assigned_to_id is not None:
            conditions.append(Task.assigned_to_id == assigned_to_id)
        if created_by_id is not None:
            conditions.append(Task.created_by_id == created_by_id)
        if related_module is not None:
            conditions.append(Task.related_module == related_module)
        if related_entity_id is not None:
            conditions.append(Task.related_entity_id == related_entity_id)
        if search:
            term = f"%{search}%"
            conditions.append(
                or_(Task.title.ilike(term), Task.description.ilike(term))
            )
        if due_from is not None:
            conditions.append(Task.due_date >= due_from)
        if due_to is not None:
            conditions.append(Task.due_date <= due_to)
        if only_overdue:
            conditions.append(Task.due_date.is_not(None))
            conditions.append(Task.due_date < func.now())
            conditions.append(Task.status.not_in(TaskRepository._TERMINAL_STATUSES))

        if conditions:
            stmt = stmt.where(and_(*conditions))
        return stmt

    # ------------------------------------------------------------------
    # Listing / searching
    # ------------------------------------------------------------------

    async def list_tasks(
        self,
        *,
        status: Optional[str] = None,
        priority: Optional[str] = None,
        task_type: Optional[str] = None,
        assigned_to_id: Optional[int] = None,
        created_by_id: Optional[int] = None,
        related_module: Optional[str] = None,
        related_entity_id: Optional[str] = None,
        search: Optional[str] = None,
        due_from: Optional[datetime] = None,
        due_to: Optional[datetime] = None,
        only_overdue: bool = False,
        include_deleted: bool = False,
        page: int = 1,
        page_size: int = 20,
        sort_by: str = "created_at",
        sort_order: str = "desc",
    ) -> tuple[list[Task], int]:
        """Retrieves a filtered, sorted, paginated page of tasks.

        Args:
            status: Optional lifecycle status filter.
            priority: Optional priority filter.
            task_type: Optional task type filter.
            assigned_to_id: Optional assignee filter.
            created_by_id: Optional creator filter.
            related_module: Optional owning-module filter.
            related_entity_id: Optional related-entity filter.
            search: Optional free-text search on title/description.
            due_from: Optional inclusive lower bound on ``due_date``.
            due_to: Optional inclusive upper bound on ``due_date``.
            only_overdue: Restrict to currently overdue tasks.
            include_deleted: Whether soft-deleted rows should be considered.
            page: 1-indexed page number.
            page_size: Number of rows per page.
            sort_by: Column name to order by.
            sort_order: ``"asc"`` or ``"desc"``.

        Returns:
            tuple[list[Task], int]: The page of matching tasks and the total
            count of tasks matching the filters (ignoring pagination).
        """
        filter_kwargs: dict[str, Any] = dict(
            status=status,
            priority=priority,
            task_type=task_type,
            assigned_to_id=assigned_to_id,
            created_by_id=created_by_id,
            related_module=related_module,
            related_entity_id=related_entity_id,
            search=search,
            due_from=due_from,
            due_to=due_to,
            only_overdue=only_overdue,
            include_deleted=include_deleted,
        )

        base_stmt = self._apply_filters(select(Task), **filter_kwargs)
        count_stmt = self._apply_filters(
            select(func.count()).select_from(Task), **filter_kwargs
        )

        sort_column = getattr(Task, sort_by, Task.created_at)
        order_expr = sort_column.asc() if sort_order == "asc" else sort_column.desc()

        page = max(page, 1)
        page_size = max(page_size, 1)
        offset = (page - 1) * page_size

        list_stmt = base_stmt.order_by(order_expr).offset(offset).limit(page_size)

        total_result = await self.session.execute(count_stmt)
        total = total_result.scalar_one()

        result = await self.session.execute(list_stmt)
        items = list(result.scalars().all())

        return items, total

    async def search_tasks(
        self,
        search_term: str,
        *,
        page: int = 1,
        page_size: int = 20,
        sort_by: str = "created_at",
        sort_order: str = "desc",
    ) -> tuple[list[Task], int]:
        """Performs a free-text search over task titles/descriptions.

        Args:
            search_term: Case-insensitive substring to match against the
                title and description fields.
            page: 1-indexed page number.
            page_size: Number of rows per page.
            sort_by: Column name to order by.
            sort_order: ``"asc"`` or ``"desc"``.

        Returns:
            tuple[list[Task], int]: The page of matching tasks and the total
            count of matching tasks.
        """
        return await self.list_tasks(
            search=search_term,
            page=page,
            page_size=page_size,
            sort_by=sort_by,
            sort_order=sort_order,
        )

    async def get_recent_tasks(
        self,
        limit: int = 20,
        *,
        assigned_to_id: Optional[int] = None,
        created_by_id: Optional[int] = None,
        status: Optional[str] = None,
    ) -> list[Task]:
        """Fetches the most recently created tasks.

        Args:
            limit: Maximum number of tasks to return.
            assigned_to_id: Optional assignee filter.
            created_by_id: Optional creator filter.
            status: Optional lifecycle status filter.

        Returns:
            list[Task]: The most recent tasks, newest first.
        """
        stmt = self._apply_filters(
            select(Task),
            assigned_to_id=assigned_to_id,
            created_by_id=created_by_id,
            status=status,
        ).order_by(Task.created_at.desc()).limit(limit)
        result = await self.session.execute(stmt)
        return list(result.scalars().all())

    async def get_due_reminders(
        self,
        *,
        as_of: Optional[datetime] = None,
        limit: int = 100,
    ) -> list[Task]:
        """Fetches non-terminal tasks whose reminder time has elapsed.

        Args:
            as_of: The reference timestamp; defaults to the current time
                if not supplied. Tasks with ``reminder_time <= as_of`` are
                matched.
            limit: Maximum number of tasks to return.

        Returns:
            list[Task]: Matching tasks, ordered by ``reminder_time`` ascending
            (most overdue reminder first).
        """
        reference = as_of or datetime.now(timezone.utc)
        stmt = (
            select(Task)
            .where(
                Task.is_deleted.is_(False),
                Task.reminder_time.is_not(None),
                Task.reminder_time <= reference,
                Task.status.not_in(self._TERMINAL_STATUSES),
            )
            .order_by(Task.reminder_time.asc())
            .limit(limit)
        )
        result = await self.session.execute(stmt)
        return list(result.scalars().all())

    # ------------------------------------------------------------------
    # Statistics / aggregations
    # ------------------------------------------------------------------

    async def get_total_count(
        self,
        *,
        status: Optional[str] = None,
        priority: Optional[str] = None,
        task_type: Optional[str] = None,
        assigned_to_id: Optional[int] = None,
        created_by_id: Optional[int] = None,
        due_from: Optional[datetime] = None,
        due_to: Optional[datetime] = None,
        include_deleted: bool = False,
    ) -> int:
        """Counts the total number of tasks in scope.

        Args:
            status: Optional lifecycle status filter.
            priority: Optional priority filter.
            task_type: Optional task type filter.
            assigned_to_id: Optional assignee filter.
            created_by_id: Optional creator filter.
            due_from: Optional inclusive lower bound on ``due_date``.
            due_to: Optional inclusive upper bound on ``due_date``.
            include_deleted: Whether soft-deleted rows should be considered.

        Returns:
            int: The total matching task count.
        """
        stmt = self._apply_filters(
            select(func.count()).select_from(Task),
            status=status,
            priority=priority,
            task_type=task_type,
            assigned_to_id=assigned_to_id,
            created_by_id=created_by_id,
            due_from=due_from,
            due_to=due_to,
            include_deleted=include_deleted,
        )
        result = await self.session.execute(stmt)
        return result.scalar_one()

    async def count_overdue(
        self, *, assigned_to_id: Optional[int] = None
    ) -> int:
        """Counts tasks that are currently overdue.

        Args:
            assigned_to_id: Optional assignee filter.

        Returns:
            int: The number of tasks past their due date that are not in
            a terminal status.
        """
        stmt = self._apply_filters(
            select(func.count()).select_from(Task),
            assigned_to_id=assigned_to_id,
            only_overdue=True,
        )
        result = await self.session.execute(stmt)
        return result.scalar_one()

    async def count_by_status(
        self,
        *,
        due_from: Optional[datetime] = None,
        due_to: Optional[datetime] = None,
        include_deleted: bool = False,
    ) -> dict[str, int]:
        """Counts tasks grouped by lifecycle status.

        Args:
            due_from: Optional inclusive lower bound on ``due_date``.
            due_to: Optional inclusive upper bound on ``due_date``.
            include_deleted: Whether soft-deleted rows should be considered.

        Returns:
            dict[str, int]: Mapping of status value to task count.
        """
        stmt = self._apply_filters(
            select(Task.status, func.count().label("count")),
            due_from=due_from,
            due_to=due_to,
            include_deleted=include_deleted,
        ).group_by(Task.status)
        result = await self.session.execute(stmt)
        return {row.status.value: row.count for row in result.all()}

    async def count_by_priority(
        self,
        *,
        due_from: Optional[datetime] = None,
        due_to: Optional[datetime] = None,
        include_deleted: bool = False,
    ) -> dict[str, int]:
        """Counts tasks grouped by priority.

        Args:
            due_from: Optional inclusive lower bound on ``due_date``.
            due_to: Optional inclusive upper bound on ``due_date``.
            include_deleted: Whether soft-deleted rows should be considered.

        Returns:
            dict[str, int]: Mapping of priority value to task count.
        """
        stmt = self._apply_filters(
            select(Task.priority, func.count().label("count")),
            due_from=due_from,
            due_to=due_to,
            include_deleted=include_deleted,
        ).group_by(Task.priority)
        result = await self.session.execute(stmt)
        return {row.priority.value: row.count for row in result.all()}

    async def count_by_type(
        self,
        *,
        due_from: Optional[datetime] = None,
        due_to: Optional[datetime] = None,
        include_deleted: bool = False,
    ) -> dict[str, int]:
        """Counts tasks grouped by task type.

        Args:
            due_from: Optional inclusive lower bound on ``due_date``.
            due_to: Optional inclusive upper bound on ``due_date``.
            include_deleted: Whether soft-deleted rows should be considered.

        Returns:
            dict[str, int]: Mapping of task type value to task count.
        """
        stmt = self._apply_filters(
            select(Task.task_type, func.count().label("count")),
            due_from=due_from,
            due_to=due_to,
            include_deleted=include_deleted,
        ).group_by(Task.task_type)
        result = await self.session.execute(stmt)
        return {row.task_type.value: row.count for row in result.all()}

    async def count_by_assignee(
        self,
        *,
        due_from: Optional[datetime] = None,
        due_to: Optional[datetime] = None,
        include_deleted: bool = False,
        limit: int = 20,
    ) -> dict[int, int]:
        """Counts tasks grouped by the assigned user.

        Args:
            due_from: Optional inclusive lower bound on ``due_date``.
            due_to: Optional inclusive upper bound on ``due_date``.
            include_deleted: Whether soft-deleted rows should be considered.
            limit: Maximum number of distinct assignees to return, ordered
                by descending task count.

        Returns:
            dict[int, int]: Mapping of user id to task count.
        """
        stmt = (
            self._apply_filters(
                select(Task.assigned_to_id, func.count().label("count")),
                due_from=due_from,
                due_to=due_to,
                include_deleted=include_deleted,
            )
            .where(Task.assigned_to_id.is_not(None))
            .group_by(Task.assigned_to_id)
            .order_by(func.count().desc())
            .limit(limit)
        )
        result = await self.session.execute(stmt)
        return {row.assigned_to_id: row.count for row in result.all()}