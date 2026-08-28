"""
backend/tests/test_integration_repository.py

Tests for `app.repositories.integration_repository.IntegrationRepository`.

Verified against: backend/app/repositories/integration_repository.py,
backend/app/models/integration.py, backend/alembic/versions/integration_migration.py.

Fixture provenance (verified against `tests/conftest.py` and
`tests/test_booking_api.py`): `db_session` is a function-scoped,
`@pytest_asyncio.fixture`-decorated async generator defined in
`tests/test_booking_api.py` (bound to a single connection wrapped in
an outer transaction that is rolled back after each test, so no test
data here is ever persisted). `tests/conftest.py` imports it
(alongside `app` / `client`) from `test_booking_api`, which makes it
available project-wide, including to this file, without redefining it
here.

Every assertion below exercises only methods that exist verbatim on
`IntegrationRepository`. No repository method is invented.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

import pytest

from app.models.integration import (
    AuthenticationType,
    IntegrationProvider,
    IntegrationStatus,
    IntegrationType,
)
from app.repositories.integration_repository import IntegrationRepository

pytestmark = pytest.mark.asyncio


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _smtp_payload(name: str = "Primary SMTP", **overrides) -> dict:
    """Builds a minimal, valid `integrations` row payload for `SMTP`.

    Args:
        name: The unique integration name to use.
        **overrides: Additional/overriding column values.

    Returns:
        dict: A payload suitable for `IntegrationRepository.create`.
    """
    payload = {
        "name": name,
        "provider": IntegrationProvider.SMTP,
        "integration_type": IntegrationType.EMAIL,
        "status": IntegrationStatus.PENDING_VERIFICATION,
        "authentication_type": AuthenticationType.BASIC_AUTH,
        "configuration": {"host": "smtp.example.com", "port": 587},
        "credentials": {"username": "user", "password": "pass"},
        "timeout_seconds": 30,
        "retry_count": 3,
        "is_default": False,
    }
    payload.update(overrides)
    return payload


@pytest.fixture
def repository(db_session) -> IntegrationRepository:
    """Builds an `IntegrationRepository` bound to the test session.

    `db_session` (an async, `pytest_asyncio.fixture`-backed
    `AsyncSession`, defined in `tests/test_booking_api.py` and made
    available project-wide via `tests/conftest.py`) is already fully
    resolved to a real `AsyncSession` instance by pytest-asyncio
    before this fixture runs. Building the `IntegrationRepository`
    itself is a plain, synchronous constructor call -- it does not
    `await` anything -- so this fixture must be a normal `@pytest.fixture`,
    not an `async def` under `@pytest.fixture`. In pytest-asyncio's
    strict mode (the project default; no `asyncio_mode` override is
    configured), an `async def` fixture registered via the plain
    `@pytest.fixture` decorator is never actually awaited by the
    plugin -- callers instead receive the unawaited coroutine object,
    not an `IntegrationRepository`, which is why every test previously
    failed with `AttributeError` on the coroutine.

    Args:
        db_session: The transactional test `AsyncSession` fixture.

    Returns:
        IntegrationRepository: The repository under test.
    """
    return IntegrationRepository(db_session)


# ---------------------------------------------------------------------------
# create / get_by_id / get_by_name
# ---------------------------------------------------------------------------
async def test_create_persists_and_returns_refreshed_instance(repository):
    """`create` persists a row and returns a refreshed ORM instance with a UUID id."""
    integration = await repository.create(_smtp_payload())

    assert integration.id is not None
    assert isinstance(integration.id, uuid.UUID)
    assert integration.name == "Primary SMTP"
    assert integration.provider == IntegrationProvider.SMTP
    assert integration.integration_type == IntegrationType.EMAIL
    assert integration.is_deleted is False


async def test_get_by_id_returns_matching_non_deleted_row(repository):
    """`get_by_id` returns the matching row when it exists and is not deleted."""
    created = await repository.create(_smtp_payload(name="Get By Id"))

    fetched = await repository.get_by_id(created.id)

    assert fetched is not None
    assert fetched.id == created.id


async def test_get_by_id_returns_none_for_unknown_id(repository):
    """`get_by_id` returns `None` for an id that does not exist."""
    result = await repository.get_by_id(uuid.uuid4())

    assert result is None


async def test_get_by_id_excludes_soft_deleted_by_default(repository):
    """`get_by_id` excludes soft-deleted rows unless `include_deleted=True`."""
    created = await repository.create(_smtp_payload(name="Soft Deleted Lookup"))
    await repository.soft_delete(created)

    assert await repository.get_by_id(created.id) is None
    assert await repository.get_by_id(created.id, include_deleted=True) is not None


async def test_get_by_name_returns_matching_row(repository):
    """`get_by_name` returns the row with an exact name match."""
    await repository.create(_smtp_payload(name="Unique Name Lookup"))

    fetched = await repository.get_by_name("Unique Name Lookup")

    assert fetched is not None
    assert fetched.name == "Unique Name Lookup"


async def test_get_by_name_returns_none_when_absent(repository):
    """`get_by_name` returns `None` when no row matches."""
    result = await repository.get_by_name("Does Not Exist")

    assert result is None


# ---------------------------------------------------------------------------
# update / soft_delete / restore
# ---------------------------------------------------------------------------
async def test_update_applies_partial_column_changes(repository):
    """`update` applies only the supplied columns and refreshes the instance."""
    created = await repository.create(_smtp_payload(name="Update Target"))

    updated = await repository.update(created, {"timeout_seconds": 45})

    assert updated.timeout_seconds == 45
    assert updated.name == "Update Target"


async def test_soft_delete_sets_flag_and_timestamp(repository):
    """`soft_delete` sets `is_deleted=True` and populates `deleted_at`."""
    created = await repository.create(_smtp_payload(name="Soft Delete Target"))

    deleted = await repository.soft_delete(created)

    assert deleted.is_deleted is True
    assert deleted.deleted_at is not None


async def test_restore_clears_soft_delete_flag(repository):
    """`restore` reverses a prior `soft_delete`."""
    created = await repository.create(_smtp_payload(name="Restore Target"))
    await repository.soft_delete(created)

    restored = await repository.restore(created)

    assert restored.is_deleted is False
    assert restored.deleted_at is None


# ---------------------------------------------------------------------------
# Status / lifecycle
# ---------------------------------------------------------------------------
async def test_set_status_updates_status_column(repository):
    """`set_status` updates the `status` column to the given value."""
    created = await repository.create(_smtp_payload(name="Set Status Target"))

    updated = await repository.set_status(created, IntegrationStatus.FAILED)

    assert updated.status == IntegrationStatus.FAILED


async def test_enable_sets_status_active(repository):
    """`enable` transitions `status` to `ACTIVE`."""
    created = await repository.create(_smtp_payload(name="Enable Target"))

    updated = await repository.enable(created)

    assert updated.status == IntegrationStatus.ACTIVE


async def test_disable_sets_status_inactive(repository):
    """`disable` transitions `status` to `INACTIVE`."""
    created = await repository.create(_smtp_payload(name="Disable Target"))

    updated = await repository.disable(created)

    assert updated.status == IntegrationStatus.INACTIVE


async def test_update_health_check_status_sets_status_and_timestamp(repository):
    """`update_health_check_status` sets `status` and `last_health_check_at`."""
    created = await repository.create(_smtp_payload(name="Health Check Target"))
    checked_at = datetime.now(timezone.utc)

    updated = await repository.update_health_check_status(
        created, status=IntegrationStatus.ACTIVE, checked_at=checked_at
    )

    assert updated.status == IntegrationStatus.ACTIVE
    assert updated.last_health_check_at is not None


async def test_update_last_sync_sets_timestamp(repository):
    """`update_last_sync` sets `last_sync_at`, defaulting to current UTC time."""
    created = await repository.create(_smtp_payload(name="Sync Target"))

    updated = await repository.update_last_sync(created)

    assert updated.last_sync_at is not None


async def test_update_last_health_check_sets_timestamp_without_changing_status(
    repository,
):
    """`update_last_health_check` updates the timestamp only, not `status`."""
    created = await repository.create(
        _smtp_payload(name="Health Timestamp Target", status=IntegrationStatus.ACTIVE)
    )

    updated = await repository.update_last_health_check(created)

    assert updated.last_health_check_at is not None
    assert updated.status == IntegrationStatus.ACTIVE


async def test_clear_default_for_type_clears_other_defaults(repository):
    """`clear_default_for_type` clears `is_default` on other rows of the same type."""
    first = await repository.create(
        _smtp_payload(name="Default One", is_default=True)
    )
    second = await repository.create(
        _smtp_payload(name="Default Two", is_default=True)
    )

    affected = await repository.clear_default_for_type(
        IntegrationType.EMAIL, exclude_id=second.id
    )

    assert affected == 1
    refreshed_first = await repository.get_by_id(first.id)
    refreshed_second = await repository.get_by_id(second.id)
    assert refreshed_first.is_default is False
    assert refreshed_second.is_default is True


# ---------------------------------------------------------------------------
# Bulk operations
# ---------------------------------------------------------------------------
async def test_bulk_enable_sets_active_for_given_ids(repository):
    """`bulk_enable` sets `ACTIVE` status for every non-deleted id supplied."""
    one = await repository.create(_smtp_payload(name="Bulk Enable One"))
    two = await repository.create(_smtp_payload(name="Bulk Enable Two"))

    affected = await repository.bulk_enable([one.id, two.id])

    assert affected == 2
    assert (await repository.get_by_id(one.id)).status == IntegrationStatus.ACTIVE
    assert (await repository.get_by_id(two.id)).status == IntegrationStatus.ACTIVE


async def test_bulk_disable_sets_inactive_for_given_ids(repository):
    """`bulk_disable` sets `INACTIVE` status for every non-deleted id supplied."""
    one = await repository.create(_smtp_payload(name="Bulk Disable One"))

    affected = await repository.bulk_disable([one.id])

    assert affected == 1
    assert (await repository.get_by_id(one.id)).status == IntegrationStatus.INACTIVE


async def test_bulk_enable_returns_zero_for_empty_ids(repository):
    """`bulk_enable` returns `0` and performs no query when `ids` is empty."""
    affected = await repository.bulk_enable([])

    assert affected == 0


async def test_bulk_delete_soft_deletes_given_ids(repository):
    """`bulk_delete` soft-deletes every non-deleted id supplied."""
    one = await repository.create(_smtp_payload(name="Bulk Delete One"))

    affected = await repository.bulk_delete([one.id])

    assert affected == 1
    assert await repository.get_by_id(one.id) is None
    assert (await repository.get_by_id(one.id, include_deleted=True)).is_deleted is True


async def test_bulk_delete_returns_zero_for_empty_ids(repository):
    """`bulk_delete` returns `0` and performs no query when `ids` is empty."""
    affected = await repository.bulk_delete([])

    assert affected == 0


# ---------------------------------------------------------------------------
# Listing / searching
# ---------------------------------------------------------------------------
async def test_list_integrations_filters_by_integration_type(repository):
    """`list_integrations` restricts results to the given `integration_type`."""
    await repository.create(_smtp_payload(name="List Filter Email"))
    await repository.create(
        _smtp_payload(
            name="List Filter Calendar",
            provider=IntegrationProvider.GOOGLE_CALENDAR,
            integration_type=IntegrationType.CALENDAR,
            authentication_type=AuthenticationType.OAUTH2,
            configuration={"calendar_id": "primary"},
            credentials={"client_id": "x", "client_secret": "y", "refresh_token": "z"},
        )
    )

    items, total = await repository.list_integrations(
        integration_type=IntegrationType.CALENDAR
    )

    assert total >= 1
    assert all(item.integration_type == IntegrationType.CALENDAR for item in items)


async def test_list_integrations_search_matches_name(repository):
    """`list_integrations` `search` performs a case-insensitive substring match on name."""
    await repository.create(_smtp_payload(name="Searchable Integration Name"))

    items, total = await repository.list_integrations(search="searchable")

    assert total >= 1
    assert any("Searchable" in item.name for item in items)


async def test_list_integrations_paginates_results(repository):
    """`list_integrations` respects `page`/`page_size` bounds."""
    for i in range(3):
        await repository.create(_smtp_payload(name=f"Page Item {i}"))

    items, total = await repository.list_integrations(page=1, page_size=2)

    assert len(items) <= 2
    assert total >= 3


async def test_list_integrations_sorts_by_requested_column(repository):
    """`list_integrations` orders results by `sort_by`/`sort_order`."""
    await repository.create(_smtp_payload(name="A Sort Item"))
    await repository.create(_smtp_payload(name="Z Sort Item"))

    items, _ = await repository.list_integrations(
        sort_by="name", sort_order="asc", page_size=200
    )

    names = [item.name for item in items]
    assert names == sorted(names)


async def test_list_integrations_excludes_soft_deleted_by_default(repository):
    """`list_integrations` excludes soft-deleted rows unless `include_deleted=True`."""
    created = await repository.create(_smtp_payload(name="Excluded From List"))
    await repository.soft_delete(created)

    items, total = await repository.list_integrations(search="Excluded From List")

    assert total == 0
    assert created.id not in {item.id for item in items}


async def test_search_integrations_matches_name_or_base_url(repository):
    """`search_integrations` matches against `name` or `base_url`."""
    await repository.create(
        _smtp_payload(
            name="REST Search Target",
            provider=IntegrationProvider.CUSTOM_REST_API,
            integration_type=IntegrationType.CUSTOM_API,
            authentication_type=AuthenticationType.NONE,
            configuration=None,
            credentials=None,
            base_url="https://api.searchtarget.example.com",
        )
    )

    results = await repository.search_integrations("searchtarget")

    assert any("searchtarget" in (r.base_url or "").lower() for r in results)


async def test_get_default_for_type_returns_default_row(repository):
    """`get_default_for_type` returns the single default row for a type."""
    created = await repository.create(
        _smtp_payload(name="Default For Type", is_default=True)
    )

    default = await repository.get_default_for_type(IntegrationType.EMAIL)

    assert default is not None
    assert default.id == created.id


async def test_get_default_for_type_returns_none_when_unset(repository):
    """`get_default_for_type` returns `None` when no default is configured."""
    default = await repository.get_default_for_type(IntegrationType.WHATSAPP)

    assert default is None


# ---------------------------------------------------------------------------
# Aggregation / statistics
# ---------------------------------------------------------------------------
async def test_get_total_count_reflects_non_deleted_rows(repository):
    """`get_total_count` counts only non-deleted rows by default."""
    before = await repository.get_total_count()
    await repository.create(_smtp_payload(name="Count Target"))

    after = await repository.get_total_count()

    assert after == before + 1


async def test_count_by_provider_groups_correctly(repository):
    """`count_by_provider` returns a mapping keyed by provider value."""
    await repository.create(_smtp_payload(name="Count By Provider Target"))

    counts = await repository.count_by_provider()

    assert counts.get(IntegrationProvider.SMTP.value, 0) >= 1


async def test_count_by_status_groups_correctly(repository):
    """`count_by_status` returns a mapping keyed by status value."""
    await repository.create(
        _smtp_payload(
            name="Count By Status Target", status=IntegrationStatus.PENDING_VERIFICATION
        )
    )

    counts = await repository.count_by_status()

    assert counts.get(IntegrationStatus.PENDING_VERIFICATION.value, 0) >= 1


async def test_count_by_type_groups_correctly(repository):
    """`count_by_type` returns a mapping keyed by integration_type value."""
    await repository.create(_smtp_payload(name="Count By Type Target"))

    counts = await repository.count_by_type()

    assert counts.get(IntegrationType.EMAIL.value, 0) >= 1


async def test_count_by_authentication_type_groups_correctly(repository):
    """`count_by_authentication_type` returns a mapping keyed by auth type value."""
    await repository.create(_smtp_payload(name="Count By Auth Target"))

    counts = await repository.count_by_authentication_type()

    assert counts.get(AuthenticationType.BASIC_AUTH.value, 0) >= 1


async def test_get_statistics_returns_expected_keys(repository):
    """`get_statistics` returns the full documented key set."""
    await repository.create(_smtp_payload(name="Statistics Target"))

    stats = await repository.get_statistics()

    expected_keys = {
        "total_integrations",
        "by_type",
        "by_provider",
        "by_status",
        "by_authentication_type",
        "active_count",
        "failed_count",
        "default_count",
        "last_sync_at",
        "last_health_check_at",
    }
    assert expected_keys.issubset(stats.keys())