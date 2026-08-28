"""
Notification Module - Phase 5
Repository Layer Test Suite

Covers:
    - CRUD
    - Pagination
    - Filtering
    - Sorting
    - Search
    - Notification Queue
    - Notification Logs
    - Templates
    - Soft Delete
    - Bulk Notifications
    - Retry Queue
    - Delivery Status
    - Read Status

These tests exercise the NotificationRepository directly against a real
async SQLAlchemy session (test database), bypassing the HTTP layer entirely.
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
from app.models.notification import (
    Notification,
    NotificationCategory,
    NotificationChannel,
    NotificationLog,
    NotificationPriority,
    NotificationQueue,
    NotificationStatus,
    NotificationTemplate,
)
from app.repositories.log_repository import LogRepository
from app.repositories.notification_repository import NotificationRepository
from app.repositories.queue_repository import QueueRepository
from app.repositories.template_repository import TemplateRepository
from app.models.notification_template import NotificationTemplate
from app.models.notification_queue import QueueStatus
from app.models.notification_log import NotificationEventType
from app.services.template_service import TemplateService
from app.schemas.notification import NotificationCreate, NotificationUpdate
from app.schemas.template import TemplateCreate

pytestmark = pytest.mark.asyncio

# NOTE: this file provisions its OWN schema via `Base.metadata.create_all()`/
# `drop_all()` (see the `async_engine` fixture below) rather than relying on
# Alembic migrations. Pointing that lifecycle at the same database this
# project's Alembic migrations manage (`settings.DATABASE_URL`) is unsafe
# for two independent reasons discovered while fixing this file:
#   1. `Base.metadata` covers every table in the whole app, so `drop_all()`
#      at teardown would wipe the entire shared schema, not just this
#      module's tables.
#   2. More importantly: this project's actual Alembic-migrated
#      `notifications` table (via `alembic/versions/notification_module.py`)
#      has drifted significantly from the current `Notification` ORM model
#      -- e.g. `recipient_id`/`sender_id` are `Integer` in the migration
#      but `UUID` on the model, `payload`/`notification_type`/`last_error`
#      vs. the model's `body`/`category`/`failure_reason`. That is a real,
#      separate production bug (the migration and model must be
#      reconciled with a proper corrective migration -- out of scope for
#      this test file to paper over) that has nothing to do with these
#      tests, but running this file's create_all-based tests against that
#      drifted table surfaces it as spurious `DatatypeMismatch` failures.
# This file's own create_all/drop_all design was always meant to be
# self-contained (build the ORM-correct schema fresh, tear it down
# after), so it gets its own dedicated scratch database instead --
# isolated from both the shared Alembic-managed database and every other
# test file, created on demand and left alone if it already exists.
TEST_DATABASE_URL = make_url(settings.DATABASE_URL).set(
    database=f"{make_url(settings.DATABASE_URL).database}_notif_scratch"
).render_as_string(hide_password=False)


def _ensure_scratch_database_exists(url: str) -> None:
    """Creates the scratch database for this test file if it doesn't exist yet.

    Uses a synchronous, autocommit connection to the admin `postgres`
    database, since `CREATE DATABASE` cannot run inside a transaction.
    Also ensures the `pgcrypto` and `vector` extensions this schema
    depends on (`gen_random_uuid()` defaults, `ai_usages.embedding_vector`)
    are present in the new database.
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


def _to_notification(payload: NotificationCreate, **overrides) -> Notification:
    """Convert a `NotificationCreate` payload into a persistable ORM entity.

    `NotificationRepository.create()`/`bulk_create()` operate on ORM
    `Notification` instances, not the pydantic creation schema -- the
    schema-to-ORM conversion is the responsibility of the service layer
    (see `NotificationService.create_notification`, which does exactly
    this: `Notification(**payload.model_dump(), created_by=created_by)`).
    This mirrors that conversion for repository-level tests that need to
    seed rows without going through the service.
    """
    data = payload.model_dump()
    data.update(overrides)
    return Notification(**data)


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
def notification_repo(db_session: AsyncSession) -> NotificationRepository:
    return NotificationRepository(db_session)


@pytest_asyncio.fixture
def queue_repo(db_session: AsyncSession) -> QueueRepository:
    return QueueRepository(db_session)


@pytest_asyncio.fixture
def log_repo(db_session: AsyncSession) -> LogRepository:
    return LogRepository(db_session)


@pytest_asyncio.fixture
def template_repo(db_session: AsyncSession) -> TemplateRepository:
    return TemplateRepository(db_session)


@pytest_asyncio.fixture
def tenant_id() -> uuid.UUID:
    return uuid.uuid4()


@pytest_asyncio.fixture
def recipient_id() -> uuid.UUID:
    return uuid.uuid4()


@pytest_asyncio.fixture
def notification_payload(tenant_id, recipient_id) -> NotificationCreate:
    # NOTE: `category` is required (checked against NotificationBase in
    # app/schemas/notification.py -- added alongside the notification
    # `category` column). The body/metadata field names below are also
    # corrected to match the schema: `body` (not `message`) and
    # `metadata_payload` (not `metadata`). `tenant_id` has no corresponding
    # schema field and is silently dropped by pydantic's default "ignore
    # extra fields" behavior; left as-is rather than removed everywhere,
    # to keep this diff focused on what actually breaks the tests.
    return NotificationCreate(
        recipient_id=recipient_id,
        channel=NotificationChannel.EMAIL,
        category=NotificationCategory.APPOINTMENT,
        priority=NotificationPriority.NORMAL,
        subject="Property Viewing Confirmed",
        body="Your viewing for 221B Baker Street has been confirmed.",
        metadata_payload={"property_id": str(uuid.uuid4())},
    )


@pytest_asyncio.fixture
async def created_notification(
    notification_repo: NotificationRepository, notification_payload: NotificationCreate
) -> Notification:
    return await notification_repo.create(_to_notification(notification_payload))


@pytest_asyncio.fixture
async def bulk_notifications(
    notification_repo: NotificationRepository, tenant_id, recipient_id
) -> list[Notification]:
    channels = [
        NotificationChannel.EMAIL,
        NotificationChannel.SMS,
        NotificationChannel.WHATSAPP,
        NotificationChannel.PUSH,
        NotificationChannel.IN_APP,
    ]
    payloads = [
        NotificationCreate(
            recipient_id=recipient_id,
            channel=channel,
            category=NotificationCategory.SYSTEM,
            priority=NotificationPriority.HIGH,
            subject=f"Bulk Notification {i}",
            body=f"Bulk message body {i}",
        )
        for i, channel in enumerate(channels)
    ]
    return await notification_repo.bulk_create([_to_notification(p) for p in payloads])


# --------------------------------------------------------------------------- #
# CRUD
# --------------------------------------------------------------------------- #

class TestNotificationCRUD:
    async def test_create_notification_persists_all_fields(
        self, notification_repo, notification_payload
    ):
        notification = await notification_repo.create(_to_notification(notification_payload))

        assert notification.id is not None
        assert notification.subject == notification_payload.subject
        assert notification.body == notification_payload.body
        assert notification.channel == NotificationChannel.EMAIL
        assert notification.status == NotificationStatus.PENDING
        assert notification.is_deleted is False
        assert notification.created_at is not None

    async def test_get_by_id_returns_correct_notification(
        self, notification_repo, created_notification
    ):
        fetched = await notification_repo.get_by_id(created_notification.id)

        assert fetched is not None
        assert fetched.id == created_notification.id
        assert fetched.subject == created_notification.subject

    async def test_get_by_id_returns_none_for_missing_record(self, notification_repo):
        fetched = await notification_repo.get_by_id(uuid.uuid4())
        assert fetched is None

    async def test_update_notification_modifies_fields(
        self, notification_repo, created_notification
    ):
        # NOTE: NotificationRepository has no `update()` method, and
        # `NotificationUpdate` has no `subject`/`body` fields (checked
        # against app/repositories/notification_repository.py and
        # app/schemas/notification.py) -- mutable-field updates go through
        # `update_fields(id, values_dict)` directly, and the model's body
        # column is named `body`, not `message`.
        updated = await notification_repo.update_fields(
            created_notification.id,
            {"subject": "Updated Subject", "body": "Updated message body"},
        )

        assert updated.subject == "Updated Subject"
        assert updated.body == "Updated message body"
        assert updated.updated_at >= created_notification.created_at

    async def test_update_nonexistent_notification_returns_none(self, notification_repo):
        result = await notification_repo.update_fields(uuid.uuid4(), {"subject": "Ghost"})
        assert result is None

    async def test_delete_notification_hard_delete_removes_row(
        self, notification_repo, db_session, created_notification
    ):
        await notification_repo.hard_delete(created_notification.id)

        result = await db_session.execute(
            select(Notification).where(Notification.id == created_notification.id)
        )
        assert result.scalar_one_or_none() is None


# --------------------------------------------------------------------------- #
# Soft Delete
# --------------------------------------------------------------------------- #

class TestSoftDelete:
    async def test_soft_delete_sets_flag_and_timestamp(
        self, notification_repo, created_notification
    ):
        # `soft_delete` requires an explicit `deleted_at` and returns a
        # bool (see NotificationRepository.soft_delete), not the updated
        # row -- re-fetch to inspect the persisted state.
        deleted_at = datetime.now(timezone.utc)
        ok = await notification_repo.soft_delete(created_notification.id, deleted_at)
        assert ok is True

        result = await notification_repo.list_notifications(
            page=1, page_size=20, include_deleted=True
        )
        results, _ = result
        updated = next(n for n in results if n.id == created_notification.id)
        assert updated.is_deleted is True
        assert updated.deleted_at is not None

    async def test_soft_deleted_record_excluded_from_default_list(
        self, notification_repo, created_notification, tenant_id
    ):
        await notification_repo.soft_delete(created_notification.id, datetime.now(timezone.utc))

        results, total = await notification_repo.list_notifications(
            page=1, page_size=20, include_deleted=False
        )

        assert total == 0
        assert created_notification.id not in [n.id for n in results]

    async def test_soft_deleted_record_included_when_flag_set(
        self, notification_repo, created_notification, tenant_id
    ):
        await notification_repo.soft_delete(created_notification.id, datetime.now(timezone.utc))

        results, total = await notification_repo.list_notifications(
            page=1, page_size=20, include_deleted=True
        )

        assert total == 1
        assert created_notification.id in [n.id for n in results]

    async def test_restore_soft_deleted_notification(
        self, notification_repo, created_notification
    ):
        await notification_repo.soft_delete(created_notification.id, datetime.now(timezone.utc))
        restored = await notification_repo.restore(created_notification.id)

        assert restored.is_deleted is False
        assert restored.deleted_at is None


# --------------------------------------------------------------------------- #
# Pagination
# --------------------------------------------------------------------------- #

class TestPagination:
    @pytest_asyncio.fixture(autouse=False)
    async def seeded_notifications(self, notification_repo, tenant_id, recipient_id):
        payloads = [
            NotificationCreate(
                recipient_id=recipient_id,
                channel=NotificationChannel.EMAIL,
                category=NotificationCategory.SYSTEM,
                priority=NotificationPriority.LOW,
                subject=f"Notification {i}",
                body="Body",
            )
            for i in range(25)
        ]
        return await notification_repo.bulk_create([_to_notification(p) for p in payloads])

    async def test_list_returns_default_page_size(
        self, notification_repo, seeded_notifications, tenant_id
    ):
        results, total = await notification_repo.list_notifications(
            page=1, page_size=10
        )
        assert len(results) == 10
        assert total == 25

    async def test_list_second_page_returns_remaining_offset_records(
        self, notification_repo, seeded_notifications, tenant_id
    ):
        results, total = await notification_repo.list_notifications(
            page=3, page_size=10
        )
        assert len(results) == 5
        assert total == 25

    async def test_list_page_beyond_range_returns_empty(
        self, notification_repo, seeded_notifications, tenant_id
    ):
        results, total = await notification_repo.list_notifications(
            page=99, page_size=10
        )
        assert results == []
        assert total == 25

    async def test_list_respects_max_page_size_cap(
        self, notification_repo, seeded_notifications, tenant_id
    ):
        results, _ = await notification_repo.list_notifications(
            page=1, page_size=500
        )
        assert len(results) <= 100  # enterprise safety cap


# --------------------------------------------------------------------------- #
# Filtering
# --------------------------------------------------------------------------- #

class TestFiltering:
    async def test_filter_by_channel(self, notification_repo, bulk_notifications, tenant_id):
        results, total = await notification_repo.list_notifications(
            page=1, page_size=20, channel=NotificationChannel.SMS
        )
        assert total == 1
        assert results[0].channel == NotificationChannel.SMS

    async def test_filter_by_status(self, notification_repo, created_notification, tenant_id):
        results, total = await notification_repo.list_notifications(
            page=1, page_size=20, status=NotificationStatus.PENDING
        )
        assert total >= 1
        assert all(n.status == NotificationStatus.PENDING for n in results)

    async def test_filter_by_priority(
        self, notification_repo, bulk_notifications, tenant_id
    ):
        results, total = await notification_repo.list_notifications(
            page=1,
            page_size=20,
            priority=NotificationPriority.HIGH,
        )
        assert total == len(bulk_notifications)

    async def test_filter_by_recipient_id(
        self, notification_repo, created_notification, tenant_id, recipient_id
    ):
        results, total = await notification_repo.list_notifications(
            page=1, page_size=20, recipient_id=recipient_id
        )
        assert total >= 1
        assert all(n.recipient_id == recipient_id for n in results)

    async def test_filter_by_date_range(
        self, notification_repo, created_notification, tenant_id
    ):
        now = datetime.now(timezone.utc)
        results, total = await notification_repo.list_notifications(
            page=1,
            page_size=20,
            date_from=now - timedelta(hours=1),
            date_to=now + timedelta(hours=1),
        )
        assert total >= 1

    # NOTE: test_filter_by_tenant_isolation removed. Checked
    # `NotificationRepository.list_notifications()` (its full parameter
    # list is above) and the `Notification` model/`NotificationCreate`
    # schema: none of the three has any `tenant_id` concept at this layer
    # -- `tenant_id` passed into `NotificationCreate(...)` elsewhere in
    # this file is silently dropped by pydantic (unrecognized field) and
    # never reaches the database. There is currently nothing in this
    # module for a tenant-isolation test to actually exercise; the
    # original test only passed by coincidence (asserting an empty
    # unrelated list), not because isolation was implemented.

    async def test_combined_filters_channel_and_priority(
        self, notification_repo, bulk_notifications, tenant_id
    ):
        results, total = await notification_repo.list_notifications(
            page=1,
            page_size=20,
            channel=NotificationChannel.PUSH,
            priority=NotificationPriority.HIGH,
        )
        assert total == 1
        assert results[0].channel == NotificationChannel.PUSH


# --------------------------------------------------------------------------- #
# Sorting
# --------------------------------------------------------------------------- #

class TestSorting:
    async def test_sort_by_created_at_descending(
        self, notification_repo, bulk_notifications, tenant_id
    ):
        results, _ = await notification_repo.list_notifications(
            page=1, page_size=20, sort_by="created_at", sort_desc=True
        )
        timestamps = [n.created_at for n in results]
        assert timestamps == sorted(timestamps, reverse=True)

    async def test_sort_by_created_at_ascending(
        self, notification_repo, bulk_notifications, tenant_id
    ):
        results, _ = await notification_repo.list_notifications(
            page=1, page_size=20, sort_by="created_at", sort_desc=False
        )
        timestamps = [n.created_at for n in results]
        assert timestamps == sorted(timestamps)

    async def test_sort_by_priority(self, notification_repo, bulk_notifications, tenant_id):
        results, _ = await notification_repo.list_notifications(
            page=1, page_size=20, sort_by="priority", sort_desc=True
        )
        assert len(results) == len(bulk_notifications)

    async def test_invalid_sort_field_falls_back_to_default(
        self, notification_repo, bulk_notifications, tenant_id
    ):
        results, total = await notification_repo.list_notifications(
            page=1, page_size=20, sort_by="not_a_real_column"
        )
        assert total == len(bulk_notifications)


# --------------------------------------------------------------------------- #
# Search
# --------------------------------------------------------------------------- #

class TestSearch:
    async def test_search_matches_subject(
        self, notification_repo, created_notification, tenant_id
    ):
        results, total = await notification_repo.list_notifications(
            page=1, page_size=20, search_term="Viewing Confirmed"
        )
        assert total == 1
        assert results[0].id == created_notification.id

    async def test_search_matches_message_body(
        self, notification_repo, created_notification, tenant_id
    ):
        results, total = await notification_repo.list_notifications(
            page=1, page_size=20, search_term="Baker Street"
        )
        assert total == 1

    async def test_search_is_case_insensitive(
        self, notification_repo, created_notification, tenant_id
    ):
        results, total = await notification_repo.list_notifications(
            page=1, page_size=20, search_term="baker street"
        )
        assert total == 1

    async def test_search_with_no_match_returns_empty(
        self, notification_repo, created_notification, tenant_id
    ):
        results, total = await notification_repo.list_notifications(
            page=1, page_size=20, search_term="Nonexistent Term XYZ"
        )
        assert total == 0
        assert results == []


# --------------------------------------------------------------------------- #
# Notification Queue
# --------------------------------------------------------------------------- #

class TestNotificationQueue:
    # NOTE ON THIS CLASS: adapted to the real `QueueRepository` API
    # (checked against app/repositories/queue_repository.py). `enqueue()`
    # persists an ORM `NotificationQueue` instance directly (not
    # `notification_id`/`priority` kwargs); there is no `dequeue_next()`,
    # `get_pending()`, or `mark_processing()` -- claiming/marking-processing
    # is done atomically in batch via `fetch_next_batch(worker_id,
    # locked_at, batch_size)`, and listing is `list_queue_entries(...)`.
    # The freshly-enqueued status is `QueueStatus.WAITING` (a
    # `NotificationQueue`-specific enum), not `NotificationStatus.QUEUED`.

    async def test_enqueue_notification_creates_queue_entry(
        self, queue_repo, created_notification
    ):
        queue_entry = await queue_repo.enqueue(
            NotificationQueue(
                notification_id=created_notification.id,
                priority=NotificationPriority.NORMAL,
            )
        )
        assert queue_entry.id is not None
        assert queue_entry.notification_id == created_notification.id
        assert queue_entry.status == QueueStatus.WAITING

    async def test_dequeue_returns_next_highest_priority_item(
        self, queue_repo, notification_repo, tenant_id, recipient_id
    ):
        low = await notification_repo.create(
            _to_notification(
                NotificationCreate(
                    recipient_id=recipient_id,
                    channel=NotificationChannel.EMAIL,
                    category=NotificationCategory.SYSTEM,
                    priority=NotificationPriority.LOW,
                    subject="Low",
                    body="Low priority",
                )
            )
        )
        urgent = await notification_repo.create(
            _to_notification(
                NotificationCreate(
                    recipient_id=recipient_id,
                    channel=NotificationChannel.EMAIL,
                    category=NotificationCategory.SYSTEM,
                    priority=NotificationPriority.URGENT,
                    subject="Urgent",
                    body="Urgent priority",
                )
            )
        )
        await queue_repo.enqueue(
            NotificationQueue(notification_id=low.id, priority=NotificationPriority.LOW)
        )
        await queue_repo.enqueue(
            NotificationQueue(notification_id=urgent.id, priority=NotificationPriority.URGENT)
        )

        claimed = await queue_repo.fetch_next_batch(
            worker_id="test-worker", locked_at=datetime.now(timezone.utc), batch_size=1
        )

        assert len(claimed) == 1
        assert claimed[0].notification_id == urgent.id

    async def test_dequeue_empty_queue_returns_none(self, queue_repo):
        claimed = await queue_repo.fetch_next_batch(
            worker_id="test-worker", locked_at=datetime.now(timezone.utc), batch_size=1
        )
        assert claimed == []

    async def test_get_pending_queue_items(self, queue_repo, created_notification):
        await queue_repo.enqueue(
            NotificationQueue(
                notification_id=created_notification.id, priority=NotificationPriority.NORMAL
            )
        )
        pending, total = await queue_repo.list_queue_entries(status=QueueStatus.WAITING)
        assert len(pending) >= 1
        assert total >= 1

    async def test_mark_queue_item_processing(self, queue_repo, created_notification):
        await queue_repo.enqueue(
            NotificationQueue(
                notification_id=created_notification.id, priority=NotificationPriority.NORMAL
            )
        )
        claimed = await queue_repo.fetch_next_batch(
            worker_id="test-worker", locked_at=datetime.now(timezone.utc), batch_size=1
        )
        assert len(claimed) == 1
        updated = claimed[0]
        assert updated.status == QueueStatus.PROCESSING
        assert updated.locked_at is not None


# --------------------------------------------------------------------------- #
# Notification Logs
# --------------------------------------------------------------------------- #

class TestNotificationLogs:
    # NOTE ON THIS CLASS: adapted to the real `LogRepository` API (checked
    # against app/repositories/log_repository.py and the `NotificationLog`
    # model). `create_log()` persists an ORM `NotificationLog` instance
    # (not `event`/`details` kwargs) -- the model's fields are
    # `event_type` (a `NotificationEventType` enum), a required `status`
    # (`NotificationStatus`), and `error_message` (not a generic
    # "details" string). Lookup/listing is `list_logs_for_notification(...)`,
    # not `get_by_notification_id()`/`list_paginated()`.

    async def test_create_log_entry(self, log_repo, created_notification):
        log = await log_repo.create_log(
            NotificationLog(
                notification_id=created_notification.id,
                event_type=NotificationEventType.SENT,
                status=NotificationStatus.SENT,
                error_message=None,
            )
        )
        assert log.id is not None
        assert log.event_type == NotificationEventType.SENT
        assert log.notification_id == created_notification.id

    async def test_get_logs_for_notification_ordered_chronologically(
        self, log_repo, created_notification
    ):
        await log_repo.create_log(
            NotificationLog(
                notification_id=created_notification.id,
                event_type=NotificationEventType.QUEUED,
                status=NotificationStatus.QUEUED,
            )
        )
        await log_repo.create_log(
            NotificationLog(
                notification_id=created_notification.id,
                event_type=NotificationEventType.SENT,
                status=NotificationStatus.SENT,
            )
        )
        await log_repo.create_log(
            NotificationLog(
                notification_id=created_notification.id,
                event_type=NotificationEventType.DELIVERED,
                status=NotificationStatus.DELIVERED,
            )
        )

        logs, total = await log_repo.list_logs_for_notification(
            created_notification.id, sort_desc=False, page=1, page_size=20
        )

        assert total == 3
        events = [log.event_type for log in logs]
        assert events == [
            NotificationEventType.QUEUED,
            NotificationEventType.SENT,
            NotificationEventType.DELIVERED,
        ]

    async def test_get_logs_paginated(self, log_repo, created_notification):
        for i in range(15):
            await log_repo.create_log(
                NotificationLog(
                    notification_id=created_notification.id,
                    event_type=NotificationEventType.DISPATCHED,
                    status=NotificationStatus.QUEUED,
                    error_message=f"Detail {i}",
                )
            )

        logs, total = await log_repo.list_logs_for_notification(
            created_notification.id, page=1, page_size=10
        )
        assert len(logs) == 10
        assert total == 15

    async def test_logs_for_nonexistent_notification_return_empty(self, log_repo):
        logs, total = await log_repo.list_logs_for_notification(uuid.uuid4())
        assert logs == []
        assert total == 0


# --------------------------------------------------------------------------- #
# Templates
# --------------------------------------------------------------------------- #
#
# NOTE ON THIS TEST CLASS: it originally exercised a `TemplateRepository`
# API (`create(TemplateCreate)`, `get_by_name_and_channel(...)`,
# `render(template, context=...)`, `list(...)`, and a `tenant_id` scope)
# that does not match the current repository. Checked against the actual
# `NotificationTemplate` model and `TemplateRepository`
# (app/repositories/template_repository.py):
#   - The repository's `create()` persists an ORM `NotificationTemplate`
#     instance (it just does `session.add()`/`flush()`/`refresh()`), not a
#     `TemplateCreate` pydantic payload -- payload -> ORM construction is
#     the job of `TemplateService.create_template()`.
#   - Templates are looked up by `(code, channel, locale)` via
#     `get_active_by_code()`, not by `(tenant_id, name, channel)` via a
#     `get_by_name_and_channel()` that doesn't exist. There is no
#     `tenant_id` column on `NotificationTemplate` at all.
#   - Rendering (`render()`) lives on `TemplateService`, not on the
#     repository -- the repository is data-access only, per this file's
#     own module docstring ("Contains only database access operations").
#   - Listing is `list_templates(...)`, not `list(...)`.
# The repository is otherwise fully-formed and self-consistent with the
# model, so this class is updated to exercise that real API rather than
# the repository being reshaped around a stale test.

class TestNotificationTemplates:
    @pytest_asyncio.fixture
    def template_payload(self) -> NotificationTemplate:
        return NotificationTemplate(
            code="viewing_confirmation",
            name="viewing_confirmation",
            channel=NotificationChannel.EMAIL,
            subject_template="Viewing Confirmed for {{property_name}}",
            body_template="Hi {{recipient_name}}, your viewing is confirmed for {{date}}.",
            is_active=True,
        )

    async def test_create_template(self, template_repo, template_payload):
        template = await template_repo.create(template_payload)
        assert template.id is not None
        assert template.name == "viewing_confirmation"
        assert template.is_active is True

    async def test_get_template_by_name_and_channel(self, template_repo, template_payload):
        await template_repo.create(template_payload)
        template = await template_repo.get_active_by_code(
            code="viewing_confirmation",
            channel=NotificationChannel.EMAIL,
        )
        assert template is not None
        assert template.name == "viewing_confirmation"

    async def test_duplicate_template_name_raises_conflict(
        self, template_repo, db_session
    ):
        first = NotificationTemplate(
            code="viewing_confirmation",
            name="viewing_confirmation",
            channel=NotificationChannel.EMAIL,
            subject_template="Viewing Confirmed",
            body_template="Hi there.",
            is_active=True,
            version=1,
        )
        duplicate = NotificationTemplate(
            code="viewing_confirmation",
            name="viewing_confirmation",
            channel=NotificationChannel.EMAIL,
            subject_template="Viewing Confirmed",
            body_template="Hi there.",
            is_active=True,
            version=1,
        )
        await template_repo.create(first)
        with pytest.raises(Exception):
            await template_repo.create(duplicate)
            await db_session.flush()

    async def test_render_template_replaces_placeholders(
        self, template_repo, template_payload
    ):
        await template_repo.create(template_payload)
        service = TemplateService(template_repo)
        rendered = await service.render(
            code="viewing_confirmation",
            channel=NotificationChannel.EMAIL,
            variables={
                "property_name": "221B Baker Street",
                "recipient_name": "John Watson",
                "date": "2026-08-05",
            },
        )
        assert "221B Baker Street" in rendered.subject
        assert "John Watson" in rendered.body
        assert "{{" not in rendered.subject
        assert "{{" not in rendered.body

    async def test_deactivate_template(self, template_repo, template_payload):
        template = await template_repo.create(template_payload)
        deactivated = await template_repo.deactivate(template.id)
        assert deactivated.is_active is False

    async def test_list_templates_filtered_by_channel(
        self, template_repo, template_payload
    ):
        await template_repo.create(template_payload)
        templates, total = await template_repo.list_templates(
            channel=NotificationChannel.EMAIL, page=1, page_size=20
        )
        assert total == 1
        assert templates[0].channel == NotificationChannel.EMAIL


# --------------------------------------------------------------------------- #
# Bulk Notifications
# --------------------------------------------------------------------------- #

class TestBulkNotifications:
    async def test_bulk_create_inserts_all_records(self, bulk_notifications):
        assert len(bulk_notifications) == 5
        assert all(n.id is not None for n in bulk_notifications)

    async def test_bulk_create_preserves_channel_order(self, bulk_notifications):
        channels = [n.channel for n in bulk_notifications]
        assert channels == [
            NotificationChannel.EMAIL,
            NotificationChannel.SMS,
            NotificationChannel.WHATSAPP,
            NotificationChannel.PUSH,
            NotificationChannel.IN_APP,
        ]

    async def test_bulk_create_empty_list_returns_empty(self, notification_repo):
        results = await notification_repo.bulk_create([])
        assert results == []

    async def test_bulk_update_status(self, notification_repo, bulk_notifications):
        ids = [n.id for n in bulk_notifications]
        updated_count = await notification_repo.bulk_update_status(
            ids, status=NotificationStatus.SENT
        )
        assert updated_count == len(ids)

        for notification_id in ids:
            record = await notification_repo.get_by_id(notification_id)
            assert record.status == NotificationStatus.SENT


# --------------------------------------------------------------------------- #
# Retry Queue
# --------------------------------------------------------------------------- #

class TestRetryQueue:
    async def test_mark_failed_increments_retry_count(
        self, notification_repo, created_notification
    ):
        result = await notification_repo.mark_failed(
            created_notification.id, error_message="SMTP timeout"
        )
        assert result.status == NotificationStatus.FAILED
        assert result.retry_count == 1
        assert result.failure_reason == "SMTP timeout"

    async def test_repeated_failures_increment_retry_count_further(
        self, notification_repo, created_notification
    ):
        # `Notification.retry_count` is CHECK-constrained to <= max_retries
        # (default 3, see app/models/notification.py), so this stays within
        # that ceiling rather than exceeding it.
        await notification_repo.mark_failed(created_notification.id, error_message="Timeout 1")
        result = await notification_repo.mark_failed(
            created_notification.id, error_message="Timeout 2"
        )
        assert result.retry_count == 2

    async def test_get_retryable_notifications_below_max_attempts(
        self, notification_repo, created_notification
    ):
        await notification_repo.mark_failed(created_notification.id, error_message="Timeout")
        retryable = await notification_repo.get_retryable(max_retries=3)
        assert created_notification.id in [n.id for n in retryable]

    async def test_notifications_exceeding_max_retries_excluded(
        self, notification_repo, created_notification
    ):
        # Default `max_retries` on the model is 3, and `retry_count` is
        # CHECK-constrained to never exceed it -- so "exceeding" is
        # exercised by driving retry_count up to that ceiling (3) and then
        # querying get_retryable() with a lower threshold, rather than by
        # actually pushing retry_count past max_retries (which the
        # database would reject).
        await notification_repo.mark_failed(created_notification.id, error_message="Timeout 1")
        await notification_repo.mark_failed(created_notification.id, error_message="Timeout 2")
        await notification_repo.mark_failed(created_notification.id, error_message="Timeout 3")

        retryable = await notification_repo.get_retryable(max_retries=2)
        assert created_notification.id not in [n.id for n in retryable]

    async def test_reset_retry_count_on_manual_retry(
        self, notification_repo, created_notification
    ):
        await notification_repo.mark_failed(created_notification.id, error_message="Timeout")
        reset = await notification_repo.reset_retry(created_notification.id)
        assert reset.retry_count == 0
        assert reset.status == NotificationStatus.PENDING


# --------------------------------------------------------------------------- #
# Delivery Status
# --------------------------------------------------------------------------- #

class TestDeliveryStatus:
    async def test_mark_sent_updates_status_and_timestamp(
        self, notification_repo, created_notification
    ):
        result = await notification_repo.mark_sent(created_notification.id)
        assert result.status == NotificationStatus.SENT
        assert result.sent_at is not None

    async def test_mark_delivered_updates_status_and_timestamp(
        self, notification_repo, created_notification
    ):
        await notification_repo.mark_sent(created_notification.id)
        result = await notification_repo.mark_delivered(created_notification.id)
        assert result.status == NotificationStatus.DELIVERED
        assert result.delivered_at is not None

    async def test_get_delivery_statistics_by_channel(
        self, notification_repo, bulk_notifications, tenant_id
    ):
        for n in bulk_notifications[:2]:
            await notification_repo.mark_sent(n.id)
            await notification_repo.mark_delivered(n.id)
        for n in bulk_notifications[2:3]:
            await notification_repo.mark_failed(n.id, error_message="Bounced")

        stats = await notification_repo.get_delivery_stats()

        assert stats["total"] == 5
        assert stats["delivered"] == 2
        assert stats["failed"] == 1


# --------------------------------------------------------------------------- #
# Read Status
# --------------------------------------------------------------------------- #

class TestReadStatus:
    async def test_mark_as_read_sets_flag_and_timestamp(
        self, notification_repo, created_notification
    ):
        result = await notification_repo.mark_as_read(
            created_notification.id, datetime.now(timezone.utc)
        )
        assert result.is_read is True
        assert result.read_at is not None

    async def test_mark_all_as_read_for_recipient(
        self, notification_repo, bulk_notifications, recipient_id
    ):
        updated_count = await notification_repo.mark_all_as_read(recipient_id=recipient_id)
        assert updated_count == len(bulk_notifications)

    async def test_get_unread_count_for_recipient(
        self, notification_repo, bulk_notifications, recipient_id
    ):
        count = await notification_repo.get_unread_count(recipient_id=recipient_id)
        assert count == len(bulk_notifications)

    async def test_unread_count_decreases_after_marking_read(
        self, notification_repo, bulk_notifications, recipient_id
    ):
        await notification_repo.mark_as_read(bulk_notifications[0].id, datetime.now(timezone.utc))
        count = await notification_repo.get_unread_count(recipient_id=recipient_id)
        assert count == len(bulk_notifications) - 1

    async def test_unread_count_is_zero_for_unknown_recipient(self, notification_repo):
        count = await notification_repo.get_unread_count(recipient_id=uuid.uuid4())
        assert count == 0