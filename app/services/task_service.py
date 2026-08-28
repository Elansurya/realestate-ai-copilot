"""
backend/app/services/task_service.py

Service layer for the Task Management module of the Enterprise Real
Estate AI Copilot CRM.

`TaskService` is the single orchestration point between the API layer
(`app.api.v1.task`) and persistence (`app.repositories.task_repository
.TaskRepository`). It is responsible for:

    * Invoking `app.utils.task_validator` business-rule checks *before*
      any repository call that would otherwise persist an illegal
      state (e.g. an out-of-order status transition).
    * Loading whatever current-state context a validator needs (e.g.
      a task's current `status` before checking a transition).
    * Raising the two domain exceptions the API layer maps to HTTP
      responses: :class:`TaskNotFoundError` (-> 404) and
      :class:`TaskConflictError` (-> 409). All other business-rule
      failures surface as :class:`app.utils.task_validator
      .TaskValidationError` (-> 422), which this module does not
      catch -- it lets it propagate to the router unchanged.
    * NOT committing the session. Every mutating method flushes (via
      the repository) but leaves the commit/rollback boundary to the
      caller (the API router wraps each request in a single
      commit-on-success transaction). This keeps the service reusable
      from contexts with different transaction boundaries (e.g. a
      Celery task processing several tasks in one commit).

Requester-based scoping:
    RBAC (which *operations* a role may call) is enforced by the API
    layer via `require_roles`. This service additionally applies a
    narrow *data*-scoping rule on read paths: a caller whose role is
    ``"agent"`` and who has not explicitly filtered by assignee is
    scoped to tasks assigned to themself, so a plain "list tasks" call
    from an agent's own client naturally shows only their queue rather
    than the whole tenant's. Roles other than ``"agent"`` (``admin``,
    ``manager``, ``viewer``) are not scoped here. Adjust
    `_scope_filter_for_requester` if the real RBAC/org-chart model
    differs.

`requester` is accepted as a loosely-typed object (only `.id` and
`.role` are read) rather than a concrete `CurrentUser` type, since
that type is owned by `app.api.deps` and this module avoids importing
API-layer concerns.

Mirrors: app/services/activity_service.py (naming/style/transaction
conventions).
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any, Optional, Sequence

from sqlalchemy.ext.asyncio import AsyncSession

from app.models.task import Task, TaskStatus
from app.models.user import UserRole
from app.repositories.task_repository import TaskRepository
from app.schemas.task import (
    TaskAssign,
    TaskCreate,
    TaskFilter,
    TaskStatisticsResponse,
    TaskStatusUpdate,
    TaskUpdate,
)
from app.utils.task_validator import (
    TaskValidationError,
    validate_assignment_target,
    validate_bulk_ids,
    validate_bulk_update_payload,
    validate_cancellation_allowed,
    validate_completion_allowed,
    validate_due_date_not_absurdly_past,
    validate_related_entity_reference,
    validate_restore_allowed,
    validate_search_term,
    validate_self_assignment_policy,
    validate_status_transition,
)

__all__ = ["TaskService", "TaskNotFoundError", "TaskConflictError"]

#: Roles permitted to self-assign a task without triggering
#: `validate_self_assignment_policy`'s restriction. Kept local to this
#: service (rather than in the validator) since it is an RBAC policy
#: choice, not a structural business rule.
#:
#: `requester.role` (see `_SELF_ASSIGN_ALLOWED_ROLES` usage below) is a
#: `UserRole` member (e.g. `UserRole.ADMIN`), so this set must contain
#: `UserRole` members -- not raw strings -- to compare equal. `app.
#: models.user.UserRole` only defines ADMIN, SALES_MANAGER, and
#: SALES_AGENT.
_SELF_ASSIGN_ALLOWED_ROLES: frozenset[UserRole] = frozenset(
    {UserRole.ADMIN, UserRole.SALES_MANAGER}
)


# ---------------------------------------------------------------------------
# Domain exceptions
# ---------------------------------------------------------------------------
class TaskNotFoundError(Exception):
    """Raised when a referenced task id does not resolve to a usable row.

    Covers both "no such id" and "id exists but is soft-deleted" for
    operations that require a non-deleted task, so the API layer can
    map either case to a uniform `404` without leaking which case it
    was (avoiding a way to enumerate soft-deleted ids).
    """


class TaskConflictError(Exception):
    """Raised when an operation cannot proceed due to the task's current state.

    Used for state conflicts that are about *timing*/*concurrency*
    rather than a structural business rule (which instead raises
    `TaskValidationError`) -- e.g. attempting to assign a task that
    was soft-deleted by another request between the caller's read and
    this write.
    """


# ---------------------------------------------------------------------------
# Service
# ---------------------------------------------------------------------------
class TaskService:
    """Coordinates validation and persistence for Task operations.

    Attributes:
        session: The active asynchronous SQLAlchemy session, shared
            with the request/unit-of-work that constructed this
            service.
        repository: The `TaskRepository` bound to `session`.
    """

    def __init__(self, session: AsyncSession) -> None:
        """Initializes the service with an active database session.

        Args:
            session: The asynchronous SQLAlchemy session to use for
                all repository calls issued by this service.
        """
        self.session = session
        self.repository = TaskRepository(session)

    # ------------------------------------------------------------------
    # Create / read / update
    # ------------------------------------------------------------------
    async def create_task(self, payload: TaskCreate, *, requester: Any) -> Task:
        """Validates and creates a new task.

        Args:
            payload: The task creation payload.
            requester: The authenticated caller (unused directly here;
                accepted for interface consistency with the rest of
                this service and for future audit-trail hooks).

        Returns:
            Task: The newly created, persisted task.

        Raises:
            TaskValidationError: If a business rule is violated (e.g.
                an unrecognized `related_module`, or a `due_date` set
                obviously in the past).
        """
        validate_related_entity_reference(
            payload.related_module, payload.related_entity_id
        )
        validate_due_date_not_absurdly_past(payload.due_date)

        data = payload.model_dump(exclude_unset=False)
        # `metadata` is the public/schema name; the ORM column is
        # mapped as `meta_data` (see app.models.task.Task.meta_data),
        # so translate the key before constructing the row.
        data["meta_data"] = data.pop("metadata", None)
        return await self.repository.create(data)

    async def get_task(
        self, task_id: uuid.UUID, *, requester: Any
    ) -> Optional[Task]:
        """Fetches a single, non-deleted task by id.

        Args:
            task_id: The task's primary key.
            requester: The authenticated caller (accepted for
                interface consistency; no additional scoping is
                applied to single-record reads -- RBAC on *whether*
                this endpoint may be called at all is enforced by the
                API layer).

        Returns:
            Optional[Task]: The matching task, or `None` if not found.
        """
        return await self.repository.get_by_id(task_id)

    async def update_task(
        self, task_id: uuid.UUID, payload: TaskUpdate, *, requester: Any
    ) -> Task:
        """Applies a partial update to a task's descriptive fields.

        Args:
            task_id: The task's primary key.
            payload: The fields to update.
            requester: The authenticated caller.

        Returns:
            Task: The updated task.

        Raises:
            TaskNotFoundError: If no matching, non-deleted task exists.
            TaskValidationError: If a business rule is violated (e.g.
                a `due_date` set obviously in the past).
        """
        task = await self._require_task(task_id)

        update_data = payload.model_dump(exclude_unset=True)
        if "due_date" in update_data:
            validate_due_date_not_absurdly_past(update_data["due_date"])
        if "metadata" in update_data:
            update_data["meta_data"] = update_data.pop("metadata")

        return await self.repository.update(task, update_data)

    # ------------------------------------------------------------------
    # Listing / search / statistics
    # ------------------------------------------------------------------
    async def list_tasks(
        self, task_filter: TaskFilter, *, requester: Any
    ) -> tuple[list[Task], int]:
        """Lists tasks matching a validated filter, scoped to the requester.

        Args:
            task_filter: The validated filter/sort/pagination
                parameters (schema-level validation, e.g. `sort_by`
                allow-listing and `due_from <= due_to`, has already
                run via `TaskFilter`).
            requester: The authenticated caller; used to apply
                requester-based scoping (see module docstring).

        Returns:
            tuple[list[Task], int]: The matching page of tasks and the
            total count across all pages.
        """
        scoped = self._scope_filter_for_requester(task_filter, requester)
        kwargs = scoped.model_dump(exclude={"page", "page_size"})
        return await self.repository.list_tasks(
            **kwargs, page=scoped.page, page_size=scoped.page_size
        )

    async def search_tasks(
        self,
        search_term: str,
        *,
        page: int = 1,
        page_size: int = 20,
        sort_by: str = "created_at",
        sort_order: str = "desc",
        requester: Any,
    ) -> tuple[list[Task], int]:
        """Searches tasks by free text, scoped to the requester.

        Args:
            search_term: Case-insensitive substring to match against
                title/description.
            page: 1-indexed page number.
            page_size: Number of items per page.
            sort_by: Column name to sort by.
            sort_order: `"asc"` or `"desc"`.
            requester: The authenticated caller; used to apply
                requester-based scoping (see module docstring).

        Returns:
            tuple[list[Task], int]: The matching page of tasks and the
            total matching count.

        Raises:
            TaskValidationError: If `search_term` is shorter than
                `app.utils.task_validator.MIN_SEARCH_TERM_LENGTH`, or
                `sort_by` is not an allowed column (validated by
                `TaskFilter` construction below).
        """
        validate_search_term(search_term)
        task_filter = TaskFilter(
            search=search_term,
            page=page,
            page_size=page_size,
            sort_by=sort_by,
            sort_order=sort_order,
        )
        return await self.list_tasks(task_filter, requester=requester)

    async def get_recent_tasks(
        self,
        *,
        limit: int = 20,
        assigned_to_id: Optional[int] = None,
        requester: Any,
    ) -> list[Task]:
        """Fetches the most recently created tasks, scoped to the requester.

        Args:
            limit: Maximum number of tasks to return.
            assigned_to_id: Optional assignee filter.
            requester: The authenticated caller; used to apply
                requester-based scoping (see module docstring).

        Returns:
            list[Task]: The most recent tasks, newest first.
        """
        effective_assignee = self._effective_assignee_scope(
            assigned_to_id, requester
        )
        return await self.repository.get_recent_tasks(
            limit=limit, assigned_to_id=effective_assignee
        )

    async def get_due_reminders(
        self, *, limit: int = 100, requester: Any
    ) -> list[Task]:
        """Fetches non-terminal tasks whose reminder time has elapsed.

        Args:
            limit: Maximum number of tasks to return.
            requester: The authenticated caller (accepted for
                interface consistency; this listing is restricted to
                `admin`/`manager` roles at the API layer, so no
                further scoping is applied here).

        Returns:
            list[Task]: Matching tasks, ordered by `reminder_time`
            ascending.
        """
        return await self.repository.get_due_reminders(limit=limit)

    async def get_statistics(
        self,
        *,
        due_from: Optional[str] = None,
        due_to: Optional[str] = None,
        requester: Any,
    ) -> TaskStatisticsResponse:
        """Computes aggregate task statistics, optionally scoped to a due-date window.

        Args:
            due_from: Optional inclusive lower bound on `due_date`, as
                an ISO 8601 string.
            due_to: Optional inclusive upper bound on `due_date`, as
                an ISO 8601 string.
            requester: The authenticated caller (accepted for
                interface consistency; statistics are not currently
                scoped per-requester).

        Returns:
            TaskStatisticsResponse: The computed aggregate statistics.

        Raises:
            TaskValidationError: If `due_from`/`due_to` are not valid
                ISO 8601 timestamps, or `due_from` is after `due_to`.
        """
        parsed_from = self._parse_iso_datetime(due_from, field="due_from")
        parsed_to = self._parse_iso_datetime(due_to, field="due_to")
        if parsed_from and parsed_to and parsed_from > parsed_to:
            raise TaskValidationError(
                "due_from must not be after due_to.", field="due_from"
            )

        total = await self.repository.get_total_count(
            due_from=parsed_from, due_to=parsed_to
        )
        by_status = await self.repository.count_by_status(
            due_from=parsed_from, due_to=parsed_to
        )
        by_priority = await self.repository.count_by_priority(
            due_from=parsed_from, due_to=parsed_to
        )
        by_type = await self.repository.count_by_type(
            due_from=parsed_from, due_to=parsed_to
        )
        overdue_count = await self.repository.count_overdue()

        return TaskStatisticsResponse(
            total_tasks=total,
            by_status=by_status,
            by_priority=by_priority,
            by_type=by_type,
            overdue_count=overdue_count,
            completed_count=by_status.get(TaskStatus.COMPLETED.value, 0),
            cancelled_count=by_status.get(TaskStatus.CANCELLED.value, 0),
            date_from=parsed_from,
            date_to=parsed_to,
        )

    # ------------------------------------------------------------------
    # Assignment
    # ------------------------------------------------------------------
    async def assign_task(
        self, task_id: uuid.UUID, payload: TaskAssign, *, requester: Any
    ) -> Task:
        """Assigns or reassigns a task.

        Args:
            task_id: The task's primary key.
            payload: The assignment payload.
            requester: The authenticated caller.

        Returns:
            Task: The updated task.

        Raises:
            TaskNotFoundError: If no matching, non-deleted task exists.
            TaskValidationError: If `assigned_to_id` is invalid, or the
                requester's role is not permitted to self-assign.
        """
        task = await self._require_task(task_id)

        validate_assignment_target(payload.assigned_to_id)
        requester_role = getattr(requester, "role", None)
        requester_id = getattr(requester, "id", None)
        if requester_id is not None:
            role_value = str(getattr(requester_role, "value", requester_role)).strip().lower()
            allow_self_assign = role_value in {
                "admin", "administrator", "manager", "sales_manager", "sales-manager",
            } or role_value == UserRole.SALES_MANAGER.value.lower()
            validate_self_assignment_policy(
                assigned_to_id=payload.assigned_to_id,
                requester_id=requester_id,
                allow_self_assign=allow_self_assign,
            )

        updated = await self.repository.assign(task, payload.assigned_to_id)
        if payload.note:
            await self._append_metadata_note(
                updated, kind="assignment", note=payload.note, requester=requester
            )
        return updated

    # ------------------------------------------------------------------
    # Lifecycle transitions
    # ------------------------------------------------------------------
    async def update_status(
        self, task_id: uuid.UUID, payload: TaskStatusUpdate, *, requester: Any
    ) -> Task:
        """Applies a non-terminal lifecycle status transition.

        Args:
            task_id: The task's primary key.
            payload: The status transition payload. `status` must not
                be `completed`/`cancelled` -- use `complete_task` /
                `cancel_task` for those, since they carry additional
                bookkeeping.
            requester: The authenticated caller.

        Returns:
            Task: The updated task.

        Raises:
            TaskNotFoundError: If no matching, non-deleted task exists.
            TaskValidationError: If the transition is not legal from
                the task's current status, or the target status is
                `completed`/`cancelled` (routed to the wrong endpoint).
        """
        task = await self._require_task(task_id)

        if payload.status in (TaskStatus.COMPLETED, TaskStatus.CANCELLED):
            raise TaskValidationError(
                f"Use the dedicated '{payload.status.value}' endpoint for "
                "this transition.",
                field="status",
                code="wrong_endpoint_for_transition",
            )
        validate_status_transition(task.status, payload.status)

        updated = await self.repository.set_status(task, payload.status)
        if payload.comment:
            await self._append_metadata_note(
                updated,
                kind="status_change",
                note=payload.comment,
                requester=requester,
            )
        return updated

    async def complete_task(
        self,
        task_id: uuid.UUID,
        *,
        completed_by_id: Optional[int],
        comment: Optional[str] = None,
        requester: Any,
    ) -> Task:
        """Marks a task as completed.

        Args:
            task_id: The task's primary key.
            completed_by_id: The user id completing the task (typically
                the requester's own id).
            comment: Optional completion note.
            requester: The authenticated caller.

        Returns:
            Task: The completed task.

        Raises:
            TaskNotFoundError: If no matching, non-deleted task exists.
            TaskValidationError: If the task is already in a terminal
                status.
        """
        task = await self._require_task(task_id)
        validate_completion_allowed(task.status)

        updated = await self.repository.complete(
            task, completed_by_id=completed_by_id
        )
        if comment:
            await self._append_metadata_note(
                updated, kind="completion", note=comment, requester=requester
            )
        return updated

    async def cancel_task(
        self,
        task_id: uuid.UUID,
        *,
        comment: Optional[str] = None,
        requester: Any,
    ) -> Task:
        """Marks a task as cancelled.

        Args:
            task_id: The task's primary key.
            comment: Optional cancellation reason.
            requester: The authenticated caller.

        Returns:
            Task: The cancelled task.

        Raises:
            TaskNotFoundError: If no matching, non-deleted task exists.
            TaskValidationError: If the task is already completed or
                already cancelled.
        """
        task = await self._require_task(task_id)
        validate_cancellation_allowed(task.status)

        updated = await self.repository.cancel(task)
        if comment:
            await self._append_metadata_note(
                updated, kind="cancellation", note=comment, requester=requester
            )
        return updated

    # ------------------------------------------------------------------
    # Soft delete / restore
    # ------------------------------------------------------------------
    async def soft_delete_task(self, task_id: uuid.UUID, *, requester: Any) -> Task:
        """Soft-deletes a task.

        Args:
            task_id: The task's primary key.
            requester: The authenticated caller.

        Returns:
            Task: The soft-deleted task.

        Raises:
            TaskNotFoundError: If no matching, non-deleted task exists.
        """
        task = await self._require_task(task_id)
        return await self.repository.soft_delete(task)

    async def restore_task(self, task_id: uuid.UUID, *, requester: Any) -> Task:
        """Restores a soft-deleted task.

        Args:
            task_id: The task's primary key.
            requester: The authenticated caller.

        Returns:
            Task: The restored task.

        Raises:
            TaskNotFoundError: If no task with that id exists at all.
            TaskValidationError: If the task is not currently
                soft-deleted.
        """
        task = await self.repository.get_by_id(task_id, include_deleted=True)
        if task is None:
            raise TaskNotFoundError(f"Task '{task_id}' was not found.")
        validate_restore_allowed(task.is_deleted)
        return await self.repository.restore(task)

    # ------------------------------------------------------------------
    # Bulk operations
    # ------------------------------------------------------------------
    async def bulk_update_tasks(
        self, ids: Sequence[uuid.UUID], data: TaskUpdate, *, requester: Any
    ) -> int:
        """Bulk-updates descriptive fields on a set of tasks.

        Args:
            ids: The primary keys of the tasks to update.
            data: The descriptive-field update payload, applied
                identically to every matched row.
            requester: The authenticated caller.

        Returns:
            int: The number of rows affected.

        Raises:
            TaskValidationError: If `ids` is empty/oversized, or `data`
                (once resolved to only its explicitly-set fields) is
                empty or contains a disallowed field.
        """
        validate_bulk_ids(ids)
        update_data = data.model_dump(exclude_unset=True)
        if "metadata" in update_data:
            update_data["meta_data"] = update_data.pop("metadata")
        validate_bulk_update_payload(update_data)
        return await self.repository.bulk_update(ids, update_data)

    async def bulk_soft_delete_tasks(
        self, ids: Sequence[uuid.UUID], *, requester: Any
    ) -> int:
        """Bulk soft-deletes a set of tasks.

        Args:
            ids: The primary keys of the tasks to soft-delete.
            requester: The authenticated caller.

        Returns:
            int: The number of rows affected.

        Raises:
            TaskValidationError: If `ids` is empty or oversized.
        """
        validate_bulk_ids(ids)
        return await self.repository.bulk_soft_delete(ids)

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------
    async def _require_task(self, task_id: uuid.UUID) -> Task:
        """Fetches a non-deleted task or raises `TaskNotFoundError`.

        Args:
            task_id: The task's primary key.

        Returns:
            Task: The matching task.

        Raises:
            TaskNotFoundError: If no matching, non-deleted task exists.
        """
        task = await self.repository.get_by_id(task_id)
        if task is None:
            raise TaskNotFoundError(f"Task '{task_id}' was not found.")
        return task

    @staticmethod
    def _parse_iso_datetime(
        value: Optional[str], *, field: str
    ) -> Optional[datetime]:
        """Parses an optional ISO 8601 string into a `datetime`.

        Args:
            value: The raw string value, if any.
            field: The originating field name, used in the error
                message on failure.

        Returns:
            Optional[datetime]: The parsed value, or `None` if `value`
            was `None`.

        Raises:
            TaskValidationError: If `value` is supplied but is not a
                valid ISO 8601 timestamp.
        """
        if value is None:
            return None
        try:
            return datetime.fromisoformat(value)
        except ValueError as exc:
            raise TaskValidationError(
                f"{field} must be a valid ISO 8601 timestamp.",
                field=field,
                code="invalid_datetime",
            ) from exc

    @staticmethod
    def _effective_assignee_scope(
        assigned_to_id: Optional[int], requester: Any
    ) -> Optional[int]:
        """Resolves the effective `assigned_to_id` filter for a requester.

        Args:
            assigned_to_id: The caller-supplied assignee filter, if any.
            requester: The authenticated caller.

        Returns:
            Optional[int]: `assigned_to_id` unchanged if it was
            supplied, or already non-scoped roles; otherwise the
            requester's own id when the requester's role is
            `"agent"`.
        """
        if assigned_to_id is not None:
            return assigned_to_id
        if getattr(requester, "role", None) == "agent":
            return getattr(requester, "id", None)
        return None

    def _scope_filter_for_requester(
        self, task_filter: TaskFilter, requester: Any
    ) -> TaskFilter:
        """Applies requester-based data scoping to a listing filter.

        See the module docstring for the scoping policy. Only the
        `assigned_to_id` field is ever overridden here; every other
        filter/sort/pagination field passes through unchanged.

        Args:
            task_filter: The caller-supplied, already schema-validated
                filter.
            requester: The authenticated caller.

        Returns:
            TaskFilter: A filter with `assigned_to_id` resolved per the
            requester scoping policy.
        """
        effective_assignee = self._effective_assignee_scope(
            task_filter.assigned_to_id, requester
        )
        if effective_assignee == task_filter.assigned_to_id:
            return task_filter
        return task_filter.model_copy(update={"assigned_to_id": effective_assignee})

    async def _append_metadata_note(
        self, task: Task, *, kind: str, note: str, requester: Any
    ) -> None:
        """Appends a lightweight audit note to a task's `meta_data` JSON blob.

        This is a pragmatic stand-in for a proper audit trail. The
        CRM's Activity Timeline / Comments module (already referenced
        in `app/models/task.py`'s module docstring as owning
        comment/attachment records) is the correct long-term home for
        assignment/status/completion/cancellation notes; wire this
        method to call into that module's service instead once it
        exposes a stable interface, and remove the `meta_data` write
        path below to avoid two competing audit stores.

        Args:
            task: The task to annotate. Mutated and flushed in place.
            kind: Short category label for the note (e.g.
                `"assignment"`, `"status_change"`, `"completion"`,
                `"cancellation"`).
            note: The free-text note content.
            requester: The authenticated caller, whose id (if present)
                is recorded alongside the note.
        """
        existing_meta = dict(task.meta_data or {})
        notes = list(existing_meta.get("notes", []))
        notes.append(
            {
                "kind": kind,
                "note": note,
                "by_user_id": getattr(requester, "id", None),
                "at": datetime.now().astimezone().isoformat(),
            }
        )
        existing_meta["notes"] = notes
        await self.repository.update(task, {"meta_data": existing_meta})