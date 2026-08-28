# backend/tests/test_audit_repository.py

"""
Audit Log Module - Phase 4
Repository Layer Test Suite

Covers:
    - Create Audit Log
    - Get By ID
    - Update
    - Delete
    - Search
    - Pagination
    - Sorting
    - Filtering
    - Date Range
    - User Filter
    - Module Filter
    - Entity Filter
    - Action Filter
    - Severity Filter
    - Status Filter
    - Statistics
    - Recent Logs
    - Failed Logs
    - Critical Logs
    - Bulk Delete
    - Cleanup

These tests exercise the AuditLogRepository directly against a real
async SQLAlchemy session (test database), bypassing the HTTP layer
entirely, mirroring the conventions established in
`test_notification_repository.py`.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

import pytest
import pytest_asyncio
from sqlalchemy import create_engine, inspect as sync_inspect, select
from sqlalchemy import text as sa_text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from app.core.config import settings
from app.db.base import Base
from app.models.audit_log import AuditAction, AuditLog, AuditModule, AuditSeverity, AuditStatus
from app.repositories.audit_log_repository import AuditLogRepository
from app.schemas.audit_log import AuditLogCreate, AuditLogSearchFilter
# AuditLogUpdate does not exist: audit logs are intentionally append-only.
# See the note above `AuditLogSearchFilter` in app/schemas/audit_log.py.
# Update/soft-delete tests below are skipped pending an explicit product
# decision, not deleted.

pytestmark = pytest.mark.asyncio

# NOTE: previously hardcoded a fallback of
# "postgresql+asyncpg://postgres:postgres@localhost:5432/test_audit_db" --
# a database that is never created anywhere in this project (no fixture,
# migration, or setup script creates "test_audit_db"), and a driver
# (asyncpg) inconsistent with the rest of the app, which uses psycopg
# (see app/core/config.py -- `settings.DATABASE_URL` resolves to
# "postgresql+psycopg://...", matching every other repository test file
# in this suite, e.g. test_notification_repository.py).
#
# This file provisions its OWN schema via `Base.metadata.create_all()`/
# `drop_all()` (see the `async_engine` fixture below). Pointing that
# lifecycle directly at `settings.DATABASE_URL` -- the same database this
# project's Alembic migrations manage, shared across every other
# repository test file -- is unsafe: `Base.metadata` covers every table
# in the whole app, so `drop_all()` at teardown would wipe the entire
# shared schema, not just this module's tables (confirmed: this
# previously left only `alembic_version` behind after a run). This file
# instead gets its own dedicated scratch database, created on demand and
# left alone if it already exists, isolated from both the shared
# Alembic-managed database and every other test file's scratch database.
TEST_DATABASE_URL = make_url(settings.DATABASE_URL).set(
    database=f"{make_url(settings.DATABASE_URL).database}_audit_scratch"
).render_as_string(hide_password=False)


def _ensure_scratch_database_exists(url: str) -> None:
    """Creates the scratch database for this test file if it doesn't exist yet.

    Uses a synchronous, autocommit connection to the admin `postgres`
    database, since `CREATE DATABASE` cannot run inside a transaction.
    Also ensures the `pgcrypto` extension this schema depends on
    (`gen_random_uuid()` defaults) is present in the new database.
    """
    target = make_url(url)
    admin_url = target.set(database="postgres", drivername="postgresql+psycopg")
    admin_engine = create_engine(admin_url, isolation_level="AUTOCOMMIT")
    try:
        with admin_engine.connect() as conn:
            exists = conn.execute(
                sa_text("SELECT 1 FROM pg_database WHERE datname = :name"),
                {"name": target.database},
            ).scalar_one_or_none()
            if not exists:
                conn.execute(sa_text(f'CREATE DATABASE "{target.database}"'))
    finally:
        admin_engine.dispose()

    scratch_engine = create_engine(
        target.set(drivername="postgresql+psycopg"), isolation_level="AUTOCOMMIT"
    )
    try:
        with scratch_engine.connect() as conn:
            conn.execute(sa_text('CREATE EXTENSION IF NOT EXISTS "pgcrypto"'))
            conn.execute(sa_text('CREATE EXTENSION IF NOT EXISTS "vector"'))
    finally:
        scratch_engine.dispose()


# --------------------------------------------------------------------------- #
# Fixtures
# --------------------------------------------------------------------------- #

@pytest_asyncio.fixture(scope="session")
def event_loop():
    import asyncio

    loop = asyncio.new_event_loop()
    yield loop
    loop.close()


@pytest_asyncio.fixture(scope="session")
async def async_engine():
    # This file now points at its own dedicated scratch database (see the
    # TEST_DATABASE_URL note above) rather than the shared, Alembic-managed
    # one, so create_all()/drop_all() here is always safe: this database
    # exists solely for this file's own tests, is never shared, and never
    # holds anything this fixture didn't create itself.
    _ensure_scratch_database_exists(TEST_DATABASE_URL)
    engine = create_async_engine(TEST_DATABASE_URL, poolclass=NullPool, future=True)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield engine
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
    await engine.dispose()


@pytest_asyncio.fixture
async def db_session(async_engine) -> AsyncSession:
    connection = await async_engine.connect()
    transaction = await connection.begin()
    session_factory = async_sessionmaker(
        bind=connection, expire_on_commit=False, class_=AsyncSession
    )
    session = session_factory()

    yield session

    await session.close()
    await transaction.rollback()
    await connection.close()


@pytest_asyncio.fixture
def audit_repo(db_session: AsyncSession) -> AuditLogRepository:
    return AuditLogRepository(db_session)


@pytest_asyncio.fixture
async def user_id(db_session: AsyncSession) -> int:
    # NOTE: audit_logs.user_id has a real FK to users.id (see
    # fk_audit_logs_user_id_users in app/models/audit_log.py); a fixed
    # placeholder int (1001) with no corresponding row failed on insert.
    # Inserting a minimal real user here (rolled back by db_session's
    # outer transaction after each test, like every other row) satisfies
    # the constraint without weakening it.
    from app.models.user import User

    unique = uuid.uuid4().hex[:8]
    user = User(
        uuid=str(uuid.uuid4()),
        full_name="Audit Test User",
        email=f"audit-test-{unique}@example.com",
        phone=f"98400{unique[:5]}",
        password_hash="not-a-real-hash",
    )
    db_session.add(user)
    await db_session.flush()
    return user.id


@pytest_asyncio.fixture
def entity_id() -> uuid.UUID:
    return uuid.uuid4()


@pytest_asyncio.fixture
def audit_payload(user_id, entity_id) -> AuditLogCreate:
    return AuditLogCreate(
        user_id=user_id,
        module=AuditModule.LEAD,
        action=AuditAction.CREATE,
        entity_type="LEAD",
        entity_id=str(entity_id),
        severity=AuditSeverity.LOW,
        status=AuditStatus.SUCCESS,
        description="Created a new lead record.",
        ip_address="10.0.0.5",
        user_agent="pytest-agent/1.0",
        old_data=None,
        new_data={"name": "Asha Rao", "phone": "9840000001"},
    )


@pytest_asyncio.fixture
async def created_audit_log(
    audit_repo: AuditLogRepository, audit_payload: AuditLogCreate
) -> AuditLog:
    return await audit_repo.create(audit_payload.model_dump())


@pytest_asyncio.fixture
async def bulk_audit_logs(audit_repo: AuditLogRepository, user_id, entity_id) -> list[AuditLog]:
    modules = [
        AuditModule.LEAD,
        AuditModule.PROPERTY,
        AuditModule.BOOKING,
        AuditModule.PAYMENT,
        AuditModule.USER,
    ]
    logs = []
    for i, module in enumerate(modules):
        payload = AuditLogCreate(
            user_id=user_id,
            module=module,
            action=AuditAction.UPDATE,
            entity_type=module.value,
            entity_id=str(uuid.uuid4()),
            severity=AuditSeverity.MEDIUM,
            status=AuditStatus.SUCCESS,
            description=f"Bulk audit entry {i}",
        )
        logs.append(await audit_repo.create(payload.model_dump()))
    return logs


# --------------------------------------------------------------------------- #
# CRUD
# --------------------------------------------------------------------------- #

class TestAuditLogCRUD:
    async def test_create_audit_log_persists_all_fields(self, audit_repo, audit_payload):
        log = await audit_repo.create(audit_payload.model_dump())

        assert log.id is not None
        assert log.module == AuditModule.LEAD
        assert log.action == AuditAction.CREATE
        assert log.severity == AuditSeverity.LOW
        assert log.status == AuditStatus.SUCCESS
        assert log.description == audit_payload.description
        assert log.created_at is not None
        # `is_active` assertion removed: AuditLog has no is_active column
        # (append-only design). See skipped tests below for the related
        # soft-delete/update coverage that depends on this field.

    async def test_get_by_id_returns_correct_log(self, audit_repo, created_audit_log):
        fetched = await audit_repo.get_by_id(created_audit_log.id)

        assert fetched is not None
        assert fetched.id == created_audit_log.id
        assert fetched.description == created_audit_log.description

    async def test_get_by_id_returns_none_for_missing_record(self, audit_repo):
        fetched = await audit_repo.get_by_id(uuid.uuid4())
        assert fetched is None

    @pytest.mark.skip(
        reason="AuditLog has no soft-delete concept; audit logs are "
        "intentionally append-only (see app/schemas/audit_log.py)."
    )
    async def test_get_by_id_excludes_inactive_by_default(self, audit_repo, created_audit_log):
        await audit_repo.soft_delete(created_audit_log.id)
        fetched = await audit_repo.get_by_id(created_audit_log.id)
        assert fetched is None

    @pytest.mark.skip(
        reason="AuditLog has no soft-delete concept or get_by_id_any_status method."
    )
    async def test_get_by_id_any_status(self, audit_repo, created_audit_log):
        await audit_repo.soft_delete(created_audit_log.id)
        fetched = await audit_repo.get_by_id_any_status(created_audit_log.id)
        assert fetched is not None
        assert fetched.is_active is False

    @pytest.mark.skip(
        reason="AuditLogUpdate does not exist; audit logs are append-only "
        "by design. Needs a product decision before this can be un-skipped."
    )
    async def test_update_audit_log_modifies_fields(self, audit_repo, created_audit_log):
        update_payload = AuditLogUpdate(
            description="Updated description", severity=AuditSeverity.HIGH
        )
        updated = await audit_repo.update(created_audit_log.id, update_payload)

        assert updated is not None
        assert updated.description == "Updated description"
        assert updated.severity == AuditSeverity.HIGH

    @pytest.mark.skip(reason="AuditLogUpdate does not exist; see above.")
    async def test_update_nonexistent_audit_log_returns_none(self, audit_repo):
        update_payload = AuditLogUpdate(description="Ghost update")
        result = await audit_repo.update(uuid.uuid4(), update_payload)
        assert result is None

    @pytest.mark.skip(reason="AuditLog has no soft-delete concept.")
    async def test_soft_delete_sets_flag(self, audit_repo, created_audit_log):
        result = await audit_repo.soft_delete(created_audit_log.id)
        assert result is True

        fetched = await audit_repo.get_by_id(created_audit_log.id)
        assert fetched is None

    @pytest.mark.skip(reason="AuditLog has no soft-delete concept.")
    async def test_soft_delete_nonexistent_returns_false(self, audit_repo):
        result = await audit_repo.soft_delete(uuid.uuid4())
        assert result is False

    async def test_hard_delete_removes_row(self, audit_repo, db_session, created_audit_log):
        # NOTE: AuditLogRepository has no single-id `hard_delete` -- the
        # existing `bulk_delete(ids)` already covers deleting a specific
        # set of ids (checked against app/repositories/audit_log_repository.py),
        # so a single-purpose alias would just duplicate it.
        deleted_count = await audit_repo.bulk_delete([created_audit_log.id])
        assert deleted_count == 1

        result = await db_session.execute(
            select(AuditLog).where(AuditLog.id == created_audit_log.id)
        )
        assert result.scalar_one_or_none() is None


# --------------------------------------------------------------------------- #
# Search / Pagination / Sorting / Filtering
# --------------------------------------------------------------------------- #

class TestAuditLogSearch:
    async def test_pagination_returns_correct_page_size(self, audit_repo, audit_payload):
        for i in range(15):
            audit_payload.entity_id = str(uuid.uuid4())
            audit_payload.description = f"Pagination entry {i}"
            await audit_repo.create(audit_payload.model_dump())

        filters = AuditLogSearchFilter(page=1, page_size=10)
        items, total = await audit_repo.list_logs(**filters.model_dump(exclude_none=True))

        assert total >= 15
        assert len(items) == 10

    async def test_pagination_second_page_returns_remainder(self, audit_repo, audit_payload):
        for i in range(12):
            audit_payload.entity_id = str(uuid.uuid4())
            audit_payload.description = f"Second page entry {i}"
            await audit_repo.create(audit_payload.model_dump())

        filters = AuditLogSearchFilter(page=2, page_size=10)
        items, total = await audit_repo.list_logs(**filters.model_dump(exclude_none=True))

        assert total >= 12
        assert len(items) >= 2

    async def test_sorting_by_created_at_descending(self, audit_repo, bulk_audit_logs):
        filters = AuditLogSearchFilter(sort_by="created_at", sort_order="desc", page_size=100)
        items, _ = await audit_repo.list_logs(**filters.model_dump(exclude_none=True))

        timestamps = [i.created_at for i in items]
        assert timestamps == sorted(timestamps, reverse=True)

    async def test_sorting_by_created_at_ascending(self, audit_repo, bulk_audit_logs):
        filters = AuditLogSearchFilter(sort_by="created_at", sort_order="asc", page_size=100)
        items, _ = await audit_repo.list_logs(**filters.model_dump(exclude_none=True))

        timestamps = [i.created_at for i in items]
        assert timestamps == sorted(timestamps)

    async def test_search_matches_description_text(self, audit_repo, created_audit_log):
        filters = AuditLogSearchFilter(search="Created a new lead")
        items, total = await audit_repo.list_logs(**filters.model_dump(exclude_none=True))

        assert total >= 1
        assert any(i.id == created_audit_log.id for i in items)

    async def test_search_case_insensitive(self, audit_repo, created_audit_log):
        filters = AuditLogSearchFilter(search="created a new lead")
        items, total = await audit_repo.list_logs(**filters.model_dump(exclude_none=True))
        assert total >= 1

    async def test_search_no_match_returns_empty(self, audit_repo, created_audit_log):
        filters = AuditLogSearchFilter(search="Nonexistent Term XYZ 12345")
        items, total = await audit_repo.list_logs(**filters.model_dump(exclude_none=True))
        assert total == 0
        assert items == []

    async def test_filter_by_module(self, audit_repo, bulk_audit_logs):
        filters = AuditLogSearchFilter(module=AuditModule.PAYMENT)
        items, total = await audit_repo.list_logs(**filters.model_dump(exclude_none=True))

        assert total == 1
        assert all(i.module == AuditModule.PAYMENT for i in items)

    async def test_filter_by_action(self, audit_repo, bulk_audit_logs):
        filters = AuditLogSearchFilter(action=AuditAction.UPDATE)
        items, total = await audit_repo.list_logs(**filters.model_dump(exclude_none=True))

        assert total == len(bulk_audit_logs)
        assert all(i.action == AuditAction.UPDATE for i in items)

    async def test_filter_by_severity(self, audit_repo, bulk_audit_logs):
        filters = AuditLogSearchFilter(severity=AuditSeverity.MEDIUM)
        items, total = await audit_repo.list_logs(**filters.model_dump(exclude_none=True))

        assert total == len(bulk_audit_logs)
        assert all(i.severity == AuditSeverity.MEDIUM for i in items)

    async def test_filter_by_status(self, audit_repo, bulk_audit_logs):
        filters = AuditLogSearchFilter(status=AuditStatus.SUCCESS)
        items, total = await audit_repo.list_logs(**filters.model_dump(exclude_none=True))

        assert total >= len(bulk_audit_logs)
        assert all(i.status == AuditStatus.SUCCESS for i in items)

    async def test_filter_by_entity_type(self, audit_repo, bulk_audit_logs):
        filters = AuditLogSearchFilter(entity_type="PAYMENT")
        items, total = await audit_repo.list_logs(**filters.model_dump(exclude_none=True))

        assert total == 1
        assert items[0].entity_type == "PAYMENT"

    async def test_filter_by_user_id(self, audit_repo, bulk_audit_logs, user_id):
        filters = AuditLogSearchFilter(user_id=user_id)
        items, total = await audit_repo.list_logs(**filters.model_dump(exclude_none=True))

        assert total >= len(bulk_audit_logs)
        assert all(i.user_id == user_id for i in items)

    async def test_filter_by_date_range(self, audit_repo, created_audit_log):
        now = datetime.now(timezone.utc)
        filters = AuditLogSearchFilter(
            date_from=now - timedelta(hours=1),
            date_to=now + timedelta(hours=1),
        )
        items, total = await audit_repo.list_logs(**filters.model_dump(exclude_none=True))
        assert total >= 1

    async def test_filter_by_date_range_excludes_out_of_range(self, audit_repo, created_audit_log):
        now = datetime.now(timezone.utc)
        filters = AuditLogSearchFilter(
            date_from=now + timedelta(days=10),
            date_to=now + timedelta(days=20),
        )
        items, total = await audit_repo.list_logs(**filters.model_dump(exclude_none=True))
        assert total == 0

    async def test_combined_filters_module_and_severity(self, audit_repo, bulk_audit_logs):
        filters = AuditLogSearchFilter(
            module=AuditModule.LEAD, severity=AuditSeverity.MEDIUM
        )
        items, total = await audit_repo.list_logs(**filters.model_dump(exclude_none=True))
        assert total == 1
        assert items[0].module == AuditModule.LEAD

    @pytest.mark.skip(
        reason="AuditLogSearchFilter has no is_active field; AuditLog has "
        "no soft-delete concept."
    )
    async def test_is_active_filter_excludes_soft_deleted(
        self, audit_repo, created_audit_log
    ):
        await audit_repo.soft_delete(created_audit_log.id)
        filters = AuditLogSearchFilter(is_active=True)
        items, _ = await audit_repo.list_logs(**filters.model_dump(exclude_none=True))
        assert all(i.is_active for i in items)


# --------------------------------------------------------------------------- #
# Statistics / Aggregations
# --------------------------------------------------------------------------- #

class TestAuditLogStatistics:
    # NOTE ON THIS CLASS: adapted to the real `AuditLogRepository` API
    # (checked against app/repositories/audit_log_repository.py). There is
    # no `get_statistics()`/`get_recent()`/`get_failed()`/`get_critical()`/
    # `get_by_user()`/`get_by_module()`/`get_by_entity()` -- the equivalents
    # are `count_by_module`/`count_by_action`/`count_by_severity`/
    # `count_by_status`/`get_total_count` (statistics), `get_latest_activities`
    # (recent), `get_recent_failed_logs`/`get_recent_critical_logs`, and
    # `list_logs(user_id=..., module=..., entity_type=..., entity_id=...)`
    # for the by-user/by-module/by-entity lookups.

    async def test_get_statistics_counts_totals(self, audit_repo, bulk_audit_logs):
        total = await audit_repo.get_total_count()
        by_module = await audit_repo.count_by_module()
        by_severity = await audit_repo.count_by_severity()
        by_status = await audit_repo.count_by_status()

        assert total >= len(bulk_audit_logs)
        assert isinstance(by_module, dict)
        assert isinstance(by_severity, dict)
        assert isinstance(by_status, dict)

    async def test_get_recent_logs_returns_latest_first(self, audit_repo, bulk_audit_logs):
        results = await audit_repo.get_latest_activities(limit=3)

        assert len(results) == 3
        timestamps = [r.created_at for r in results]
        assert timestamps == sorted(timestamps, reverse=True)

    async def test_get_recent_logs_respects_limit(self, audit_repo, bulk_audit_logs):
        results = await audit_repo.get_latest_activities(limit=2)
        assert len(results) == 2

    async def test_get_failed_logs_only_returns_failures(self, audit_repo, audit_payload):
        audit_payload.status = AuditStatus.FAILED
        audit_payload.entity_id = str(uuid.uuid4())
        failed_log = await audit_repo.create(audit_payload.model_dump())

        results = await audit_repo.get_recent_failed_logs()

        assert any(r.id == failed_log.id for r in results)
        assert all(r.status == AuditStatus.FAILED for r in results)

    async def test_get_critical_logs_only_returns_critical_severity(
        self, audit_repo, audit_payload
    ):
        audit_payload.severity = AuditSeverity.CRITICAL
        audit_payload.entity_id = str(uuid.uuid4())
        critical_log = await audit_repo.create(audit_payload.model_dump())

        results = await audit_repo.get_recent_critical_logs()

        assert any(r.id == critical_log.id for r in results)
        assert all(r.severity == AuditSeverity.CRITICAL for r in results)

    async def test_get_by_user_returns_only_matching_logs(
        self, audit_repo, bulk_audit_logs, user_id
    ):
        results, total = await audit_repo.list_logs(user_id=user_id, page=1, page_size=20)
        assert total >= len(bulk_audit_logs)
        assert all(r.user_id == user_id for r in results)

    async def test_get_by_module_returns_only_matching_logs(self, audit_repo, bulk_audit_logs):
        results, total = await audit_repo.list_logs(
            module=AuditModule.BOOKING, page=1, page_size=20
        )
        assert total == 1
        assert results[0].module == AuditModule.BOOKING

    async def test_get_by_entity_returns_matching_logs(self, audit_repo, audit_payload, entity_id):
        await audit_repo.create(audit_payload.model_dump())
        results, total = await audit_repo.list_logs(
            entity_type="LEAD", entity_id=str(entity_id), page_size=100
        )

        assert total >= 1
        assert all(r.entity_id == str(entity_id) for r in results)


# --------------------------------------------------------------------------- #
# Bulk Delete / Cleanup
# --------------------------------------------------------------------------- #

class TestAuditLogBulkOperationsAndCleanup:
    async def test_bulk_delete_removes_multiple_logs(self, audit_repo, bulk_audit_logs):
        ids = [log.id for log in bulk_audit_logs[:3]]
        deleted_count = await audit_repo.bulk_delete(ids)

        assert deleted_count == 3
        for log_id in ids:
            fetched = await audit_repo.get_by_id(log_id)
            assert fetched is None

    async def test_bulk_delete_empty_list_returns_zero(self, audit_repo):
        deleted_count = await audit_repo.bulk_delete([])
        assert deleted_count == 0

    async def test_bulk_delete_nonexistent_ids_returns_zero(self, audit_repo):
        deleted_count = await audit_repo.bulk_delete([uuid.uuid4(), uuid.uuid4()])
        assert deleted_count == 0

    async def test_cleanup_removes_logs_older_than_threshold(
        self, audit_repo, db_session, audit_payload
    ):
        # NOTE: no `cleanup(older_than_days=...)`/`get_by_id_any_status()` on
        # the repository -- the real retention method is
        # `delete_old_logs(before: datetime)`, and there is no
        # status-agnostic id lookup beyond `get_by_id` (there's no
        # soft-delete/status concept on AuditLog to distinguish).
        old_log = await audit_repo.create(audit_payload.model_dump())
        old_log.created_at = datetime.now(timezone.utc) - timedelta(days=400)
        db_session.add(old_log)
        await db_session.flush()

        threshold = datetime.now(timezone.utc) - timedelta(days=365)
        deleted_count = await audit_repo.delete_old_logs(threshold)

        assert deleted_count >= 1
        fetched = await audit_repo.get_by_id(old_log.id)
        assert fetched is None

    async def test_cleanup_preserves_recent_logs(self, audit_repo, created_audit_log):
        threshold = datetime.now(timezone.utc) - timedelta(days=365)
        deleted_count = await audit_repo.delete_old_logs(threshold)

        fetched = await audit_repo.get_by_id(created_audit_log.id)
        assert fetched is not None
        assert deleted_count == 0 or fetched.id == created_audit_log.id

    async def test_cleanup_returns_count_of_removed_logs(
        self, audit_repo, db_session, audit_payload
    ):
        stale_logs = []
        for i in range(3):
            audit_payload.entity_id = str(uuid.uuid4())
            log = await audit_repo.create(audit_payload.model_dump())
            log.created_at = datetime.now(timezone.utc) - timedelta(days=500)
            db_session.add(log)
            stale_logs.append(log)
        await db_session.flush()

        threshold = datetime.now(timezone.utc) - timedelta(days=365)
        deleted_count = await audit_repo.delete_old_logs(threshold)
        assert deleted_count == 3