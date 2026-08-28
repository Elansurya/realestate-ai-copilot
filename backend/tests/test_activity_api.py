"""
backend/tests/test_activity_api.py

Test suite for ``app.api.v1.activity`` (the Activity Timeline REST
API).

Uses FastAPI's ``TestClient`` against the project's ``app`` instance
with dependency overrides for ``get_current_user`` and
``get_activity_service`` -- so these tests exercise routing,
status codes, request/response schema validation, and RBAC wiring
without touching a real database.

Mirrors the existing project's API test conventions (dependency
overrides via ``app.dependency_overrides``, a shared ``client``
fixture, and role-based fixtures for admin/manager/agent/viewer
personas).
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from unittest.mock import AsyncMock

import pytest
from fastapi import HTTPException, status
from fastapi.testclient import TestClient

from app.api.deps import get_current_user
from app.api.v1.activity import get_activity_service
from app.core.exceptions import NotFoundException
from app.main import app
from app.models.activity import (
    Activity,
    ActivityModule,
    ActivityPriority,
    ActivityStatus,
    ActivityType,
)
from app.models.user import UserRole
from app.schemas.activity import ActivityListResponse, ActivityResponse, TimelineResponse


# ---------------------------------------------------------------------------
# Helpers / fixtures
# ---------------------------------------------------------------------------
def make_activity(**overrides) -> Activity:
    """Builds an in-memory ``Activity`` instance for mocked service returns.

    Args:
        **overrides: Field values to override the defaults with.

    Returns:
        Activity: A fully-populated, unpersisted ORM instance.
    """
    defaults = dict(
        id=uuid.uuid4(),
        module=ActivityModule.BOOKING,
        entity_type="Booking",
        entity_id=str(uuid.uuid4()),
        action=ActivityType.CREATED,
        title="Booking created",
        description=None,
        old_value=None,
        new_value=None,
        meta_data=None,
        priority=ActivityPriority.NORMAL,
        status=ActivityStatus.ACTIVE,
        performed_by_id=1,
        assigned_to_id=None,
        ip_address=None,
        user_agent=None,
        source="web",
        is_deleted=False,
        deleted_at=None,
        created_at=datetime.now(timezone.utc),
        updated_at=datetime.now(timezone.utc),
    )
    defaults.update(overrides)
    return Activity(**defaults)


def make_list_response(
    activities: list[Activity], total: int, *, page: int = 1, page_size: int = 20
) -> ActivityListResponse:
    """Builds an ``ActivityListResponse`` for mocked ``ActivityService`` returns.

    ``ActivityService.list_activities`` (unlike ``get_module_timeline``/
    ``get_user_timeline``) returns an already-assembled
    ``ActivityListResponse`` -- the router returns it straight through
    (``return await service.list_activities(filters)``) rather than
    building the envelope itself -- so mocks for it must return this
    type, not a raw ``(items, total)`` tuple.

    Args:
        activities: The page of ``Activity`` ORM instances to wrap.
        total: Total number of entries matching the query.
        page: 1-indexed page number.
        page_size: Number of items requested per page.

    Returns:
        ActivityListResponse: The assembled response envelope.
    """
    return ActivityListResponse(
        items=[ActivityResponse.model_validate(item) for item in activities],
        total=total,
        page=page,
        page_size=page_size,
        total_pages=(total + page_size - 1) // page_size if page_size else 0,
    )


class _FakeUser:
    """Minimal stand-in for the authenticated ``User`` dependency.

    ``app/api/dependencies/rbac.py``'s ``require_roles`` checks
    ``current_user.role`` (singular) against a tuple of ``UserRole``
    enum members -- not a ``roles`` list -- matching the real
    ``User`` model (``app/models/user.py``, which defines a single
    ``role: Mapped[UserRole]`` column, not a collection. ``role``
    defaults to ``UserRole.ADMIN`` (the least-restricted persona) and
    accepts a raw string for tests that intentionally exercise a role
    outside the ``UserRole`` enum (e.g. a caller with no recognized
    role at all) to prove ``require_roles`` denies unrecognized roles
    without weakening that check.
    """

    def __init__(self, user_id: int = 1, role: "UserRole | str" = UserRole.ADMIN):
        self.id = user_id
        self.role = role


@pytest.fixture
def mock_service() -> AsyncMock:
    """Provides a mocked ``ActivityService`` for dependency override."""
    return AsyncMock()


@pytest.fixture
def client(mock_service):
    """Provides a ``TestClient`` with auth and service dependencies overridden.

    Defaults the caller to an "admin" persona (passes every RBAC check
    used by this router). Individual tests override roles as needed.
    """
    app.dependency_overrides[get_current_user] = lambda: _FakeUser(role=UserRole.ADMIN)
    app.dependency_overrides[get_activity_service] = lambda: mock_service
    with TestClient(app) as test_client:
        yield test_client
    app.dependency_overrides.clear()


ACTIVITIES_URL = "/api/v1/activities"


# ---------------------------------------------------------------------------
# Authentication
# ---------------------------------------------------------------------------
class TestAuthentication:
    def test_list_requires_authentication(self, mock_service):
        def _raise_unauthorized():
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED)

        app.dependency_overrides[get_current_user] = _raise_unauthorized
        app.dependency_overrides[get_activity_service] = lambda: mock_service
        with TestClient(app) as unauth_client:
            response = unauth_client.get(ACTIVITIES_URL)
        app.dependency_overrides.clear()

        assert response.status_code == status.HTTP_401_UNAUTHORIZED

    def test_get_by_id_requires_authentication(self, mock_service):
        def _raise_unauthorized():
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED)

        app.dependency_overrides[get_current_user] = _raise_unauthorized
        app.dependency_overrides[get_activity_service] = lambda: mock_service
        with TestClient(app) as unauth_client:
            response = unauth_client.get(f"{ACTIVITIES_URL}/{uuid.uuid4()}")
        app.dependency_overrides.clear()

        assert response.status_code == status.HTTP_401_UNAUTHORIZED


# ---------------------------------------------------------------------------
# Authorization (RBAC)
# ---------------------------------------------------------------------------
class TestAuthorization:
    def test_create_forbidden_for_insufficient_role(self, mock_service):
        # `WRITE_ROLES` (app/api/v1/activity.py) permits every role
        # currently defined on `UserRole` (ADMIN, SALES_MANAGER,
        # SALES_AGENT), so there is no *real* role that is insufficient
        # for POST /activities. This uses a role string outside the
        # `UserRole` enum entirely to prove `require_roles` denies any
        # caller whose role isn't in its allow-list -- exercising the
        # real RBAC dependency (`app/api/dependencies/rbac.py`) rather
        # than bypassing it, since overriding the `require_roles`
        # factory itself has no effect (routes depend on the *closure*
        # `require_roles(...)` returns, not the factory function).
        app.dependency_overrides[get_current_user] = lambda: _FakeUser(
            role="viewer"
        )
        app.dependency_overrides[get_activity_service] = lambda: mock_service
        with TestClient(app) as restricted_client:
            response = restricted_client.post(
                ACTIVITIES_URL,
                json={
                    "module": "booking",
                    "entity_type": "Booking",
                    "entity_id": "b-1",
                    "action": "created",
                    "title": "Should be forbidden",
                },
            )
        app.dependency_overrides.clear()

        assert response.status_code == status.HTTP_403_FORBIDDEN

    def test_delete_forbidden_for_agent_role(self, mock_service):
        # `DELETE_ROLES` (app/api/v1/activity.py) is `(ADMIN,
        # SALES_MANAGER)` -- SALES_AGENT is a real, valid `UserRole`
        # that is genuinely excluded from delete permissions, so this
        # exercises the real RBAC dependency with a real role rather
        # than a placeholder string.
        app.dependency_overrides[get_current_user] = lambda: _FakeUser(
            role=UserRole.SALES_AGENT
        )
        app.dependency_overrides[get_activity_service] = lambda: mock_service
        with TestClient(app) as restricted_client:
            response = restricted_client.delete(f"{ACTIVITIES_URL}/{uuid.uuid4()}")
        app.dependency_overrides.clear()

        assert response.status_code == status.HTTP_403_FORBIDDEN


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------
class TestHappyPath:
    def test_create_activity_returns_201(self, client, mock_service):
        mock_service.create_activity.return_value = make_activity(
            title="Payment received for invoice 1042"
        )

        response = client.post(
            ACTIVITIES_URL,
            json={
                "module": "payment",
                "entity_type": "Payment",
                "entity_id": "pay-1042",
                "action": "payment_received",
                "title": "Payment received for invoice 1042",
            },
        )

        assert response.status_code == status.HTTP_201_CREATED
        assert response.json()["title"] == "Payment received for invoice 1042"

    def test_get_activity_returns_200(self, client, mock_service):
        activity = make_activity()
        mock_service.get_activity.return_value = activity

        response = client.get(f"{ACTIVITIES_URL}/{activity.id}")

        assert response.status_code == status.HTTP_200_OK
        assert response.json()["id"] == str(activity.id)

    def test_update_activity_returns_200(self, client, mock_service):
        activity = make_activity(title="Updated title")
        mock_service.update_activity.return_value = activity

        response = client.put(
            f"{ACTIVITIES_URL}/{activity.id}", json={"title": "Updated title"}
        )

        assert response.status_code == status.HTTP_200_OK
        assert response.json()["title"] == "Updated title"

    def test_list_activities_returns_200(self, client, mock_service):
        mock_service.list_activities.return_value = make_list_response(
            [make_activity()], 1
        )

        response = client.get(ACTIVITIES_URL)

        assert response.status_code == status.HTTP_200_OK
        body = response.json()
        assert body["total"] == 1
        assert len(body["items"]) == 1


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------
class TestValidation:
    def test_create_activity_missing_required_field_returns_422(self, client):
        response = client.post(
            ACTIVITIES_URL,
            json={
                "module": "booking",
                "entity_type": "Booking",
                # entity_id intentionally omitted
                "action": "created",
                "title": "Missing entity_id",
            },
        )

        assert response.status_code == status.HTTP_422_UNPROCESSABLE_ENTITY

    def test_create_activity_invalid_enum_value_returns_422(self, client):
        response = client.post(
            ACTIVITIES_URL,
            json={
                "module": "not_a_real_module",
                "entity_type": "Booking",
                "entity_id": "b-1",
                "action": "created",
                "title": "Invalid module",
            },
        )

        assert response.status_code == status.HTTP_422_UNPROCESSABLE_ENTITY

    def test_create_activity_blank_title_returns_422(self, client):
        response = client.post(
            ACTIVITIES_URL,
            json={
                "module": "booking",
                "entity_type": "Booking",
                "entity_id": "b-1",
                "action": "created",
                "title": "   ",
            },
        )

        assert response.status_code == status.HTTP_422_UNPROCESSABLE_ENTITY

    def test_get_activity_invalid_uuid_returns_422(self, client):
        response = client.get(f"{ACTIVITIES_URL}/not-a-uuid")
        assert response.status_code == status.HTTP_422_UNPROCESSABLE_ENTITY

    def test_list_activities_invalid_sort_order_returns_422(self, client):
        response = client.get(ACTIVITIES_URL, params={"sort_order": "sideways"})
        assert response.status_code == status.HTTP_422_UNPROCESSABLE_ENTITY

    def test_list_activities_invalid_sort_by_returns_422(self, client):
        response = client.get(ACTIVITIES_URL, params={"sort_by": "not_a_column"})
        assert response.status_code == status.HTTP_422_UNPROCESSABLE_ENTITY

    def test_list_activities_page_size_over_limit_returns_422(self, client):
        response = client.get(ACTIVITIES_URL, params={"page_size": 999})
        assert response.status_code == status.HTTP_422_UNPROCESSABLE_ENTITY


# ---------------------------------------------------------------------------
# Not found
# ---------------------------------------------------------------------------
class TestNotFound:
    def test_get_activity_not_found_returns_404(self, client, mock_service):
        mock_service.get_activity.side_effect = NotFoundException("Activity not found")

        response = client.get(f"{ACTIVITIES_URL}/{uuid.uuid4()}")

        assert response.status_code == status.HTTP_404_NOT_FOUND

    def test_update_activity_not_found_returns_404(self, client, mock_service):
        mock_service.update_activity.side_effect = NotFoundException(
            "Activity not found"
        )

        response = client.put(
            f"{ACTIVITIES_URL}/{uuid.uuid4()}", json={"title": "New title"}
        )

        assert response.status_code == status.HTTP_404_NOT_FOUND

    def test_delete_activity_not_found_returns_404(self, client, mock_service):
        mock_service.delete_activity.side_effect = NotFoundException(
            "Activity not found"
        )

        response = client.delete(f"{ACTIVITIES_URL}/{uuid.uuid4()}")

        assert response.status_code == status.HTTP_404_NOT_FOUND


# ---------------------------------------------------------------------------
# Search / filtering / sorting / pagination
# ---------------------------------------------------------------------------
class TestSearchFilterSortPaginate:
    def test_search_query_param_is_forwarded(self, client, mock_service):
        mock_service.list_activities.return_value = make_list_response([], 0)

        client.get(ACTIVITIES_URL, params={"search": "invoice 1042"})

        called_filters = mock_service.list_activities.await_args.args[0]
        assert called_filters.search == "invoice 1042"

    def test_module_and_status_filters_are_forwarded(self, client, mock_service):
        mock_service.list_activities.return_value = make_list_response([], 0)

        client.get(
            ACTIVITIES_URL, params={"module": "payment", "status": "completed"}
        )

        called_filters = mock_service.list_activities.await_args.args[0]
        assert called_filters.module == ActivityModule.PAYMENT
        assert called_filters.status == ActivityStatus.COMPLETED

    def test_sort_params_are_forwarded(self, client, mock_service):
        mock_service.list_activities.return_value = make_list_response([], 0)

        client.get(ACTIVITIES_URL, params={"sort_by": "title", "sort_order": "asc"})

        called_filters = mock_service.list_activities.await_args.args[0]
        assert called_filters.sort_by == "title"
        assert called_filters.sort_order == "asc"

    def test_pagination_params_are_forwarded(self, client, mock_service):
        mock_service.list_activities.return_value = make_list_response(
            [], 0, page=3, page_size=15
        )

        client.get(ACTIVITIES_URL, params={"page": 3, "page_size": 15})

        called_filters = mock_service.list_activities.await_args.args[0]
        assert called_filters.page == 3
        assert called_filters.page_size == 15

    def test_response_reports_computed_total_pages(self, client, mock_service):
        mock_service.list_activities.return_value = make_list_response(
            [make_activity()] * 5, 47, page=1, page_size=10
        )

        response = client.get(ACTIVITIES_URL, params={"page": 1, "page_size": 10})

        assert response.json()["total_pages"] == 5


# ---------------------------------------------------------------------------
# Statistics
# ---------------------------------------------------------------------------
class TestStatistics:
    def test_get_statistics_returns_200(self, client, mock_service):
        mock_service.get_statistics.return_value = {
            "total_activities": 10,
            "by_module": {"booking": 6, "payment": 4},
            "by_action": {"created": 10},
            "by_priority": {"normal": 10},
            "by_status": {"active": 10},
            "date_from": None,
            "date_to": None,
        }

        response = client.get(f"{ACTIVITIES_URL}/statistics")

        assert response.status_code == status.HTTP_200_OK
        assert response.json()["total_activities"] == 10


# ---------------------------------------------------------------------------
# Restore / soft delete
# ---------------------------------------------------------------------------
class TestRestoreAndSoftDelete:
    def test_delete_activity_soft_deletes_and_returns_200(self, client, mock_service):
        activity = make_activity(
            is_deleted=True, deleted_at=datetime.now(timezone.utc)
        )
        mock_service.delete_activity.return_value = activity

        response = client.delete(f"{ACTIVITIES_URL}/{activity.id}")

        assert response.status_code == status.HTTP_200_OK
        assert response.json()["is_deleted"] is True

    def test_restore_activity_returns_200_and_clears_deleted_flag(
        self, client, mock_service
    ):
        activity = make_activity(is_deleted=False, deleted_at=None)
        mock_service.restore_activity.return_value = activity

        response = client.patch(f"{ACTIVITIES_URL}/{activity.id}/restore")

        assert response.status_code == status.HTTP_200_OK
        assert response.json()["is_deleted"] is False

    def test_restore_activity_not_found_returns_404(self, client, mock_service):
        mock_service.restore_activity.side_effect = NotFoundException(
            "Activity not found"
        )

        response = client.patch(f"{ACTIVITIES_URL}/{uuid.uuid4()}/restore")

        assert response.status_code == status.HTTP_404_NOT_FOUND


# ---------------------------------------------------------------------------
# Timeline endpoints
# ---------------------------------------------------------------------------
class TestTimelineEndpoints:
    def test_entity_timeline_returns_200(self, client, mock_service):
        entity_id = str(uuid.uuid4())
        mock_service.get_entity_timeline.return_value = TimelineResponse(
            entity_type="Booking",
            entity_id=entity_id,
            items=[],
            total_count=0,
            first_activity_at=None,
            last_activity_at=None,
        )

        response = client.get(f"{ACTIVITIES_URL}/timeline/Booking/{entity_id}")

        assert response.status_code == status.HTTP_200_OK
        assert response.json()["entity_type"] == "Booking"

    def test_entity_timeline_forwards_sort_order(self, client, mock_service):
        entity_id = str(uuid.uuid4())
        mock_service.get_entity_timeline.return_value = TimelineResponse(
            entity_type="Booking",
            entity_id=entity_id,
            items=[],
            total_count=0,
            first_activity_at=None,
            last_activity_at=None,
        )

        client.get(
            f"{ACTIVITIES_URL}/timeline/Booking/{entity_id}",
            params={"sort_order": "desc"},
        )

        _, kwargs = mock_service.get_entity_timeline.await_args
        assert kwargs.get("sort_order") == "desc"

    def test_module_timeline_returns_200(self, client, mock_service):
        mock_service.get_module_timeline.return_value = ([make_activity()], 1)

        response = client.get(f"{ACTIVITIES_URL}/module/booking")

        assert response.status_code == status.HTTP_200_OK
        assert response.json()["total"] == 1

    def test_user_timeline_returns_200(self, client, mock_service):
        mock_service.get_user_timeline.return_value = ([make_activity()], 1)

        response = client.get(f"{ACTIVITIES_URL}/user/1")

        assert response.status_code == status.HTTP_200_OK
        assert response.json()["total"] == 1