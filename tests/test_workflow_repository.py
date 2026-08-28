"""
backend/tests/test_workflow_repository.py

Integration tests for `WorkflowRepository`.

These tests exercise the repository directly against a real async
SQLAlchemy session (Postgres-backed, since the ORM models use native
PostgreSQL types: `gen_random_uuid()`, `JSONB`, native enums). They
assume the project's shared test infrastructure already provides:

    - `db_session`      : an `AsyncSession` fixture, rolled back after
                          every test (see `backend/tests/conftest.py`).
    - `seed_users`       : a fixture returning a dict of ready-made,
                          already-committed `users.id` integers (e.g.
                          `{"initiator": 1, "assignee": 2, "approver": 3,
                          "escalation_target": 4}`) so that the
                          Workflow module's FK constraints
                          (`initiated_by_id`, `assigned_to_id`,
                          `approver_id`, `escalated_to_id`, ...) are
                          satisfiable without this module owning user
                          creation.

If those fixture names differ in the project's actual `conftest.py`,
only the fixture parameters below need to change -- the assertions
are written against `WorkflowRepository`'s public contract only.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

import pytest

from app.models.workflow import ApprovalStatus, WorkflowStepStatus
from app.repositories.workflow_repository import (
    PaginationParams,
    SortParams,
    WorkflowFilterParams,
    WorkflowRepository,
)

pytestmark = pytest.mark.asyncio


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------
@pytest.fixture
def repo(db_session) -> WorkflowRepository:
    return WorkflowRepository(db_session)


@pytest.fixture
def workflow_values(seed_users) -> dict:
    return {
        "name": "New Lead Conversion",
        "description": "Convert a qualified lead into a customer.",
        "workflow_type": "lead_conversion",
        "entity_type": "lead",
        "entity_id": str(uuid.uuid4()),
        "initiated_by_id": seed_users["initiator"],
        "assigned_to_id": seed_users["assignee"],
        "priority": "high",
    }


async def _create_workflow(repo: WorkflowRepository, values: dict):
    return await repo.create_workflow(dict(values))


async def _create_step(repo, workflow_id, step_order=1, **overrides):
    values = {
        "workflow_id": workflow_id,
        "step_order": step_order,
        "step_name": f"Step {step_order}",
        "step_type": "document_verification",
        "is_approval_required": False,
    }
    values.update(overrides)
    return await repo.create_step(values)


# ---------------------------------------------------------------------------
# Workflow: CRUD
# ---------------------------------------------------------------------------
class TestWorkflowCrud:
    async def test_create_workflow_persists_and_defaults(self, repo, workflow_values):
        workflow = await _create_workflow(repo, workflow_values)

        assert workflow.id is not None
        assert workflow.name == workflow_values["name"]
        assert workflow.status.value == "draft"
        assert workflow.priority == "high"
        assert workflow.is_deleted is False

    async def test_get_workflow_by_id_found(self, repo, workflow_values):
        created = await _create_workflow(repo, workflow_values)
        fetched = await repo.get_workflow_by_id(created.id)
        assert fetched is not None
        assert fetched.id == created.id

    async def test_get_workflow_by_id_missing_returns_none(self, repo):
        assert await repo.get_workflow_by_id(uuid.uuid4()) is None

    async def test_get_workflow_by_id_excludes_soft_deleted_by_default(
        self, repo, workflow_values
    ):
        created = await _create_workflow(repo, workflow_values)
        await repo.soft_delete_workflow(created)

        assert await repo.get_workflow_by_id(created.id) is None
        assert (
            await repo.get_workflow_by_id(created.id, include_deleted=True)
            is not None
        )

    async def test_get_workflow_with_steps_nests_steps_and_approvals(
        self, repo, workflow_values, seed_users
    ):
        workflow = await _create_workflow(repo, workflow_values)
        step = await _create_step(repo, workflow.id, is_approval_required=True)
        await repo.create_approval(
            {
                "workflow_step_id": step.id,
                "workflow_id": workflow.id,
                "approver_id": seed_users["approver"],
            }
        )

        result = await repo.get_workflow_with_steps(workflow.id)
        assert result is not None
        assert len(result.steps) == 1
        assert len(result.steps[0].approvals) == 1

    async def test_update_workflow_applies_arbitrary_fields(self, repo, workflow_values):
        workflow = await _create_workflow(repo, workflow_values)
        updated = await repo.update_workflow(workflow, {"name": "Renamed Workflow"})
        assert updated.name == "Renamed Workflow"

    async def test_soft_delete_and_restore_roundtrip(self, repo, workflow_values):
        workflow = await _create_workflow(repo, workflow_values)

        deleted = await repo.soft_delete_workflow(workflow)
        assert deleted.is_deleted is True
        assert deleted.deleted_at is not None

        restored = await repo.restore_workflow(deleted)
        assert restored.is_deleted is False
        assert restored.deleted_at is None


# ---------------------------------------------------------------------------
# Workflow: list / search / filter / sort / pagination
# ---------------------------------------------------------------------------
class TestWorkflowListing:
    async def test_list_workflows_paginates(self, repo, workflow_values):
        for i in range(3):
            values = dict(workflow_values)
            values["name"] = f"Workflow {i}"
            values["entity_id"] = str(uuid.uuid4())
            await _create_workflow(repo, values)

        page = await repo.list_workflows(
            WorkflowFilterParams(),
            PaginationParams(page=1, page_size=2),
            SortParams(field="created_at", direction="desc"),
        )
        assert page.total >= 3
        assert len(page.items) == 2
        assert page.page == 1
        assert page.page_size == 2

    async def test_list_workflows_filters_by_status_and_priority(
        self, repo, workflow_values
    ):
        workflow = await _create_workflow(repo, workflow_values)

        matching = await repo.list_workflows(
            WorkflowFilterParams(status=["draft"], priority=["high"]),
            PaginationParams(),
            SortParams(),
        )
        assert any(w.id == workflow.id for w in matching.items)

        non_matching = await repo.list_workflows(
            WorkflowFilterParams(status=["completed"]),
            PaginationParams(),
            SortParams(),
        )
        assert all(w.id != workflow.id for w in non_matching.items)

    async def test_list_workflows_excludes_deleted_unless_requested(
        self, repo, workflow_values
    ):
        workflow = await _create_workflow(repo, workflow_values)
        await repo.soft_delete_workflow(workflow)

        default_page = await repo.list_workflows(
            WorkflowFilterParams(entity_id=workflow.entity_id),
            PaginationParams(),
            SortParams(),
        )
        assert all(w.id != workflow.id for w in default_page.items)

        with_deleted_page = await repo.list_workflows(
            WorkflowFilterParams(
                entity_id=workflow.entity_id, include_deleted=True
            ),
            PaginationParams(),
            SortParams(),
        )
        assert any(w.id == workflow.id for w in with_deleted_page.items)

    async def test_search_workflows_matches_name(self, repo, workflow_values):
        values = dict(workflow_values)
        values["name"] = "Unique Searchable Workflow Name"
        workflow = await _create_workflow(repo, values)

        result = await repo.search_workflows(
            "Searchable", WorkflowFilterParams(), PaginationParams(), SortParams()
        )
        assert any(w.id == workflow.id for w in result.items)

    async def test_list_workflows_is_overdue_filter(self, repo, workflow_values):
        overdue_values = dict(workflow_values)
        overdue_values["entity_id"] = str(uuid.uuid4())
        overdue_values["due_date"] = datetime.now(timezone.utc) - timedelta(days=1)
        overdue = await _create_workflow(repo, overdue_values)

        result = await repo.list_workflows(
            WorkflowFilterParams(is_overdue=True),
            PaginationParams(),
            SortParams(),
        )
        assert any(w.id == overdue.id for w in result.items)

    async def test_list_workflows_sort_direction(self, repo, workflow_values):
        first = dict(workflow_values)
        first["name"] = "A Workflow"
        first["entity_id"] = str(uuid.uuid4())
        second = dict(workflow_values)
        second["name"] = "Z Workflow"
        second["entity_id"] = str(uuid.uuid4())
        await _create_workflow(repo, first)
        await _create_workflow(repo, second)

        asc = await repo.list_workflows(
            WorkflowFilterParams(),
            PaginationParams(page_size=200),
            SortParams(field="name", direction="asc"),
        )
        names = [w.name for w in asc.items]
        assert names == sorted(names)


# ---------------------------------------------------------------------------
# Workflow: statistics
# ---------------------------------------------------------------------------
class TestWorkflowStatistics:
    async def test_get_status_counts(self, repo, workflow_values):
        await _create_workflow(repo, workflow_values)
        counts = await repo.get_status_counts(WorkflowFilterParams())
        assert counts.get("draft", 0) >= 1

    async def test_get_priority_counts(self, repo, workflow_values):
        await _create_workflow(repo, workflow_values)
        counts = await repo.get_priority_counts(WorkflowFilterParams())
        assert counts.get("high", 0) >= 1

    async def test_get_overdue_count(self, repo, workflow_values):
        values = dict(workflow_values)
        values["due_date"] = datetime.now(timezone.utc) - timedelta(days=2)
        await _create_workflow(repo, values)
        assert await repo.get_overdue_count(WorkflowFilterParams()) >= 1

    async def test_get_average_completion_seconds_none_when_no_completions(
        self, repo, workflow_values
    ):
        await _create_workflow(repo, workflow_values)
        result = await repo.get_average_completion_seconds(
            WorkflowFilterParams(entity_id=workflow_values["entity_id"])
        )
        assert result is None

    async def test_get_average_completion_seconds_computed(
        self, repo, workflow_values
    ):
        workflow = await _create_workflow(repo, workflow_values)
        started = datetime.now(timezone.utc) - timedelta(hours=2)
        completed = datetime.now(timezone.utc)
        await repo.update_workflow(
            workflow, {"started_at": started, "completed_at": completed}
        )
        avg = await repo.get_average_completion_seconds(
            WorkflowFilterParams(entity_id=workflow.entity_id)
        )
        assert avg is not None
        assert avg > 0

    async def test_get_total_count(self, repo, workflow_values):
        before = await repo.get_total_count(WorkflowFilterParams())
        await _create_workflow(repo, workflow_values)
        after = await repo.get_total_count(WorkflowFilterParams())
        assert after == before + 1


# ---------------------------------------------------------------------------
# WorkflowStep: CRUD
# ---------------------------------------------------------------------------
class TestWorkflowStepCrud:
    async def test_create_step(self, repo, workflow_values):
        workflow = await _create_workflow(repo, workflow_values)
        step = await _create_step(repo, workflow.id)
        assert step.id is not None
        assert step.status.value == "pending"

    async def test_bulk_create_steps(self, repo, workflow_values):
        workflow = await _create_workflow(repo, workflow_values)
        values_list = [
            {
                "workflow_id": workflow.id,
                "step_order": i,
                "step_name": f"Step {i}",
                "step_type": "generic",
            }
            for i in range(1, 4)
        ]
        steps = await repo.bulk_create_steps(values_list)
        assert len(steps) == 3

    async def test_get_step_by_id(self, repo, workflow_values):
        workflow = await _create_workflow(repo, workflow_values)
        step = await _create_step(repo, workflow.id)
        fetched = await repo.get_step_by_id(step.id)
        assert fetched is not None
        assert fetched.id == step.id

    async def test_get_step_with_approvals(self, repo, workflow_values, seed_users):
        workflow = await _create_workflow(repo, workflow_values)
        step = await _create_step(repo, workflow.id, is_approval_required=True)
        await repo.create_approval(
            {
                "workflow_step_id": step.id,
                "workflow_id": workflow.id,
                "approver_id": seed_users["approver"],
            }
        )
        fetched = await repo.get_step_with_approvals(step.id)
        assert len(fetched.approvals) == 1

    async def test_list_steps_by_workflow_ordered(self, repo, workflow_values):
        workflow = await _create_workflow(repo, workflow_values)
        await _create_step(repo, workflow.id, step_order=2)
        await _create_step(repo, workflow.id, step_order=1)

        steps = await repo.list_steps_by_workflow(workflow.id)
        assert [s.step_order for s in steps] == [1, 2]

    async def test_get_step_by_order(self, repo, workflow_values):
        workflow = await _create_workflow(repo, workflow_values)
        step = await _create_step(repo, workflow.id, step_order=1)
        found = await repo.get_step_by_order(workflow.id, 1)
        assert found.id == step.id
        assert await repo.get_step_by_order(workflow.id, 99) is None

    async def test_get_next_pending_step(self, repo, workflow_values):
        workflow = await _create_workflow(repo, workflow_values)
        await _create_step(repo, workflow.id, step_order=1)
        step2 = await _create_step(repo, workflow.id, step_order=2)

        step1 = await repo.get_step_by_order(workflow.id, 1)
        await repo.update_step(step1, {"status": WorkflowStepStatus.COMPLETED})

        next_step = await repo.get_next_pending_step(workflow.id)
        assert next_step.id == step2.id

    async def test_update_step(self, repo, workflow_values):
        workflow = await _create_workflow(repo, workflow_values)
        step = await _create_step(repo, workflow.id)
        updated = await repo.update_step(step, {"step_name": "Renamed"})
        assert updated.step_name == "Renamed"

    async def test_delete_step(self, repo, workflow_values):
        workflow = await _create_workflow(repo, workflow_values)
        step = await _create_step(repo, workflow.id)
        await repo.delete_step(step)
        assert await repo.get_step_by_id(step.id) is None

    async def test_count_steps_by_status(self, repo, workflow_values):
        workflow = await _create_workflow(repo, workflow_values)
        await _create_step(repo, workflow.id, step_order=1)
        await _create_step(repo, workflow.id, step_order=2)
        counts = await repo.count_steps_by_status(workflow.id)
        assert counts.get("pending", 0) == 2


# ---------------------------------------------------------------------------
# WorkflowApproval: CRUD
# ---------------------------------------------------------------------------
class TestWorkflowApprovalCrud:
    async def test_create_approval(self, repo, workflow_values, seed_users):
        workflow = await _create_workflow(repo, workflow_values)
        step = await _create_step(repo, workflow.id, is_approval_required=True)
        approval = await repo.create_approval(
            {
                "workflow_step_id": step.id,
                "workflow_id": workflow.id,
                "approver_id": seed_users["approver"],
            }
        )
        assert approval.status.value == "pending"

    async def test_get_approval_by_id(self, repo, workflow_values, seed_users):
        workflow = await _create_workflow(repo, workflow_values)
        step = await _create_step(repo, workflow.id, is_approval_required=True)
        approval = await repo.create_approval(
            {
                "workflow_step_id": step.id,
                "workflow_id": workflow.id,
                "approver_id": seed_users["approver"],
            }
        )
        fetched = await repo.get_approval_by_id(approval.id)
        assert fetched.id == approval.id
        assert await repo.get_approval_by_id(uuid.uuid4()) is None

    async def test_list_approvals_by_step_and_workflow(
        self, repo, workflow_values, seed_users
    ):
        workflow = await _create_workflow(repo, workflow_values)
        step = await _create_step(repo, workflow.id, is_approval_required=True)
        await repo.create_approval(
            {
                "workflow_step_id": step.id,
                "workflow_id": workflow.id,
                "approver_id": seed_users["approver"],
            }
        )
        by_step = await repo.list_approvals_by_step(step.id)
        by_workflow = await repo.list_approvals_by_workflow(workflow.id)
        assert len(by_step) == 1
        assert len(by_workflow) == 1

    async def test_list_pending_approvals_for_approver(
        self, repo, workflow_values, seed_users
    ):
        workflow = await _create_workflow(repo, workflow_values)
        step = await _create_step(repo, workflow.id, is_approval_required=True)
        await repo.create_approval(
            {
                "workflow_step_id": step.id,
                "workflow_id": workflow.id,
                "approver_id": seed_users["approver"],
            }
        )
        page = await repo.list_pending_approvals_for_approver(
            seed_users["approver"], PaginationParams()
        )
        assert page.total >= 1

    async def test_has_pending_approvals(self, repo, workflow_values, seed_users):
        workflow = await _create_workflow(repo, workflow_values)
        step = await _create_step(repo, workflow.id, is_approval_required=True)
        assert await repo.has_pending_approvals(step.id) is False

        approval = await repo.create_approval(
            {
                "workflow_step_id": step.id,
                "workflow_id": workflow.id,
                "approver_id": seed_users["approver"],
            }
        )
        assert await repo.has_pending_approvals(step.id) is True

        await repo.update_approval(
            approval,
            {
                "status": ApprovalStatus.APPROVED,
                "decided_at": datetime.now(timezone.utc),
            },
        )
        assert await repo.has_pending_approvals(step.id) is False

    async def test_update_approval(self, repo, workflow_values, seed_users):
        workflow = await _create_workflow(repo, workflow_values)
        step = await _create_step(repo, workflow.id, is_approval_required=True)
        approval = await repo.create_approval(
            {
                "workflow_step_id": step.id,
                "workflow_id": workflow.id,
                "approver_id": seed_users["approver"],
            }
        )
        updated = await repo.update_approval(
            approval, {"decision_notes": "Looks good."}
        )
        assert updated.decision_notes == "Looks good."

    async def test_get_approval_counts_by_status(
        self, repo, workflow_values, seed_users
    ):
        workflow = await _create_workflow(repo, workflow_values)
        step = await _create_step(repo, workflow.id, is_approval_required=True)
        await repo.create_approval(
            {
                "workflow_step_id": step.id,
                "workflow_id": workflow.id,
                "approver_id": seed_users["approver"],
            }
        )
        counts = await repo.get_approval_counts_by_status(workflow.id)
        assert counts.get("pending", 0) >= 1


# ---------------------------------------------------------------------------
# History
# ---------------------------------------------------------------------------
class TestWorkflowHistory:
    async def test_get_workflow_history_merges_steps_and_approvals(
        self, repo, workflow_values, seed_users
    ):
        workflow = await _create_workflow(repo, workflow_values)
        step = await _create_step(repo, workflow.id, is_approval_required=True)
        await repo.create_approval(
            {
                "workflow_step_id": step.id,
                "workflow_id": workflow.id,
                "approver_id": seed_users["approver"],
            }
        )

        history = await repo.get_workflow_history(workflow.id)
        types = {entry["type"] for entry in history}
        assert types == {"step", "approval"}

    async def test_get_workflow_history_sorted_chronologically(
        self, repo, workflow_values
    ):
        workflow = await _create_workflow(repo, workflow_values)
        await _create_step(repo, workflow.id, step_order=1)
        await _create_step(repo, workflow.id, step_order=2)

        history = await repo.get_workflow_history(workflow.id)
        timestamps = [entry["timestamp"] for entry in history if entry["timestamp"]]
        assert timestamps == sorted(timestamps)