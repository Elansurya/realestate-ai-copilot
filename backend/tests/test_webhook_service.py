"""
backend/tests/test_webhook_service.py

Unit tests for `app.services.webhook_service.WebhookService`.

SCOPE NOTE:
    These tests exercise only the methods and business rules that
    exist on `WebhookService` as implemented in
    `backend/app/services/webhook_service.py`. No service methods or
    business rules have been invented.

APPROACH:
    `WebhookRepository` is mocked (via `unittest.mock.AsyncMock`) so
    these are true unit tests of the Service layer's orchestration and
    validation logic, independent of a real database. The `session`
    passed to `WebhookService` is also a mock, since the Service layer
    only calls `session.commit()` on it (transaction boundary), never
    query methods directly.

COULD NOT BE VERIFIED:
    - The exact exception message text is not asserted verbatim
      anywhere the referenced source uses an f-string with
      interpolated values that could reasonably change; tests assert
      on exception type and, where stable, a substring.
    - Outbound HTTP delivery (`httpx.AsyncClient`) is monkeypatched at
      the `httpx` import inside `webhook_service.py`'s `_dispatch`
      method; the referenced source imports `httpx` locally inside
      that method, so tests patch `httpx.AsyncClient` globally via
      `monkeypatch.setattr("httpx.AsyncClient", ...)`.
"""

from __future__ import annotations

import socket
import uuid
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.core.exceptions import (
    BusinessRuleException,
    ConflictException,
    NotFoundException,
    ValidationException,
)
from app.models.webhook import (
    AuthenticationType,
    DeliveryStatus,
    Webhook,
    WebhookEvent,
    WebhookLog,
    WebhookStatus,
)
from app.schemas.webhook import WebhookCreate, WebhookUpdate
from app.services.webhook_service import WebhookService

pytestmark = pytest.mark.asyncio


def _make_webhook(**overrides) -> Webhook:
    """Builds an in-memory `Webhook` ORM instance (not persisted) for
    service-layer unit tests.

    Args:
        **overrides: Attribute overrides applied on top of the defaults.

    Returns:
        Webhook: An unpersisted `Webhook` instance with sane defaults.
    """
    webhook = Webhook(
        id=uuid.uuid4(),
        name="test-webhook",
        event=WebhookEvent.LEAD_CREATED,
        target_url="https://example.com/hooks/incoming",
        http_method="POST",
        status=WebhookStatus.ACTIVE,
        authentication_type=AuthenticationType.HMAC_SIGNATURE,
        secret_key="a-valid-secret-key",
        custom_headers=None,
        payload_template=None,
        retry_count=3,
        timeout_seconds=30,
        rate_limit_per_minute=None,
        enabled=True,
        last_delivery_at=None,
        last_success_at=None,
        last_failure_at=None,
        created_by_id=None,
        is_deleted=False,
        deleted_at=None,
        created_at=datetime.now(timezone.utc),
        updated_at=datetime.now(timezone.utc),
    )
    for key, value in overrides.items():
        setattr(webhook, key, value)
    return webhook


def _make_service() -> tuple[WebhookService, AsyncMock]:
    """Builds a `WebhookService` wired to a fully mocked repository.

    Returns:
        tuple[WebhookService, AsyncMock]: The service instance and its
        mocked `WebhookRepository`.
    """
    session = AsyncMock()
    repository = AsyncMock()
    service = WebhookService(session, repository=repository)
    return service, repository


def _valid_create_payload(**overrides) -> WebhookCreate:
    """Builds a minimal, valid `WebhookCreate` payload.

    Args:
        **overrides: Field overrides applied on top of the defaults.

    Returns:
        WebhookCreate: The validated creation schema instance.
    """
    data = {
        "name": "new-webhook",
        "event": WebhookEvent.LEAD_CREATED,
        "target_url": "https://example.com/hooks/incoming",
        "authentication_type": AuthenticationType.HMAC_SIGNATURE,
        "secret_key": "a-valid-secret-key",
    }
    data.update(overrides)
    return WebhookCreate(**data)


# ---------------------------------------------------------------------------
# Business Rule Validators
# ---------------------------------------------------------------------------
class TestValidators:
    def test_validate_target_url_rejects_non_http_scheme(self):
        service, _ = _make_service()
        with pytest.raises(ValidationException):
            service.validate_target_url("ftp://example.com/hook")

    def test_validate_target_url_rejects_missing_hostname(self):
        service, _ = _make_service()
        with pytest.raises(ValidationException):
            service.validate_target_url("https:///path-only")

    def test_validate_target_url_rejects_private_network_targets(self):
        service, _ = _make_service()
        with pytest.raises(ValidationException):
            service.validate_target_url("http://127.0.0.1/hook")

    def test_validate_target_url_rejects_unresolvable_hostname(self):
        service, _ = _make_service()
        with pytest.raises(ValidationException):
            service.validate_target_url(
                "https://this-host-should-not-resolve.invalid/hook"
            )

    def test_validate_event_rejects_non_webhook_event(self):
        service, _ = _make_service()
        with pytest.raises(ValidationException):
            service.validate_event("not-a-real-event")  # type: ignore[arg-type]

    def test_validate_event_accepts_valid_event(self):
        service, _ = _make_service()
        assert service.validate_event(WebhookEvent.TASK_CREATED) == WebhookEvent.TASK_CREATED

    def test_get_event_category_returns_mapped_category(self):
        service, _ = _make_service()
        assert service.get_event_category(WebhookEvent.PAYMENT_RECEIVED) == "Payment"

    def test_get_event_category_falls_back_to_integration(self):
        service, _ = _make_service()
        assert service.get_event_category(WebhookEvent.CUSTOM) == "Integration"

    def test_validate_authentication_rejects_secret_with_none_auth(self):
        service, _ = _make_service()
        with pytest.raises(ValidationException):
            service.validate_authentication(AuthenticationType.NONE, "some-secret")

    def test_validate_authentication_requires_secret_for_non_none_auth(self):
        service, _ = _make_service()
        with pytest.raises(ValidationException):
            service.validate_authentication(AuthenticationType.BEARER_TOKEN, None)

    def test_validate_authentication_passes_with_none_and_no_secret(self):
        service, _ = _make_service()
        service.validate_authentication(AuthenticationType.NONE, None)

    def test_validate_secret_rejects_short_secret(self):
        service, _ = _make_service()
        with pytest.raises(ValidationException):
            service.validate_secret(AuthenticationType.API_KEY, "short")

    def test_validate_secret_allows_none_auth_with_no_secret(self):
        service, _ = _make_service()
        assert service.validate_secret(AuthenticationType.NONE, None) is None

    def test_validate_headers_rejects_too_many_headers(self):
        service, _ = _make_service()
        headers = {f"X-Header-{i}": "value" for i in range(21)}
        with pytest.raises(ValidationException):
            service.validate_headers(headers)

    def test_validate_headers_rejects_forbidden_header_name(self):
        service, _ = _make_service()
        with pytest.raises(ValidationException):
            service.validate_headers({"Authorization": "Bearer xyz"})

    def test_validate_headers_rejects_blank_header_name(self):
        service, _ = _make_service()
        with pytest.raises(ValidationException):
            service.validate_headers({"   ": "value"})

    def test_validate_headers_rejects_value_too_long(self):
        service, _ = _make_service()
        with pytest.raises(ValidationException):
            service.validate_headers({"X-Custom": "a" * 2049})

    def test_validate_headers_accepts_none(self):
        service, _ = _make_service()
        assert service.validate_headers(None) is None

    def test_validate_payload_rejects_too_many_keys(self):
        service, _ = _make_service()
        payload = {f"key{i}": i for i in range(101)}
        with pytest.raises(ValidationException):
            service.validate_payload(payload)

    def test_validate_payload_rejects_non_serializable(self):
        service, _ = _make_service()
        with pytest.raises(ValidationException):
            service.validate_payload({"bad": object()})

    def test_validate_payload_accepts_none(self):
        service, _ = _make_service()
        assert service.validate_payload(None) is None


# ---------------------------------------------------------------------------
# create_webhook
# ---------------------------------------------------------------------------
class TestCreateWebhook:
    async def test_create_webhook_raises_conflict_when_name_exists(self):
        service, repository = _make_service()
        repository.get_by_name.return_value = _make_webhook()

        # The service intentionally performs DNS validation. Keep that
        # production validation intact, but make this unit test independent
        # of the machine/network DNS resolver.
        with patch(
            "app.services.webhook_service.socket.getaddrinfo",
            return_value=[
                (
                    socket.AF_INET,
                    socket.SOCK_STREAM,
                    6,
                    "",
                    ("93.184.216.34", 0),
                )
            ],
        ):
            with pytest.raises(ConflictException):
                await service.create_webhook(_valid_create_payload())

    async def test_create_webhook_persists_and_commits(self):
        service, repository = _make_service()
        repository.get_by_name.return_value = None
        repository.create.return_value = _make_webhook()

        # Keep production DNS/SSRF validation enabled while avoiding an
        # external DNS dependency in this isolated service test.
        with patch(
            "app.services.webhook_service.socket.getaddrinfo",
            return_value=[
                (
                    socket.AF_INET,
                    socket.SOCK_STREAM,
                    6,
                    "",
                    ("93.184.216.34", 0),
                )
            ],
        ):
            result = await service.create_webhook(
                _valid_create_payload(), created_by_id=42
            )

        repository.create.assert_awaited_once()
        service.session.commit.assert_awaited_once()
        assert result.name == "test-webhook"

    async def test_create_webhook_raises_validation_for_bad_url(self):
        service, repository = _make_service()
        repository.get_by_name.return_value = None

        with pytest.raises(ValidationException):
            await service.create_webhook(
                _valid_create_payload(target_url="http://127.0.0.1/hook")
            )


# ---------------------------------------------------------------------------
# update_webhook
# ---------------------------------------------------------------------------
class TestUpdateWebhook:
    async def test_update_webhook_raises_not_found(self):
        service, repository = _make_service()
        repository.get_by_id.return_value = None

        with pytest.raises(NotFoundException):
            await service.update_webhook(uuid.uuid4(), WebhookUpdate(name="new-name"))

    async def test_update_webhook_raises_conflict_on_duplicate_name(self):
        service, repository = _make_service()
        existing = _make_webhook()
        other = _make_webhook(id=uuid.uuid4(), name="taken-name")
        repository.get_by_id.return_value = existing
        repository.get_by_name.return_value = other

        with pytest.raises(ConflictException):
            await service.update_webhook(existing.id, WebhookUpdate(name="taken-name"))

    async def test_update_webhook_applies_update_and_commits(self):
        service, repository = _make_service()
        existing = _make_webhook()
        repository.get_by_id.return_value = existing
        repository.get_by_name.return_value = None
        repository.update.return_value = _make_webhook(name="renamed")

        result = await service.update_webhook(existing.id, WebhookUpdate(name="renamed"))

        repository.update.assert_awaited_once()
        service.session.commit.assert_awaited_once()
        assert result.name == "renamed"


# ---------------------------------------------------------------------------
# delete_webhook / restore_webhook
# ---------------------------------------------------------------------------
class TestDeleteRestoreWebhook:
    async def test_delete_webhook_raises_not_found(self):
        service, repository = _make_service()
        repository.get_by_id.return_value = None

        with pytest.raises(NotFoundException):
            await service.delete_webhook(uuid.uuid4())

    async def test_delete_webhook_soft_deletes_and_commits(self):
        service, repository = _make_service()
        webhook = _make_webhook()
        repository.get_by_id.return_value = webhook

        await service.delete_webhook(webhook.id)

        repository.soft_delete.assert_awaited_once_with(webhook)
        service.session.commit.assert_awaited_once()

    async def test_restore_webhook_raises_not_found_when_missing_entirely(self):
        service, repository = _make_service()
        repository.get_by_id.return_value = None

        with pytest.raises(NotFoundException):
            await service.restore_webhook(uuid.uuid4())

    async def test_restore_webhook_raises_business_rule_when_not_deleted(self):
        service, repository = _make_service()
        webhook = _make_webhook(is_deleted=False)
        repository.get_by_id.return_value = webhook

        with pytest.raises(BusinessRuleException):
            await service.restore_webhook(webhook.id)

    async def test_restore_webhook_restores_and_commits(self):
        service, repository = _make_service()
        webhook = _make_webhook(is_deleted=True)
        repository.get_by_id.return_value = webhook
        repository.restore.return_value = _make_webhook(is_deleted=False)

        result = await service.restore_webhook(webhook.id)

        repository.restore.assert_awaited_once_with(webhook)
        service.session.commit.assert_awaited_once()
        assert result.is_deleted is False


# ---------------------------------------------------------------------------
# enable_webhook / disable_webhook / bulk operations
# ---------------------------------------------------------------------------
class TestEnableDisable:
    async def test_enable_webhook_raises_business_rule_when_suspended(self):
        service, repository = _make_service()
        webhook = _make_webhook(status=WebhookStatus.SUSPENDED)
        repository.get_by_id.return_value = webhook

        with pytest.raises(BusinessRuleException):
            await service.enable_webhook(webhook.id)

    async def test_enable_webhook_raises_business_rule_when_failed(self):
        service, repository = _make_service()
        webhook = _make_webhook(status=WebhookStatus.FAILED)
        repository.get_by_id.return_value = webhook

        with pytest.raises(BusinessRuleException):
            await service.enable_webhook(webhook.id)

    async def test_enable_webhook_succeeds_when_active(self):
        service, repository = _make_service()
        webhook = _make_webhook(status=WebhookStatus.ACTIVE, enabled=False)
        repository.get_by_id.return_value = webhook
        repository.enable.return_value = _make_webhook(enabled=True)

        result = await service.enable_webhook(webhook.id)

        service.session.commit.assert_awaited_once()
        assert result.enabled is True

    async def test_disable_webhook_succeeds(self):
        service, repository = _make_service()
        webhook = _make_webhook(enabled=True)
        repository.get_by_id.return_value = webhook
        repository.disable.return_value = _make_webhook(enabled=False)

        result = await service.disable_webhook(webhook.id)

        service.session.commit.assert_awaited_once()
        assert result.enabled is False

    async def test_bulk_enable_delegates_to_repository_and_commits(self):
        service, repository = _make_service()
        repository.bulk_enable.return_value = 3
        ids = [uuid.uuid4(), uuid.uuid4(), uuid.uuid4()]

        count = await service.bulk_enable(ids)

        repository.bulk_enable.assert_awaited_once_with(ids)
        service.session.commit.assert_awaited_once()
        assert count == 3

    async def test_bulk_disable_delegates_to_repository_and_commits(self):
        service, repository = _make_service()
        repository.bulk_disable.return_value = 2
        ids = [uuid.uuid4(), uuid.uuid4()]

        count = await service.bulk_disable(ids)

        repository.bulk_disable.assert_awaited_once_with(ids)
        service.session.commit.assert_awaited_once()
        assert count == 2


# ---------------------------------------------------------------------------
# get_webhook / list_webhooks / search_webhooks / get_statistics
# ---------------------------------------------------------------------------
class TestRetrieval:
    async def test_get_webhook_raises_not_found(self):
        service, repository = _make_service()
        repository.get_by_id.return_value = None

        with pytest.raises(NotFoundException):
            await service.get_webhook(uuid.uuid4())

    async def test_get_webhook_returns_response(self):
        service, repository = _make_service()
        webhook = _make_webhook()
        repository.get_by_id.return_value = webhook

        result = await service.get_webhook(webhook.id)

        assert result.id == webhook.id

    async def test_list_webhooks_computes_total_pages(self):
        from app.schemas.webhook import WebhookFilter

        service, repository = _make_service()
        repository.list_webhooks.return_value = ([_make_webhook()], 21)

        result = await service.list_webhooks(WebhookFilter(page=1, page_size=20))

        assert result.total == 21
        assert result.total_pages == 2

    async def test_search_webhooks_rejects_blank_term(self):
        service, _ = _make_service()
        with pytest.raises(ValidationException):
            await service.search_webhooks("   ")

    async def test_search_webhooks_delegates_to_list_webhooks(self):
        service, repository = _make_service()
        repository.list_webhooks.return_value = ([], 0)

        result = await service.search_webhooks("alpha")

        repository.list_webhooks.assert_awaited_once()
        assert result.total == 0

    async def test_get_statistics_raises_not_found_for_missing_webhook(self):
        service, repository = _make_service()
        repository.get_by_id.return_value = None

        with pytest.raises(NotFoundException):
            await service.get_statistics(webhook_id=uuid.uuid4())

    async def test_get_statistics_returns_populated_response(self):
        service, repository = _make_service()
        repository.get_statistics.return_value = {
            "total_webhooks": 1,
            "active_count": 1,
            "suspended_count": 0,
            "failed_count": 0,
            "by_event": {},
            "by_status": {},
            "total_deliveries": 0,
            "successful_deliveries": 0,
            "failed_deliveries": 0,
            "dead_lettered_deliveries": 0,
            "success_rate_percentage": None,
            "average_duration_ms": None,
            "last_delivery_at": None,
        }

        result = await service.get_statistics()

        assert result.total_webhooks == 1
        assert result.generated_at is not None


# ---------------------------------------------------------------------------
# Delivery Logs / Retry / Manual Trigger / Test (dispatch mocked)
# ---------------------------------------------------------------------------
class TestDeliveryOrchestration:
    async def test_get_delivery_logs_raises_not_found_for_missing_webhook(self):
        from app.schemas.webhook import WebhookLogFilter

        service, repository = _make_service()
        repository.get_by_id.return_value = None

        with pytest.raises(NotFoundException):
            await service.get_delivery_logs(WebhookLogFilter(webhook_id=uuid.uuid4()))

    async def test_retry_delivery_raises_not_found_when_no_eligible_log(self):
        service, repository = _make_service()
        repository.get_failed_log_for_retry.return_value = None

        with pytest.raises(NotFoundException):
            await service.retry_delivery(uuid.uuid4())

    async def test_retry_delivery_raises_business_rule_when_webhook_disabled(self):
        service, repository = _make_service()
        webhook = _make_webhook(enabled=False)
        failed_log = WebhookLog(
            id=uuid.uuid4(),
            webhook_id=webhook.id,
            delivery_status=DeliveryStatus.FAILED,
            attempt_count=1,
            delivered_at=datetime.now(timezone.utc),
        )
        repository.get_failed_log_for_retry.return_value = failed_log
        repository.get_by_id.return_value = webhook

        with pytest.raises(BusinessRuleException):
            await service.retry_delivery(failed_log.id)

    async def test_retry_delivery_raises_business_rule_when_retries_exhausted(self):
        service, repository = _make_service()
        webhook = _make_webhook(enabled=True, retry_count=3)
        failed_log = WebhookLog(
            id=uuid.uuid4(),
            webhook_id=webhook.id,
            delivery_status=DeliveryStatus.FAILED,
            attempt_count=4,
            delivered_at=datetime.now(timezone.utc),
        )
        repository.get_failed_log_for_retry.return_value = failed_log
        repository.get_by_id.return_value = webhook

        with pytest.raises(BusinessRuleException):
            await service.retry_delivery(failed_log.id)

    async def test_manual_trigger_raises_not_found_for_missing_webhook(self):
        service, repository = _make_service()
        repository.get_by_id.return_value = None

        with pytest.raises(NotFoundException):
            await service.manual_trigger(uuid.uuid4(), {"foo": "bar"})

    async def test_manual_trigger_raises_business_rule_when_disabled(self):
        service, repository = _make_service()
        webhook = _make_webhook(enabled=False)
        repository.get_by_id.return_value = webhook

        with pytest.raises(BusinessRuleException):
            await service.manual_trigger(webhook.id, {"foo": "bar"})

    async def test_manual_trigger_raises_validation_for_non_serializable_payload(self):
        service, repository = _make_service()
        webhook = _make_webhook(enabled=True)
        repository.get_by_id.return_value = webhook

        with pytest.raises(ValidationException):
            await service.manual_trigger(webhook.id, {"bad": object()})

    async def test_manual_trigger_dispatches_and_commits(self, monkeypatch):
        service, repository = _make_service()
        webhook = _make_webhook(enabled=True)
        repository.get_by_id.return_value = webhook
        repository.create_log.return_value = WebhookLog(
            id=uuid.uuid4(),
            webhook_id=webhook.id,
            delivery_status=DeliveryStatus.SUCCESS,
            attempt_count=1,
            delivered_at=datetime.now(timezone.utc),
        )
        repository.record_delivery_outcome.return_value = webhook

        mock_response = MagicMock(status_code=200, text="OK")
        mock_client = AsyncMock()
        mock_client.request.return_value = mock_response
        mock_client_cm = AsyncMock()
        mock_client_cm.__aenter__.return_value = mock_client
        mock_client_cm.__aexit__.return_value = False

        import httpx

        monkeypatch.setattr(httpx, "AsyncClient", MagicMock(return_value=mock_client_cm))

        result = await service.manual_trigger(webhook.id, {"foo": "bar"})

        repository.create_log.assert_awaited_once()
        service.session.commit.assert_awaited_once()
        assert result.delivery_status == DeliveryStatus.SUCCESS

    async def test_test_webhook_raises_not_found(self):
        service, repository = _make_service()
        repository.get_by_id.return_value = None

        with pytest.raises(NotFoundException):
            await service.test_webhook(uuid.uuid4())

    async def test_test_webhook_sends_synthetic_payload_and_commits(self, monkeypatch):
        service, repository = _make_service()
        webhook = _make_webhook(enabled=True)
        repository.get_by_id.return_value = webhook
        repository.create_log.return_value = WebhookLog(
            id=uuid.uuid4(),
            webhook_id=webhook.id,
            delivery_status=DeliveryStatus.FAILED,
            attempt_count=1,
            delivered_at=datetime.now(timezone.utc),
        )
        repository.record_delivery_outcome.return_value = webhook

        import httpx

        monkeypatch.setattr(
            httpx,
            "AsyncClient",
            MagicMock(side_effect=httpx.TimeoutException("timed out")),
        )

        result = await service.test_webhook(webhook.id)

        service.session.commit.assert_awaited_once()
        assert result.delivery_status in {DeliveryStatus.FAILED, DeliveryStatus.DEAD_LETTERED}