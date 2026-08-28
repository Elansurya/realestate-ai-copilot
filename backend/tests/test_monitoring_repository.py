"""
backend/tests/test_monitoring_repository.py

Integration tests for `app.repositories.monitoring_repository.MonitoringRepository`.

Scope:
    These tests exercise the repository's actual SQL behavior --
    filtering, sorting, pagination, soft-delete semantics, the
    upsert-in-place write path, parent/child health-history lookup,
    and aggregation queries -- against a real database. Unlike
    `test_monitoring_service.py` (which mocks `MonitoringRepository`
    entirely), these tests deliberately do NOT mock SQLAlchemy, because
    the behaviors under test rely on PostgreSQL-native features declared
    on `SystemHealth` (see `app/models/monitoring.py`): native ENUM
    columns (`component_type_enum`, `health_status_enum`), a JSONB
    `metadata` column, `gen_random_uuid()`-generated primary keys (via
    the `pgcrypto` extension), composite indexes, a `UniqueConstraint`
    on (`component_name`, `component_type`), several `CheckConstraint`s,
    and a self-referential `parent_component_id` foreign key. None of
    these are meaningfully verifiable against a mocked session or a
    non-PostgreSQL backend.

Database requirement:
    A running PostgreSQL instance reachable via the `TEST_DATABASE_URL`
    environment variable is required to run this file, e.g.:

        postgresql+psycopg://user:password@localhost:5432/test_db

    (matching this project's `psycopg[binary]==3.2.1` driver, per
    `backend/requirements.txt`, used with SQLAlchemy 2.x's async
    `postgresql+psycopg` dialect). If `TEST_DATABASE_URL` is not set,
    every test in this module is SKIPPED rather than failed.

    NOT VERIFIED: This task's file scope explicitly excludes
    `app/db/base.py`, `app/db/session.py`, and `app/models/user.py`, so
    this project's actual test-database bootstrap convention (fixture
    location, factory pattern, migration-based setup, CI wiring, etc.)
    could not be confirmed against those files and is NOT assumed here.
    The fixtures below are therefore fully self-contained:
        - They create ONLY the `system_health` table, via
          `SystemHealth.metadata.create_all(tables=[SystemHealth.__table__])`,
          rather than assuming any other project table exists.
        - `SystemHealth.created_by_id` / `updated_by_id` / `deleted_by_id`
          are nullable foreign keys to `users.id` (per the model's
          module docstring). Since the real `users` table schema is
          outside this task's referenced files, a minimal stand-in
          `users(id SERIAL PRIMARY KEY)` table is created purely to
          satisfy referential integrity for these nullable FKs -- it is
          NOT a reproduction of the actual `User` model and only exists
          for the lifetime of this test module's fixtures.
        - The `pgcrypto` extension is created if not already present,
          since `SystemHealth.id`'s server default is
          `gen_random_uuid()`.

Isolation:
    Each test runs inside its own outer transaction (opened on a
    dedicated connection) that is rolled back in fixture teardown, so
    no test's writes are ever visible to another test and no explicit
    cleanup/reset step is required between runs. `MonitoringRepository`
    itself never calls `commit()` (only `flush()`/`refresh()`, per
    `monitoring_repository.py`), so a single outer-transaction rollback
    per test is sufficient for isolation.
"""

from __future__ import annotations

import os
import uuid
from datetime import datetime, timedelta, timezone
from typing import AsyncIterator

import pytest
import pytest_asyncio
from sqlalchemy import inspect as sync_inspect, text
from sqlalchemy.ext.asyncio import (
    AsyncConnection,
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from app.models.monitoring import ComponentType, HealthStatus, SystemHealth
from app.repositories.monitoring_repository import MonitoringRepository
from app.schemas.monitoring import HealthFilter, SystemHealthCreate, SystemHealthUpdate

TEST_DATABASE_URL = os.environ.get("TEST_DATABASE_URL")

pytestmark = [
    pytest.mark.asyncio,
    pytest.mark.skipif(
        not TEST_DATABASE_URL,
        reason=(
            "TEST_DATABASE_URL is not set. MonitoringRepository integration "
            "tests require a real PostgreSQL database because SystemHealth "
            "relies on PostgreSQL-native ENUM, JSONB, and gen_random_uuid() "
            "features (see app/models/monitoring.py) that cannot be "
            "verified against a mock or a non-PostgreSQL backend."
        ),
    ),
]


# --------------------------------------------------------------------------
# Engine / Schema Fixtures (Session-Scoped)
# --------------------------------------------------------------------------
@pytest_asyncio.fixture(scope="session")
async def _engine() -> AsyncIterator[AsyncEngine]:
    """
    Creates the async engine for the test database and provisions ONLY
    the `system_health` table (plus a minimal `users` stand-in table
    for FK integrity -- see module docstring), then tears both down.

    NOTE: this file's original teardown unconditionally ran
    `DROP TABLE IF EXISTS users` and `SystemHealth.__table__.drop()`,
    which is safe only against the standalone/throwaway database this
    file's docstring describes. Run against this project's actual
    Alembic-migrated database (as this suite does, sharing one test DB
    across files, matching every other repository test file's pattern),
    both `users` and `system_health` already exist as real, populated,
    heavily-FK-referenced tables -- unconditionally dropping them would
    destroy production-shaped data and cascade-break every other table
    that references `users`. This fixture now checks whether each table
    already existed *before* this fixture ran, and only creates/drops
    the ones it actually created itself, leaving pre-existing tables
    (and their data) untouched either way.
    """
    engine = create_async_engine(TEST_DATABASE_URL, future=True)

    async with engine.begin() as conn:
        await conn.execute(text("CREATE EXTENSION IF NOT EXISTS pgcrypto"))

        users_existed = await conn.run_sync(
            lambda sync_conn: sync_inspect(sync_conn).has_table("users")
        )
        if not users_existed:
            await conn.execute(text("CREATE TABLE IF NOT EXISTS users (id SERIAL PRIMARY KEY)"))

        system_health_existed = await conn.run_sync(
            lambda sync_conn: sync_inspect(sync_conn).has_table("system_health")
        )
        if not system_health_existed:
            await conn.run_sync(
                SystemHealth.metadata.create_all, tables=[SystemHealth.__table__]
            )

    yield engine

    async with engine.begin() as conn:
        if not system_health_existed:
            await conn.run_sync(SystemHealth.__table__.drop, checkfirst=True)
        if not users_existed:
            await conn.execute(text("DROP TABLE IF EXISTS users"))

    await engine.dispose()


@pytest_asyncio.fixture
async def _connection(_engine: AsyncEngine) -> AsyncIterator[AsyncConnection]:
    """Opens a dedicated connection + outer transaction per test."""
    async with _engine.connect() as connection:
        trans = await connection.begin()
        try:
            yield connection
        finally:
            if trans.is_active:
                await trans.rollback()


@pytest_asyncio.fixture
async def session(_connection: AsyncConnection) -> AsyncIterator[AsyncSession]:
    """An `AsyncSession` bound to the per-test connection/transaction."""
    session_factory = async_sessionmaker(bind=_connection, expire_on_commit=False)
    async with session_factory() as db_session:
        yield db_session


@pytest.fixture
def repository(session: AsyncSession) -> MonitoringRepository:
    return MonitoringRepository(session)


# --------------------------------------------------------------------------
# Payload Helpers
# --------------------------------------------------------------------------
def _unique_name(prefix: str = "component") -> str:
    return f"{prefix}-{uuid.uuid4().hex[:10]}"


def _make_create_payload(**overrides) -> SystemHealthCreate:
    now = datetime.now(timezone.utc)
    defaults = dict(
        component_name=_unique_name(),
        component_type=ComponentType.DATABASE,
        status=HealthStatus.HEALTHY,
        cpu_usage_percent=10.0,
        memory_usage_percent=20.0,
        disk_usage_percent=30.0,
        response_time_ms=15.0,
        error_count=0,
        warning_count=0,
        last_health_check_at=now,
        last_success_at=now,
        last_failure_at=None,
        status_message=None,
        meta_data=None,
        is_active=True,
    )
    defaults.update(overrides)
    return SystemHealthCreate(**defaults)


# ==========================================================================
# Create
# ==========================================================================
class TestCreate:
    async def test_create_persists_record_with_supplied_fields(
        self, repository: MonitoringRepository
    ) -> None:
        payload = _make_create_payload(component_name=_unique_name(), status=HealthStatus.HEALTHY)

        record = await repository.create(payload, created_by_id=None)

        assert record.id is not None
        assert record.component_name == payload.component_name
        assert record.component_type == ComponentType.DATABASE
        assert record.status == HealthStatus.HEALTHY
        assert record.is_deleted is False

    async def test_create_sets_created_by_and_updated_by(
        self, repository: MonitoringRepository, session: AsyncSession
    ) -> None:
        # NOTE: the module-level `users` stand-in table
        # (`CREATE TABLE IF NOT EXISTS users (id SERIAL PRIMARY KEY)`) is a
        # no-op when run against a database that already has the real,
        # fully-columned `users` table (NOT NULL `full_name`/`email`/
        # `phone`/`password_hash`, no defaults) -- a bare
        # `INSERT INTO users (id) VALUES (7)` then violates those NOT NULL
        # constraints. Inserting a minimal but fully valid row instead
        # works against both the standalone stand-in table and the real
        # schema.
        await session.execute(
            text(
                "INSERT INTO users (id, uuid, full_name, email, phone, password_hash) "
                "VALUES (7, gen_random_uuid()::text, 'Test User', "
                "'monitoring-test-7@example.com', '9840000007', 'not-a-real-hash') "
                "ON CONFLICT DO NOTHING"
            )
        )
        payload = _make_create_payload()

        record = await repository.create(payload, created_by_id=7)

        assert record.created_by_id == 7
        assert record.updated_by_id == 7

    async def test_create_defaults_error_and_warning_counts_to_zero(
        self, repository: MonitoringRepository
    ) -> None:
        payload = _make_create_payload()

        record = await repository.create(payload)

        assert record.error_count == 0
        assert record.warning_count == 0


# ==========================================================================
# Retrieve by ID / by Name+Type / Component Status
# ==========================================================================
class TestGetById:
    async def test_returns_none_when_missing(self, repository: MonitoringRepository) -> None:
        result = await repository.get_by_id(uuid.uuid4())
        assert result is None

    async def test_returns_matching_record(self, repository: MonitoringRepository) -> None:
        created = await repository.create(_make_create_payload())

        result = await repository.get_by_id(created.id)

        assert result is not None
        assert result.id == created.id

    async def test_excludes_soft_deleted_by_default(
        self, repository: MonitoringRepository
    ) -> None:
        created = await repository.create(_make_create_payload())
        await repository.soft_delete(created.id)

        result = await repository.get_by_id(created.id)

        assert result is None

    async def test_includes_soft_deleted_when_requested(
        self, repository: MonitoringRepository
    ) -> None:
        created = await repository.create(_make_create_payload())
        await repository.soft_delete(created.id)

        result = await repository.get_by_id(created.id, include_deleted=True)

        assert result is not None
        assert result.is_deleted is True


class TestGetByNameAndType:
    async def test_returns_none_when_missing(self, repository: MonitoringRepository) -> None:
        result = await repository.get_by_name_and_type("does-not-exist", ComponentType.DATABASE)
        assert result is None

    async def test_returns_matching_record(self, repository: MonitoringRepository) -> None:
        name = _unique_name()
        await repository.create(
            _make_create_payload(component_name=name, component_type=ComponentType.AI_PROVIDER)
        )

        result = await repository.get_by_name_and_type(name, ComponentType.AI_PROVIDER)

        assert result is not None
        assert result.component_name == name
        assert result.component_type == ComponentType.AI_PROVIDER

    async def test_same_name_different_type_is_not_a_match(
        self, repository: MonitoringRepository
    ) -> None:
        name = _unique_name()
        await repository.create(
            _make_create_payload(component_name=name, component_type=ComponentType.DATABASE)
        )

        result = await repository.get_by_name_and_type(name, ComponentType.STORAGE)

        assert result is None


class TestGetComponentStatus:
    async def test_is_equivalent_to_get_by_name_and_type(
        self, repository: MonitoringRepository
    ) -> None:
        name = _unique_name()
        created = await repository.create(
            _make_create_payload(component_name=name, component_type=ComponentType.SEARCH_ENGINE)
        )

        result = await repository.get_component_status(name, ComponentType.SEARCH_ENGINE)

        assert result is not None
        assert result.id == created.id


# ==========================================================================
# Health History (Own Snapshot + Child Rollups)
# ==========================================================================
class TestGetHealthHistory:
    async def test_returns_empty_list_when_component_missing(
        self, repository: MonitoringRepository
    ) -> None:
        history = await repository.get_health_history("missing", ComponentType.AI_PROVIDER)
        assert history == []

    async def test_returns_only_own_snapshot_when_no_children(
        self, repository: MonitoringRepository
    ) -> None:
        name = _unique_name()
        parent = await repository.create(
            _make_create_payload(component_name=name, component_type=ComponentType.AI_PROVIDER)
        )

        history = await repository.get_health_history(name, ComponentType.AI_PROVIDER)

        assert [record.id for record in history] == [parent.id]

    async def test_includes_child_rollup_rows(self, repository: MonitoringRepository) -> None:
        parent_name = _unique_name("ai-providers")
        parent = await repository.create(
            _make_create_payload(
                component_name=parent_name, component_type=ComponentType.AI_PROVIDER
            )
        )
        child = await repository.create(
            _make_create_payload(
                component_name=_unique_name("openai-instance"),
                component_type=ComponentType.AI_PROVIDER,
                parent_component_id=parent.id,
            )
        )

        history = await repository.get_health_history(parent_name, ComponentType.AI_PROVIDER)

        assert {record.id for record in history} == {parent.id, child.id}

    async def test_orders_most_recent_check_first(self, repository: MonitoringRepository) -> None:
        parent_name = _unique_name("db-cluster")
        now = datetime.now(timezone.utc)
        parent = await repository.create(
            _make_create_payload(
                component_name=parent_name,
                component_type=ComponentType.DATABASE,
                last_health_check_at=now - timedelta(minutes=10),
            )
        )
        newer_child = await repository.create(
            _make_create_payload(
                component_name=_unique_name("db-replica"),
                component_type=ComponentType.DATABASE,
                parent_component_id=parent.id,
                last_health_check_at=now,
            )
        )

        history = await repository.get_health_history(parent_name, ComponentType.DATABASE)

        assert history[0].id == newer_child.id

    async def test_respects_limit(self, repository: MonitoringRepository) -> None:
        parent_name = _unique_name("workflow")
        parent = await repository.create(
            _make_create_payload(
                component_name=parent_name, component_type=ComponentType.WORKFLOW_ENGINE
            )
        )
        for _ in range(3):
            await repository.create(
                _make_create_payload(
                    component_name=_unique_name("workflow-child"),
                    component_type=ComponentType.WORKFLOW_ENGINE,
                    parent_component_id=parent.id,
                )
            )

        history = await repository.get_health_history(
            parent_name, ComponentType.WORKFLOW_ENGINE, limit=2
        )

        assert len(history) == 2


# ==========================================================================
# List / Search / Filter / Sort / Paginate
# ==========================================================================
class TestListPaginated:
    async def test_filters_by_component_type(self, repository: MonitoringRepository) -> None:
        await repository.create(
            _make_create_payload(component_type=ComponentType.DATABASE, component_name=_unique_name())
        )
        await repository.create(
            _make_create_payload(component_type=ComponentType.STORAGE, component_name=_unique_name())
        )

        items, total = await repository.list_paginated(
            HealthFilter(component_type=ComponentType.STORAGE)
        )

        assert total == 1
        assert items[0].component_type == ComponentType.STORAGE

    async def test_filters_by_status(self, repository: MonitoringRepository) -> None:
        await repository.create(
            _make_create_payload(status=HealthStatus.HEALTHY, component_name=_unique_name())
        )
        await repository.create(
            _make_create_payload(status=HealthStatus.DOWN, component_name=_unique_name())
        )

        items, total = await repository.list_paginated(HealthFilter(status=HealthStatus.DOWN))

        assert total == 1
        assert items[0].status == HealthStatus.DOWN

    async def test_search_matches_partial_name_case_insensitive(
        self, repository: MonitoringRepository
    ) -> None:
        unique_token = uuid.uuid4().hex[:8]
        await repository.create(
            _make_create_payload(component_name=f"Primary-Postgres-{unique_token}")
        )
        await repository.create(_make_create_payload(component_name=_unique_name("unrelated")))

        items, total = await repository.list_paginated(
            HealthFilter(search=unique_token.upper())
        )

        assert total == 1
        assert unique_token.lower() in items[0].component_name.lower()

    async def test_is_deleted_filter_overrides_default_exclusion(
        self, repository: MonitoringRepository
    ) -> None:
        created = await repository.create(_make_create_payload())
        await repository.soft_delete(created.id)

        default_items, default_total = await repository.list_paginated(HealthFilter())
        deleted_items, deleted_total = await repository.list_paginated(
            HealthFilter(is_deleted=True)
        )

        assert created.id not in [item.id for item in default_items]
        assert default_total == 0
        assert deleted_total == 1
        assert deleted_items[0].id == created.id

    async def test_filters_by_is_active(self, repository: MonitoringRepository) -> None:
        await repository.create(
            _make_create_payload(is_active=True, component_name=_unique_name())
        )
        await repository.create(
            _make_create_payload(is_active=False, component_name=_unique_name())
        )

        items, total = await repository.list_paginated(HealthFilter(is_active=False))

        assert total == 1
        assert items[0].is_active is False

    async def test_pagination_page_and_page_size(self, repository: MonitoringRepository) -> None:
        marker = uuid.uuid4().hex[:8]
        for index in range(5):
            await repository.create(
                _make_create_payload(component_name=f"page-test-{marker}-{index}")
            )

        page_1, total = await repository.list_paginated(
            HealthFilter(search=marker, page=1, page_size=2, sort_by="component_name", sort_order="asc")
        )
        page_2, _ = await repository.list_paginated(
            HealthFilter(search=marker, page=2, page_size=2, sort_by="component_name", sort_order="asc")
        )

        assert total == 5
        assert len(page_1) == 2
        assert len(page_2) == 2
        assert {item.id for item in page_1}.isdisjoint({item.id for item in page_2})

    async def test_sorting_ascending_and_descending(
        self, repository: MonitoringRepository
    ) -> None:
        marker = uuid.uuid4().hex[:8]
        low = await repository.create(
            _make_create_payload(component_name=f"sort-{marker}-a", response_time_ms=5.0)
        )
        high = await repository.create(
            _make_create_payload(component_name=f"sort-{marker}-b", response_time_ms=500.0)
        )

        asc_items, _ = await repository.list_paginated(
            HealthFilter(search=marker, sort_by="response_time_ms", sort_order="asc")
        )
        desc_items, _ = await repository.list_paginated(
            HealthFilter(search=marker, sort_by="response_time_ms", sort_order="desc")
        )

        assert [item.id for item in asc_items] == [low.id, high.id]
        assert [item.id for item in desc_items] == [high.id, low.id]

    async def test_filters_by_parent_component_id(
        self, repository: MonitoringRepository
    ) -> None:
        parent = await repository.create(
            _make_create_payload(component_name=_unique_name("parent"))
        )
        child = await repository.create(
            _make_create_payload(
                component_name=_unique_name("child"), parent_component_id=parent.id
            )
        )
        await repository.create(_make_create_payload(component_name=_unique_name("unrelated")))

        items, total = await repository.list_paginated(
            HealthFilter(parent_component_id=parent.id)
        )

        assert total == 1
        assert items[0].id == child.id


# ==========================================================================
# Update
# ==========================================================================
class TestUpdate:
    async def test_returns_none_when_missing(self, repository: MonitoringRepository) -> None:
        result = await repository.update(uuid.uuid4(), SystemHealthUpdate(status=HealthStatus.DOWN))
        assert result is None

    async def test_applies_only_supplied_fields(self, repository: MonitoringRepository) -> None:
        created = await repository.create(
            _make_create_payload(status=HealthStatus.HEALTHY, cpu_usage_percent=10.0)
        )

        updated = await repository.update(created.id, SystemHealthUpdate(status=HealthStatus.DEGRADED))

        assert updated is not None
        assert updated.status == HealthStatus.DEGRADED
        assert updated.cpu_usage_percent == 10.0  # untouched field preserved

    async def test_sets_updated_by_id(self, repository: MonitoringRepository, session: AsyncSession) -> None:
        # See the identical note in test_create_sets_created_by_and_updated_by
        # above -- a bare `INSERT INTO users (id) VALUES (...)` violates the
        # real `users` table's NOT NULL columns when this file runs against
        # the fully-migrated schema rather than its own standalone stand-in.
        await session.execute(
            text(
                "INSERT INTO users (id, uuid, full_name, email, phone, password_hash) "
                "VALUES (9, gen_random_uuid()::text, 'Test User', "
                "'monitoring-test-9@example.com', '9840000009', 'not-a-real-hash') "
                "ON CONFLICT DO NOTHING"
            )
        )
        created = await repository.create(_make_create_payload())

        updated = await repository.update(
            created.id, SystemHealthUpdate(status=HealthStatus.UNHEALTHY), updated_by_id=9
        )

        assert updated is not None
        assert updated.updated_by_id == 9

    async def test_excludes_soft_deleted_records_from_update_eligibility(
        self, repository: MonitoringRepository
    ) -> None:
        created = await repository.create(_make_create_payload())
        await repository.soft_delete(created.id)

        result = await repository.update(created.id, SystemHealthUpdate(status=HealthStatus.DOWN))

        assert result is None


# ==========================================================================
# Upsert (Continuous-In-Place Write Path)
# ==========================================================================
class TestUpsertHealthCheckResult:
    async def test_creates_when_no_existing_record(
        self, repository: MonitoringRepository
    ) -> None:
        name = _unique_name()
        payload = _make_create_payload(component_name=name, component_type=ComponentType.AI_PROVIDER)

        result = await repository.upsert_health_check_result(
            name, ComponentType.AI_PROVIDER, payload
        )

        assert result.component_name == name
        assert result.component_type == ComponentType.AI_PROVIDER

    async def test_updates_existing_record_in_place(
        self, repository: MonitoringRepository
    ) -> None:
        name = _unique_name()
        first_payload = _make_create_payload(
            component_name=name, component_type=ComponentType.STORAGE, status=HealthStatus.HEALTHY
        )
        first = await repository.upsert_health_check_result(
            name, ComponentType.STORAGE, first_payload
        )

        second_payload = _make_create_payload(
            component_name=name, component_type=ComponentType.STORAGE, status=HealthStatus.DOWN
        )
        second = await repository.upsert_health_check_result(
            name, ComponentType.STORAGE, second_payload
        )

        assert second.id == first.id  # same live row, updated in place
        assert second.status == HealthStatus.DOWN

        items, total = await repository.list_paginated(HealthFilter(component_name=name))
        assert total == 1  # no duplicate row was created


# ==========================================================================
# Soft Delete / Restore
# ==========================================================================
class TestSoftDeleteRestore:
    async def test_soft_delete_sets_flags(self, repository: MonitoringRepository) -> None:
        created = await repository.create(_make_create_payload())

        result = await repository.soft_delete(created.id, deleted_by_id=None)

        assert result is not None
        assert result.is_deleted is True
        assert result.deleted_at is not None

    async def test_soft_delete_returns_none_when_missing(
        self, repository: MonitoringRepository
    ) -> None:
        result = await repository.soft_delete(uuid.uuid4())
        assert result is None

    async def test_soft_delete_returns_none_when_already_deleted(
        self, repository: MonitoringRepository
    ) -> None:
        created = await repository.create(_make_create_payload())
        await repository.soft_delete(created.id)

        result = await repository.soft_delete(created.id)

        assert result is None

    async def test_restore_clears_flags(self, repository: MonitoringRepository) -> None:
        created = await repository.create(_make_create_payload())
        await repository.soft_delete(created.id)

        restored = await repository.restore(created.id)

        assert restored is not None
        assert restored.is_deleted is False
        assert restored.deleted_at is None
        assert restored.deleted_by_id is None

    async def test_restore_returns_none_when_missing(
        self, repository: MonitoringRepository
    ) -> None:
        result = await repository.restore(uuid.uuid4())
        assert result is None

    async def test_restore_returns_none_when_not_deleted(
        self, repository: MonitoringRepository
    ) -> None:
        created = await repository.create(_make_create_payload())

        result = await repository.restore(created.id)

        assert result is None


# ==========================================================================
# Statistics / Aggregation
# ==========================================================================
class TestAggregation:
    async def test_count_by_status(self, repository: MonitoringRepository) -> None:
        await repository.create(
            _make_create_payload(status=HealthStatus.HEALTHY, component_name=_unique_name())
        )
        await repository.create(
            _make_create_payload(status=HealthStatus.HEALTHY, component_name=_unique_name())
        )
        await repository.create(
            _make_create_payload(status=HealthStatus.DOWN, component_name=_unique_name())
        )

        counts = await repository.count_by_status()

        assert counts[HealthStatus.HEALTHY] >= 2
        assert counts[HealthStatus.DOWN] >= 1

    async def test_count_by_component_type(self, repository: MonitoringRepository) -> None:
        await repository.create(
            _make_create_payload(component_type=ComponentType.SEARCH_ENGINE, component_name=_unique_name())
        )

        counts = await repository.count_by_component_type()

        assert counts[ComponentType.SEARCH_ENGINE] >= 1

    async def test_get_aggregate_metrics(self, repository: MonitoringRepository) -> None:
        await repository.create(
            _make_create_payload(
                component_name=_unique_name(), response_time_ms=100.0, error_count=2, warning_count=3
            )
        )
        await repository.create(
            _make_create_payload(
                component_name=_unique_name(), response_time_ms=300.0, error_count=1, warning_count=1
            )
        )

        aggregates = await repository.get_aggregate_metrics()

        assert aggregates["total_components"] >= 2
        assert aggregates["average_response_time_ms"] is not None
        assert aggregates["total_error_count"] >= 3
        assert aggregates["total_warning_count"] >= 4

    async def test_get_aggregate_metrics_excludes_soft_deleted(
        self, repository: MonitoringRepository
    ) -> None:
        created = await repository.create(
            _make_create_payload(error_count=99, warning_count=99)
        )
        await repository.soft_delete(created.id)

        aggregates = await repository.get_aggregate_metrics()

        assert aggregates["total_error_count"] < 99

    async def test_list_all_active_excludes_inactive_and_deleted(
        self, repository: MonitoringRepository
    ) -> None:
        active = await repository.create(
            _make_create_payload(is_active=True, component_name=_unique_name())
        )
        inactive = await repository.create(
            _make_create_payload(is_active=False, component_name=_unique_name())
        )
        deleted = await repository.create(
            _make_create_payload(is_active=True, component_name=_unique_name())
        )
        await repository.soft_delete(deleted.id)

        results = await repository.list_all_active()
        result_ids = {record.id for record in results}

        assert active.id in result_ids
        assert inactive.id not in result_ids
        assert deleted.id not in result_ids