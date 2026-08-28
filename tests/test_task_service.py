"""
backend/tests/test_task_service.py

Service-layer unit tests for the Task Management module.

Scope:
    Exercises `app.services.task_service.TaskService` in isolation
    from the database: `TaskRepository` is replaced with an
    `unittest.mock.AsyncMock` on every test, so these tests verify
    orchestration -- which validators run, in what order, what gets
    passed to the repository, and which domain exception is raised --
    without depending on a live database connection. Repository SQL
    correctness is covered separately in `test_task_repository.py`.

No real `AsyncSession` is required: `TaskService.__init__` only stores
`session` and constructs a `TaskRepository(session)`, and nothing in
`TaskService` touches `session` directly afterwards, so a plain
`unittest.mock.MagicMock()` is sufficient as the constructor argument;
`service.repository` is then swapped for an `AsyncMock` in the
`service` fixture below.

A minimal `_Requester` stand-in (a `SimpleNamespace` with `.id` and
`.role`) is used instead of importing any concrete `CurrentUser` type
from `app.api.deps`, since this service layer only ever reads those
two attributes off whatever `requester` object it is handed.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.models.task import Task, TaskStatus
from app.schemas.task import (
    TaskAssign,
    TaskCreate,
    TaskFilter,
    TaskStatusUpdate,
    TaskUpdate,
)
from app.services.task_service import (
    TaskConflictError,
    TaskNotFoundError,
    TaskService,
)
from app.utils.task_validator import TaskValidationError

pytestmark = pytest.mark.asyncio


# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------
@pytest.fixture
def service() -> TaskService:
    """Builds a `TaskService` with its repository replaced by an `AsyncMock`."""
    svc = TaskService(MagicMock(name="session"))
    svc.repository = AsyncMock(name="repository")
    return svc


def _requester(*, user_id: int = 1, role: str = "admin") -> SimpleNamespace:
    """Builds a minimal requester stand-in.

    Args:
        user_id: The requester's user id.
        role: The requester's role string.

    Returns:
        SimpleNamespace: An object exposing `.id` and `.role`.
    """
    return SimpleNamespace(id=user_id, role=role)


def _task(**overrides) -> Task:
    """Builds an in-memory (non-persisted) `Task` instance for assertions.

    Args:
        **overrides: Column values to override on top of the defaults.

    Returns:
        Task: A `Task` instance with sane defaults, not bound to any
        session.
    """
    defaults = dict(
        id=uuid.uuid4(),
        title="Sample task",
        status=TaskStatus.PENDING,
        is_deleted=False,
        meta_data=None,
        assigned_to_id=None,
    )
    defaults.update(overrides)
    return Task(**defaults)


# ---------------------------------------------------------------------------
# create_task
# ---------------------------------------------------------------------------
class TestCreateTask:
    async def test_creates_task_via_repository(self, service: TaskService) -> None:
        payload = TaskCreate(title="Call lead back")
        service.repository.create.return_value = _task(title="Call lead back")

        result = await service.create_task(payload, requester=_requester())

        service.repository.create.assert_awaited_once()
        called_data = service.repository.create.await_args.args[0]
        assert called_data["title"] == "Call lead back"
        assert "metadata" not in called_data  # translated to meta_data
        assert "meta_data" in called_data
        assert result.title == "Call lead back"

    async def test_rejects_unknown_related_module(self, service: TaskService) -> None:
        payload = TaskCreate(
            title="Follow up", related_module="unknown_module", related_entity_id="1"
        )

        with pytest.raises(TaskValidationError, match="related_module"):
            await service.create_task(payload, requester=_requester())
        service.repository.create.assert_not_awaited()

    async def test_rejects_due_date_far_in_the_past(
        self, service: TaskService
    ) -> None:
        payload = TaskCreate(
            title="Old task",
            due_date=datetime.now(timezone.utc) - timedelta(days=30),
        )

        with pytest.raises(TaskValidationError, match="due_date"):
            await service.create_task(payload, requester=_requester())
        service.repository.create.assert_not_awaited()


# ---------------------------------------------------------------------------
# get_task
# ---------------------------------------------------------------------------
class TestGetTask:
    async def test_returns_task_when_found(self, service: TaskService) -> None:
        task = _task()
        service.repository.get_by_id.return_value = task

        result = await service.get_task(task.id, requester=_requester())

        assert result is task

    async def test_returns_none_when_not_found(self, service: TaskService) -> None:
        service.repository.get_by_id.return_value = None

        result = await service.get_task(uuid.uuid4(), requester=_requester())

        assert result is None


# ---------------------------------------------------------------------------
# update_task
# ---------------------------------------------------------------------------
class TestUpdateTask:
    async def test_raises_not_found_when_missing(self, service: TaskService) -> None:
        service.repository.get_by_id.return_value = None

        with pytest.raises(TaskNotFoundError):
            await service.update_task(
                uuid.uuid4(), TaskUpdate(title="New title"), requester=_requester()
            )

    async def test_applies_only_explicitly_set_fields(
        self, service: TaskService
    ) -> None:
        task = _task()
        service.repository.get_by_id.return_value = task
        service.repository.update.return_value = task

        await service.update_task(
            task.id, TaskUpdate(title="New title"), requester=_requester()
        )

        called_data = service.repository.update.await_args.args[1]
        assert called_data == {"title": "New title"}

    async def test_rejects_due_date_far_in_the_past(
        self, service: TaskService
    ) -> None:
        task = _task()
        service.repository.get_by_id.return_value = task

        with pytest.raises(TaskValidationError, match="due_date"):
            await service.update_task(
                task.id,
                TaskUpdate(due_date=datetime.now(timezone.utc) - timedelta(days=30)),
                requester=_requester(),
            )
        service.repository.update.assert_not_awaited()


# ---------------------------------------------------------------------------
# list_tasks (requester scoping)
# ---------------------------------------------------------------------------
class TestListTasksScoping:
    async def test_agent_without_explicit_assignee_is_scoped_to_self(
        self, service: TaskService
    ) -> None:
        service.repository.list_tasks.return_value = ([], 0)
        task_filter = TaskFilter()

        await service.list_tasks(
            task_filter, requester=_requester(user_id=42, role="agent")
        )

        _args, kwargs = service.repository.list_tasks.await_args
        assert kwargs["assigned_to_id"] == 42

    async def test_agent_explicit_assignee_filter_is_not_overridden(
        self, service: TaskService
    ) -> None:
        service.repository.list_tasks.return_value = ([], 0)
        task_filter = TaskFilter(assigned_to_id=99)

        await service.list_tasks(
            task_filter, requester=_requester(user_id=42, role="agent")
        )

        _args, kwargs = service.repository.list_tasks.await_args
        assert kwargs["assigned_to_id"] == 99

    async def test_manager_is_not_scoped_by_default(
        self, service: TaskService
    ) -> None:
        service.repository.list_tasks.return_value = ([], 0)
        task_filter = TaskFilter()

        await service.list_tasks(
            task_filter, requester=_requester(user_id=42, role="manager")
        )

        _args, kwargs = service.repository.list_tasks.await_args
        assert kwargs["assigned_to_id"] is None


# ---------------------------------------------------------------------------
# search_tasks
# ---------------------------------------------------------------------------
class TestSearchTasks:
    async def test_delegates_to_list_tasks_with_search_filter(
        self, service: TaskService
    ) -> None:
        service.repository.list_tasks.return_value = ([], 0)

        await service.search_tasks("site visit", requester=_requester())

        _args, kwargs = service.repository.list_tasks.await_args
        assert kwargs["search"] == "site visit"

    async def test_rejects_search_term_below_minimum_length(
        self, service: TaskService
    ) -> None:
        with pytest.raises(TaskValidationError, match="search"):
            await service.search_tasks("a", requester=_requester())
        service.repository.list_tasks.assert_not_awaited()


# ---------------------------------------------------------------------------
# get_statistics
# ---------------------------------------------------------------------------
class TestGetStatistics:
    async def test_aggregates_completed_and_cancelled_from_by_status(
        self, service: TaskService
    ) -> None:
        service.repository.get_total_count.return_value = 10
        service.repository.count_by_status.return_value = {
            "completed": 3,
            "cancelled": 2,
            "pending": 5,
        }
        service.repository.count_by_priority.return_value = {}
        service.repository.count_by_type.return_value = {}
        service.repository.count_overdue.return_value = 1

        stats = await service.get_statistics(requester=_requester())

        assert stats.total_tasks == 10
        assert stats.completed_count == 3
        assert stats.cancelled_count == 2
        assert stats.overdue_count == 1

    async def test_rejects_due_from_after_due_to(self, service: TaskService) -> None:
        with pytest.raises(TaskValidationError, match="due_from"):
            await service.get_statistics(
                due_from="2026-06-01T00:00:00+00:00",
                due_to="2026-01-01T00:00:00+00:00",
                requester=_requester(),
            )

    async def test_rejects_malformed_iso_datetime(
        self, service: TaskService
    ) -> None:
        with pytest.raises(TaskValidationError, match="due_from"):
            await service.get_statistics(
                due_from="not-a-date", requester=_requester()
            )


# ---------------------------------------------------------------------------
# assign_task
# ---------------------------------------------------------------------------
class TestAssignTask:
    async def test_raises_not_found_when_missing(self, service: TaskService) -> None:
        service.repository.get_by_id.return_value = None

        with pytest.raises(TaskNotFoundError):
            await service.assign_task(
                uuid.uuid4(), TaskAssign(assigned_to_id=5), requester=_requester()
            )

    async def test_rejects_non_positive_assignee_id(
        self, service: TaskService
    ) -> None:
        task = _task()
        service.repository.get_by_id.return_value = task

        with pytest.raises(TaskValidationError):
            # TaskAssign itself enforces gt=0, so bypass schema
            # validation to exercise the service-level guard directly.
            payload = TaskAssign.model_construct(assigned_to_id=0, note=None)
            await service.assign_task(task.id, payload, requester=_requester())

    async def test_self_assignment_forbidden_for_agent(
        self, service: TaskService
    ) -> None:
        task = _task()
        service.repository.get_by_id.return_value = task

        with pytest.raises(TaskValidationError, match="Self-assignment"):
            await service.assign_task(
                task.id,
                TaskAssign(assigned_to_id=42),
                requester=_requester(user_id=42, role="agent"),
            )
        service.repository.assign.assert_not_awaited()

    async def test_self_assignment_allowed_for_manager(
        self, service: TaskService
    ) -> None:
        task = _task()
        service.repository.get_by_id.return_value = task
        service.repository.assign.return_value = _task(assigned_to_id=42)

        await service.assign_task(
            task.id,
            TaskAssign(assigned_to_id=42),
            requester=_requester(user_id=42, role="manager"),
        )

        service.repository.assign.assert_awaited_once_with(task, 42)

    async def test_assignment_note_is_recorded(self, service: TaskService) -> None:
        task = _task()
        service.repository.get_by_id.return_value = task
        assigned = _task(assigned_to_id=7, meta_data=None)
        service.repository.assign.return_value = assigned

        await service.assign_task(
            task.id,
            TaskAssign(assigned_to_id=7, note="Please prioritize this."),
            requester=_requester(),
        )

        service.repository.update.assert_awaited_once()
        _task_arg, data_arg = service.repository.update.await_args.args
        assert data_arg["meta_data"]["notes"][0]["kind"] == "assignment"
        assert data_arg["meta_data"]["notes"][0]["note"] == "Please prioritize this."


# ---------------------------------------------------------------------------
# update_status
# ---------------------------------------------------------------------------
class TestUpdateStatus:
    async def test_raises_not_found_when_missing(self, service: TaskService) -> None:
        service.repository.get_by_id.return_value = None

        with pytest.raises(TaskNotFoundError):
            await service.update_status(
                uuid.uuid4(),
                TaskStatusUpdate(status=TaskStatus.IN_PROGRESS),
                requester=_requester(),
            )

    async def test_rejects_completed_as_wrong_endpoint(
        self, service: TaskService
    ) -> None:
        task = _task(status=TaskStatus.IN_PROGRESS)
        service.repository.get_by_id.return_value = task

        with pytest.raises(TaskValidationError, match="dedicated"):
            await service.update_status(
                task.id,
                TaskStatusUpdate(status=TaskStatus.COMPLETED),
                requester=_requester(),
            )
        service.repository.set_status.assert_not_awaited()

    async def test_rejects_illegal_transition(self, service: TaskService) -> None:
        task = _task(status=TaskStatus.CANCELLED)
        service.repository.get_by_id.return_value = task

        with pytest.raises(TaskValidationError):
            await service.update_status(
                task.id,
                TaskStatusUpdate(status=TaskStatus.IN_PROGRESS),
                requester=_requester(),
            )

    async def test_applies_legal_transition(self, service: TaskService) -> None:
        task = _task(status=TaskStatus.PENDING)
        service.repository.get_by_id.return_value = task
        service.repository.set_status.return_value = _task(
            status=TaskStatus.IN_PROGRESS
        )

        result = await service.update_status(
            task.id,
            TaskStatusUpdate(status=TaskStatus.IN_PROGRESS),
            requester=_requester(),
        )

        service.repository.set_status.assert_awaited_once_with(
            task, TaskStatus.IN_PROGRESS
        )
        assert result.status == TaskStatus.IN_PROGRESS


# ---------------------------------------------------------------------------
# complete_task / cancel_task
# ---------------------------------------------------------------------------
class TestCompleteAndCancel:
    async def test_complete_raises_when_already_terminal(
        self, service: TaskService
    ) -> None:
        task = _task(status=TaskStatus.CANCELLED)
        service.repository.get_by_id.return_value = task

        with pytest.raises(TaskValidationError):
            await service.complete_task(
                task.id, completed_by_id=1, requester=_requester()
            )

    async def test_complete_delegates_to_repository(
        self, service: TaskService
    ) -> None:
        task = _task(status=TaskStatus.IN_PROGRESS)
        service.repository.get_by_id.return_value = task
        service.repository.complete.return_value = _task(
            status=TaskStatus.COMPLETED
        )

        result = await service.complete_task(
            task.id, completed_by_id=9, requester=_requester()
        )

        service.repository.complete.assert_awaited_once_with(
            task, completed_by_id=9
        )
        assert result.status == TaskStatus.COMPLETED

    async def test_cancel_raises_when_already_completed(
        self, service: TaskService
    ) -> None:
        task = _task(status=TaskStatus.COMPLETED)
        service.repository.get_by_id.return_value = task

        with pytest.raises(TaskValidationError):
            await service.cancel_task(task.id, requester=_requester())

    async def test_cancel_delegates_to_repository(
        self, service: TaskService
    ) -> None:
        task = _task(status=TaskStatus.PENDING)
        service.repository.get_by_id.return_value = task
        service.repository.cancel.return_value = _task(status=TaskStatus.CANCELLED)

        result = await service.cancel_task(task.id, requester=_requester())

        service.repository.cancel.assert_awaited_once_with(task)
        assert result.status == TaskStatus.CANCELLED


# ---------------------------------------------------------------------------
# soft_delete_task / restore_task
# ---------------------------------------------------------------------------
class TestSoftDeleteRestore:
    async def test_soft_delete_raises_not_found_when_missing(
        self, service: TaskService
    ) -> None:
        service.repository.get_by_id.return_value = None

        with pytest.raises(TaskNotFoundError):
            await service.soft_delete_task(uuid.uuid4(), requester=_requester())

    async def test_soft_delete_delegates_to_repository(
        self, service: TaskService
    ) -> None:
        task = _task()
        service.repository.get_by_id.return_value = task
        service.repository.soft_delete.return_value = _task(is_deleted=True)

        result = await service.soft_delete_task(task.id, requester=_requester())

        service.repository.soft_delete.assert_awaited_once_with(task)
        assert result.is_deleted is True

    async def test_restore_raises_not_found_when_no_such_task_at_all(
        self, service: TaskService
    ) -> None:
        service.repository.get_by_id.return_value = None

        with pytest.raises(TaskNotFoundError):
            await service.restore_task(uuid.uuid4(), requester=_requester())

    async def test_restore_raises_validation_error_when_not_deleted(
        self, service: TaskService
    ) -> None:
        task = _task(is_deleted=False)
        service.repository.get_by_id.return_value = task

        with pytest.raises(TaskValidationError, match="not deleted"):
            await service.restore_task(task.id, requester=_requester())

    async def test_restore_delegates_to_repository_when_deleted(
        self, service: TaskService
    ) -> None:
        task = _task(is_deleted=True)
        service.repository.get_by_id.return_value = task
        service.repository.restore.return_value = _task(is_deleted=False)

        result = await service.restore_task(task.id, requester=_requester())

        service.repository.restore.assert_awaited_once_with(task)
        assert result.is_deleted is False


# ---------------------------------------------------------------------------
# bulk_update_tasks / bulk_soft_delete_tasks
# ---------------------------------------------------------------------------
class TestBulkOperations:
    async def test_bulk_update_rejects_empty_id_list(
        self, service: TaskService
    ) -> None:
        with pytest.raises(TaskValidationError, match="task id"):
            await service.bulk_update_tasks(
                [], TaskUpdate(title="X"), requester=_requester()
            )
        service.repository.bulk_update.assert_not_awaited()

    async def test_bulk_update_delegates_with_only_set_fields(
        self, service: TaskService
    ) -> None:
        service.repository.bulk_update.return_value = 2
        ids = [uuid.uuid4(), uuid.uuid4()]

        count = await service.bulk_update_tasks(
            ids, TaskUpdate(title="Batched title"), requester=_requester()
        )

        service.repository.bulk_update.assert_awaited_once_with(
            ids, {"title": "Batched title"}
        )
        assert count == 2

    async def test_bulk_soft_delete_rejects_empty_id_list(
        self, service: TaskService
    ) -> None:
        with pytest.raises(TaskValidationError):
            await service.bulk_soft_delete_tasks([], requester=_requester())
        service.repository.bulk_soft_delete.assert_not_awaited()

    async def test_bulk_soft_delete_delegates_to_repository(
        self, service: TaskService
    ) -> None:
        service.repository.bulk_soft_delete.return_value = 3
        ids = [uuid.uuid4(), uuid.uuid4(), uuid.uuid4()]

        count = await service.bulk_soft_delete_tasks(ids, requester=_requester())

        service.repository.bulk_soft_delete.assert_awaited_once_with(ids)
        assert count == 3