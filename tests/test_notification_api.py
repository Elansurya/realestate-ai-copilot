"""
Notification Module - Phase 5
API Layer Test Suite

Covers:
    - Authentication
    - Authorization
    - HTTP status codes: 200, 201, 400, 401, 403, 404, 409, 422
    - CRUD
    - Bulk Send
    - Schedule Notification
    - Cancel Schedule
    - Retry Notification
    - Notification Statistics
    - Unread Count
    - Mark Read
    - Queue APIs
    - Logs APIs

These tests drive the FastAPI application end-to-end through an
httpx.AsyncClient using ASGITransport, with the database session and
current-user dependencies overridden for full isolation.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock

import pytest
import pytest_asyncio
from httpx import AsyncClient, ASGITransport

from app.core.security import get_current_user
from app.db.session import get_db
from app.main import app
from app.models.notification import (
    NotificationChannel,
    NotificationPriority,
    NotificationStatus,
)
from app.models.user import User, UserRole

pytestmark = pytest.mark.asyncio

API_PREFIX = "/api/v1/notifications"


# --------------------------------------------------------------------------- #
# Fixtures - Auth & Client
# --------------------------------------------------------------------------- #

@pytest.fixture
def tenant_id() -> uuid.UUID:
    return uuid.uuid4()


@pytest.fixture
def current_user(tenant_id) -> User:
    user = MagicMock(spec=User)
    user.id = uuid.uuid4()
    user.tenant_id = tenant_id
    user.email = "agent@realestateco.com"
    user.roles = [UserRole.SALES_AGENT]
    user.is_active = True
    return user


@pytest.fixture
def admin_user(tenant_id) -> User:
    user = MagicMock(spec=User)
    user.id = uuid.uuid4()
    user.tenant_id = tenant_id
    user.email = "admin@realestateco.com"
    user.roles = [UserRole.ADMIN]
    user.is_active = True
    return user


@pytest.fixture
def mock_db_session():
    return AsyncMock()


@pytest_asyncio.fixture
async def authed_client(current_user, mock_db_session):
    app.dependency_overrides[get_current_user] = lambda: current_user
    app.dependency_overrides[get_db] = lambda: mock_db_session

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://testserver") as client:
        yield client

    app.dependency_overrides.clear()


@pytest_asyncio.fixture
async def admin_client(admin_user, mock_db_session):
    app.dependency_overrides[get_current_user] = lambda: admin_user
    app.dependency_overrides[get_db] = lambda: mock_db_session

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://testserver") as client:
        yield client

    app.dependency_overrides.clear()


@pytest_asyncio.fixture
async def unauthed_client(mock_db_session):
    app.dependency_overrides[get_db] = lambda: mock_db_session

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://testserver") as client:
        yield client

    app.dependency_overrides.clear()


@pytest.fixture
def sample_notification_response(current_user):
    notification_id = uuid.uuid4()
    return {
        "id": str(notification_id),
        "tenant_id": str(current_user.tenant_id),
        "recipient_id": str(uuid.uuid4()),
        "channel": "EMAIL",
        "priority": "MEDIUM",
        "status": "PENDING",
        "subject": "Lead Assigned",
        "message": "A new lead has been assigned to you.",
        "recipient_address": "agent@realestateco.com",
        "is_read": False,
        "retry_count": 0,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }


def _mock_service(monkeypatch, target: str, method: str, return_value=None, side_effect=None):
    """Patch a method on the NotificationService dependency singleton."""
    mock_method = AsyncMock(return_value=return_value, side_effect=side_effect)
    monkeypatch.setattr(f"{target}.{method}", mock_method)
    return mock_method


# --------------------------------------------------------------------------- #
# Authentication
# --------------------------------------------------------------------------- #

class TestAuthentication:
    async def test_list_notifications_without_token_returns_401(self, unauthed_client):
        response = await unauthed_client.get(API_PREFIX)
        assert response.status_code == 401

    async def test_create_notification_without_token_returns_401(self, unauthed_client):
        response = await unauthed_client.post(API_PREFIX, json={})
        assert response.status_code == 401

    async def test_expired_token_returns_401(self, mock_db_session):
        async def raise_unauthorized():
            from fastapi import HTTPException
            raise HTTPException(status_code=401, detail="Token expired")

        app.dependency_overrides[get_current_user] = raise_unauthorized
        app.dependency_overrides[get_db] = lambda: mock_db_session

        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://testserver") as client:
            response = await client.get(API_PREFIX)

        app.dependency_overrides.clear()
        assert response.status_code == 401

    async def test_valid_token_grants_access(self, authed_client, monkeypatch):
        _mock_service(
            monkeypatch,
            "app.api.v1.notifications.notification_service",
            "list_notifications",
            return_value=([], 0),
        )
        response = await authed_client.get(API_PREFIX)
        assert response.status_code == 200


# --------------------------------------------------------------------------- #
# Authorization
# --------------------------------------------------------------------------- #

class TestAuthorization:
    async def test_agent_cannot_access_admin_only_bulk_broadcast(
        self, authed_client, monkeypatch
    ):
        response = await authed_client.post(
            f"{API_PREFIX}/broadcast",
            json={
                "channel": "IN_APP",
                "priority": "LOW",
                "subject": "Org Announcement",
                "message": "New policy effective immediately.",
            },
        )
        assert response.status_code == 403

    async def test_admin_can_access_bulk_broadcast(
        self, admin_client, monkeypatch
    ):
        _mock_service(
            monkeypatch,
            "app.api.v1.notifications.notification_service",
            "broadcast",
            return_value={"dispatched": 120},
        )
        response = await admin_client.post(
            f"{API_PREFIX}/broadcast",
            json={
                "channel": "IN_APP",
                "priority": "LOW",
                "subject": "Org Announcement",
                "message": "New policy effective immediately.",
            },
        )
        assert response.status_code == 200

    async def test_agent_cannot_access_other_tenants_notification(
        self, authed_client, monkeypatch
    ):
        other_tenant_notification_id = uuid.uuid4()
        _mock_service(
            monkeypatch,
            "app.api.v1.notifications.notification_service",
            "get_by_id",
            side_effect=PermissionError("Cross-tenant access denied"),
        )
        response = await authed_client.get(
            f"{API_PREFIX}/{other_tenant_notification_id}"
        )
        assert response.status_code == 403


# --------------------------------------------------------------------------- #
# CRUD
# --------------------------------------------------------------------------- #

class TestNotificationCRUDApi:
    async def test_create_notification_returns_201(
        self, authed_client, monkeypatch, sample_notification_response
    ):
        _mock_service(
            monkeypatch,
            "app.api.v1.notifications.notification_service",
            "send",
            return_value=sample_notification_response,
        )
        payload = {
            "recipient_id": sample_notification_response["recipient_id"],
            "channel": "EMAIL",
            "priority": "MEDIUM",
            "subject": "Lead Assigned",
            "message": "A new lead has been assigned to you.",
            "recipient_address": "agent@realestateco.com",
        }
        response = await authed_client.post(API_PREFIX, json=payload)
        assert response.status_code == 201
        assert response.json()["subject"] == "Lead Assigned"

    async def test_create_notification_missing_required_field_returns_422(self, authed_client):
        response = await authed_client.post(API_PREFIX, json={"channel": "EMAIL"})
        assert response.status_code == 422

    async def test_create_notification_invalid_channel_returns_422(self, authed_client):
        payload = {
            "recipient_id": str(uuid.uuid4()),
            "channel": "CARRIER_PIGEON",
            "priority": "MEDIUM",
            "subject": "Subject",
            "message": "Message",
        }
        response = await authed_client.post(API_PREFIX, json=payload)
        assert response.status_code == 422

    async def test_get_notification_by_id_returns_200(
        self, authed_client, monkeypatch, sample_notification_response
    ):
        _mock_service(
            monkeypatch,
            "app.api.v1.notifications.notification_service",
            "get_by_id",
            return_value=sample_notification_response,
        )
        response = await authed_client.get(
            f"{API_PREFIX}/{sample_notification_response['id']}"
        )
        assert response.status_code == 200
        assert response.json()["id"] == sample_notification_response["id"]

    async def test_get_notification_not_found_returns_404(self, authed_client, monkeypatch):
        _mock_service(
            monkeypatch,
            "app.api.v1.notifications.notification_service",
            "get_by_id",
            return_value=None,
        )
        response = await authed_client.get(f"{API_PREFIX}/{uuid.uuid4()}")
        assert response.status_code == 404

    async def test_get_notification_invalid_uuid_returns_422(self, authed_client):
        response = await authed_client.get(f"{API_PREFIX}/not-a-uuid")
        assert response.status_code == 422

    async def test_update_notification_returns_200(
        self, authed_client, monkeypatch, sample_notification_response
    ):
        updated = {**sample_notification_response, "subject": "Updated Subject"}
        _mock_service(
            monkeypatch,
            "app.api.v1.notifications.notification_service",
            "update",
            return_value=updated,
        )
        response = await authed_client.patch(
            f"{API_PREFIX}/{sample_notification_response['id']}",
            json={"subject": "Updated Subject"},
        )
        assert response.status_code == 200
        assert response.json()["subject"] == "Updated Subject"

    async def test_update_nonexistent_notification_returns_404(self, authed_client, monkeypatch):
        _mock_service(
            monkeypatch,
            "app.api.v1.notifications.notification_service",
            "update",
            return_value=None,
        )
        response = await authed_client.patch(
            f"{API_PREFIX}/{uuid.uuid4()}", json={"subject": "Ghost"}
        )
        assert response.status_code == 404

    async def test_delete_notification_returns_200(
        self, authed_client, monkeypatch, sample_notification_response
    ):
        _mock_service(
            monkeypatch,
            "app.api.v1.notifications.notification_service",
            "soft_delete",
            return_value={**sample_notification_response, "is_deleted": True},
        )
        response = await authed_client.delete(
            f"{API_PREFIX}/{sample_notification_response['id']}"
        )
        assert response.status_code == 200

    async def test_delete_already_deleted_notification_returns_409(
        self, authed_client, monkeypatch
    ):
        from app.core.exceptions import NotificationConflictError

        _mock_service(
            monkeypatch,
            "app.api.v1.notifications.notification_service",
            "soft_delete",
            side_effect=NotificationConflictError("Notification already deleted"),
        )
        response = await authed_client.delete(f"{API_PREFIX}/{uuid.uuid4()}")
        assert response.status_code == 409

    async def test_list_notifications_returns_200_with_pagination_metadata(
        self, authed_client, monkeypatch, sample_notification_response
    ):
        _mock_service(
            monkeypatch,
            "app.api.v1.notifications.notification_service",
            "list_notifications",
            return_value=([sample_notification_response], 1),
        )
        response = await authed_client.get(f"{API_PREFIX}?page=1&page_size=20")
        body = response.json()
        assert response.status_code == 200
        assert body["total"] == 1
        assert len(body["items"]) == 1


# --------------------------------------------------------------------------- #
# Bulk Send
# --------------------------------------------------------------------------- #

class TestBulkSendApi:
    async def test_bulk_send_returns_201(self, authed_client, monkeypatch):
        _mock_service(
            monkeypatch,
            "app.api.v1.notifications.notification_service",
            "bulk_send",
            return_value=[{"id": str(uuid.uuid4()), "status": "SENT"} for _ in range(3)],
        )
        payload = {
            "recipient_ids": [str(uuid.uuid4()) for _ in range(3)],
            "channel": "IN_APP",
            "priority": "LOW",
            "subject": "System Maintenance",
            "message": "Scheduled maintenance tonight at 11 PM.",
        }
        response = await authed_client.post(f"{API_PREFIX}/bulk", json=payload)
        assert response.status_code == 201
        assert len(response.json()) == 3

    async def test_bulk_send_empty_recipients_returns_400(self, authed_client, monkeypatch):
        from app.core.exceptions import NotificationValidationError

        _mock_service(
            monkeypatch,
            "app.api.v1.notifications.notification_service",
            "bulk_send",
            side_effect=NotificationValidationError("recipient_ids cannot be empty"),
        )
        payload = {
            "recipient_ids": [],
            "channel": "IN_APP",
            "priority": "LOW",
            "subject": "Empty",
            "message": "Empty",
        }
        response = await authed_client.post(f"{API_PREFIX}/bulk", json=payload)
        assert response.status_code == 400

    async def test_bulk_send_missing_channel_returns_422(self, authed_client):
        payload = {"recipient_ids": [str(uuid.uuid4())], "subject": "s", "message": "m"}
        response = await authed_client.post(f"{API_PREFIX}/bulk", json=payload)
        assert response.status_code == 422


# --------------------------------------------------------------------------- #
# Schedule Notification / Cancel Schedule
# --------------------------------------------------------------------------- #

class TestScheduleApi:
    async def test_schedule_notification_returns_201(self, authed_client, monkeypatch):
        scheduled_at = (datetime.now(timezone.utc) + timedelta(hours=2)).isoformat()
        _mock_service(
            monkeypatch,
            "app.api.v1.notifications.notification_service",
            "schedule",
            return_value={
                "id": str(uuid.uuid4()),
                "status": "SCHEDULED",
                "scheduled_at": scheduled_at,
            },
        )
        payload = {
            "recipient_id": str(uuid.uuid4()),
            "channel": "EMAIL",
            "priority": "MEDIUM",
            "subject": "Reminder",
            "message": "Your appointment is tomorrow.",
            "recipient_address": "client@example.com",
            "scheduled_at": scheduled_at,
        }
        response = await authed_client.post(f"{API_PREFIX}/schedule", json=payload)
        assert response.status_code == 201
        assert response.json()["status"] == "SCHEDULED"

    async def test_schedule_notification_in_past_returns_400(self, authed_client, monkeypatch):
        from app.core.exceptions import NotificationValidationError

        _mock_service(
            monkeypatch,
            "app.api.v1.notifications.notification_service",
            "schedule",
            side_effect=NotificationValidationError("scheduled_at must be in the future"),
        )
        payload = {
            "recipient_id": str(uuid.uuid4()),
            "channel": "EMAIL",
            "priority": "MEDIUM",
            "subject": "Reminder",
            "message": "Past reminder",
            "recipient_address": "client@example.com",
            "scheduled_at": (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat(),
        }
        response = await authed_client.post(f"{API_PREFIX}/schedule", json=payload)
        assert response.status_code == 400

    async def test_cancel_scheduled_notification_returns_200(self, authed_client, monkeypatch):
        notification_id = uuid.uuid4()
        _mock_service(
            monkeypatch,
            "app.api.v1.notifications.notification_service",
            "cancel_schedule",
            return_value={"id": str(notification_id), "status": "CANCELLED"},
        )
        response = await authed_client.post(f"{API_PREFIX}/{notification_id}/cancel")
        assert response.status_code == 200
        assert response.json()["status"] == "CANCELLED"

    async def test_cancel_already_sent_notification_returns_409(self, authed_client, monkeypatch):
        from app.core.exceptions import NotificationConflictError

        _mock_service(
            monkeypatch,
            "app.api.v1.notifications.notification_service",
            "cancel_schedule",
            side_effect=NotificationConflictError("Notification already sent"),
        )
        response = await authed_client.post(f"{API_PREFIX}/{uuid.uuid4()}/cancel")
        assert response.status_code == 409

    async def test_cancel_nonexistent_schedule_returns_404(self, authed_client, monkeypatch):
        _mock_service(
            monkeypatch,
            "app.api.v1.notifications.notification_service",
            "cancel_schedule",
            return_value=None,
        )
        response = await authed_client.post(f"{API_PREFIX}/{uuid.uuid4()}/cancel")
        assert response.status_code == 404


# --------------------------------------------------------------------------- #
# Retry Notification
# --------------------------------------------------------------------------- #

class TestRetryApi:
    async def test_retry_notification_returns_200(self, authed_client, monkeypatch):
        notification_id = uuid.uuid4()
        _mock_service(
            monkeypatch,
            "app.api.v1.notifications.notification_service",
            "retry",
            return_value={"id": str(notification_id), "status": "PENDING", "retry_count": 0},
        )
        response = await authed_client.post(f"{API_PREFIX}/{notification_id}/retry")
        assert response.status_code == 200
        assert response.json()["status"] == "PENDING"

    async def test_retry_non_failed_notification_returns_400(self, authed_client, monkeypatch):
        from app.core.exceptions import NotificationValidationError

        _mock_service(
            monkeypatch,
            "app.api.v1.notifications.notification_service",
            "retry",
            side_effect=NotificationValidationError("Only failed notifications can be retried"),
        )
        response = await authed_client.post(f"{API_PREFIX}/{uuid.uuid4()}/retry")
        assert response.status_code == 400

    async def test_retry_nonexistent_notification_returns_404(self, authed_client, monkeypatch):
        _mock_service(
            monkeypatch,
            "app.api.v1.notifications.notification_service",
            "retry",
            return_value=None,
        )
        response = await authed_client.post(f"{API_PREFIX}/{uuid.uuid4()}/retry")
        assert response.status_code == 404


# --------------------------------------------------------------------------- #
# Notification Statistics
# --------------------------------------------------------------------------- #

class TestStatisticsApi:
    async def test_get_statistics_returns_200(self, authed_client, monkeypatch):
        _mock_service(
            monkeypatch,
            "app.api.v1.notifications.notification_service",
            "get_statistics",
            return_value={
                "total": 500,
                "delivered": 430,
                "failed": 40,
                "pending": 30,
                "by_channel": {
                    "EMAIL": 300,
                    "SMS": 100,
                    "WHATSAPP": 50,
                    "PUSH": 30,
                    "IN_APP": 20,
                },
            },
        )
        response = await authed_client.get(f"{API_PREFIX}/statistics")
        body = response.json()
        assert response.status_code == 200
        assert body["total"] == 500
        assert body["by_channel"]["EMAIL"] == 300

    async def test_get_statistics_with_date_range_filters(self, authed_client, monkeypatch):
        mock = _mock_service(
            monkeypatch,
            "app.api.v1.notifications.notification_service",
            "get_statistics",
            return_value={"total": 10, "delivered": 8, "failed": 1, "pending": 1},
        )
        response = await authed_client.get(
            f"{API_PREFIX}/statistics?date_from=2026-07-01&date_to=2026-08-01"
        )
        assert response.status_code == 200
        mock.assert_awaited_once()


# --------------------------------------------------------------------------- #
# Unread Count / Mark Read
# --------------------------------------------------------------------------- #

class TestReadStatusApi:
    async def test_get_unread_count_returns_200(self, authed_client, monkeypatch):
        _mock_service(
            monkeypatch,
            "app.api.v1.notifications.notification_service",
            "get_unread_count",
            return_value=7,
        )
        response = await authed_client.get(f"{API_PREFIX}/unread-count")
        assert response.status_code == 200
        assert response.json()["unread_count"] == 7

    async def test_mark_notification_as_read_returns_200(
        self, authed_client, monkeypatch, sample_notification_response
    ):
        _mock_service(
            monkeypatch,
            "app.api.v1.notifications.notification_service",
            "mark_as_read",
            return_value={**sample_notification_response, "is_read": True},
        )
        response = await authed_client.post(
            f"{API_PREFIX}/{sample_notification_response['id']}/read"
        )
        assert response.status_code == 200
        assert response.json()["is_read"] is True

    async def test_mark_all_as_read_returns_200(self, authed_client, monkeypatch):
        _mock_service(
            monkeypatch,
            "app.api.v1.notifications.notification_service",
            "mark_all_as_read",
            return_value={"updated_count": 12},
        )
        response = await authed_client.post(f"{API_PREFIX}/read-all")
        assert response.status_code == 200
        assert response.json()["updated_count"] == 12

    async def test_mark_read_nonexistent_notification_returns_404(
        self, authed_client, monkeypatch
    ):
        _mock_service(
            monkeypatch,
            "app.api.v1.notifications.notification_service",
            "mark_as_read",
            return_value=None,
        )
        response = await authed_client.post(f"{API_PREFIX}/{uuid.uuid4()}/read")
        assert response.status_code == 404


# --------------------------------------------------------------------------- #
# Queue APIs
# --------------------------------------------------------------------------- #

class TestQueueApi:
    async def test_get_queue_status_returns_200(self, admin_client, monkeypatch):
        _mock_service(
            monkeypatch,
            "app.api.v1.notifications.queue_service",
            "get_queue_depth",
            return_value=42,
        )
        response = await admin_client.get(f"{API_PREFIX}/queue/status")
        assert response.status_code == 200
        assert response.json()["depth"] == 42

    async def test_agent_forbidden_from_queue_status(self, authed_client):
        response = await authed_client.get(f"{API_PREFIX}/queue/status")
        assert response.status_code == 403

    async def test_get_queue_item_by_id_returns_200(self, admin_client, monkeypatch):
        queue_id = uuid.uuid4()
        _mock_service(
            monkeypatch,
            "app.api.v1.notifications.queue_service",
            "get_by_id",
            return_value={
                "id": str(queue_id),
                "notification_id": str(uuid.uuid4()),
                "status": "QUEUED",
                "priority": "HIGH",
            },
        )
        response = await admin_client.get(f"{API_PREFIX}/queue/{queue_id}")
        assert response.status_code == 200
        assert response.json()["id"] == str(queue_id)

    async def test_get_queue_item_not_found_returns_404(self, admin_client, monkeypatch):
        _mock_service(
            monkeypatch,
            "app.api.v1.notifications.queue_service",
            "get_by_id",
            return_value=None,
        )
        response = await admin_client.get(f"{API_PREFIX}/queue/{uuid.uuid4()}")
        assert response.status_code == 404


# --------------------------------------------------------------------------- #
# Logs APIs
# --------------------------------------------------------------------------- #

class TestLogsApi:
    async def test_get_logs_for_notification_returns_200(self, authed_client, monkeypatch):
        notification_id = uuid.uuid4()
        _mock_service(
            monkeypatch,
            "app.api.v1.notifications.notification_service",
            "get_logs",
            return_value=(
                [
                    {"event": "QUEUED", "details": "Queued", "created_at": datetime.now(timezone.utc).isoformat()},
                    {"event": "SENT", "details": "Sent", "created_at": datetime.now(timezone.utc).isoformat()},
                ],
                2,
            ),
        )
        response = await authed_client.get(f"{API_PREFIX}/{notification_id}/logs")
        body = response.json()
        assert response.status_code == 200
        assert body["total"] == 2
        assert body["items"][0]["event"] == "QUEUED"

    async def test_get_logs_for_nonexistent_notification_returns_404(
        self, authed_client, monkeypatch
    ):
        _mock_service(
            monkeypatch,
            "app.api.v1.notifications.notification_service",
            "get_logs",
            return_value=None,
        )
        response = await authed_client.get(f"{API_PREFIX}/{uuid.uuid4()}/logs")
        assert response.status_code == 404

    async def test_get_logs_paginated(self, authed_client, monkeypatch):
        _mock_service(
            monkeypatch,
            "app.api.v1.notifications.notification_service",
            "get_logs",
            return_value=([{"event": f"E{i}"} for i in range(10)], 25),
        )
        response = await authed_client.get(
            f"{API_PREFIX}/{uuid.uuid4()}/logs?page=1&page_size=10"
        )
        body = response.json()
        assert response.status_code == 200
        assert len(body["items"]) == 10
        assert body["total"] == 25


# --------------------------------------------------------------------------- #
# Conflict (409) - cross-cutting
# --------------------------------------------------------------------------- #

class TestConflictHandling:
    async def test_create_duplicate_template_binding_returns_409(
        self, admin_client, monkeypatch
    ):
        from app.core.exceptions import NotificationConflictError

        _mock_service(
            monkeypatch,
            "app.api.v1.notifications.template_service",
            "create",
            side_effect=NotificationConflictError("Template name already exists for channel"),
        )
        payload = {
            "name": "viewing_confirmation",
            "channel": "EMAIL",
            "subject_template": "Subject {{x}}",
            "body_template": "Body {{x}}",
            "is_active": True,
        }
        response = await admin_client.post(f"{API_PREFIX}/templates", json=payload)
        assert response.status_code == 409