"""
backend/tests/test_task_api.py

API-layer tests for the Task Management module.

Scope:
    Exercises `app.api.v1.task.router` as a mounted FastAPI router,
    verifying HTTP-level concerns: route wiring (paths/methods/status
    codes), RBAC enforcement via `require_roles`, request/response
    schema shape, and translation of domain exceptions
    (`TaskNotFoundError` -> 404, `TaskValidationError` -> 422) into
    HTTP responses.

    `app.services.task_service.TaskService` is patched to a fake with
    `AsyncMock` methods for every test, so no database is touched.
    This isolates "does the router wire things up correctly" from
    "does the service/repository behave correctly" (covered by
    `test_task_service.py` and `test_task_repository.py`
    respectively).

Auth/RBAC strategy:
    `app.api.deps.get_current_user` is overridden via FastAPI's
    `app.dependency_overrides` to return a configurable fake principal
    (a `SimpleNamespace` with `.id` and `.role`), so tests don't need a
    real JWT. `app.api.deps.require_roles` is left as the *real*
    implementation: it is assumed (per `app.api.v1.task`'s own
    docstring) to read `current_user.role` and raise `403` if it is
    not in the allowed set, so exercising it for real -- rather than
    also overriding it -- is what lets these tests actually verify
    RBAC behavior instead of merely trusting it.

    If your project's real `get_current_user` / `require_roles`
    signatures differ from what `app.api.v1.task` assumes (see that
    module's docstring), update the `current_user` fixture and the
    `override_role` helper below to match.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from app.api.deps import get_current_user, get_db_session
from app.api.v1 import task as task_api_module
from app.api.v1.task import router
from app.models.task import Task, TaskPriority, TaskStatus, TaskType
from app.schemas.task import TaskStatisticsResponse
from app.services.task_service import TaskConflictError, TaskNotFoundError
from app.utils.task_validator import TaskValidationError

pytestmark = pytest.mark.asyncio


# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------
def _fake_task(**overrides) -> Task:
    """Builds a fully-populated, non-persisted `Task` for response serialization.

    Args:
        **overrides: Column values to override on top of the defaults.

    Returns:
        Task: A `Task` instance with every field `TaskResponse`
        requires populated, so `TaskResponse.model_validate(task)`
        succeeds without a database round-trip.
    """
    now = datetime.now(timezone.utc)
    defaults = dict(
        id=uuid.uuid4(),
        title="Sample task",
        description=None,
        task_type=TaskType.GENERAL,
        status=TaskStatus.PENDING,
        priority=TaskPriority.NORMAL,
        due_date=None,
        reminder_time=None,
        assigned_to_id=None,
        created_by_id=None,
        related_module=None,
        related_entity_id=None,
        comments_count=0,
        attachments_count=0,
        meta_data=None,
        completed_at=None,
        completed_by_id=None,
        is_deleted=False,
        deleted_at=None,
        created_at=now,
        updated_at=now,
    )
    defaults.update(overrides)
    return Task(**defaults)


@pytest.fixture
def fake_service():
    """Builds an `AsyncMock` standing in for `TaskService`'s public interface."""
    return AsyncMock(name="fake_task_service")


@pytest.fixture
def app(monkeypatch, fake_service):
    """Builds a FastAPI app mounting the Task router with a patched service.

    Args:
        monkeypatch: Pytest's monkeypatch fixture.
        fake_service: The `AsyncMock` service instance to inject.

    Returns:
        FastAPI: An app with `router` mounted under `/api/v1` and
        `TaskService` patched (module-level, inside
        `app.api.v1.task`) to always return `fake_service`.
    """
    monkeypatch.setattr(
        task_api_module, "TaskService", lambda session: fake_service
    )
    fastapi_app = FastAPI()
    fastapi_app.include_router(router, prefix="/api/v1")
    fastapi_app.dependency_overrides[get_db_session] = lambda: None
    return fastapi_app


@pytest.fixture
def current_user_role():
    """Mutable holder for the role the fake `current_user` should carry."""
    return {"role": "admin", "id": 1}


@pytest.fixture
def client(app, current_user_role):
    """Builds an `AsyncClient` bound to `app`, with `get_current_user` overridden.

    Args:
        app: The FastAPI app fixture.
        current_user_role: The mutable role/id holder; mutate its
            `"role"`/`"id"` entries *before* making a request to
            change who the request is authenticated as.

    Returns:
        AsyncClient: An httpx client wired directly to the ASGI app
        (no real network I/O).
    """

    def _override_current_user():
        return SimpleNamespace(
            id=current_user_role["id"], role=current_user_role["role"]
        )

    app.dependency_overrides[get_current_user] = _override_current_user
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


def _set_role(current_user_role: dict, role: str, user_id: int = 1) -> None:
    """Updates the fake authenticated principal's role/id for a test.

    Args:
        current_user_role: The fixture-provided mutable holder.
        role: The role to authenticate as.
        user_id: The user id to authenticate as.
    """
    current_user_role["role"] = role
    current_user_role["id"] = user_id


# ---------------------------------------------------------------------------
# Create
# ---------------------------------------------------------------------------
class TestCreateTaskEndpoint:
    async def test_create_returns_201_with_created_task(
        self, client, fake_service, current_user_role
    ) -> None:
        _set_role(current_user_role, "agent")
        fake_service.create_task.return_value = _fake_task(title="Call back lead")

        async with client as c:
            response = await c.post(
                "/api/v1/tasks", json={"title": "Call back lead"}
            )

        assert response.status_code == 201
        assert response.json()["title"] == "Call back lead"

    async def test_create_forbidden_for_viewer_role(
        self, client, current_user_role
    ) -> None:
        _set_role(current_user_role, "viewer")

        async with client as c:
            response = await c.post("/api/v1/tasks", json={"title": "X"})

        assert response.status_code == 403

    async def test_create_returns_422_on_validation_error(
        self, client, fake_service, current_user_role
    ) -> None:
        _set_role(current_user_role, "agent")
        fake_service.create_task.side_effect = TaskValidationError(
            "related_module 'bogus' is not recognized.",
            field="related_module",
            code="unknown_related_module",
        )

        async with client as c:
            response = await c.post(
                "/api/v1/tasks",
                json={
                    "title": "X",
                    "related_module": "bogus",
                    "related_entity_id": "1",
                },
            )

        assert response.status_code == 422
        assert response.json()["detail"]["code"] == "unknown_related_module"


# ---------------------------------------------------------------------------
# List
# ---------------------------------------------------------------------------
class TestListTasksEndpoint:
    async def test_list_returns_paginated_envelope(
        self, client, fake_service, current_user_role
    ) -> None:
        _set_role(current_user_role, "viewer")
        fake_service.list_tasks.return_value = ([_fake_task()], 1)

        async with client as c:
            response = await c.get("/api/v1/tasks", params={"page": 1, "page_size": 20})

        body = response.json()
        assert response.status_code == 200
        assert body["total"] == 1
        assert body["page"] == 1
        assert len(body["items"]) == 1

    async def test_list_rejects_invalid_sort_by(
        self, client, current_user_role
    ) -> None:
        _set_role(current_user_role, "viewer")

        async with client as c:
            response = await c.get(
                "/api/v1/tasks", params={"sort_by": "not_a_real_column"}
            )

        assert response.status_code == 422


# ---------------------------------------------------------------------------
# Get single
# ---------------------------------------------------------------------------
class TestGetTaskEndpoint:
    async def test_get_returns_200_when_found(
        self, client, fake_service, current_user_role
    ) -> None:
        _set_role(current_user_role, "viewer")
        task = _fake_task()
        fake_service.get_task.return_value = task

        async with client as c:
            response = await c.get(f"/api/v1/tasks/{task.id}")

        assert response.status_code == 200
        assert response.json()["id"] == str(task.id)

    async def test_get_returns_404_when_missing(
        self, client, fake_service, current_user_role
    ) -> None:
        _set_role(current_user_role, "viewer")
        fake_service.get_task.return_value = None

        async with client as c:
            response = await c.get(f"/api/v1/tasks/{uuid.uuid4()}")

        assert response.status_code == 404


# ---------------------------------------------------------------------------
# Update
# ---------------------------------------------------------------------------
class TestUpdateTaskEndpoint:
    async def test_update_returns_404_when_missing(
        self, client, fake_service, current_user_role
    ) -> None:
        _set_role(current_user_role, "agent")
        fake_service.update_task.side_effect = TaskNotFoundError("not found")

        async with client as c:
            response = await c.patch(
                f"/api/v1/tasks/{uuid.uuid4()}", json={"title": "New"}
            )

        assert response.status_code == 404

    async def test_update_forbidden_for_viewer(
        self, client, current_user_role
    ) -> None:
        _set_role(current_user_role, "viewer")

        async with client as c:
            response = await c.patch(
                f"/api/v1/tasks/{uuid.uuid4()}", json={"title": "New"}
            )

        assert response.status_code == 403


# ---------------------------------------------------------------------------
# Assignment
# ---------------------------------------------------------------------------
class TestAssignTaskEndpoint:
    async def test_assign_returns_200_with_updated_task(
        self, client, fake_service, current_user_role
    ) -> None:
        _set_role(current_user_role, "manager")
        task = _fake_task(assigned_to_id=7)
        fake_service.assign_task.return_value = task

        async with client as c:
            response = await c.post(
                f"/api/v1/tasks/{task.id}/assign", json={"assigned_to_id": 7}
            )

        assert response.status_code == 200
        assert response.json()["assigned_to_id"] == 7

    async def test_assign_forbidden_for_agent(
        self, client, current_user_role
    ) -> None:
        _set_role(current_user_role, "agent")

        async with client as c:
            response = await c.post(
                f"/api/v1/tasks/{uuid.uuid4()}/assign", json={"assigned_to_id": 7}
            )

        assert response.status_code == 403


# ---------------------------------------------------------------------------
# Status / complete / cancel
# ---------------------------------------------------------------------------
class TestLifecycleEndpoints:
    async def test_status_update_returns_422_on_illegal_transition(
        self, client, fake_service, current_user_role
    ) -> None:
        _set_role(current_user_role, "agent")
        fake_service.update_status.side_effect = TaskValidationError(
            "Cannot transition task from 'cancelled' to 'in_progress'.",
            field="status",
            code="illegal_status_transition",
        )

        async with client as c:
            response = await c.post(
                f"/api/v1/tasks/{uuid.uuid4()}/status",
                json={"status": "in_progress"},
            )

        assert response.status_code == 422
        assert response.json()["detail"]["code"] == "illegal_status_transition"

    async def test_complete_returns_200(
        self, client, fake_service, current_user_role
    ) -> None:
        _set_role(current_user_role, "agent", user_id=3)
        task = _fake_task(status=TaskStatus.COMPLETED, completed_by_id=3)
        fake_service.complete_task.return_value = task

        async with client as c:
            response = await c.post(f"/api/v1/tasks/{task.id}/complete")

        assert response.status_code == 200
        assert response.json()["status"] == "completed"

    async def test_cancel_returns_200(
        self, client, fake_service, current_user_role
    ) -> None:
        _set_role(current_user_role, "agent")
        task = _fake_task(status=TaskStatus.CANCELLED)
        fake_service.cancel_task.return_value = task

        async with client as c:
            response = await c.post(f"/api/v1/tasks/{task.id}/cancel")

        assert response.status_code == 200
        assert response.json()["status"] == "cancelled"


# ---------------------------------------------------------------------------
# Soft delete / restore
# ---------------------------------------------------------------------------
class TestDeleteRestoreEndpoints:
    async def test_delete_forbidden_for_agent(
        self, client, current_user_role
    ) -> None:
        _set_role(current_user_role, "agent")

        async with client as c:
            response = await c.delete(f"/api/v1/tasks/{uuid.uuid4()}")

        assert response.status_code == 403

    async def test_delete_returns_200_for_manager(
        self, client, fake_service, current_user_role
    ) -> None:
        _set_role(current_user_role, "manager")
        task = _fake_task(is_deleted=True)
        fake_service.soft_delete_task.return_value = task

        async with client as c:
            response = await c.delete(f"/api/v1/tasks/{task.id}")

        assert response.status_code == 200
        assert response.json()["is_deleted"] is True

    async def test_restore_forbidden_for_manager(
        self, client, current_user_role
    ) -> None:
        _set_role(current_user_role, "manager")

        async with client as c:
            response = await c.post(f"/api/v1/tasks/{uuid.uuid4()}/restore")

        assert response.status_code == 403

    async def test_restore_returns_200_for_admin(
        self, client, fake_service, current_user_role
    ) -> None:
        _set_role(current_user_role, "admin")
        task = _fake_task(is_deleted=False)
        fake_service.restore_task.return_value = task

        async with client as c:
            response = await c.post(f"/api/v1/tasks/{task.id}/restore")

        assert response.status_code == 200
        assert response.json()["is_deleted"] is False


# ---------------------------------------------------------------------------
# Bulk operations
# ---------------------------------------------------------------------------
class TestBulkEndpoints:
    async def test_bulk_update_forbidden_for_agent(
        self, client, current_user_role
    ) -> None:
        _set_role(current_user_role, "agent")

        async with client as c:
            response = await c.request(
                "PATCH",
                "/api/v1/tasks/bulk",
                json={"ids": [str(uuid.uuid4())], "data": {"title": "X"}},
            )

        assert response.status_code == 403

    async def test_bulk_delete_returns_deleted_count(
        self, client, fake_service, current_user_role
    ) -> None:
        _set_role(current_user_role, "manager")
        fake_service.bulk_soft_delete_tasks.return_value = 3
        ids = [str(uuid.uuid4()) for _ in range(3)]

        async with client as c:
            response = await c.post("/api/v1/tasks/bulk/delete", json=ids)

        assert response.status_code == 200
        assert response.json()["deleted_count"] == 3


# ---------------------------------------------------------------------------
# Statistics / recent / overdue / reminders
# ---------------------------------------------------------------------------
class TestAncillaryEndpoints:
    async def test_statistics_returns_200(
        self, client, fake_service, current_user_role
    ) -> None:
        _set_role(current_user_role, "viewer")
        fake_service.get_statistics.return_value = TaskStatisticsResponse(
            total_tasks=5,
            by_status={"pending": 5},
            by_priority={},
            by_type={},
            overdue_count=0,
            completed_count=0,
            cancelled_count=0,
        )

        async with client as c:
            response = await c.get("/api/v1/tasks/statistics")

        assert response.status_code == 200
        assert response.json()["total_tasks"] == 5

    async def test_recent_returns_list(
        self, client, fake_service, current_user_role
    ) -> None:
        _set_role(current_user_role, "viewer")
        fake_service.get_recent_tasks.return_value = [_fake_task(), _fake_task()]

        async with client as c:
            response = await c.get("/api/v1/tasks/recent")

        assert response.status_code == 200
        assert len(response.json()) == 2

    async def test_reminders_due_forbidden_for_agent(
        self, client, current_user_role
    ) -> None:
        _set_role(current_user_role, "agent")

        async with client as c:
            response = await c.get("/api/v1/tasks/reminders/due")

        assert response.status_code == 403

    async def test_reminders_due_returns_200_for_manager(
        self, client, fake_service, current_user_role
    ) -> None:
        _set_role(current_user_role, "manager")
        fake_service.get_due_reminders.return_value = [_fake_task()]

        async with client as c:
            response = await c.get("/api/v1/tasks/reminders/due")

        assert response.status_code == 200
        assert len(response.json()) == 1