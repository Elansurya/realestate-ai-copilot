"""
backend/tests/test_webhook_repository.py

Unit/integration tests for `app.repositories.webhook_repository.WebhookRepository`.

SCOPE NOTE:
    These tests exercise only the methods that exist on
    `WebhookRepository` as implemented in
    `backend/app/repositories/webhook_repository.py`. No repository
    methods, business rules, or behaviors have been invented.

ASSUMPTION / COULD NOT BE VERIFIED:
    The referenced files do not include this project's `conftest.py`,
    test database fixtures, or async engine/session setup. It could
    NOT be verified from the referenced files how the project
    provisions an `AsyncSession` for tests (e.g. a Postgres test
    container, `pytest-asyncio` fixtures, transactional rollback
    fixtures, or factory helpers for `User`). These tests therefore
    assume the project's standard pattern of an injected, function-
    scoped `async_session` fixture (an `AsyncSession` bound to a real
    PostgreSQL test database, required because `Webhook`/`WebhookLog`
    use native PostgreSQL ENUM types, `JSONB`, and
    `gen_random_uuid()`, none of which are supported by SQLite). If
    the project's actual fixture has a different name or signature,
    only the fixture references below need to be adjusted.

    Similarly, it could NOT be verified how a `users.id` row (the FK
    target of `Webhook.created_by_id`) is seeded in this project's
    test suite. A `created_user_id` fixture is assumed to yield a
    valid, pre-existing `users.id` value; if no such helper exists,
    tests relying on `created_by_id` should be adjusted to pass `None`.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

import pytest

from app.models.webhook import (
    AuthenticationType,
    DeliveryStatus,
    Webhook,
    WebhookEvent,
    WebhookStatus,
)
from app.repositories.webhook_repository import WebhookRepository
from app.schemas.webhook import WebhookFilter, WebhookLogFilter

pytestmark = pytest.mark.asyncio


def _webhook_data(**overrides) -> dict:
    """Builds a minimal, valid `Webhook` column dict for repository tests.

    Args:
        **overrides: Column overrides applied on top of the defaults.

    Returns:
        dict: Column values suitable for `WebhookRepository.create`.
    """
    data = {
        "name": f"webhook-{uuid.uuid4().hex[:8]}",
        "event": WebhookEvent.LEAD_CREATED,
        "target_url": "https://example.com/hooks/incoming",
        "http_method": "POST",
        "status": WebhookStatus.ACTIVE,
        "authentication_type": AuthenticationType.HMAC_SIGNATURE,
        "secret_key": "a-valid-secret-key",
        "retry_count": 3,
        "timeout_seconds": 30,
        "enabled": True,
    }
    data.update(overrides)
    return data


# ---------------------------------------------------------------------------
# create / update / soft_delete / restore
# ---------------------------------------------------------------------------
class TestCreateUpdateSoftDeleteRestore:
    async def test_create_persists_and_returns_webhook_with_id(self, async_session):
        """`create` should flush and return a `Webhook` with a populated id."""
        repo = WebhookRepository(async_session)
        webhook = await repo.create(_webhook_data())

        assert webhook.id is not None
        assert webhook.is_deleted is False
        assert webhook.status == WebhookStatus.ACTIVE

    async def test_update_applies_only_supplied_fields(self, async_session):
        """`update` should apply only the keys present in `data` (PATCH semantics)."""
        repo = WebhookRepository(async_session)
        webhook = await repo.create(_webhook_data(name="original-name"))

        updated = await repo.update(webhook, {"name": "updated-name"})

        assert updated.name == "updated-name"
        assert updated.event == WebhookEvent.LEAD_CREATED

    async def test_soft_delete_sets_flags_and_disables(self, async_session):
        """`soft_delete` should set `is_deleted`, `deleted_at`, and `enabled=False`."""
        repo = WebhookRepository(async_session)
        webhook = await repo.create(_webhook_data(enabled=True))

        deleted = await repo.soft_delete(webhook)

        assert deleted.is_deleted is True
        assert deleted.deleted_at is not None
        assert deleted.enabled is False

    async def test_soft_delete_accepts_explicit_timestamp(self, async_session):
        """`soft_delete` should honor an explicit `deleted_at` argument."""
        repo = WebhookRepository(async_session)
        webhook = await repo.create(_webhook_data())
        explicit_ts = datetime.now(timezone.utc) - timedelta(days=1)

        deleted = await repo.soft_delete(webhook, deleted_at=explicit_ts)

        assert deleted.deleted_at == explicit_ts

    async def test_restore_clears_soft_delete_markers(self, async_session):
        """`restore` should clear `is_deleted` and `deleted_at`."""
        repo = WebhookRepository(async_session)
        webhook = await repo.create(_webhook_data())
        await repo.soft_delete(webhook)

        restored = await repo.restore(webhook)

        assert restored.is_deleted is False
        assert restored.deleted_at is None


# ---------------------------------------------------------------------------
# enable / disable / bulk_enable / bulk_disable
# ---------------------------------------------------------------------------
class TestEnableDisable:
    async def test_enable_sets_enabled_true(self, async_session):
        repo = WebhookRepository(async_session)
        webhook = await repo.create(_webhook_data(enabled=False))

        enabled = await repo.enable(webhook)

        assert enabled.enabled is True

    async def test_disable_sets_enabled_false(self, async_session):
        repo = WebhookRepository(async_session)
        webhook = await repo.create(_webhook_data(enabled=True))

        disabled = await repo.disable(webhook)

        assert disabled.enabled is False

    async def test_bulk_enable_returns_zero_for_empty_list(self, async_session):
        repo = WebhookRepository(async_session)
        result = await repo.bulk_enable([])
        assert result == 0

    async def test_bulk_disable_returns_zero_for_empty_list(self, async_session):
        repo = WebhookRepository(async_session)
        result = await repo.bulk_disable([])
        assert result == 0

    async def test_bulk_enable_updates_only_non_deleted_matching_ids(self, async_session):
        """`bulk_enable` should only affect non-deleted webhooks whose id is
        in the supplied list, per its `Webhook.is_deleted.is_(False)` filter."""
        repo = WebhookRepository(async_session)
        active = await repo.create(_webhook_data(enabled=False))
        deleted = await repo.create(_webhook_data(enabled=False))
        await repo.soft_delete(deleted)

        count = await repo.bulk_enable([active.id, deleted.id])

        assert count == 1
        refreshed_active = await repo.get_by_id(active.id)
        assert refreshed_active.enabled is True

    async def test_bulk_disable_updates_only_non_deleted_matching_ids(self, async_session):
        repo = WebhookRepository(async_session)
        active = await repo.create(_webhook_data(enabled=True))

        count = await repo.bulk_disable([active.id])

        assert count == 1
        refreshed = await repo.get_by_id(active.id)
        assert refreshed.enabled is False


# ---------------------------------------------------------------------------
# get_by_id / get_by_name
# ---------------------------------------------------------------------------
class TestSingleRecordRetrieval:
    async def test_get_by_id_returns_none_for_missing_id(self, async_session):
        repo = WebhookRepository(async_session)
        result = await repo.get_by_id(uuid.uuid4())
        assert result is None

    async def test_get_by_id_excludes_soft_deleted_by_default(self, async_session):
        repo = WebhookRepository(async_session)
        webhook = await repo.create(_webhook_data())
        await repo.soft_delete(webhook)

        result = await repo.get_by_id(webhook.id)

        assert result is None

    async def test_get_by_id_include_deleted_returns_soft_deleted_record(self, async_session):
        repo = WebhookRepository(async_session)
        webhook = await repo.create(_webhook_data())
        await repo.soft_delete(webhook)

        result = await repo.get_by_id(webhook.id, include_deleted=True)

        assert result is not None
        assert result.is_deleted is True

    async def test_get_by_name_returns_matching_webhook(self, async_session):
        repo = WebhookRepository(async_session)
        webhook = await repo.create(_webhook_data(name="unique-lookup-name"))

        result = await repo.get_by_name("unique-lookup-name")

        assert result is not None
        assert result.id == webhook.id

    async def test_get_by_name_excludes_soft_deleted_by_default(self, async_session):
        repo = WebhookRepository(async_session)
        webhook = await repo.create(_webhook_data(name="soft-deleted-name"))
        await repo.soft_delete(webhook)

        result = await repo.get_by_name("soft-deleted-name")

        assert result is None


# ---------------------------------------------------------------------------
# list_webhooks (filter / search / pagination / sort)
# ---------------------------------------------------------------------------
class TestListWebhooks:
    async def test_list_webhooks_returns_total_count_and_items(self, async_session):
        repo = WebhookRepository(async_session)
        await repo.create(_webhook_data())
        await repo.create(_webhook_data())

        filter_ = WebhookFilter(page=1, page_size=20)
        items, total = await repo.list_webhooks(filter_)

        assert total >= 2
        assert len(items) <= filter_.page_size

    async def test_list_webhooks_filters_by_event(self, async_session):
        repo = WebhookRepository(async_session)
        await repo.create(_webhook_data(event=WebhookEvent.DEAL_CREATED))
        await repo.create(_webhook_data(event=WebhookEvent.TASK_CREATED))

        filter_ = WebhookFilter(event=WebhookEvent.DEAL_CREATED, page=1, page_size=50)
        items, _ = await repo.list_webhooks(filter_)

        assert all(item.event == WebhookEvent.DEAL_CREATED for item in items)

    async def test_list_webhooks_search_matches_name_or_target_url(self, async_session):
        repo = WebhookRepository(async_session)
        await repo.create(
            _webhook_data(name="alpha-hook", target_url="https://alpha.example.com/hook")
        )

        filter_ = WebhookFilter(search="alpha", page=1, page_size=50)
        items, total = await repo.list_webhooks(filter_)

        assert total >= 1
        assert any("alpha" in item.name for item in items)

    async def test_list_webhooks_excludes_deleted_by_default(self, async_session):
        repo = WebhookRepository(async_session)
        webhook = await repo.create(_webhook_data())
        await repo.soft_delete(webhook)

        filter_ = WebhookFilter(page=1, page_size=100)
        items, _ = await repo.list_webhooks(filter_)

        assert all(item.id != webhook.id for item in items)


# ---------------------------------------------------------------------------
# count_by_status / count_by_event / get_statistics
# ---------------------------------------------------------------------------
class TestAggregateStatistics:
    async def test_count_by_status_groups_correctly(self, async_session):
        repo = WebhookRepository(async_session)
        await repo.create(_webhook_data(status=WebhookStatus.ACTIVE))
        await repo.create(_webhook_data(status=WebhookStatus.SUSPENDED))

        counts = await repo.count_by_status()

        assert counts.get(WebhookStatus.ACTIVE.value, 0) >= 1
        assert counts.get(WebhookStatus.SUSPENDED.value, 0) >= 1

    async def test_count_by_event_groups_correctly(self, async_session):
        repo = WebhookRepository(async_session)
        await repo.create(_webhook_data(event=WebhookEvent.PAYMENT_RECEIVED))

        counts = await repo.count_by_event()

        assert counts.get(WebhookEvent.PAYMENT_RECEIVED.value, 0) >= 1

    async def test_get_statistics_returns_expected_keys(self, async_session):
        repo = WebhookRepository(async_session)
        webhook = await repo.create(_webhook_data())
        await repo.create_log(
            {
                "webhook_id": webhook.id,
                "delivery_status": DeliveryStatus.SUCCESS,
                "attempt_count": 1,
                "duration_ms": 12.5,
                "delivered_at": datetime.now(timezone.utc),
            }
        )

        stats = await repo.get_statistics(webhook_id=webhook.id)

        expected_keys = {
            "total_webhooks",
            "active_count",
            "suspended_count",
            "failed_count",
            "by_event",
            "by_status",
            "total_deliveries",
            "successful_deliveries",
            "failed_deliveries",
            "dead_lettered_deliveries",
            "success_rate_percentage",
            "average_duration_ms",
            "last_delivery_at",
        }
        assert expected_keys.issubset(stats.keys())
        assert stats["total_deliveries"] >= 1
        assert stats["successful_deliveries"] >= 1
        assert stats["success_rate_percentage"] == 100.0

    async def test_get_statistics_success_rate_is_none_when_no_deliveries(self, async_session):
        repo = WebhookRepository(async_session)
        webhook = await repo.create(_webhook_data())

        stats = await repo.get_statistics(webhook_id=webhook.id)

        assert stats["total_deliveries"] == 0
        assert stats["success_rate_percentage"] is None


# ---------------------------------------------------------------------------
# Delivery Logs
# ---------------------------------------------------------------------------
class TestDeliveryLogs:
    async def test_create_log_persists_and_returns_log(self, async_session):
        repo = WebhookRepository(async_session)
        webhook = await repo.create(_webhook_data())

        log = await repo.create_log(
            {
                "webhook_id": webhook.id,
                "delivery_status": DeliveryStatus.SUCCESS,
                "attempt_count": 1,
                "delivered_at": datetime.now(timezone.utc),
            }
        )

        assert log.id is not None
        assert log.webhook_id == webhook.id

    async def test_get_log_by_id_returns_none_for_missing_id(self, async_session):
        repo = WebhookRepository(async_session)
        result = await repo.get_log_by_id(uuid.uuid4())
        assert result is None

    async def test_get_latest_log_returns_most_recent_by_delivered_at(self, async_session):
        repo = WebhookRepository(async_session)
        webhook = await repo.create(_webhook_data())
        now = datetime.now(timezone.utc)

        await repo.create_log(
            {
                "webhook_id": webhook.id,
                "delivery_status": DeliveryStatus.FAILED,
                "attempt_count": 1,
                "delivered_at": now - timedelta(minutes=5),
            }
        )
        latest = await repo.create_log(
            {
                "webhook_id": webhook.id,
                "delivery_status": DeliveryStatus.SUCCESS,
                "attempt_count": 2,
                "delivered_at": now,
            }
        )

        result = await repo.get_latest_log(webhook.id)

        assert result is not None
        assert result.id == latest.id

    async def test_get_delivery_logs_filters_by_webhook_and_status(self, async_session):
        repo = WebhookRepository(async_session)
        webhook = await repo.create(_webhook_data())
        await repo.create_log(
            {
                "webhook_id": webhook.id,
                "delivery_status": DeliveryStatus.FAILED,
                "attempt_count": 1,
                "delivered_at": datetime.now(timezone.utc),
            }
        )

        filter_ = WebhookLogFilter(
            webhook_id=webhook.id, delivery_status=DeliveryStatus.FAILED
        )
        items, total = await repo.get_delivery_logs(filter_)

        assert total >= 1
        assert all(item.delivery_status == DeliveryStatus.FAILED for item in items)

    async def test_get_failed_log_for_retry_returns_none_for_success_status(
        self, async_session
    ):
        repo = WebhookRepository(async_session)
        webhook = await repo.create(_webhook_data())
        log = await repo.create_log(
            {
                "webhook_id": webhook.id,
                "delivery_status": DeliveryStatus.SUCCESS,
                "attempt_count": 1,
                "delivered_at": datetime.now(timezone.utc),
            }
        )

        result = await repo.get_failed_log_for_retry(log.id)

        assert result is None

    async def test_get_failed_log_for_retry_returns_log_for_failed_or_retrying(
        self, async_session
    ):
        repo = WebhookRepository(async_session)
        webhook = await repo.create(_webhook_data())
        log = await repo.create_log(
            {
                "webhook_id": webhook.id,
                "delivery_status": DeliveryStatus.FAILED,
                "attempt_count": 1,
                "delivered_at": datetime.now(timezone.utc),
            }
        )

        result = await repo.get_failed_log_for_retry(log.id)

        assert result is not None
        assert result.id == log.id


# ---------------------------------------------------------------------------
# record_delivery_outcome
# ---------------------------------------------------------------------------
class TestRecordDeliveryOutcome:
    async def test_record_delivery_outcome_sets_success_timestamp(self, async_session):
        repo = WebhookRepository(async_session)
        webhook = await repo.create(_webhook_data())
        occurred_at = datetime.now(timezone.utc)

        updated = await repo.record_delivery_outcome(
            webhook, succeeded=True, occurred_at=occurred_at
        )

        assert updated.last_delivery_at == occurred_at
        assert updated.last_success_at == occurred_at
        assert updated.last_failure_at is None

    async def test_record_delivery_outcome_sets_failure_timestamp(self, async_session):
        repo = WebhookRepository(async_session)
        webhook = await repo.create(_webhook_data())
        occurred_at = datetime.now(timezone.utc)

        updated = await repo.record_delivery_outcome(
            webhook, succeeded=False, occurred_at=occurred_at
        )

        assert updated.last_delivery_at == occurred_at
        assert updated.last_failure_at == occurred_at
        assert updated.last_success_at is None