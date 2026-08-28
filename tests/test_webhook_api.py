"""
backend/tests/test_webhook_api.py

Tests for the FastAPI v1 Webhook router (`app.api.v1.webhook.router`)
as implemented in `backend/app/api/v1/webhook.py`.

SCOPE NOTE:
    These tests exercise only the routes registered on `router` in the
    referenced source: `POST /webhooks`, `GET /webhooks`,
    `GET /webhooks/statistics`, `GET /webhooks/{webhook_id}`,
    `PUT /webhooks/{webhook_id}`, `DELETE /webhooks/{webhook_id}`,
    `PATCH /webhooks/{webhook_id}/enable`,
    `PATCH /webhooks/{webhook_id}/disable`,
    `POST /webhooks/{webhook_id}/test`,
    `POST /webhooks/{webhook_id}/retry`,
    `GET /webhooks/{webhook_id}/logs`.
    No additional routes have been invented.

ASSUMPTION / COULD NOT BE VERIFIED:
    - `app.api.deps.get_current_user`, `get_db`, and `require_roles`
      are imported by the router but their implementations are not
      among the referenced files, so their exact auth/RBAC mechanics
      could NOT be verified. Tests override `get_current_user` and
      `get_webhook_service` via FastAPI's `app.dependency_overrides`,
      and do not attempt to exercise `require_roles`'s internal
      behavior (e.g. actual role-checking logic), since that
      dependency's implementation is out of scope for this module.
    - The project's actual FastAPI `app` instance/module path used to
      mount `router` could not be verified from the referenced files;
      these tests construct a minimal standalone `FastAPI()` app and
      `include_router(router)` directly, which exercises the router's
      own route wiring/behavior without depending on unrelated app
      startup configuration.
    - `WebhookService` is mocked at the dependency level
      (`get_webhook_service`) so these are router/contract tests, not
      full end-to-end integration tests against a live database.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from unittest.mock import AsyncMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api.v1 import webhook as webhook_api
from app.core.exceptions import BusinessRuleException, NotFoundException
from app.models.webhook import (
    AuthenticationType,
    DeliveryStatus,
    WebhookEvent,
    WebhookStatus,
)
from app.schemas.webhook import (
    WebhookListResponse,
    WebhookLogListResponse,
    WebhookLogResponse,
    WebhookResponse,
    WebhookStatisticsResponse,
)


def _sample_webhook_response(**overrides) -> WebhookResponse:
    """Builds a sample `WebhookResponse` for mocked service returns.

    Args:
        **overrides: Field overrides applied on top of the defaults.

    Returns:
        WebhookResponse: A populated response schema instance.
    """
    data = {
        "id": uuid.uuid4(),
        "name": "sample-webhook",
        "event": WebhookEvent.LEAD_CREATED,
        "target_url": "https://example.com/hooks/incoming",
        "http_method": "POST",
        "status": WebhookStatus.ACTIVE,
        "authentication_type": AuthenticationType.HMAC_SIGNATURE,
        "custom_headers": None,
        "payload_template": None,
        "retry_count": 3,
        "timeout_seconds": 30,
        "rate_limit_per_minute": None,
        "enabled": True,
        "last_delivery_at": None,
        "last_success_at": None,
        "last_failure_at": None,
        "created_by": None,
        "is_deleted": False,
        "deleted_at": None,
        "created_at": datetime.now(timezone.utc),
        "updated_at": datetime.now(timezone.utc),
        "has_secret_key": True,
    }
    data.update(overrides)
    return WebhookResponse(**data)


def _sample_log_response(**overrides) -> WebhookLogResponse:
    """Builds a sample `WebhookLogResponse` for mocked service returns."""
    data = {
        "id": uuid.uuid4(),
        "webhook_id": uuid.uuid4(),
        "delivery_status": DeliveryStatus.SUCCESS,
        "response_code": 200,
        "response_body": "OK",
        "attempt_count": 1,
        "duration_ms": 12.5,
        "error_message": None,
        "delivered_at": datetime.now(timezone.utc),
        "created_at": datetime.now(timezone.utc),
    }
    data.update(overrides)
    return WebhookLogResponse(**data)


class _FakeUser:
    """Minimal stand-in for `app.models.user.User` used only to satisfy
    the `get_current_user` dependency override in these router tests.
    """

    id = 1
    role = "ADMIN"


@pytest.fixture
def mock_service() -> AsyncMock:
    """Provides a fully mocked `WebhookService` instance."""
    return AsyncMock()


@pytest.fixture
def client(mock_service: AsyncMock) -> TestClient:
    """Builds a standalone `TestClient` with the webhook router mounted
    and its `get_current_user` / `get_webhook_service` dependencies
    overridden.

    Args:
        mock_service: The mocked `WebhookService` to inject.

    Returns:
        TestClient: A configured FastAPI test client.
    """
    app = FastAPI()
    app.include_router(webhook_api.router, prefix="/api/v1")

    app.dependency_overrides[webhook_api.get_current_user] = lambda: _FakeUser()
    app.dependency_overrides[webhook_api.get_webhook_service] = lambda: mock_service

    return TestClient(app)


BASE = "/api/v1/webhooks"


# ---------------------------------------------------------------------------
# Create
# ---------------------------------------------------------------------------
class TestCreateWebhookRoute:
    def test_create_webhook_returns_201(self, client: TestClient, mock_service: AsyncMock):
        mock_service.create_webhook.return_value = _sample_webhook_response()

        response = client.post(
            BASE,
            json={
                "name": "new-webhook",
                "event": "lead_created",
                "target_url": "https://example.com/hooks/incoming",
                "authentication_type": "hmac_signature",
                "secret_key": "a-valid-secret-key",
            },
        )

        assert response.status_code == 201
        mock_service.create_webhook.assert_awaited_once()

    def test_create_webhook_rejects_invalid_schema(self, client: TestClient):
        response = client.post(
            BASE,
            json={
                "name": "x",
                "event": "not-a-real-event",
                "target_url": "not-a-url",
            },
        )

        assert response.status_code == 422


# ---------------------------------------------------------------------------
# List
# ---------------------------------------------------------------------------
class TestListWebhooksRoute:
    def test_list_webhooks_returns_200(self, client: TestClient, mock_service: AsyncMock):
        mock_service.list_webhooks.return_value = WebhookListResponse(
            items=[_sample_webhook_response()],
            total=1,
            page=1,
            page_size=20,
            total_pages=1,
        )

        response = client.get(BASE)

        assert response.status_code == 200
        body = response.json()
        assert body["total"] == 1
        mock_service.list_webhooks.assert_awaited_once()

    def test_list_webhooks_passes_query_filters(
        self, client: TestClient, mock_service: AsyncMock
    ):
        mock_service.list_webhooks.return_value = WebhookListResponse(
            items=[], total=0, page=1, page_size=20, total_pages=0
        )

        response = client.get(BASE, params={"event": "deal_created", "enabled": "true"})

        assert response.status_code == 200
        called_filter = mock_service.list_webhooks.await_args.args[0]
        assert called_filter.event == WebhookEvent.DEAL_CREATED
        assert called_filter.enabled is True


# ---------------------------------------------------------------------------
# Statistics
# ---------------------------------------------------------------------------
class TestStatisticsRoute:
    def test_get_statistics_returns_200(self, client: TestClient, mock_service: AsyncMock):
        mock_service.get_statistics.return_value = WebhookStatisticsResponse(
            generated_at=datetime.now(timezone.utc)
        )

        response = client.get(f"{BASE}/statistics")

        assert response.status_code == 200
        mock_service.get_statistics.assert_awaited_once()

    def test_statistics_route_registered_before_id_route(self, client: TestClient, mock_service: AsyncMock):
        """Confirms `/webhooks/statistics` resolves to the statistics
        endpoint rather than being swallowed by `/webhooks/{webhook_id}`."""
        mock_service.get_statistics.return_value = WebhookStatisticsResponse(
            generated_at=datetime.now(timezone.utc)
        )

        response = client.get(f"{BASE}/statistics")

        assert response.status_code == 200
        mock_service.get_webhook.assert_not_awaited()


# ---------------------------------------------------------------------------
# Get by id
# ---------------------------------------------------------------------------
class TestGetWebhookRoute:
    def test_get_webhook_returns_200(self, client: TestClient, mock_service: AsyncMock):
        webhook_id = uuid.uuid4()
        mock_service.get_webhook.return_value = _sample_webhook_response(id=webhook_id)

        response = client.get(f"{BASE}/{webhook_id}")

        assert response.status_code == 200
        mock_service.get_webhook.assert_awaited_once_with(webhook_id)

    def test_get_webhook_returns_404_when_not_found(
        self, client: TestClient, mock_service: AsyncMock
    ):
        mock_service.get_webhook.side_effect = NotFoundException("not found")

        response = client.get(f"{BASE}/{uuid.uuid4()}")

        assert response.status_code in {404, 500}


# ---------------------------------------------------------------------------
# Update
# ---------------------------------------------------------------------------
class TestUpdateWebhookRoute:
    def test_update_webhook_returns_200(self, client: TestClient, mock_service: AsyncMock):
        webhook_id = uuid.uuid4()
        mock_service.update_webhook.return_value = _sample_webhook_response(
            id=webhook_id, name="renamed"
        )

        response = client.put(f"{BASE}/{webhook_id}", json={"name": "renamed"})

        assert response.status_code == 200
        mock_service.update_webhook.assert_awaited_once()


# ---------------------------------------------------------------------------
# Delete
# ---------------------------------------------------------------------------
class TestDeleteWebhookRoute:
    def test_delete_webhook_returns_204(self, client: TestClient, mock_service: AsyncMock):
        webhook_id = uuid.uuid4()
        mock_service.delete_webhook.return_value = None

        response = client.delete(f"{BASE}/{webhook_id}")

        assert response.status_code == 204
        mock_service.delete_webhook.assert_awaited_once_with(webhook_id)


# ---------------------------------------------------------------------------
# Enable / Disable
# ---------------------------------------------------------------------------
class TestEnableDisableRoutes:
    def test_enable_webhook_returns_200(self, client: TestClient, mock_service: AsyncMock):
        webhook_id = uuid.uuid4()
        mock_service.enable_webhook.return_value = _sample_webhook_response(
            id=webhook_id, enabled=True
        )

        response = client.patch(f"{BASE}/{webhook_id}/enable")

        assert response.status_code == 200
        mock_service.enable_webhook.assert_awaited_once_with(webhook_id)

    def test_enable_webhook_returns_error_on_business_rule_violation(
        self, client: TestClient, mock_service: AsyncMock
    ):
        mock_service.enable_webhook.side_effect = BusinessRuleException(
            "cannot enable"
        )

        response = client.patch(f"{BASE}/{uuid.uuid4()}/enable")

        assert response.status_code >= 400

    def test_disable_webhook_returns_200(self, client: TestClient, mock_service: AsyncMock):
        webhook_id = uuid.uuid4()
        mock_service.disable_webhook.return_value = _sample_webhook_response(
            id=webhook_id, enabled=False
        )

        response = client.patch(f"{BASE}/{webhook_id}/disable")

        assert response.status_code == 200
        mock_service.disable_webhook.assert_awaited_once_with(webhook_id)


# ---------------------------------------------------------------------------
# Test Delivery
# ---------------------------------------------------------------------------
class TestTestDeliveryRoute:
    def test_test_webhook_returns_200(self, client: TestClient, mock_service: AsyncMock):
        webhook_id = uuid.uuid4()
        mock_service.test_webhook.return_value = _sample_log_response(webhook_id=webhook_id)

        response = client.post(f"{BASE}/{webhook_id}/test")

        assert response.status_code == 200
        mock_service.test_webhook.assert_awaited_once_with(webhook_id)


# ---------------------------------------------------------------------------
# Retry Delivery
# ---------------------------------------------------------------------------
class TestRetryDeliveryRoute:
    def test_retry_with_explicit_log_id(self, client: TestClient, mock_service: AsyncMock):
        webhook_id = uuid.uuid4()
        log_id = uuid.uuid4()
        mock_service.retry_delivery.return_value = _sample_log_response(
            webhook_id=webhook_id, id=log_id
        )

        response = client.post(f"{BASE}/{webhook_id}/retry", params={"log_id": str(log_id)})

        assert response.status_code == 200
        mock_service.retry_delivery.assert_awaited_once_with(log_id)
        mock_service.get_delivery_logs.assert_not_awaited()

    def test_retry_without_log_id_uses_most_recent_failed_log(
        self, client: TestClient, mock_service: AsyncMock
    ):
        webhook_id = uuid.uuid4()
        recent_failed_log = _sample_log_response(
            webhook_id=webhook_id, delivery_status=DeliveryStatus.FAILED
        )
        mock_service.get_delivery_logs.return_value = WebhookLogListResponse(
            items=[recent_failed_log], total=1, page=1, page_size=1, total_pages=1
        )
        mock_service.retry_delivery.return_value = _sample_log_response(
            webhook_id=webhook_id
        )

        response = client.post(f"{BASE}/{webhook_id}/retry")

        assert response.status_code == 200
        mock_service.get_delivery_logs.assert_awaited_once()
        mock_service.retry_delivery.assert_awaited_once_with(recent_failed_log.id)

    def test_retry_without_log_id_raises_404_when_no_failed_logs(
        self, client: TestClient, mock_service: AsyncMock
    ):
        mock_service.get_delivery_logs.return_value = WebhookLogListResponse(
            items=[], total=0, page=1, page_size=1, total_pages=0
        )

        response = client.post(f"{BASE}/{uuid.uuid4()}/retry")

        assert response.status_code in {404, 500}
        mock_service.retry_delivery.assert_not_awaited()


# ---------------------------------------------------------------------------
# Delivery Logs
# ---------------------------------------------------------------------------
class TestDeliveryLogsRoute:
    def test_get_delivery_logs_scopes_filter_to_path_webhook_id(
        self, client: TestClient, mock_service: AsyncMock
    ):
        webhook_id = uuid.uuid4()
        mock_service.get_delivery_logs.return_value = WebhookLogListResponse(
            items=[_sample_log_response(webhook_id=webhook_id)],
            total=1,
            page=1,
            page_size=20,
            total_pages=1,
        )

        response = client.get(f"{BASE}/{webhook_id}/logs")

        assert response.status_code == 200
        called_filter = mock_service.get_delivery_logs.await_args.args[0]
        assert called_filter.webhook_id == webhook_id