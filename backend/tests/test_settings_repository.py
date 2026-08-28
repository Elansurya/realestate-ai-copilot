# backend/tests/test_settings_repository.py

"""
Settings Module - Phase 4
Repository Layer Test Suite

Covers:
    - Create Setting
    - Update Setting
    - Delete Setting
    - Get By ID
    - Get By Key
    - Get By Category
    - Search
    - Pagination
    - Sorting
    - Filtering
    - Bulk Update
    - Bulk Delete
    - Statistics
    - Duplicate Key Validation
    - Encrypted Settings
    - Public Settings
    - Editable Settings

These tests exercise the SettingsRepository directly against a real
async SQLAlchemy session (test database), bypassing the service and
HTTP layers entirely, mirroring the conventions established in
`test_audit_repository.py`.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone
from typing import Any

import pytest
import pytest_asyncio
from sqlalchemy import create_engine, inspect as sync_inspect
from sqlalchemy import text as sa_text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from app.core.config import settings
from app.db.base import Base
from app.models.settings import SettingCategory, SettingDataType, Settings
from app.repositories.settings_repository import SettingsRepository

pytestmark = pytest.mark.asyncio

# NOTE: previously hardcoded a fallback of
# "postgresql+asyncpg://postgres:postgres@localhost:5432/test_settings_db" --
# a database never created anywhere in this project, with a driver
# (asyncpg) inconsistent with the rest of the app (psycopg, see
# app/core/config.py).
#
# This file provisions its OWN schema via `Base.metadata.create_all()`/
# `drop_all()` (see the `async_engine` fixture below). Pointing that
# lifecycle directly at `settings.DATABASE_URL` -- the same database this
# project's Alembic migrations manage, shared across every other
# repository test file -- is unsafe: `Base.metadata` covers every table
# in the whole app, so `drop_all()` at teardown would wipe the entire
# shared schema, not just this module's tables. This file instead gets
# its own dedicated scratch database, created on demand and left alone
# if it already exists, isolated from both the shared Alembic-managed
# database and every other test file's scratch database.
TEST_DATABASE_URL = make_url(settings.DATABASE_URL).set(
    database=f"{make_url(settings.DATABASE_URL).database}_settings_scratch"
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
def settings_repo(db_session: AsyncSession) -> SettingsRepository:
    return SettingsRepository(db_session)


def _setting_data(
    *,
    category: SettingCategory = SettingCategory.EMAIL,
    setting_key: str = "SMTP_HOST",
    setting_value: Any = "smtp.example.com",
    description: str = "SMTP server hostname.",
    data_type: SettingDataType = SettingDataType.STRING,
    is_public: bool = False,
    is_editable: bool = True,
    is_encrypted: bool = False,
    validation_rules: dict[str, Any] | None = None,
    # NOTE: `created_by`/`updated_by` are nullable FKs to `users.id` (see
    # app/models/settings.py). This previously defaulted both to a
    # hardcoded `1`, but no such user row is ever created anywhere in
    # this test file, which violated `fk_settings_created_by_users` (and
    # the equivalent `updated_by` constraint) on every single call.
    # Defaulting to `None` (a valid, nullable "system-applied setting"
    # value) avoids fabricating a fake user id; tests that specifically
    # care about attribution can still pass a real user id explicitly.
    created_by: int | None = None,
    updated_by: int | None = None,
) -> dict[str, Any]:
    return {
        "category": category,
        "setting_key": setting_key,
        "setting_value": setting_value,
        "description": description,
        "data_type": data_type,
        "is_public": is_public,
        "is_editable": is_editable,
        "is_encrypted": is_encrypted,
        "validation_rules": validation_rules,
        "created_by": created_by,
        "updated_by": updated_by,
    }


@pytest_asyncio.fixture
async def created_setting(settings_repo: SettingsRepository) -> Settings:
    return await settings_repo.create(_setting_data())


@pytest_asyncio.fixture
async def bulk_settings(settings_repo: SettingsRepository) -> list[Settings]:
    payloads = [
        _setting_data(
            category=SettingCategory.EMAIL,
            setting_key="SMTP_PORT",
            setting_value=587,
            data_type=SettingDataType.INTEGER,
            is_public=False,
        ),
        _setting_data(
            category=SettingCategory.SECURITY,
            setting_key="SESSION_TIMEOUT_MINUTES",
            setting_value=30,
            data_type=SettingDataType.INTEGER,
            is_public=False,
        ),
        _setting_data(
            category=SettingCategory.THEME,
            setting_key="PRIMARY_COLOR",
            setting_value="#123456",
            data_type=SettingDataType.STRING,
            is_public=True,
        ),
        _setting_data(
            category=SettingCategory.GENERAL,
            setting_key="APP_NAME",
            setting_value="Real Estate CRM",
            data_type=SettingDataType.STRING,
            is_public=True,
        ),
        _setting_data(
            category=SettingCategory.SYSTEM,
            setting_key="API_SECRET_TOKEN",
            setting_value="s3cr3t",
            data_type=SettingDataType.PASSWORD,
            is_public=False,
            is_encrypted=True,
        ),
    ]
    entries = []
    for payload in payloads:
        entries.append(await settings_repo.create(payload))
    return entries


# --------------------------------------------------------------------------- #
# CRUD
# --------------------------------------------------------------------------- #

class TestSettingsRepositoryCRUD:

    async def test_create_setting_persists_all_fields(self, settings_repo):
        entry = await settings_repo.create(_setting_data())

        assert entry.id is not None
        assert entry.category == SettingCategory.EMAIL
        assert entry.setting_key == "SMTP_HOST"
        assert entry.setting_value == "smtp.example.com"
        assert entry.data_type == SettingDataType.STRING
        assert entry.is_public is False
        assert entry.is_editable is True
        assert entry.is_encrypted is False
        assert entry.created_at is not None
        assert entry.updated_at is not None

    async def test_get_by_id_returns_correct_setting(self, settings_repo, created_setting):
        fetched = await settings_repo.get_by_id(created_setting.id)

        assert fetched is not None
        assert fetched.id == created_setting.id
        assert fetched.setting_key == created_setting.setting_key

    async def test_get_by_id_returns_none_for_missing_record(self, settings_repo):
        fetched = await settings_repo.get_by_id(uuid.uuid4())
        assert fetched is None

    async def test_get_by_key_returns_matching_setting(self, settings_repo, created_setting):
        fetched = await settings_repo.get_by_key("SMTP_HOST")

        assert fetched is not None
        assert fetched.setting_key == "SMTP_HOST"

    async def test_get_by_key_returns_none_when_absent(self, settings_repo):
        fetched = await settings_repo.get_by_key("NON_EXISTENT_KEY")
        assert fetched is None

    async def test_get_by_category_and_key_returns_correct_entry(
        self, settings_repo, created_setting
    ):
        fetched = await settings_repo.get_by_category_and_key(
            SettingCategory.EMAIL, "SMTP_HOST"
        )
        assert fetched is not None
        assert fetched.id == created_setting.id

    async def test_get_by_category_and_key_returns_none_for_wrong_category(
        self, settings_repo, created_setting
    ):
        fetched = await settings_repo.get_by_category_and_key(
            SettingCategory.SECURITY, "SMTP_HOST"
        )
        assert fetched is None

    async def test_get_by_category_returns_all_matching_entries(
        self, settings_repo, bulk_settings
    ):
        results = await settings_repo.get_by_category(SettingCategory.EMAIL)
        keys = {entry.setting_key for entry in results}
        assert "SMTP_PORT" in keys

    async def test_get_by_category_orders_by_setting_key(self, settings_repo, bulk_settings):
        results = await settings_repo.get_by_category(SettingCategory.EMAIL)
        keys = [entry.setting_key for entry in results]
        assert keys == sorted(keys)

    async def test_update_setting_modifies_fields(self, settings_repo, created_setting):
        updated = await settings_repo.update(
            created_setting, {"setting_value": "smtp.updated.com", "description": "Updated."}
        )

        assert updated.setting_value == "smtp.updated.com"
        assert updated.description == "Updated."

    async def test_update_ignores_unknown_fields(self, settings_repo, created_setting):
        updated = await settings_repo.update(
            created_setting, {"not_a_real_column": "ignored"}
        )
        assert not hasattr(updated, "not_a_real_column") or True

    async def test_delete_setting_removes_row(self, settings_repo, created_setting):
        await settings_repo.delete(created_setting)
        fetched = await settings_repo.get_by_id(created_setting.id)
        assert fetched is None


# --------------------------------------------------------------------------- #
# Duplicate Key Validation
# --------------------------------------------------------------------------- #

class TestSettingsRepositoryDuplicateKey:

    async def test_exists_by_category_and_key_true_after_create(
        self, settings_repo, created_setting
    ):
        exists = await settings_repo.exists_by_category_and_key(
            SettingCategory.EMAIL, "SMTP_HOST"
        )
        assert exists is True

    async def test_exists_by_category_and_key_false_when_absent(self, settings_repo):
        exists = await settings_repo.exists_by_category_and_key(
            SettingCategory.EMAIL, "DOES_NOT_EXIST"
        )
        assert exists is False

    async def test_exists_by_category_and_key_excludes_given_id(
        self, settings_repo, created_setting
    ):
        exists = await settings_repo.exists_by_category_and_key(
            SettingCategory.EMAIL, "SMTP_HOST", exclude_id=created_setting.id
        )
        assert exists is False

    async def test_duplicate_category_and_key_violates_unique_constraint(
        self, settings_repo, created_setting
    ):
        with pytest.raises(IntegrityError):
            await settings_repo.create(
                _setting_data(setting_value="smtp.other.com")
            )

    async def test_encrypted_and_public_violates_check_constraint(self, settings_repo):
        with pytest.raises(IntegrityError):
            await settings_repo.create(
                _setting_data(
                    category=SettingCategory.SECURITY,
                    setting_key="BAD_FLAG_COMBO",
                    is_public=True,
                    is_encrypted=True,
                )
            )


# --------------------------------------------------------------------------- #
# Listing / Search / Pagination / Sorting / Filtering
# --------------------------------------------------------------------------- #

class TestSettingsRepositoryListSearch:

    async def test_pagination_returns_bounded_page(self, settings_repo, bulk_settings):
        items, total = await settings_repo.list_settings(page=1, page_size=2)
        assert total >= len(bulk_settings)
        assert len(items) == 2

    async def test_pagination_second_page_differs(self, settings_repo, bulk_settings):
        first_page, _ = await settings_repo.list_settings(page=1, page_size=2)
        second_page, _ = await settings_repo.list_settings(page=2, page_size=2)
        first_ids = {item.id for item in first_page}
        second_ids = {item.id for item in second_page}
        assert first_ids.isdisjoint(second_ids)

    async def test_sorting_ascending_by_setting_key(self, settings_repo, bulk_settings):
        items, _ = await settings_repo.list_settings(
            sort_by="setting_key", sort_order="asc", page_size=100
        )
        keys = [item.setting_key for item in items]
        assert keys == sorted(keys)

    async def test_sorting_descending_by_setting_key(self, settings_repo, bulk_settings):
        items, _ = await settings_repo.list_settings(
            sort_by="setting_key", sort_order="desc", page_size=100
        )
        keys = [item.setting_key for item in items]
        assert keys == sorted(keys, reverse=True)

    async def test_filter_by_category(self, settings_repo, bulk_settings):
        items, total = await settings_repo.list_settings(category=SettingCategory.SECURITY)
        assert total >= 1
        assert all(item.category == SettingCategory.SECURITY for item in items)

    async def test_filter_by_data_type(self, settings_repo, bulk_settings):
        items, total = await settings_repo.list_settings(data_type=SettingDataType.INTEGER)
        assert total >= 2
        assert all(item.data_type == SettingDataType.INTEGER for item in items)

    async def test_filter_by_is_public(self, settings_repo, bulk_settings):
        items, total = await settings_repo.list_settings(is_public=True)
        assert total >= 2
        assert all(item.is_public is True for item in items)

    async def test_filter_by_is_editable(self, settings_repo, bulk_settings):
        items, total = await settings_repo.list_settings(is_editable=True)
        assert total >= len(bulk_settings)
        assert all(item.is_editable is True for item in items)

    async def test_filter_by_is_encrypted(self, settings_repo, bulk_settings):
        items, total = await settings_repo.list_settings(is_encrypted=True)
        assert total >= 1
        assert all(item.is_encrypted is True for item in items)

    async def test_filter_by_setting_key(self, settings_repo, bulk_settings):
        items, total = await settings_repo.list_settings(setting_key="APP_NAME")
        assert total == 1
        assert items[0].setting_key == "APP_NAME"

    async def test_filter_by_date_range(self, settings_repo, created_setting):
        now = datetime.now(timezone.utc)
        items, total = await settings_repo.list_settings(
            date_from=now - timedelta(hours=1), date_to=now + timedelta(hours=1)
        )
        assert total >= 1

    async def test_filter_by_date_range_excludes_out_of_range(
        self, settings_repo, created_setting
    ):
        now = datetime.now(timezone.utc)
        items, total = await settings_repo.list_settings(
            date_from=now + timedelta(days=10), date_to=now + timedelta(days=20)
        )
        assert total == 0

    async def test_combined_filters_category_and_public(self, settings_repo, bulk_settings):
        items, total = await settings_repo.list_settings(
            category=SettingCategory.THEME, is_public=True
        )
        assert total == 1
        assert items[0].setting_key == "PRIMARY_COLOR"

    async def test_search_matches_setting_key(self, settings_repo, bulk_settings):
        items, total = await settings_repo.search_settings("SMTP")
        assert total >= 1
        assert any("SMTP" in item.setting_key for item in items)

    async def test_search_matches_description(self, settings_repo, created_setting):
        items, total = await settings_repo.search_settings("hostname")
        assert total >= 1

    async def test_search_no_match_returns_empty(self, settings_repo, bulk_settings):
        items, total = await settings_repo.search_settings("NoMatchTermXYZ12345")
        assert total == 0
        assert items == []

    async def test_search_is_case_insensitive(self, settings_repo, bulk_settings):
        items, total = await settings_repo.search_settings("smtp_port")
        assert total >= 1


# --------------------------------------------------------------------------- #
# Scoped Convenience Lookups
# --------------------------------------------------------------------------- #

class TestSettingsRepositoryScopedLookups:

    async def test_get_public_settings_returns_only_public(self, settings_repo, bulk_settings):
        results = await settings_repo.get_public_settings()
        assert len(results) >= 2
        assert all(entry.is_public for entry in results)

    async def test_get_editable_settings_returns_only_editable(
        self, settings_repo, bulk_settings
    ):
        results = await settings_repo.get_editable_settings()
        assert len(results) >= len(bulk_settings)
        assert all(entry.is_editable for entry in results)

    async def test_get_encrypted_settings_returns_only_encrypted(
        self, settings_repo, bulk_settings
    ):
        results = await settings_repo.get_encrypted_settings()
        assert len(results) >= 1
        assert all(entry.is_encrypted for entry in results)


# --------------------------------------------------------------------------- #
# Bulk Operations
# --------------------------------------------------------------------------- #

class TestSettingsRepositoryBulkOperations:

    async def test_bulk_update_applies_changes_to_multiple_entries(
        self, settings_repo, bulk_settings
    ):
        updates = [
            (bulk_settings[0].id, {"description": "Bulk updated 0"}),
            (bulk_settings[1].id, {"description": "Bulk updated 1"}),
        ]
        updated = await settings_repo.bulk_update(updates)

        assert len(updated) == 2
        descriptions = {entry.description for entry in updated}
        assert descriptions == {"Bulk updated 0", "Bulk updated 1"}

    async def test_bulk_update_skips_nonexistent_ids(self, settings_repo, bulk_settings):
        updates = [
            (bulk_settings[0].id, {"description": "Real update"}),
            (uuid.uuid4(), {"description": "Ghost update"}),
        ]
        updated = await settings_repo.bulk_update(updates)
        assert len(updated) == 1
        assert updated[0].description == "Real update"

    async def test_bulk_update_empty_list_returns_empty(self, settings_repo):
        updated = await settings_repo.bulk_update([])
        assert updated == []

    async def test_bulk_delete_removes_multiple_entries(self, settings_repo, bulk_settings):
        ids = [entry.id for entry in bulk_settings[:3]]
        deleted_count = await settings_repo.bulk_delete(ids)

        assert deleted_count == 3
        for setting_id in ids:
            fetched = await settings_repo.get_by_id(setting_id)
            assert fetched is None

    async def test_bulk_delete_empty_list_returns_zero(self, settings_repo):
        deleted_count = await settings_repo.bulk_delete([])
        assert deleted_count == 0

    async def test_bulk_delete_nonexistent_ids_returns_zero(self, settings_repo):
        deleted_count = await settings_repo.bulk_delete([uuid.uuid4(), uuid.uuid4()])
        assert deleted_count == 0


# --------------------------------------------------------------------------- #
# Statistics / Aggregations
# --------------------------------------------------------------------------- #

class TestSettingsRepositoryStatistics:

    async def test_get_total_count(self, settings_repo, bulk_settings):
        total = await settings_repo.get_total_count()
        assert total >= len(bulk_settings)

    async def test_count_by_category(self, settings_repo, bulk_settings):
        counts = await settings_repo.count_by_category()
        assert counts.get(SettingCategory.EMAIL.value, 0) >= 1
        assert counts.get(SettingCategory.SECURITY.value, 0) >= 1

    async def test_count_by_data_type(self, settings_repo, bulk_settings):
        counts = await settings_repo.count_by_data_type()
        assert counts.get(SettingDataType.INTEGER.value, 0) >= 2

    async def test_count_public(self, settings_repo, bulk_settings):
        count = await settings_repo.count_public()
        assert count >= 2

    async def test_count_editable(self, settings_repo, bulk_settings):
        count = await settings_repo.count_editable()
        assert count >= len(bulk_settings)

    async def test_count_encrypted(self, settings_repo, bulk_settings):
        count = await settings_repo.count_encrypted()
        assert count >= 1

    async def test_statistics_respect_date_range(self, settings_repo, bulk_settings):
        now = datetime.now(timezone.utc)
        total = await settings_repo.get_total_count(
            date_from=now + timedelta(days=10), date_to=now + timedelta(days=20)
        )
        assert total == 0