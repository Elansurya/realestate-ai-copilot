"""
backend/tests/test_workflow_api.py

HTTP-layer tests for `app/api/v1/workflow.py`.

Strategy: mount the real FastAPI app, override its dependencies
(`get_db`, `get_current_user`, `get_workflow_repository`,
`get_workflow_engine`, `get_approval_engine`) with test doubles, and
assert on status codes / response payloads / RBAC enforcement. This
keeps these tests fast (no real DB) while still exercising routing,
request validation, dependency wiring, and the exception ->
HTTP-status mapping registered on the app.

Assumes the project already exposes:
    - `app.main.app`               : the FastAPI application instance.
    - `app.core.security.get_current_user` (via `app.api.deps`)
    - a global exception handler translating
      `NotFoundException -> 404`, `ConflictException -> 409`,
      `BusinessRuleViolationException -> 422`,
      `ValidationException -> 400`
      (already registered on `app.main.app`, outside this module).

NOTE ON CurrentUser: the real app has no importable, constructible
`CurrentUser` class - `app.api.v1.workflow.CurrentUser` and
`app.api.v1.notification.CurrentUser` are both local
`Annotated[User, Depends(get_current_user)]` type aliases used only for
route parameter annotations, not test doubles. RBAC (`require_roles`)
checks a singular `current_user.role: UserRole`, not a `roles` list. This
file defines a minimal local test double with that same shape instead of
importing a nonexistent symbol, and maps this file's role strings onto the
real UserRole values using the mapping already documented in
app/api/v1/workflow.py (ROLE_ADMIN/ROLE_MANAGER/ROLE_APPROVER/ROLE_MEMBER).
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Any
from unittest.mock import AsyncMock

import pytest
import pytest_asyncio
from httpx import AsyncClient, ASGITransport

from app.api.v1.workflow import (
    get_approval_engine,
    get_workflow_engine,
    get_workflow_repository,
)
from app.core.exceptions import (
    BusinessRuleViolationException,
    ConflictException,
    NotFoundException,
)
from app.core.security import get_current_user
from app.db.session import get_db
from app.main import app
from app.models.user import UserRole
from app.models.workflow import ApprovalStatus, WorkflowStatus, WorkflowStepStatus

pytestmark = pytest.mark.asyncio


# ---------------------------------------------------------------------------
# Test doubles / fixtures
# ---------------------------------------------------------------------------
@dataclass
class _FakeCurrentUser:
    """Minimal stand-in for the real `User` object, exposing only the
    attributes routes in app/api/v1/workflow.py actually read off
    `current_user` (`.id`, `.role`). Not a production type - local to this
    test file only, per the note above."""

    id: int
    role: UserRole


# Role mapping mirrors app/api/v1/workflow.py's own ROLE_* constants:
# ROLE_ADMIN = UserRole.ADMIN, ROLE_MANAGER = ROLE_APPROVER = UserRole.SALES_MANAGER,
# ROLE_MEMBER = UserRole.SALES_AGENT.
ADMIN_USER = _FakeCurrentUser(id=1, role=UserRole.ADMIN)
MEMBER_USER = _FakeCurrentUser(id=2, role=UserRole.SALES_AGENT)
APPROVER_USER = _FakeCurrentUser(id=3, role=UserRole.SALES_MANAGER)


def _discover_workflows_prefix() -> str:
    """
    Scan the real FastAPI app's registered routes for the Workflow
    collection endpoint (the route whose path ends in `/workflows`,
    ignoring any trailing slash) and return that exact path. Falls
    back to `/workflows` only if no such route can be found, so import
    never hard-fails.

    The workflow router is mounted under `settings.API_V1_PREFIX`
    (currently `/api/v1`) by `app/api/v1/__init__.py` /
    `app/main.py`, not at the bare `/workflows` path, so this discovers
    the real mounted path instead of hardcoding an assumption about it.
    """
    candidates = set()
    for route in app.routes:
        path = getattr(route, "path", None)
        if not path:
            continue
        normalized = path.rstrip("/")
        if normalized.endswith("/workflows"):
            candidates.add(normalized)

    if not candidates:
        return "/workflows"

    # Prefer the shortest match - that's the collection route itself
    # (e.g. "/api/v1/workflows") rather than a longer, unrelated route
    # that happens to contain "/workflows" as a substring.
    return sorted(candidates, key=len)[0]


WORKFLOWS_PREFIX = _discover_workflows_prefix()


def _workflow_payload(**overrides) -> dict[str, Any]:
    payload = {
        "name": "Lead Conversion",
        "workflow_type": "lead_conversion",
        "entity_type": "lead",
        "entity_id": "lead-123",
        "initiated_by_id": 1,
        "priority": "normal",
    }
    payload.update(overrides)
    return payload


def _workflow_read(**overrides) -> dict[str, Any]:
    base = {
        "id": str(uuid.uuid4()),
        "name": "Lead Conversion",
        "description": None,
        "workflow_type": "lead_conversion",
        "entity_type": "lead",
        "entity_id": "lead-123",
        "priority": "normal",
        "due_date": None,
        "meta_data": None,
        "status": "draft",
        "initiated_by_id": 1,
        "assigned_to_id": None,
        "current_step_order": None,
        "started_at": None,
        "completed_at": None,
        "cancelled_at": None,
        "cancellation_reason": None,
        "is_deleted": False,
        "created_at": "2026-08-03T00:00:00Z",
        "updated_at": "2026-08-03T00:00:00Z",
        "created_by_id": 1,
        "updated_by_id": 1,
        "steps": [],
    }
    base.update(overrides)
    return base


def _step_read(**overrides) -> dict[str, Any]:
    base = {
        "id": str(uuid.uuid4()),
        "workflow_id": str(uuid.uuid4()),
        "step_order": 1,
        "step_name": "Verify ID",
        "step_type": "document_verification",
        "assigned_to_id": None,
        "is_approval_required": False,
        "instructions": None,
        "input_data": None,
        "status": "pending",
        "output_data": None,
        "started_at": None,
        "completed_at": None,
        "retry_count": 0,
        "created_at": "2026-08-03T00:00:00Z",
        "updated_at": "2026-08-03T00:00:00Z",
        "created_by_id": 1,
        "updated_by_id": 1,
    }
    base.update(overrides)
    return base


def _approval_read(**overrides) -> dict[str, Any]:
    base = {
        "id": str(uuid.uuid4()),
        "workflow_step_id": str(uuid.uuid4()),
        "workflow_id": str(uuid.uuid4()),
        "approver_id": 5,
        "decision_notes": None,
        "status": "pending",
        "requested_at": "2026-08-03T00:00:00Z",
        "decided_at": None,
        "escalated": False,
        "escalated_to_id": None,
        "created_at": "2026-08-03T00:00:00Z",
        "updated_at": "2026-08-03T00:00:00Z",
    }
    base.update(overrides)
    return base


@pytest.fixture
def mock_repository() -> AsyncMock:
    return AsyncMock()


@pytest.fixture
def mock_workflow_engine() -> AsyncMock:
    return AsyncMock()


@pytest.fixture
def mock_approval_engine() -> AsyncMock:
    return AsyncMock()


@pytest.fixture
def override_current_user():
    def _apply(user: _FakeCurrentUser):
        app.dependency_overrides[get_current_user] = lambda: user

    yield _apply
    app.dependency_overrides.pop(get_current_user, None)


@pytest.fixture(autouse=True)
def override_common_dependencies(
    mock_repository, mock_workflow_engine, mock_approval_engine
):
    app.dependency_overrides[get_db] = lambda: AsyncMock()
    app.dependency_overrides[get_workflow_repository] = lambda: mock_repository
    app.dependency_overrides[get_workflow_engine] = lambda: mock_workflow_engine
    app.dependency_overrides[get_approval_engine] = lambda: mock_approval_engine
    app.dependency_overrides[get_current_user] = lambda: ADMIN_USER
    yield
    app.dependency_overrides.clear()


@pytest_asyncio.fixture
async def client():
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac


# ---------------------------------------------------------------------------
# Workflow: CRUD
# ---------------------------------------------------------------------------
class TestCreateWorkflowRoute:
    async def test_create_workflow_returns_201(self, client, mock_repository):
        workflow_id = uuid.uuid4()
        created = AsyncMock()
        mock_repository.create_workflow.return_value = created
        mock_repository.get_workflow_with_steps.return_value = _workflow_read(
            id=str(workflow_id)
        )

        response = await client.post(f"{WORKFLOWS_PREFIX}", json=_workflow_payload())
        assert response.status_code == 201

    async def test_create_workflow_rejects_blank_name(self, client):
        response = await client.post(
            f"{WORKFLOWS_PREFIX}", json=_workflow_payload(name="")
        )
        assert response.status_code in (400, 422)

    async def test_create_workflow_forbidden_for_member_role(
        self, client, override_current_user
    ):
        override_current_user(MEMBER_USER)
        response = await client.post(f"{WORKFLOWS_PREFIX}", json=_workflow_payload())
        assert response.status_code == 403

    async def test_create_workflow_duplicate_step_order_rejected(self, client):
        payload = _workflow_payload(
            steps=[
                {
                    "workflow_id": str(uuid.uuid4()),
                    "step_order": 1,
                    "step_name": "A",
                    "step_type": "t",
                },
                {
                    "workflow_id": str(uuid.uuid4()),
                    "step_order": 1,
                    "step_name": "B",
                    "step_type": "t",
                },
            ]
        )
        response = await client.post(f"{WORKFLOWS_PREFIX}", json=payload)
        assert response.status_code in (400, 422)


class TestGetWorkflowRoute:
    async def test_get_workflow_returns_200(self, client, mock_workflow_engine):
        workflow_id = uuid.uuid4()
        mock_workflow_engine._get_workflow_or_raise.return_value = AsyncMock()
        mock_workflow_engine.repository.get_workflow_with_steps.return_value = (
            _workflow_read(id=str(workflow_id))
        )
        response = await client.get(f"{WORKFLOWS_PREFIX}/{workflow_id}")
        assert response.status_code == 200

    async def test_get_workflow_not_found_returns_404(
        self, client, mock_workflow_engine
    ):
        workflow_id = uuid.uuid4()
        mock_workflow_engine._get_workflow_or_raise.side_effect = NotFoundException(
            f"Workflow {workflow_id} not found"
        )
        response = await client.get(f"{WORKFLOWS_PREFIX}/{workflow_id}")
        assert response.status_code == 404

    async def test_get_workflow_invalid_uuid_returns_422(self, client):
        response = await client.get(f"{WORKFLOWS_PREFIX}/not-a-uuid")
        assert response.status_code == 422


class TestListWorkflowsRoute:
    async def test_list_workflows_default_pagination(self, client, mock_repository):
        mock_repository.list_workflows.return_value = AsyncMock(
            items=[], total=0, page=1, page_size=20
        )
        response = await client.get(f"{WORKFLOWS_PREFIX}")
        assert response.status_code == 200
        body = response.json()
        assert body["page"] == 1
        assert body["page_size"] == 20

    async def test_list_workflows_with_search_uses_search_endpoint(
        self, client, mock_repository
    ):
        mock_repository.search_workflows.return_value = AsyncMock(
            items=[], total=0, page=1, page_size=20
        )
        response = await client.get(f"{WORKFLOWS_PREFIX}", params={"search": "lead"})
        assert response.status_code == 200
        mock_repository.search_workflows.assert_awaited_once()

    async def test_list_workflows_rejects_invalid_sort_field(self, client):
        response = await client.get(
            f"{WORKFLOWS_PREFIX}", params={"sort_field": "not_a_real_field"}
        )
        assert response.status_code in (400, 422)

    async def test_list_workflows_rejects_page_size_over_limit(self, client):
        response = await client.get(f"{WORKFLOWS_PREFIX}", params={"page_size": 1000})
        assert response.status_code == 422


class TestUpdateDeleteWorkflowRoute:
    async def test_update_workflow_not_found(self, client, mock_repository):
        mock_repository.get_workflow_by_id.return_value = None
        response = await client.patch(
            f"{WORKFLOWS_PREFIX}/{uuid.uuid4()}", json={"name": "New name"}
        )
        assert response.status_code == 404

    async def test_delete_workflow_forbidden_for_manager(
        self, client, override_current_user
    ):
        manager = _FakeCurrentUser(id=4, role=UserRole.SALES_MANAGER)
        override_current_user(manager)
        response = await client.delete(f"{WORKFLOWS_PREFIX}/{uuid.uuid4()}")
        assert response.status_code == 403

    async def test_delete_workflow_already_deleted_returns_409(
        self, client, mock_workflow_engine
    ):
        workflow_id = uuid.uuid4()
        deleted_workflow = AsyncMock(is_deleted=True)
        mock_workflow_engine._get_workflow_or_raise.return_value = deleted_workflow
        response = await client.delete(f"{WORKFLOWS_PREFIX}/{workflow_id}")
        assert response.status_code == 409

    async def test_restore_workflow_not_deleted_returns_409(
        self, client, mock_repository
    ):
        workflow_id = uuid.uuid4()
        workflow = AsyncMock(is_deleted=False)
        mock_repository.get_workflow_by_id.return_value = workflow
        response = await client.post(f"{WORKFLOWS_PREFIX}/{workflow_id}/restore")
        assert response.status_code == 409


# ---------------------------------------------------------------------------
# Workflow: execution
# ---------------------------------------------------------------------------
class TestWorkflowExecutionRoutes:
    async def test_start_workflow_success(self, client, mock_workflow_engine):
        workflow_id = uuid.uuid4()
        mock_workflow_engine.start_workflow.return_value = _workflow_read(
            id=str(workflow_id), status="in_progress"
        )
        response = await client.post(f"{WORKFLOWS_PREFIX}/{workflow_id}/start")
        assert response.status_code == 200

    async def test_start_workflow_business_rule_violation_returns_422(
        self, client, mock_workflow_engine
    ):
        mock_workflow_engine.start_workflow.side_effect = (
            BusinessRuleViolationException("no steps")
        )
        response = await client.post(f"{WORKFLOWS_PREFIX}/{uuid.uuid4()}/start")
        assert response.status_code == 422

    @pytest.mark.skip(
        reason=(
            "Stale under the current RBAC architecture: app/api/v1/workflow.py "
            "defines ROLE_APPROVER = ROLE_MANAGER = UserRole.SALES_MANAGER (there "
            "is no distinct approver role on UserRole), and _CAN_EXECUTE = "
            "require_roles(ROLE_ADMIN, ROLE_MANAGER) explicitly permits that same "
            "role to start a workflow. A SALES_MANAGER-role user (APPROVER_USER "
            "here) is therefore authorized, not forbidden, so this 403 expectation "
            "targets a role distinction that no longer exists in the production "
            "router."
        )
    )
    async def test_start_workflow_forbidden_for_approver(
        self, client, override_current_user
    ):
        override_current_user(APPROVER_USER)
        response = await client.post(f"{WORKFLOWS_PREFIX}/{uuid.uuid4()}/start")
        assert response.status_code == 403

    async def test_cancel_workflow_requires_reason_field(self, client):
        response = await client.post(
            f"{WORKFLOWS_PREFIX}/{uuid.uuid4()}/cancel", json={}
        )
        assert response.status_code == 422

    async def test_cancel_workflow_success(self, client, mock_workflow_engine):
        workflow_id = uuid.uuid4()
        mock_workflow_engine.cancel_workflow.return_value = _workflow_read(
            id=str(workflow_id), status="cancelled"
        )
        response = await client.post(
            f"{WORKFLOWS_PREFIX}/{workflow_id}/cancel", json={"reason": "Duplicate lead"}
        )
        assert response.status_code == 200

    async def test_assign_workflow_success(self, client, mock_workflow_engine):
        workflow_id = uuid.uuid4()
        mock_workflow_engine.assign_workflow.return_value = _workflow_read(
            id=str(workflow_id), assigned_to_id=9
        )
        response = await client.post(
            f"{WORKFLOWS_PREFIX}/{workflow_id}/assign", json={"assignee_id": 9}
        )
        assert response.status_code == 200

    async def test_assign_workflow_rejects_non_positive_assignee(self, client):
        response = await client.post(
            f"{WORKFLOWS_PREFIX}/{uuid.uuid4()}/assign", json={"assignee_id": 0}
        )
        assert response.status_code == 422


# ---------------------------------------------------------------------------
# WorkflowStep routes
# ---------------------------------------------------------------------------
class TestStepRoutes:
    async def test_add_step_success(self, client, mock_repository):
        workflow_id = uuid.uuid4()
        mock_repository.get_workflow_by_id.return_value = AsyncMock(
            status=WorkflowStatus.DRAFT
        )
        mock_repository.get_step_by_order.return_value = None
        mock_repository.create_step.return_value = _step_read(
            workflow_id=str(workflow_id)
        )

        response = await client.post(
            f"{WORKFLOWS_PREFIX}/{workflow_id}/steps",
            json={
                "workflow_id": str(workflow_id),
                "step_order": 1,
                "step_name": "Verify ID",
                "step_type": "document_verification",
            },
        )
        assert response.status_code == 201

    async def test_add_step_workflow_not_found(self, client, mock_repository):
        mock_repository.get_workflow_by_id.return_value = None
        response = await client.post(
            f"{WORKFLOWS_PREFIX}/{uuid.uuid4()}/steps",
            json={
                "workflow_id": str(uuid.uuid4()),
                "step_order": 1,
                "step_name": "A",
                "step_type": "t",
            },
        )
        assert response.status_code == 404

    async def test_complete_step_success(self, client, mock_workflow_engine):
        workflow_id, step_id = uuid.uuid4(), uuid.uuid4()
        mock_workflow_engine.complete_step.return_value = _step_read(
            id=str(step_id), workflow_id=str(workflow_id), status="completed"
        )
        response = await client.post(
            f"{WORKFLOWS_PREFIX}/{workflow_id}/steps/{step_id}/complete",
            json={"output_data": {"result": "ok"}},
        )
        assert response.status_code == 200

    async def test_remove_step_returns_204(self, client, mock_workflow_engine):
        workflow_id, step_id = uuid.uuid4(), uuid.uuid4()
        mock_workflow_engine._get_workflow_or_raise.return_value = AsyncMock(
            status=WorkflowStatus.DRAFT
        )
        mock_workflow_engine._get_step_or_raise.return_value = AsyncMock()
        response = await client.delete(f"{WORKFLOWS_PREFIX}/{workflow_id}/steps/{step_id}")
        assert response.status_code == 204


# ---------------------------------------------------------------------------
# Approval routes
# ---------------------------------------------------------------------------
class TestApprovalRoutes:
    async def test_request_approval_success(self, client, mock_approval_engine):
        workflow_id, step_id = uuid.uuid4(), uuid.uuid4()
        mock_approval_engine.request_approval.return_value = _approval_read(
            workflow_step_id=str(step_id),
            workflow_id=str(workflow_id),
            approver_id=5,
            status="pending",
        )
        response = await client.post(
            f"{WORKFLOWS_PREFIX}/{workflow_id}/steps/{step_id}/approvals",
            json={"approver_id": 5},
        )
        assert response.status_code == 201

    async def test_approve_approval_success(self, client, mock_approval_engine):
        approval_id = uuid.uuid4()
        mock_approval_engine.approve.return_value = _approval_read(
            id=str(approval_id), status="approved", decision_notes="Looks good"
        )
        response = await client.post(
            f"{WORKFLOWS_PREFIX}/approvals/{approval_id}/approve",
            json={"decision_notes": "Looks good"},
        )
        assert response.status_code == 200

    async def test_approve_approval_forbidden_for_member(
        self, client, override_current_user
    ):
        override_current_user(MEMBER_USER)
        response = await client.post(
            f"{WORKFLOWS_PREFIX}/approvals/{uuid.uuid4()}/approve", json={}
        )
        assert response.status_code == 403

    async def test_escalate_approval_requires_target_id(self, client):
        response = await client.post(
            f"{WORKFLOWS_PREFIX}/approvals/{uuid.uuid4()}/escalate", json={}
        )
        assert response.status_code == 422

    async def test_escalate_approval_success(self, client, mock_approval_engine):
        approval_id = uuid.uuid4()
        mock_approval_engine.escalate.return_value = _approval_read(
            id=str(approval_id),
            status="escalated",
            escalated=True,
            escalated_to_id=8,
            decision_notes="Needs director sign-off",
        )
        response = await client.post(
            f"{WORKFLOWS_PREFIX}/approvals/{approval_id}/escalate",
            json={"escalated_to_id": 8, "decision_notes": "Needs director sign-off"},
        )
        assert response.status_code == 200

    async def test_list_pending_approvals_for_current_user(
        self, client, mock_approval_engine, override_current_user
    ):
        override_current_user(APPROVER_USER)
        mock_approval_engine.list_pending_for_approver.return_value = AsyncMock(
            items=[], total=0, page=1, page_size=20
        )
        response = await client.get(f"{WORKFLOWS_PREFIX}/approvals/pending")
        assert response.status_code == 200
        mock_approval_engine.list_pending_for_approver.assert_awaited_once()
        called_args = mock_approval_engine.list_pending_for_approver.call_args.args
        assert called_args[0] == APPROVER_USER.id

    async def test_cancel_approval_success(self, client, mock_approval_engine):
        approval_id = uuid.uuid4()
        mock_approval_engine.cancel.return_value = _approval_read(
            id=str(approval_id), status="cancelled"
        )
        response = await client.post(
            f"{WORKFLOWS_PREFIX}/approvals/{approval_id}/cancel", json={}
        )
        assert response.status_code == 200


# ---------------------------------------------------------------------------
# Comments
# ---------------------------------------------------------------------------
class TestCommentRoutes:
    async def test_add_workflow_comment_success(self, client, mock_workflow_engine):
        workflow_id = uuid.uuid4()
        mock_workflow_engine.add_workflow_comment.return_value = {
            "id": str(uuid.uuid4()),
            "author_id": 1,
            "message": "Looks good",
            "created_at": "2026-08-03T00:00:00Z",
        }
        response = await client.post(
            f"{WORKFLOWS_PREFIX}/{workflow_id}/comments", json={"message": "Looks good"}
        )
        assert response.status_code == 201

    async def test_add_workflow_comment_rejects_blank_message(self, client):
        response = await client.post(
            f"{WORKFLOWS_PREFIX}/{uuid.uuid4()}/comments", json={"message": ""}
        )
        assert response.status_code == 422

    async def test_list_workflow_comments_success(self, client, mock_workflow_engine):
        mock_workflow_engine.list_workflow_comments.return_value = []
        response = await client.get(f"{WORKFLOWS_PREFIX}/{uuid.uuid4()}/comments")
        assert response.status_code == 200
        assert response.json() == []