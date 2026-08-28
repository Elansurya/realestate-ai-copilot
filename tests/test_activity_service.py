"""
backend/tests/test_activity_service.py

Test suite for ``app.services.activity_service.ActivityService``.

The repository dependency is mocked with ``unittest.mock.AsyncMock``
so these tests exercise only the service's own responsibilities:
translating repository results into API-facing shapes, raising
``NotFoundException`` for missing/soft-deleted entries, applying
default actor/priority/status business rules, and delegating
filter/sort/pagination parameters to the repository unchanged.

Mirrors the existing project test conventions used for other
service-layer suites (mock repository injected via constructor,
``pytest.mark.asyncio`` for coroutine tests).
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.core.exceptions import NotFoundException
from app.models.activity import (
    Activity,
    ActivityModule,
    ActivityPriority,
    ActivityStatus,
    ActivityType,
)
from app.repositories.activity_repository import ActivityRepository
from app.schemas.activity import ActivityCreate, ActivityFilter, ActivityUpdate
from app.services.activity_service import ActivityService

pytestmark = pytest.mark.asyncio


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def make_activity(**overrides) -> Activity:
    """Builds an in-memory ``Activity`` instance for mocked repository returns.

    Args:
        **overrides: Field values to override the defaults with.

    Returns:
        Activity: A fully-populated, unpersisted ORM instance.
    """
    defaults = dict(
        id=uuid.uuid4(),
        module=ActivityModule.BOOKING,
        entity_type="Booking",
        entity_id=str(uuid.uuid4()),
        action=ActivityType.CREATED,
        title="Booking created",
        description=None,
        old_value=None,
        new_value=None,
        meta_data=None,
        priority=ActivityPriority.NORMAL,
        status=ActivityStatus.ACTIVE,
        performed_by_id=None,
        assigned_to_id=None,
        ip_address=None,
        user_agent=None,
        source="system",
        is_deleted=False,
        deleted_at=None,
        created_at=datetime.now(timezone.utc),
        updated_at=datetime.now(timezone.utc),
    )
    defaults.update(overrides)
    return Activity(**defaults)


@pytest.fixture
def mock_repo() -> AsyncMock:
    """Provides an ``ActivityRepository``-shaped async mock.

    Returns:
        AsyncMock: A mock with ``spec=ActivityRepository`` so unknown
        attribute access fails fast if the service interface drifts.
        ``ActivityRepository.session`` (app/repositories/
        activity_repository.py) is a plain instance attribute set in
        ``__init__``, not a class-level attribute, so it is invisible
        to ``dir(ActivityRepository)`` and therefore not created
        automatically by ``spec=``. ``ActivityService._validate_user_
        exists`` (app/services/activity_service.py) reaches through
        ``self.repository.session.execute(...)`` to check a referenced
        user exists, so ``session`` is wired up here by hand with an
        ``execute().scalar_one_or_none()`` that resolves truthy (as if
        the referenced user exists) by default, so tests that don't
        care about that validation path aren't tripped up by it.
    """
    repo = AsyncMock(spec=ActivityRepository)
    session = AsyncMock()
    execute_result = MagicMock()
    execute_result.scalar_one_or_none.return_value = 1
    session.execute.return_value = execute_result
    repo.session = session
    return repo


@pytest.fixture
def service(mock_repo: AsyncMock) -> ActivityService:
    """Builds an ``ActivityService`` wired to the mocked repository.

    Args:
        mock_repo: The mocked repository fixture.

    Returns:
        ActivityService: The service under test.
    """
    return ActivityService(mock_repo)


# ---------------------------------------------------------------------------
# create_activity
# ---------------------------------------------------------------------------
class TestCreateActivity:
    async def test_creates_with_provided_fields(self, service, mock_repo):
        payload = ActivityCreate(
            module=ActivityModule.LEAD,
            entity_type="Lead",
            entity_id="lead-123",
            action=ActivityType.CREATED,
            title="New lead captured",
        )
        mock_repo.create.return_value = make_activity(
            module=ActivityModule.LEAD, entity_type="Lead", entity_id="lead-123"
        )

        result = await service.create_activity(payload, actor_id=7)

        mock_repo.create.assert_awaited_once()
        assert result.entity_type == "Lead"

    async def test_defaults_performed_by_to_actor_when_unset(
        self, service, mock_repo
    ):
        payload = ActivityCreate(
            module=ActivityModule.LEAD,
            entity_type="Lead",
            entity_id="lead-123",
            action=ActivityType.CREATED,
            title="New lead captured",
        )
        mock_repo.create.return_value = make_activity(performed_by_id=7)

        await service.create_activity(payload, actor_id=7)

        call_kwargs = mock_repo.create.await_args.args[0]
        assert call_kwargs.get("performed_by_id") == 7

    async def test_respects_explicit_performed_by_over_actor(
        self, service, mock_repo
    ):
        payload = ActivityCreate(
            module=ActivityModule.LEAD,
            entity_type="Lead",
            entity_id="lead-123",
            action=ActivityType.CREATED,
            title="New lead captured",
            performed_by_id=42,
        )
        mock_repo.create.return_value = make_activity(performed_by_id=42)

        await service.create_activity(payload, actor_id=7)

        call_kwargs = mock_repo.create.await_args.args[0]
        assert call_kwargs.get("performed_by_id") == 42


# ---------------------------------------------------------------------------
# get_activity
# ---------------------------------------------------------------------------
class TestGetActivity:
    async def test_returns_activity_when_found(self, service, mock_repo):
        activity = make_activity()
        mock_repo.get_by_id.return_value = activity

        result = await service.get_activity(activity.id)

        assert result.id == activity.id
        # `ActivityService.get_activity` calls
        # `self.repository.get_by_id(activity_id, include_deleted=
        # include_deleted)` -- always passing `include_deleted`
        # explicitly (default `False`) -- not just the bare id.
        mock_repo.get_by_id.assert_awaited_once_with(
            activity.id, include_deleted=False
        )

    async def test_raises_not_found_when_missing(self, service, mock_repo):
        mock_repo.get_by_id.return_value = None

        with pytest.raises(NotFoundException):
            await service.get_activity(uuid.uuid4())


# ---------------------------------------------------------------------------
# update_activity
# ---------------------------------------------------------------------------
class TestUpdateActivity:
    async def test_updates_existing_entry(self, service, mock_repo):
        activity = make_activity()
        mock_repo.get_by_id.return_value = activity
        mock_repo.update.return_value = make_activity(
            id=activity.id, title="Revised title"
        )

        result = await service.update_activity(
            activity.id, ActivityUpdate(title="Revised title")
        )

        assert result.title == "Revised title"
        mock_repo.update.assert_awaited_once()

    async def test_raises_not_found_for_missing_entry(self, service, mock_repo):
        mock_repo.get_by_id.return_value = None

        with pytest.raises(NotFoundException):
            await service.update_activity(
                uuid.uuid4(), ActivityUpdate(title="Doesn't matter")
            )

    async def test_only_supplied_fields_are_passed_through(self, service, mock_repo):
        activity = make_activity()
        mock_repo.get_by_id.return_value = activity
        mock_repo.update.return_value = activity

        await service.update_activity(
            activity.id, ActivityUpdate(status=ActivityStatus.COMPLETED)
        )

        update_kwargs = mock_repo.update.await_args.args[1]
        assert "status" in update_kwargs
        assert "title" not in update_kwargs


# ---------------------------------------------------------------------------
# delete_activity / restore_activity
# ---------------------------------------------------------------------------
class TestDeleteAndRestore:
    async def test_delete_activity_soft_deletes_existing_entry(
        self, service, mock_repo
    ):
        activity = make_activity()
        mock_repo.get_by_id.return_value = activity
        mock_repo.soft_delete.return_value = make_activity(
            id=activity.id, is_deleted=True, deleted_at=datetime.now(timezone.utc)
        )

        result = await service.delete_activity(activity.id)

        assert result.is_deleted is True
        mock_repo.soft_delete.assert_awaited_once()

    async def test_delete_activity_raises_not_found_for_missing_entry(
        self, service, mock_repo
    ):
        mock_repo.get_by_id.return_value = None

        with pytest.raises(NotFoundException):
            await service.delete_activity(uuid.uuid4())

    async def test_restore_activity_restores_soft_deleted_entry(
        self, service, mock_repo
    ):
        activity = make_activity(is_deleted=True, deleted_at=datetime.now(timezone.utc))
        mock_repo.get_by_id.return_value = activity
        mock_repo.restore.return_value = make_activity(
            id=activity.id, is_deleted=False, deleted_at=None
        )

        result = await service.restore_activity(activity.id)

        assert result.is_deleted is False
        assert result.deleted_at is None

    async def test_restore_activity_raises_not_found_when_missing(
        self, service, mock_repo
    ):
        mock_repo.get_by_id.return_value = None

        with pytest.raises(NotFoundException):
            await service.restore_activity(uuid.uuid4())


# ---------------------------------------------------------------------------
# list_activities
# ---------------------------------------------------------------------------
class TestListActivities:
    async def test_delegates_filters_to_repository(self, service, mock_repo):
        mock_repo.list_activities.return_value = ([], 0)
        filters = ActivityFilter(
            module=ActivityModule.PAYMENT,
            search="invoice",
            page=2,
            page_size=10,
            sort_by="title",
            sort_order="asc",
        )

        await service.list_activities(filters)

        _, kwargs = mock_repo.list_activities.await_args
        assert kwargs.get("module") == ActivityModule.PAYMENT.value or kwargs.get(
            "module"
        ) == ActivityModule.PAYMENT
        assert kwargs.get("search") == "invoice"
        assert kwargs.get("page") == 2
        assert kwargs.get("page_size") == 10
        assert kwargs.get("sort_by") == "title"
        assert kwargs.get("sort_order") == "asc"

    async def test_returns_items_and_total(self, service, mock_repo):
        activities = [make_activity(), make_activity()]
        mock_repo.list_activities.return_value = (activities, 2)

        # `ActivityService.list_activities` (unlike the repository
        # method it wraps) returns an already-assembled
        # `ActivityListResponse`, not a raw `(items, total)` tuple.
        result = await service.list_activities(ActivityFilter())

        assert len(result.items) == 2
        assert result.total == 2


# ---------------------------------------------------------------------------
# Timeline delegation
# ---------------------------------------------------------------------------
class TestTimelineDelegation:
    async def test_get_entity_timeline_builds_timeline_response(
        self, service, mock_repo
    ):
        entity_id = str(uuid.uuid4())
        activities = [make_activity(entity_type="Booking", entity_id=entity_id)]
        mock_repo.get_timeline_by_entity.return_value = (activities, 1)

        result = await service.get_entity_timeline(
            entity_type="Booking",
            entity_id=entity_id,
            page=1,
            page_size=200,
            sort_order="asc",
        )

        assert result.entity_type == "Booking"
        assert result.entity_id == entity_id
        assert result.total_count == 1

    async def test_get_module_timeline_delegates_to_repository(
        self, service, mock_repo
    ):
        mock_repo.get_timeline_by_module.return_value = ([], 0)

        await service.get_module_timeline(
            module=ActivityModule.WORKFLOW.value,
            page=1,
            page_size=50,
            sort_order="desc",
        )

        mock_repo.get_timeline_by_module.assert_awaited_once()

    async def test_get_user_timeline_delegates_to_repository(
        self, service, mock_repo
    ):
        mock_repo.get_timeline_by_user.return_value = ([], 0)

        await service.get_user_timeline(
            user_id=99, page=1, page_size=50, sort_order="desc"
        )

        mock_repo.get_timeline_by_user.assert_awaited_once()


# ---------------------------------------------------------------------------
# Statistics
# ---------------------------------------------------------------------------
class TestStatistics:
    async def test_get_statistics_aggregates_repository_counts(
        self, service, mock_repo
    ):
        mock_repo.count_by_module.return_value = {"booking": 3}
        mock_repo.count_by_action.return_value = {"created": 3}
        mock_repo.count_by_priority.return_value = {"normal": 3}
        mock_repo.count_by_status.return_value = {"active": 3}
        mock_repo.get_total_count.return_value = 3

        result = await service.get_statistics(date_from=None, date_to=None)

        assert result.total_activities == 3
        assert result.by_module.get("booking") == 3
        assert result.by_action.get("created") == 3
        assert result.by_priority.get("normal") == 3
        assert result.by_status.get("active") == 3