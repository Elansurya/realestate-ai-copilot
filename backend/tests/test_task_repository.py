"""
backend/tests/test_task_repository.py

Repository-layer tests for the Task Management module.

Scope:
    Exercises `app.repositories.task_repository.TaskRepository`
    directly against a real, isolated `AsyncSession`. No mocking of
    SQLAlchemy is performed here -- these tests verify the actual SQL
    that gets generated and executed (filters, sorting, pagination,
    aggregation) behaves as documented in
    `TaskRepository`'s own docstrings. Business-rule validation
    (`app.utils.task_validator`) and orchestration
    (`app.services.task_service.TaskService`) are covered separately
    in `test_task_service.py`, not here -- this file assumes whatever
    is handed to the repository is already well-formed.

Assumed test fixtures (from `tests/conftest.py`, not part of this
module -- mirrors the existing pattern used by e.g.
`tests/test_activity_repository.py`):

    * ``db_session`` -- function-scoped `AsyncSession` fixture bound
      to the test database, wrapped in an outer transaction that is
      rolled back after every test so tests never leak state into one
      another. Table DDL (including the `tasks` table and its enum
      types created by
      `alembic/versions/20260803_0003_task_management_module.py`) is
      assumed already applied to the test database by the project's
      existing migration-bootstrap fixture.
    * ``create_user_id`` -- async factory fixture:
      ``async def create_user_id(**overrides) -> int`` that inserts a
      minimal row into the pre-existing `users` table and returns its
      id. Needed because `assigned_to_id` / `created_by_id` /
      `completed_by_id` are real foreign keys to `users.id`.

If your project's actual fixture names differ, update the fixture
parameters below to match -- the test bodies themselves do not
otherwise depend on fixture plumbing.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone
from typing import Any

import pytest

from app.models.task import Task, TaskPriority, TaskStatus, TaskType
from app.repositories.task_repository import TaskRepository

pytestmark = pytest.mark.asyncio


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _task_data(**overrides: Any) -> dict[str, Any]:
    """Builds a minimal, valid `Task` column mapping for `repository.create`.

    Args:
        **overrides: Column values to override on top of the defaults.

    Returns:
        dict[str, Any]: The merged column mapping.
    """
    data: dict[str, Any] = {
        "title": "Follow up with prospective buyer",
        "description": "Call regarding unit 4B viewing.",
        "task_type": TaskType.CALL,
        "status": TaskStatus.PENDING,
        "priority": TaskPriority.NORMAL,
    }
    data.update(overrides)
    return data


async def _create_task(repository: TaskRepository, **overrides: Any) -> Task:
    """Creates and returns a task via `repository.create`.

    Args:
        repository: The repository under test.
        **overrides: Column values to override on top of the defaults.

    Returns:
        Task: The newly created, persisted task.
    """
    return await repository.create(_task_data(**overrides))


# ---------------------------------------------------------------------------
# Create / get_by_id
# ---------------------------------------------------------------------------
class TestCreateAndGetById:
    """Tests for `TaskRepository.create` and `TaskRepository.get_by_id`."""

    async def test_create_persists_and_returns_refreshed_instance(
        self, db_session
    ) -> None:
        repository = TaskRepository(db_session)
        task = await _create_task(repository, title="Send contract draft")

        assert task.id is not None
        assert task.title == "Send contract draft"
        assert task.status == TaskStatus.PENDING
        assert task.priority == TaskPriority.NORMAL
        assert task.comments_count == 0
        assert task.attachments_count == 0
        assert task.is_deleted is False
        assert task.created_at is not None
        assert task.updated_at is not None

    async def test_get_by_id_returns_matching_task(self, db_session) -> None:
        repository = TaskRepository(db_session)
        created = await _create_task(repository)

        fetched = await repository.get_by_id(created.id)

        assert fetched is not None
        assert fetched.id == created.id

    async def test_get_by_id_returns_none_for_unknown_id(self, db_session) -> None:
        repository = TaskRepository(db_session)

        fetched = await repository.get_by_id(uuid.uuid4())

        assert fetched is None

    async def test_get_by_id_excludes_soft_deleted_by_default(
        self, db_session
    ) -> None:
        repository = TaskRepository(db_session)
        created = await _create_task(repository)
        await repository.soft_delete(created)

        assert await repository.get_by_id(created.id) is None
        assert (
            await repository.get_by_id(created.id, include_deleted=True)
        ) is not None


# ---------------------------------------------------------------------------
# Update
# ---------------------------------------------------------------------------
class TestUpdate:
    """Tests for `TaskRepository.update`."""

    async def test_update_applies_partial_column_changes(self, db_session) -> None:
        repository = TaskRepository(db_session)
        task = await _create_task(repository, title="Original title")

        updated = await repository.update(
            task, {"title": "Updated title", "priority": TaskPriority.URGENT}
        )

        assert updated.title == "Updated title"
        assert updated.priority == TaskPriority.URGENT
        # Untouched columns are preserved.
        assert updated.task_type == TaskType.CALL


# ---------------------------------------------------------------------------
# Assignment
# ---------------------------------------------------------------------------
class TestAssign:
    """Tests for `TaskRepository.assign`."""

    async def test_assign_sets_assignee(
        self, db_session, create_user_id
    ) -> None:
        repository = TaskRepository(db_session)
        task = await _create_task(repository)
        user_id = await create_user_id()

        assigned = await repository.assign(task, user_id)

        assert assigned.assigned_to_id == user_id

    async def test_assign_none_clears_assignee(
        self, db_session, create_user_id
    ) -> None:
        repository = TaskRepository(db_session)
        user_id = await create_user_id()
        task = await _create_task(repository, assigned_to_id=user_id)

        cleared = await repository.assign(task, None)

        assert cleared.assigned_to_id is None


# ---------------------------------------------------------------------------
# Lifecycle transitions
# ---------------------------------------------------------------------------
class TestLifecycleTransitions:
    """Tests for `set_status`, `complete`, and `cancel`."""

    async def test_set_status_updates_status_column(self, db_session) -> None:
        repository = TaskRepository(db_session)
        task = await _create_task(repository)

        updated = await repository.set_status(task, TaskStatus.IN_PROGRESS)

        assert updated.status == TaskStatus.IN_PROGRESS

    async def test_complete_sets_status_timestamp_and_completer(
        self, db_session, create_user_id
    ) -> None:
        repository = TaskRepository(db_session)
        task = await _create_task(repository, status=TaskStatus.IN_PROGRESS)
        user_id = await create_user_id()

        completed = await repository.complete(task, completed_by_id=user_id)

        assert completed.status == TaskStatus.COMPLETED
        assert completed.completed_at is not None
        assert completed.completed_by_id == user_id

    async def test_cancel_clears_completion_fields(self, db_session) -> None:
        repository = TaskRepository(db_session)
        task = await _create_task(repository, status=TaskStatus.IN_PROGRESS)

        cancelled = await repository.cancel(task)

        assert cancelled.status == TaskStatus.CANCELLED
        assert cancelled.completed_at is None
        assert cancelled.completed_by_id is None


# ---------------------------------------------------------------------------
# Soft delete / restore
# ---------------------------------------------------------------------------
class TestSoftDeleteRestore:
    """Tests for `soft_delete` and `restore`."""

    async def test_soft_delete_sets_flag_and_timestamp(self, db_session) -> None:
        repository = TaskRepository(db_session)
        task = await _create_task(repository)

        deleted = await repository.soft_delete(task)

        assert deleted.is_deleted is True
        assert deleted.deleted_at is not None

    async def test_restore_clears_flag_and_timestamp(self, db_session) -> None:
        repository = TaskRepository(db_session)
        task = await _create_task(repository)
        await repository.soft_delete(task)

        restored = await repository.restore(task)

        assert restored.is_deleted is False
        assert restored.deleted_at is None


# ---------------------------------------------------------------------------
# Bulk operations
# ---------------------------------------------------------------------------
class TestBulkOperations:
    """Tests for `bulk_update` and `bulk_soft_delete`."""

    async def test_bulk_update_applies_to_all_matched_rows(
        self, db_session
    ) -> None:
        repository = TaskRepository(db_session)
        first = await _create_task(repository, priority=TaskPriority.LOW)
        second = await _create_task(repository, priority=TaskPriority.LOW)

        affected = await repository.bulk_update(
            [first.id, second.id], {"priority": TaskPriority.HIGH}
        )

        assert affected == 2
        assert (await repository.get_by_id(first.id)).priority == TaskPriority.HIGH
        assert (await repository.get_by_id(second.id)).priority == TaskPriority.HIGH

    async def test_bulk_update_with_empty_ids_is_a_noop(self, db_session) -> None:
        repository = TaskRepository(db_session)

        affected = await repository.bulk_update([], {"priority": TaskPriority.HIGH})

        assert affected == 0

    async def test_bulk_soft_delete_marks_all_matched_rows(
        self, db_session
    ) -> None:
        repository = TaskRepository(db_session)
        first = await _create_task(repository)
        second = await _create_task(repository)

        affected = await repository.bulk_soft_delete([first.id, second.id])

        assert affected == 2
        assert await repository.get_by_id(first.id) is None
        assert await repository.get_by_id(second.id) is None


# ---------------------------------------------------------------------------
# Listing / filtering / sorting / pagination
# ---------------------------------------------------------------------------
class TestListTasks:
    """Tests for `list_tasks` and its filter predicates."""

    async def test_filters_by_status(self, db_session) -> None:
        repository = TaskRepository(db_session)
        await _create_task(repository, status=TaskStatus.PENDING)
        await _create_task(repository, status=TaskStatus.COMPLETED)

        items, total = await repository.list_tasks(status=TaskStatus.PENDING.value)

        assert total == 1
        assert all(item.status == TaskStatus.PENDING for item in items)

    async def test_filters_by_priority_and_task_type(self, db_session) -> None:
        repository = TaskRepository(db_session)
        await _create_task(
            repository, priority=TaskPriority.URGENT, task_type=TaskType.EMAIL
        )
        await _create_task(
            repository, priority=TaskPriority.LOW, task_type=TaskType.CALL
        )

        items, total = await repository.list_tasks(
            priority=TaskPriority.URGENT.value, task_type=TaskType.EMAIL.value
        )

        assert total == 1
        assert items[0].priority == TaskPriority.URGENT
        assert items[0].task_type == TaskType.EMAIL

    async def test_filters_by_assigned_and_created_by(
        self, db_session, create_user_id
    ) -> None:
        repository = TaskRepository(db_session)
        assignee_id = await create_user_id()
        creator_id = await create_user_id()
        await _create_task(
            repository, assigned_to_id=assignee_id, created_by_id=creator_id
        )
        await _create_task(repository)

        items, total = await repository.list_tasks(assigned_to_id=assignee_id)

        assert total == 1
        assert items[0].assigned_to_id == assignee_id

    async def test_filters_by_related_module_and_entity(self, db_session) -> None:
        repository = TaskRepository(db_session)
        await _create_task(
            repository, related_module="lead", related_entity_id="lead-123"
        )
        await _create_task(
            repository, related_module="booking", related_entity_id="booking-9"
        )

        items, total = await repository.list_tasks(
            related_module="lead", related_entity_id="lead-123"
        )

        assert total == 1
        assert items[0].related_module == "lead"

    async def test_search_matches_title_or_description_case_insensitively(
        self, db_session
    ) -> None:
        repository = TaskRepository(db_session)
        await _create_task(repository, title="Schedule SITE VISIT", description="")
        await _create_task(repository, title="Unrelated", description="")

        items, total = await repository.list_tasks(search="site visit")

        assert total == 1
        assert "SITE VISIT" in items[0].title

    async def test_due_date_range_filters_are_inclusive(self, db_session) -> None:
        repository = TaskRepository(db_session)
        now = datetime.now(timezone.utc)
        in_range = await _create_task(repository, due_date=now)
        await _create_task(repository, due_date=now + timedelta(days=10))

        items, total = await repository.list_tasks(
            due_from=now - timedelta(minutes=1), due_to=now + timedelta(minutes=1)
        )

        assert total == 1
        assert items[0].id == in_range.id

    async def test_only_overdue_excludes_terminal_and_future_tasks(
        self, db_session
    ) -> None:
        repository = TaskRepository(db_session)
        past = datetime.now(timezone.utc) - timedelta(days=1)
        future = datetime.now(timezone.utc) + timedelta(days=1)
        overdue = await _create_task(
            repository, due_date=past, status=TaskStatus.PENDING
        )
        await _create_task(repository, due_date=future, status=TaskStatus.PENDING)
        await _create_task(
            repository, due_date=past, status=TaskStatus.COMPLETED
        )

        items, total = await repository.list_tasks(only_overdue=True)

        assert total == 1
        assert items[0].id == overdue.id

    async def test_soft_deleted_excluded_unless_include_deleted(
        self, db_session
    ) -> None:
        repository = TaskRepository(db_session)
        task = await _create_task(repository)
        await repository.soft_delete(task)

        _items, total_default = await repository.list_tasks()
        _items, total_included = await repository.list_tasks(include_deleted=True)

        assert total_default == 0
        assert total_included == 1

    async def test_pagination_limits_page_size_and_offsets(
        self, db_session
    ) -> None:
        repository = TaskRepository(db_session)
        for i in range(5):
            await _create_task(repository, title=f"Task {i}")

        page_one, total = await repository.list_tasks(
            page=1, page_size=2, sort_by="title", sort_order="asc"
        )
        page_two, _total = await repository.list_tasks(
            page=2, page_size=2, sort_by="title", sort_order="asc"
        )

        assert total == 5
        assert len(page_one) == 2
        assert len(page_two) == 2
        assert page_one[0].id != page_two[0].id

    async def test_sort_order_desc_reverses_default_ascending_order(
        self, db_session
    ) -> None:
        repository = TaskRepository(db_session)
        await _create_task(repository, title="Alpha")
        await _create_task(repository, title="Zulu")

        ascending, _ = await repository.list_tasks(sort_by="title", sort_order="asc")
        descending, _ = await repository.list_tasks(
            sort_by="title", sort_order="desc"
        )

        assert [t.title for t in ascending] == list(
            reversed([t.title for t in descending])
        )

    async def test_page_and_page_size_are_clamped_to_minimum_one(
        self, db_session
    ) -> None:
        repository = TaskRepository(db_session)
        await _create_task(repository)

        items, total = await repository.list_tasks(page=0, page_size=0)

        assert total == 1
        assert len(items) == 1


# ---------------------------------------------------------------------------
# search_tasks / get_recent_tasks / get_due_reminders
# ---------------------------------------------------------------------------
class TestSearchRecentReminders:
    """Tests for `search_tasks`, `get_recent_tasks`, and `get_due_reminders`."""

    async def test_search_tasks_delegates_to_list_tasks_with_search_kwarg(
        self, db_session
    ) -> None:
        repository = TaskRepository(db_session)
        await _create_task(repository, title="Payment reminder call")
        await _create_task(repository, title="Unrelated")

        items, total = await repository.search_tasks("payment reminder")

        assert total == 1
        assert "Payment reminder" in items[0].title

    async def test_get_recent_tasks_orders_newest_first(self, db_session) -> None:
        repository = TaskRepository(db_session)
        first = await _create_task(repository, title="First")
        second = await _create_task(repository, title="Second")

        recent = await repository.get_recent_tasks(limit=10)

        ids_in_order = [t.id for t in recent]
        assert ids_in_order.index(second.id) < ids_in_order.index(first.id)

    async def test_get_due_reminders_returns_only_elapsed_non_terminal_tasks(
        self, db_session
    ) -> None:
        repository = TaskRepository(db_session)
        past = datetime.now(timezone.utc) - timedelta(minutes=5)
        future = datetime.now(timezone.utc) + timedelta(minutes=5)
        due = await _create_task(repository, reminder_time=past)
        await _create_task(repository, reminder_time=future)
        await _create_task(
            repository, reminder_time=past, status=TaskStatus.COMPLETED
        )

        reminders = await repository.get_due_reminders()

        reminder_ids = {t.id for t in reminders}
        assert due.id in reminder_ids
        assert len(reminder_ids) == 1


# ---------------------------------------------------------------------------
# Statistics / aggregations
# ---------------------------------------------------------------------------
class TestStatisticsAggregations:
    """Tests for the `count_*`/`get_total_count` aggregation methods."""

    async def test_get_total_count_matches_filtered_row_count(
        self, db_session
    ) -> None:
        repository = TaskRepository(db_session)
        await _create_task(repository, priority=TaskPriority.HIGH)
        await _create_task(repository, priority=TaskPriority.LOW)

        total = await repository.get_total_count(priority=TaskPriority.HIGH.value)

        assert total == 1

    async def test_count_overdue_counts_only_non_terminal_past_due_tasks(
        self, db_session
    ) -> None:
        repository = TaskRepository(db_session)
        past = datetime.now(timezone.utc) - timedelta(days=1)
        await _create_task(repository, due_date=past, status=TaskStatus.PENDING)
        await _create_task(repository, due_date=past, status=TaskStatus.CANCELLED)

        count = await repository.count_overdue()

        assert count == 1

    async def test_count_by_status_groups_correctly(self, db_session) -> None:
        repository = TaskRepository(db_session)
        await _create_task(repository, status=TaskStatus.PENDING)
        await _create_task(repository, status=TaskStatus.PENDING)
        await _create_task(repository, status=TaskStatus.COMPLETED)

        counts = await repository.count_by_status()

        assert counts.get(TaskStatus.PENDING.value) == 2
        assert counts.get(TaskStatus.COMPLETED.value) == 1

    async def test_count_by_priority_groups_correctly(self, db_session) -> None:
        repository = TaskRepository(db_session)
        await _create_task(repository, priority=TaskPriority.URGENT)
        await _create_task(repository, priority=TaskPriority.URGENT)

        counts = await repository.count_by_priority()

        assert counts.get(TaskPriority.URGENT.value) == 2

    async def test_count_by_type_groups_correctly(self, db_session) -> None:
        repository = TaskRepository(db_session)
        await _create_task(repository, task_type=TaskType.MEETING)

        counts = await repository.count_by_type()

        assert counts.get(TaskType.MEETING.value) == 1

    async def test_count_by_assignee_excludes_unassigned_and_orders_desc(
        self, db_session, create_user_id
    ) -> None:
        repository = TaskRepository(db_session)
        busy_user = await create_user_id()
        await _create_task(repository, assigned_to_id=busy_user)
        await _create_task(repository, assigned_to_id=busy_user)
        await _create_task(repository)  # unassigned

        counts = await repository.count_by_assignee()

        assert counts.get(busy_user) == 2
        assert None not in counts