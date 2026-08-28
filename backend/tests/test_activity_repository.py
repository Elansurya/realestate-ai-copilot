"""
backend/tests/test_activity_repository.py

Test suite for ``app.repositories.activity_repository.ActivityRepository``.

Mirrors the existing project test conventions: pytest + pytest-asyncio,
an injected ``db_session`` fixture (async SQLAlchemy session, provided
by the project's shared ``conftest.py`` against the test database),
and helper factories for building valid ``Activity`` rows from the
fields defined in ``app/models/activity.py``.

These tests exercise the repository in isolation from
``ActivityService`` -- no domain validation or exception translation
is expected here, only persistence and querying behavior.
"""

from __future__ import annotations

import itertools
import uuid
from datetime import datetime, timedelta, timezone

import pytest

from app.models.activity import (
    Activity,
    ActivityModule,
    ActivityPriority,
    ActivityStatus,
    ActivityType,
)
from app.models.user import User, UserRole
from app.repositories.activity_repository import ActivityRepository

pytestmark = pytest.mark.asyncio


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
_TEST_USER_COUNTER = itertools.count(1)


async def make_user(db_session, **overrides) -> User:
    """Persists and returns a valid ``User`` row (app/models/user.py).

    ``activities.performed_by_id`` / ``activities.assigned_to_id`` are
    real ``FOREIGN KEY``s into ``users.id`` (``ondelete="SET NULL"``),
    so any activity row that references a user id must reference an
    id that actually exists, or the insert raises
    ``ForeignKeyViolationError``. This builds a minimal, valid row
    satisfying every ``NOT NULL``/``UNIQUE`` column on the real
    ``User`` model (``uuid``, ``email``, ``phone``, ``password_hash``)
    with unique values per call, so tests can create as many
    performer/assignee users as they need without collisions.

    Args:
        db_session: The active test-transaction-scoped async session.
        **overrides: Field values to override the defaults with.

    Returns:
        User: The persisted, refreshed ``User`` instance.
    """
    n = next(_TEST_USER_COUNTER)
    defaults = dict(
        uuid=str(uuid.uuid4()),
        full_name=f"Test User {n}",
        email=f"activity-repo-test-user-{n}@example.com",
        phone=f"9{n:09d}",
        password_hash="not-a-real-hash-$2b$12$test.value.only",
        role=UserRole.SALES_AGENT,
        is_active=True,
        is_verified=True,
    )
    defaults.update(overrides)
    user = User(**defaults)
    db_session.add(user)
    await db_session.flush()
    await db_session.refresh(user)
    return user


def make_activity_data(**overrides) -> dict:
    """Builds a valid ``activities`` row payload with sensible defaults.

    Args:
        **overrides: Field values to override the defaults with.

    Returns:
        dict: A payload suitable for ``ActivityRepository.create``.
    """
    data = {
        "module": ActivityModule.BOOKING,
        "entity_type": "Booking",
        "entity_id": str(uuid.uuid4()),
        "action": ActivityType.CREATED,
        "title": "Booking created",
        "description": "A new booking was created for the property.",
        "priority": ActivityPriority.NORMAL,
        "status": ActivityStatus.ACTIVE,
        "source": "web",
    }
    data.update(overrides)
    return data


# ---------------------------------------------------------------------------
# CRUD
# ---------------------------------------------------------------------------
class TestCreate:
    async def test_create_persists_and_returns_entry(self, db_session):
        repo = ActivityRepository(db_session)
        entry = await repo.create(make_activity_data(title="Lead assigned to agent"))

        assert entry.id is not None
        assert entry.title == "Lead assigned to agent"
        assert entry.is_deleted is False
        assert entry.created_at is not None
        assert entry.priority == ActivityPriority.NORMAL
        assert entry.status == ActivityStatus.ACTIVE

    async def test_create_defaults_priority_and_status(self, db_session):
        repo = ActivityRepository(db_session)
        data = make_activity_data()
        data.pop("priority")
        data.pop("status")
        entry = await repo.create(data)

        assert entry.priority == ActivityPriority.NORMAL
        assert entry.status == ActivityStatus.ACTIVE

    async def test_bulk_create_persists_all_rows_in_order(self, db_session):
        repo = ActivityRepository(db_session)
        rows = [
            make_activity_data(title="Row one"),
            make_activity_data(title="Row two"),
            make_activity_data(title="Row three"),
        ]
        entries = await repo.bulk_create(rows)

        assert [entry.title for entry in entries] == [
            "Row one",
            "Row two",
            "Row three",
        ]
        assert all(entry.id is not None for entry in entries)


class TestGetById:
    async def test_get_by_id_returns_matching_entry(self, db_session):
        repo = ActivityRepository(db_session)
        created = await repo.create(make_activity_data())

        fetched = await repo.get_by_id(created.id)

        assert fetched is not None
        assert fetched.id == created.id

    async def test_get_by_id_returns_none_for_unknown_id(self, db_session):
        repo = ActivityRepository(db_session)
        result = await repo.get_by_id(uuid.uuid4())
        assert result is None

    async def test_get_by_id_excludes_soft_deleted_by_default(self, db_session):
        repo = ActivityRepository(db_session)
        created = await repo.create(make_activity_data())
        await repo.soft_delete(created)

        assert await repo.get_by_id(created.id) is None
        assert await repo.get_by_id(created.id, include_deleted=True) is not None


class TestUpdate:
    async def test_update_applies_partial_field_changes(self, db_session):
        repo = ActivityRepository(db_session)
        created = await repo.create(make_activity_data(title="Original title"))

        updated = await repo.update(created, {"title": "Updated title"})

        assert updated.title == "Updated title"
        assert updated.id == created.id


class TestSoftDeleteAndRestore:
    async def test_soft_delete_sets_flag_and_timestamp(self, db_session):
        repo = ActivityRepository(db_session)
        created = await repo.create(make_activity_data())

        deleted = await repo.soft_delete(created)

        assert deleted.is_deleted is True
        assert deleted.deleted_at is not None

    async def test_restore_clears_flag_and_timestamp(self, db_session):
        repo = ActivityRepository(db_session)
        created = await repo.create(make_activity_data())
        await repo.soft_delete(created)

        restored = await repo.restore(created)

        assert restored.is_deleted is False
        assert restored.deleted_at is None

    async def test_bulk_soft_delete_returns_affected_count(self, db_session):
        repo = ActivityRepository(db_session)
        entries = await repo.bulk_create(
            [make_activity_data(title=f"Bulk {i}") for i in range(3)]
        )
        ids = [entry.id for entry in entries]

        affected = await repo.bulk_soft_delete(ids)

        assert affected == 3
        for entry_id in ids:
            assert await repo.get_by_id(entry_id) is None

    async def test_bulk_soft_delete_with_empty_list_returns_zero(self, db_session):
        repo = ActivityRepository(db_session)
        assert await repo.bulk_soft_delete([]) == 0


# ---------------------------------------------------------------------------
# Listing / filtering / search / sorting / pagination
# ---------------------------------------------------------------------------
class TestListActivities:
    async def test_filters_by_module(self, db_session):
        repo = ActivityRepository(db_session)
        await repo.create(make_activity_data(module=ActivityModule.BOOKING))
        await repo.create(make_activity_data(module=ActivityModule.PAYMENT))

        items, total = await repo.list_activities(module=ActivityModule.PAYMENT.value)

        assert total == 1
        assert items[0].module == ActivityModule.PAYMENT

    async def test_filters_by_entity_type_and_entity_id(self, db_session):
        repo = ActivityRepository(db_session)
        target_id = str(uuid.uuid4())
        await repo.create(
            make_activity_data(entity_type="Booking", entity_id=target_id)
        )
        await repo.create(make_activity_data(entity_type="Booking"))

        items, total = await repo.list_activities(
            entity_type="Booking", entity_id=target_id
        )

        assert total == 1
        assert items[0].entity_id == target_id

    async def test_filters_by_action_priority_status(self, db_session):
        repo = ActivityRepository(db_session)
        await repo.create(
            make_activity_data(
                action=ActivityType.APPROVED,
                priority=ActivityPriority.URGENT,
                status=ActivityStatus.COMPLETED,
            )
        )
        await repo.create(make_activity_data(action=ActivityType.CREATED))

        items, total = await repo.list_activities(
            action=ActivityType.APPROVED.value,
            priority=ActivityPriority.URGENT.value,
            status=ActivityStatus.COMPLETED.value,
        )

        assert total == 1
        assert items[0].action == ActivityType.APPROVED

    async def test_filters_by_performed_by_and_assigned_to(self, db_session):
        repo = ActivityRepository(db_session)
        performer = await make_user(db_session)
        assignee = await make_user(db_session)
        other = await make_user(db_session)
        await repo.create(
            make_activity_data(
                performed_by_id=performer.id, assigned_to_id=assignee.id
            )
        )
        await repo.create(make_activity_data(performed_by_id=other.id))

        items, total = await repo.list_activities(performed_by_id=performer.id)
        assert total == 1
        assert items[0].performed_by_id == performer.id

        items, total = await repo.list_activities(assigned_to_id=assignee.id)
        assert total == 1
        assert items[0].assigned_to_id == assignee.id

    async def test_search_matches_title_or_description_case_insensitively(
        self, db_session
    ):
        repo = ActivityRepository(db_session)
        await repo.create(
            make_activity_data(title="Payment RECEIVED for invoice 1042")
        )
        await repo.create(
            make_activity_data(
                title="Unrelated entry", description="mentions invoice 1042 too"
            )
        )
        await repo.create(make_activity_data(title="Completely different"))

        items, total = await repo.list_activities(search="invoice 1042")

        assert total == 2

    async def test_date_range_filters_bound_created_at(self, db_session):
        repo = ActivityRepository(db_session)
        await repo.create(make_activity_data(title="In range"))

        now = datetime.now(timezone.utc)
        items, total = await repo.list_activities(
            date_from=now - timedelta(days=1), date_to=now + timedelta(days=1)
        )
        assert total >= 1

        items, total = await repo.list_activities(
            date_from=now + timedelta(days=5), date_to=now + timedelta(days=10)
        )
        assert total == 0

    async def test_excludes_soft_deleted_by_default(self, db_session):
        repo = ActivityRepository(db_session)
        created = await repo.create(make_activity_data(title="Will be deleted"))
        await repo.soft_delete(created)

        items, total = await repo.list_activities()
        assert created.id not in [item.id for item in items]

        items, total = await repo.list_activities(include_deleted=True)
        assert created.id in [item.id for item in items]

    async def test_pagination_splits_results_across_pages(self, db_session):
        repo = ActivityRepository(db_session)
        # Explicit, strictly increasing `created_at` values (see the
        # comment in `test_sort_order_asc_vs_desc` below): the default
        # `sort_by="created_at"` would otherwise tie across all 5 rows
        # within this test's single wrapping transaction (Postgres's
        # `now()` is transaction-scoped), leaving the two independently
        # issued page-1/page-2 queries free to break those ties
        # differently and flakily overlap.
        now = datetime.now(timezone.utc)
        await repo.bulk_create(
            [
                make_activity_data(
                    title=f"Page item {i}", created_at=now + timedelta(seconds=i)
                )
                for i in range(5)
            ]
        )

        page_one, total = await repo.list_activities(page=1, page_size=2)
        page_two, _ = await repo.list_activities(page=2, page_size=2)

        assert total >= 5
        assert len(page_one) == 2
        assert len(page_two) == 2
        assert {item.id for item in page_one}.isdisjoint(
            {item.id for item in page_two}
        )

    async def test_sort_order_asc_vs_desc(self, db_session):
        repo = ActivityRepository(db_session)
        # `created_at` defaults to `func.now()` (a Postgres server
        # default), and Postgres's `now()` is fixed for the entire
        # duration of a transaction -- not per-statement. Since both
        # rows are created inside the single wrapping transaction the
        # `db_session` fixture provides, relying on the server default
        # would give both rows an *identical* `created_at`, making
        # ASC/DESC ordering on it nondeterministic (a tie the database
        # is free to break either way) rather than a genuine test of
        # `ActivityRepository.list_activities`'s ordering. Setting
        # explicit, distinct timestamps makes the ordering
        # deterministic and actually exercises the ASC/DESC behavior.
        now = datetime.now(timezone.utc)
        first = await repo.create(
            make_activity_data(title="First created", created_at=now)
        )
        second = await repo.create(
            make_activity_data(
                title="Second created", created_at=now + timedelta(seconds=1)
            )
        )

        asc_items, _ = await repo.list_activities(
            sort_by="created_at", sort_order="asc", page_size=100
        )
        desc_items, _ = await repo.list_activities(
            sort_by="created_at", sort_order="desc", page_size=100
        )

        asc_ids = [item.id for item in asc_items]
        desc_ids = [item.id for item in desc_items]
        assert asc_ids.index(first.id) < asc_ids.index(second.id)
        assert desc_ids.index(first.id) > desc_ids.index(second.id)

    async def test_search_activities_delegates_to_list_activities(self, db_session):
        repo = ActivityRepository(db_session)
        await repo.create(make_activity_data(title="Searchable unique token XYZ123"))

        items, total = await repo.search_activities("XYZ123")

        assert total == 1
        assert "XYZ123" in items[0].title


# ---------------------------------------------------------------------------
# Timeline feeds
# ---------------------------------------------------------------------------
class TestTimelineFeeds:
    async def test_get_timeline_by_entity_returns_chronological_feed(
        self, db_session
    ):
        repo = ActivityRepository(db_session)
        entity_id = str(uuid.uuid4())
        await repo.create(
            make_activity_data(entity_type="Booking", entity_id=entity_id)
        )
        await repo.create(
            make_activity_data(entity_type="Booking", entity_id=entity_id)
        )
        await repo.create(make_activity_data(entity_type="Payment"))

        items, total = await repo.get_timeline_by_entity("Booking", entity_id)

        assert total == 2
        assert all(item.entity_id == entity_id for item in items)

    async def test_get_timeline_by_module_scopes_to_module(self, db_session):
        repo = ActivityRepository(db_session)
        await repo.create(make_activity_data(module=ActivityModule.WORKFLOW))
        await repo.create(make_activity_data(module=ActivityModule.LEAD))

        items, total = await repo.get_timeline_by_module(ActivityModule.WORKFLOW.value)

        assert total == 1
        assert items[0].module == ActivityModule.WORKFLOW

    async def test_get_timeline_by_user_includes_performer_and_assignee(
        self, db_session
    ):
        repo = ActivityRepository(db_session)
        target = await make_user(db_session)
        other = await make_user(db_session)
        await repo.create(make_activity_data(performed_by_id=target.id))
        await repo.create(make_activity_data(assigned_to_id=target.id))
        await repo.create(
            make_activity_data(performed_by_id=other.id, assigned_to_id=other.id)
        )

        items, total = await repo.get_timeline_by_user(target.id)

        assert total == 2
        for item in items:
            assert target.id in (item.performed_by_id, item.assigned_to_id)

    async def test_get_recent_activities_orders_newest_first_and_respects_limit(
        self, db_session
    ):
        repo = ActivityRepository(db_session)
        await repo.bulk_create(
            [make_activity_data(title=f"Recent {i}") for i in range(5)]
        )

        recent = await repo.get_recent_activities(limit=3)

        assert len(recent) == 3
        for earlier, later in zip(recent, recent[1:]):
            assert earlier.created_at >= later.created_at


# ---------------------------------------------------------------------------
# Statistics / aggregations
# ---------------------------------------------------------------------------
class TestStatistics:
    async def test_get_total_count_respects_filters(self, db_session):
        repo = ActivityRepository(db_session)
        await repo.create(make_activity_data(module=ActivityModule.PROPERTY))
        await repo.create(make_activity_data(module=ActivityModule.PROPERTY))
        await repo.create(make_activity_data(module=ActivityModule.LEAD))

        total = await repo.get_total_count(module=ActivityModule.PROPERTY.value)
        assert total == 2

    async def test_count_by_module_groups_correctly(self, db_session):
        repo = ActivityRepository(db_session)
        await repo.create(make_activity_data(module=ActivityModule.CUSTOMER))
        await repo.create(make_activity_data(module=ActivityModule.CUSTOMER))
        await repo.create(make_activity_data(module=ActivityModule.DOCUMENT))

        counts = await repo.count_by_module()

        assert counts.get("customer") == 2
        assert counts.get("document") == 1

    async def test_count_by_action_groups_correctly(self, db_session):
        repo = ActivityRepository(db_session)
        await repo.create(make_activity_data(action=ActivityType.APPROVED))
        await repo.create(make_activity_data(action=ActivityType.APPROVED))
        await repo.create(make_activity_data(action=ActivityType.REJECTED))

        counts = await repo.count_by_action()

        assert counts.get("approved") == 2
        assert counts.get("rejected") == 1

    async def test_count_by_user_excludes_null_performer_and_respects_limit(
        self, db_session
    ):
        repo = ActivityRepository(db_session)
        performer = await make_user(db_session)
        await repo.create(make_activity_data(performed_by_id=performer.id))
        await repo.create(make_activity_data(performed_by_id=performer.id))
        await repo.create(make_activity_data(performed_by_id=None))

        counts = await repo.count_by_user(limit=10)

        assert counts.get(performer.id) == 2
        assert None not in counts

    async def test_count_by_status_groups_correctly(self, db_session):
        repo = ActivityRepository(db_session)
        await repo.create(make_activity_data(status=ActivityStatus.FAILED))
        await repo.create(make_activity_data(status=ActivityStatus.FAILED))
        await repo.create(make_activity_data(status=ActivityStatus.COMPLETED))

        counts = await repo.count_by_status()

        assert counts.get("failed") == 2
        assert counts.get("completed") == 1

    async def test_count_by_priority_groups_correctly(self, db_session):
        repo = ActivityRepository(db_session)
        await repo.create(make_activity_data(priority=ActivityPriority.URGENT))
        await repo.create(make_activity_data(priority=ActivityPriority.LOW))

        counts = await repo.count_by_priority()

        assert counts.get("urgent") == 1
        assert counts.get("low") == 1