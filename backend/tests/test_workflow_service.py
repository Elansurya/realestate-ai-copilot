"""
backend/tests/test_workflow_service.py

Unit tests for `WorkflowService`.

The repository is fully mocked (`unittest.mock.AsyncMock`) so these
tests exercise only the business logic owned by the service layer:
state-machine enforcement, payload validation, side effects applied on
status transitions, and translation of "not found" / "invalid state"
situations into the project's domain exceptions.

No database or event loop fixtures beyond `pytest-asyncio` are
required.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.core.exceptions import (
    BusinessRuleViolationException,
    ConflictException,
    NotFoundException,
    ValidationException,
)
from app.models.workflow import (
    ApprovalStatus,
    Workflow,
    WorkflowApproval,
    WorkflowStep,
    WorkflowStepStatus,
    WorkflowStatus,
)
from app.schemas.workflow import (
    WorkflowApprovalUpdate,
    WorkflowCreate,
    WorkflowPriority,
    WorkflowStepCreate,
    WorkflowStepUpdate,
    WorkflowUpdate,
)
from app.services.workflow_service import WorkflowService

pytestmark = pytest.mark.asyncio


# ---------------------------------------------------------------------------
# Helpers / fixtures
# ---------------------------------------------------------------------------
def make_workflow(**overrides) -> Workflow:
    workflow = Workflow()
    workflow.id = overrides.get("id", uuid.uuid4())
    workflow.name = overrides.get("name", "Test Workflow")
    workflow.workflow_type = overrides.get("workflow_type", "lead_conversion")
    workflow.entity_type = overrides.get("entity_type", "lead")
    workflow.entity_id = overrides.get("entity_id", "lead-123")
    workflow.status = overrides.get("status", WorkflowStatus.DRAFT)
    workflow.priority = overrides.get("priority", "normal")
    workflow.is_deleted = overrides.get("is_deleted", False)
    workflow.current_step_order = overrides.get("current_step_order")
    workflow.steps = overrides.get("steps", [])
    workflow.initiated_by_id = overrides.get("initiated_by_id", 1)
    workflow.assigned_to_id = overrides.get("assigned_to_id")
    return workflow


def make_step(**overrides) -> WorkflowStep:
    step = WorkflowStep()
    step.id = overrides.get("id", uuid.uuid4())
    step.workflow_id = overrides.get("workflow_id", uuid.uuid4())
    step.step_order = overrides.get("step_order", 1)
    step.step_name = overrides.get("step_name", "Verify documents")
    step.step_type = overrides.get("step_type", "document_verification")
    step.status = overrides.get("status", WorkflowStepStatus.PENDING)
    step.is_approval_required = overrides.get("is_approval_required", False)
    step.retry_count = overrides.get("retry_count", 0)
    step.output_data = overrides.get("output_data")
    step.started_at = overrides.get("started_at")
    return step


def make_approval(**overrides) -> WorkflowApproval:
    approval = WorkflowApproval()
    approval.id = overrides.get("id", uuid.uuid4())
    approval.workflow_step_id = overrides.get("workflow_step_id", uuid.uuid4())
    approval.workflow_id = overrides.get("workflow_id", uuid.uuid4())
    approval.approver_id = overrides.get("approver_id", 5)
    approval.status = overrides.get("status", ApprovalStatus.PENDING)
    approval.escalated = overrides.get("escalated", False)
    return approval


@pytest.fixture
def repository() -> AsyncMock:
    repo = AsyncMock()

    async def _update_workflow(workflow, values):
        for key, value in values.items():
            setattr(workflow, key, value)
        return workflow

    async def _update_step(step, values):
        for key, value in values.items():
            setattr(step, key, value)
        return step

    async def _update_approval(approval, values):
        for key, value in values.items():
            setattr(approval, key, value)
        return approval

    repo.update_workflow.side_effect = _update_workflow
    repo.update_step.side_effect = _update_step
    repo.update_approval.side_effect = _update_approval
    return repo


@pytest.fixture
def service(repository) -> WorkflowService:
    return WorkflowService(repository)


# ---------------------------------------------------------------------------
# Workflow: create / read / update / delete
# ---------------------------------------------------------------------------
class TestCreateWorkflow:
    async def test_create_workflow_rejects_duplicate_step_orders(self, service):
        payload = WorkflowCreate(
            name="Test",
            workflow_type="lead_conversion",
            entity_type="lead",
            entity_id="lead-1",
            initiated_by_id=1,
            steps=[
                WorkflowStepCreate(
                    workflow_id=uuid.uuid4(),
                    step_order=1,
                    step_name="A",
                    step_type="t",
                ),
                WorkflowStepCreate(
                    workflow_id=uuid.uuid4(),
                    step_order=1,
                    step_name="B",
                    step_type="t",
                ),
            ],
        )
        with pytest.raises(ValidationException):
            await service.create_workflow(payload)

    async def test_create_workflow_rejects_non_contiguous_step_orders(self, service):
        payload = WorkflowCreate(
            name="Test",
            workflow_type="lead_conversion",
            entity_type="lead",
            entity_id="lead-1",
            initiated_by_id=1,
            steps=[
                WorkflowStepCreate(
                    workflow_id=uuid.uuid4(),
                    step_order=1,
                    step_name="A",
                    step_type="t",
                ),
                WorkflowStepCreate(
                    workflow_id=uuid.uuid4(),
                    step_order=3,
                    step_name="B",
                    step_type="t",
                ),
            ],
        )
        with pytest.raises(ValidationException):
            await service.create_workflow(payload)

    async def test_create_workflow_happy_path(self, service, repository):
        created = make_workflow()
        repository.create_workflow.return_value = created
        repository.get_workflow_with_steps.return_value = created

        payload = WorkflowCreate(
            name="Test",
            workflow_type="lead_conversion",
            entity_type="lead",
            entity_id="lead-1",
            initiated_by_id=1,
        )
        result = await service.create_workflow(payload, actor_id=42)

        assert result is created
        repository.create_workflow.assert_awaited_once()
        repository.bulk_create_steps.assert_not_awaited()

    async def test_create_workflow_bulk_creates_steps_when_provided(
        self, service, repository
    ):
        created = make_workflow()
        repository.create_workflow.return_value = created
        repository.get_workflow_with_steps.return_value = created

        payload = WorkflowCreate(
            name="Test",
            workflow_type="lead_conversion",
            entity_type="lead",
            entity_id="lead-1",
            initiated_by_id=1,
            steps=[
                WorkflowStepCreate(
                    workflow_id=uuid.uuid4(),
                    step_order=1,
                    step_name="A",
                    step_type="t",
                )
            ],
        )
        await service.create_workflow(payload)
        repository.bulk_create_steps.assert_awaited_once()


class TestGetWorkflow:
    async def test_get_workflow_not_found_raises(self, service, repository):
        repository.get_workflow_by_id.return_value = None
        with pytest.raises(NotFoundException):
            await service.get_workflow(uuid.uuid4())

    async def test_get_workflow_found_returns_workflow(self, service, repository):
        workflow = make_workflow()
        repository.get_workflow_by_id.return_value = workflow
        result = await service.get_workflow(workflow.id)
        assert result is workflow


class TestUpdateWorkflow:
    async def test_update_workflow_not_found(self, service, repository):
        repository.get_workflow_by_id.return_value = None
        with pytest.raises(NotFoundException):
            await service.update_workflow(uuid.uuid4(), WorkflowUpdate(name="X"))

    async def test_update_workflow_rejects_deleted(self, service, repository):
        workflow = make_workflow(is_deleted=True)
        repository.get_workflow_by_id.return_value = workflow
        with pytest.raises(ConflictException):
            await service.update_workflow(workflow.id, WorkflowUpdate(name="X"))

    async def test_update_workflow_invalid_status_transition(
        self, service, repository
    ):
        workflow = make_workflow(status=WorkflowStatus.COMPLETED)
        repository.get_workflow_by_id.return_value = workflow
        with pytest.raises(BusinessRuleViolationException):
            await service.update_workflow(
                workflow.id, WorkflowUpdate(status=WorkflowStatus.ACTIVE)
            )

    async def test_update_workflow_valid_status_transition_sets_started_at(
        self, service, repository
    ):
        workflow = make_workflow(status=WorkflowStatus.DRAFT)
        repository.get_workflow_by_id.return_value = workflow

        result = await service.update_workflow(
            workflow.id, WorkflowUpdate(status=WorkflowStatus.ACTIVE)
        )
        assert result.status == WorkflowStatus.ACTIVE
        assert result.started_at is not None

    async def test_update_workflow_maps_meta_data_to_metadata_column(
        self, service, repository
    ):
        workflow = make_workflow()
        repository.get_workflow_by_id.return_value = workflow
        await service.update_workflow(
            workflow.id, WorkflowUpdate(meta_data={"a": 1})
        )
        assert workflow.metadata == {"a": 1}


class TestDeleteRestoreWorkflow:
    async def test_delete_workflow_already_deleted_raises_conflict(
        self, service, repository
    ):
        workflow = make_workflow(is_deleted=True)
        repository.get_workflow_by_id.return_value = workflow
        with pytest.raises(ConflictException):
            await service.delete_workflow(workflow.id)

    async def test_delete_workflow_soft_deletes(self, service, repository):
        workflow = make_workflow(is_deleted=False)
        repository.get_workflow_by_id.return_value = workflow
        repository.soft_delete_workflow.return_value = workflow
        result = await service.delete_workflow(workflow.id, actor_id=9)
        repository.soft_delete_workflow.assert_awaited_once_with(workflow)
        assert result is workflow

    async def test_restore_workflow_not_deleted_raises_conflict(
        self, service, repository
    ):
        workflow = make_workflow(is_deleted=False)
        repository.get_workflow_by_id.return_value = workflow
        with pytest.raises(ConflictException):
            await service.restore_workflow(workflow.id)

    async def test_restore_workflow_not_found(self, service, repository):
        repository.get_workflow_by_id.return_value = None
        with pytest.raises(NotFoundException):
            await service.restore_workflow(uuid.uuid4())


# ---------------------------------------------------------------------------
# Workflow: listing / search
# ---------------------------------------------------------------------------
class TestListSearchWorkflows:
    async def test_list_workflows_validates_page(self, service):
        from app.repositories.workflow_repository import PaginationParams

        with pytest.raises(ValidationException):
            await service.list_workflows(pagination=PaginationParams(page=0))

    async def test_list_workflows_validates_page_size(self, service):
        from app.repositories.workflow_repository import PaginationParams

        with pytest.raises(ValidationException):
            await service.list_workflows(pagination=PaginationParams(page_size=500))

    async def test_search_workflows_requires_search_term(self, service):
        with pytest.raises(ValidationException):
            await service.search_workflows("   ")

    async def test_search_workflows_delegates_to_repository(
        self, service, repository
    ):
        repository.search_workflows.return_value = MagicMock(items=[], total=0)
        await service.search_workflows("lead")
        repository.search_workflows.assert_awaited_once()


# ---------------------------------------------------------------------------
# Workflow execution
# ---------------------------------------------------------------------------
class TestStartWorkflow:
    async def test_start_workflow_requires_steps(self, service, repository):
        workflow = make_workflow(steps=[])
        repository.get_workflow_with_steps.return_value = workflow
        with pytest.raises(BusinessRuleViolationException):
            await service.start_workflow(workflow.id)

    async def test_start_workflow_rejects_deleted(self, service, repository):
        step = make_step()
        workflow = make_workflow(is_deleted=True, steps=[step])
        repository.get_workflow_with_steps.return_value = workflow
        with pytest.raises(ConflictException):
            await service.start_workflow(workflow.id)

    async def test_start_workflow_invalid_transition(self, service, repository):
        step = make_step()
        workflow = make_workflow(status=WorkflowStatus.COMPLETED, steps=[step])
        repository.get_workflow_with_steps.return_value = workflow
        with pytest.raises(BusinessRuleViolationException):
            await service.start_workflow(workflow.id)

    async def test_start_workflow_transitions_to_in_progress_and_starts_first_step(
        self, service, repository
    ):
        step = make_step(step_order=1, status=WorkflowStepStatus.PENDING)
        workflow = make_workflow(status=WorkflowStatus.DRAFT, steps=[step])
        repository.get_workflow_with_steps.return_value = workflow

        result = await service.start_workflow(workflow.id, actor_id=1)

        assert result.status == WorkflowStatus.IN_PROGRESS
        assert step.status == WorkflowStepStatus.IN_PROGRESS
        assert step.started_at is not None


class TestCompleteStep:
    async def test_complete_step_not_found(self, service, repository):
        workflow = make_workflow()
        repository.get_workflow_by_id.return_value = workflow
        repository.get_step_by_id.return_value = None
        with pytest.raises(NotFoundException):
            await service.complete_step(workflow.id, uuid.uuid4())

    async def test_complete_step_wrong_workflow_not_found(self, service, repository):
        workflow = make_workflow()
        other_step = make_step(workflow_id=uuid.uuid4())
        repository.get_workflow_by_id.return_value = workflow
        repository.get_step_by_id.return_value = other_step
        with pytest.raises(NotFoundException):
            await service.complete_step(workflow.id, other_step.id)

    async def test_complete_step_blocked_by_pending_approval(
        self, service, repository
    ):
        workflow = make_workflow()
        step = make_step(
            workflow_id=workflow.id,
            is_approval_required=True,
            status=WorkflowStepStatus.IN_PROGRESS,
        )
        repository.get_workflow_by_id.return_value = workflow
        repository.get_step_by_id.return_value = step
        repository.has_pending_approvals.return_value = True

        with pytest.raises(BusinessRuleViolationException):
            await service.complete_step(workflow.id, step.id)

    async def test_complete_step_advances_workflow_to_next_step(
        self, service, repository
    ):
        workflow = make_workflow(status=WorkflowStatus.IN_PROGRESS)
        step = make_step(
            workflow_id=workflow.id, status=WorkflowStepStatus.IN_PROGRESS
        )
        next_step = make_step(
            workflow_id=workflow.id,
            step_order=2,
            status=WorkflowStepStatus.PENDING,
        )
        repository.get_workflow_by_id.return_value = workflow
        repository.get_step_by_id.return_value = step
        repository.get_next_pending_step.return_value = next_step

        result = await service.complete_step(
            workflow.id, step.id, output_data={"ok": True}
        )

        assert result.status == WorkflowStepStatus.COMPLETED
        assert next_step.status == WorkflowStepStatus.IN_PROGRESS
        assert workflow.current_step_order == 2

    async def test_complete_step_marks_workflow_completed_when_no_steps_remain(
        self, service, repository
    ):
        workflow = make_workflow(status=WorkflowStatus.IN_PROGRESS)
        step = make_step(
            workflow_id=workflow.id, status=WorkflowStepStatus.IN_PROGRESS
        )
        repository.get_workflow_by_id.return_value = workflow
        repository.get_step_by_id.return_value = step
        repository.get_next_pending_step.return_value = None

        await service.complete_step(workflow.id, step.id)
        assert workflow.status == WorkflowStatus.COMPLETED
        assert workflow.completed_at is not None


class TestFailRetrySkipStep:
    async def test_fail_step_transitions_workflow_to_failed(
        self, service, repository
    ):
        workflow = make_workflow(status=WorkflowStatus.IN_PROGRESS)
        step = make_step(
            workflow_id=workflow.id, status=WorkflowStepStatus.IN_PROGRESS
        )
        repository.get_workflow_by_id.return_value = workflow
        repository.get_step_by_id.return_value = step

        result = await service.fail_step(workflow.id, step.id, reason="boom")
        assert result.status == WorkflowStepStatus.FAILED
        assert workflow.status == WorkflowStatus.FAILED

    async def test_retry_step_increments_retry_count_and_resumes_workflow(
        self, service, repository
    ):
        workflow = make_workflow(status=WorkflowStatus.FAILED)
        step = make_step(
            workflow_id=workflow.id,
            status=WorkflowStepStatus.FAILED,
            retry_count=1,
        )
        repository.get_workflow_by_id.return_value = workflow
        repository.get_step_by_id.return_value = step

        result = await service.retry_step(workflow.id, step.id)
        assert result.retry_count == 2
        assert result.status == WorkflowStepStatus.IN_PROGRESS
        assert workflow.status == WorkflowStatus.IN_PROGRESS

    async def test_skip_step_advances_workflow(self, service, repository):
        workflow = make_workflow(status=WorkflowStatus.IN_PROGRESS)
        step = make_step(
            workflow_id=workflow.id, status=WorkflowStepStatus.PENDING
        )
        repository.get_workflow_by_id.return_value = workflow
        repository.get_step_by_id.return_value = step
        repository.get_next_pending_step.return_value = None

        result = await service.skip_step(workflow.id, step.id)
        assert result.status == WorkflowStepStatus.SKIPPED
        assert workflow.status == WorkflowStatus.COMPLETED


class TestHoldResumeCancelWorkflow:
    async def test_hold_workflow(self, service, repository):
        workflow = make_workflow(status=WorkflowStatus.IN_PROGRESS)
        repository.get_workflow_by_id.return_value = workflow
        result = await service.hold_workflow(workflow.id)
        assert result.status == WorkflowStatus.ON_HOLD

    async def test_resume_workflow_without_current_step_goes_active(
        self, service, repository
    ):
        workflow = make_workflow(
            status=WorkflowStatus.ON_HOLD, current_step_order=None
        )
        repository.get_workflow_by_id.return_value = workflow
        result = await service.resume_workflow(workflow.id)
        assert result.status == WorkflowStatus.ACTIVE

    async def test_resume_workflow_with_current_step_goes_in_progress(
        self, service, repository
    ):
        workflow = make_workflow(status=WorkflowStatus.ON_HOLD, current_step_order=2)
        repository.get_workflow_by_id.return_value = workflow
        result = await service.resume_workflow(workflow.id)
        assert result.status == WorkflowStatus.IN_PROGRESS

    async def test_cancel_workflow_requires_reason(self, service, repository):
        workflow = make_workflow()
        repository.get_workflow_by_id.return_value = workflow
        with pytest.raises(ValidationException):
            await service.cancel_workflow(workflow.id, "   ")

    async def test_cancel_workflow_sets_cancelled_fields(self, service, repository):
        workflow = make_workflow(status=WorkflowStatus.ACTIVE)
        repository.get_workflow_by_id.return_value = workflow
        result = await service.cancel_workflow(workflow.id, "No longer needed")
        assert result.status == WorkflowStatus.CANCELLED
        assert result.cancellation_reason == "No longer needed"
        assert result.cancelled_at is not None


# ---------------------------------------------------------------------------
# WorkflowStep: standalone CRUD
# ---------------------------------------------------------------------------
class TestStepCrud:
    async def test_add_step_rejects_terminal_workflow(self, service, repository):
        workflow = make_workflow(status=WorkflowStatus.COMPLETED)
        repository.get_workflow_by_id.return_value = workflow
        payload = WorkflowStepCreate(
            workflow_id=workflow.id, step_order=1, step_name="A", step_type="t"
        )
        with pytest.raises(BusinessRuleViolationException):
            await service.add_step(payload)

    async def test_add_step_rejects_duplicate_order(self, service, repository):
        workflow = make_workflow(status=WorkflowStatus.DRAFT)
        repository.get_workflow_by_id.return_value = workflow
        repository.get_step_by_order.return_value = make_step(step_order=1)
        payload = WorkflowStepCreate(
            workflow_id=workflow.id, step_order=1, step_name="A", step_type="t"
        )
        with pytest.raises(ConflictException):
            await service.add_step(payload)

    async def test_add_step_success(self, service, repository):
        workflow = make_workflow(status=WorkflowStatus.DRAFT)
        repository.get_workflow_by_id.return_value = workflow
        repository.get_step_by_order.return_value = None
        created_step = make_step()
        repository.create_step.return_value = created_step

        payload = WorkflowStepCreate(
            workflow_id=workflow.id, step_order=1, step_name="A", step_type="t"
        )
        result = await service.add_step(payload, actor_id=3)
        assert result is created_step

    async def test_remove_step_rejects_non_draft_workflow(self, service, repository):
        step = make_step()
        workflow = make_workflow(status=WorkflowStatus.ACTIVE, id=step.workflow_id)
        repository.get_step_by_id.return_value = step
        repository.get_workflow_by_id.return_value = workflow
        with pytest.raises(BusinessRuleViolationException):
            await service.remove_step(step.id)

    async def test_remove_step_allowed_for_draft(self, service, repository):
        step = make_step()
        workflow = make_workflow(status=WorkflowStatus.DRAFT, id=step.workflow_id)
        repository.get_step_by_id.return_value = step
        repository.get_workflow_by_id.return_value = workflow
        await service.remove_step(step.id)
        repository.delete_step.assert_awaited_once_with(step)

    async def test_update_step_details_validates_transition(
        self, service, repository
    ):
        step = make_step(status=WorkflowStepStatus.COMPLETED)
        repository.get_step_by_id.return_value = step
        with pytest.raises(BusinessRuleViolationException):
            await service.update_step_details(
                step.id, WorkflowStepUpdate(status=WorkflowStepStatus.IN_PROGRESS)
            )


# ---------------------------------------------------------------------------
# Approval flow
# ---------------------------------------------------------------------------
class TestApprovalFlow:
    async def test_request_approval_step_does_not_require_approval(
        self, service, repository
    ):
        workflow = make_workflow()
        step = make_step(workflow_id=workflow.id, is_approval_required=False)
        repository.get_workflow_by_id.return_value = workflow
        repository.get_step_by_id.return_value = step
        with pytest.raises(BusinessRuleViolationException):
            await service.request_approval(workflow.id, step.id, approver_id=2)

    async def test_request_approval_invalid_approver_id(self, service, repository):
        workflow = make_workflow()
        step = make_step(workflow_id=workflow.id, is_approval_required=True)
        repository.get_workflow_by_id.return_value = workflow
        repository.get_step_by_id.return_value = step
        with pytest.raises(ValidationException):
            await service.request_approval(workflow.id, step.id, approver_id=0)

    async def test_request_approval_blocks_pending_step(self, service, repository):
        workflow = make_workflow()
        step = make_step(
            workflow_id=workflow.id,
            is_approval_required=True,
            status=WorkflowStepStatus.PENDING,
        )
        repository.get_workflow_by_id.return_value = workflow
        repository.get_step_by_id.return_value = step
        repository.create_approval.return_value = make_approval(
            workflow_step_id=step.id, workflow_id=workflow.id
        )

        await service.request_approval(workflow.id, step.id, approver_id=2)
        assert step.status == WorkflowStepStatus.BLOCKED

    async def test_decide_approval_requires_status(self, service, repository):
        approval = make_approval()
        repository.get_approval_by_id.return_value = approval
        with pytest.raises(ValidationException):
            await service.decide_approval(approval.id, WorkflowApprovalUpdate())

    async def test_approve_unblocks_step_when_no_other_pending_approvals(
        self, service, repository
    ):
        step = make_step(status=WorkflowStepStatus.BLOCKED)
        approval = make_approval(workflow_step_id=step.id, workflow_id=uuid.uuid4())
        repository.get_approval_by_id.return_value = approval
        repository.get_step_by_id.return_value = step
        repository.has_pending_approvals.return_value = False

        result = await service.approve(approval.id, decision_notes="LGTM")
        assert result.status == ApprovalStatus.APPROVED
        assert step.status == WorkflowStepStatus.IN_PROGRESS

    async def test_approve_leaves_step_blocked_when_other_approvals_pending(
        self, service, repository
    ):
        step = make_step(status=WorkflowStepStatus.BLOCKED)
        approval = make_approval(workflow_step_id=step.id, workflow_id=uuid.uuid4())
        repository.get_approval_by_id.return_value = approval
        repository.get_step_by_id.return_value = step
        repository.has_pending_approvals.return_value = True

        await service.approve(approval.id)
        assert step.status == WorkflowStepStatus.BLOCKED

    async def test_reject_fails_step_and_workflow(self, service, repository):
        workflow = make_workflow(status=WorkflowStatus.IN_PROGRESS)
        step = make_step(
            workflow_id=workflow.id, status=WorkflowStepStatus.BLOCKED
        )
        approval = make_approval(
            workflow_step_id=step.id, workflow_id=workflow.id
        )
        repository.get_approval_by_id.return_value = approval
        repository.get_step_by_id.return_value = step
        repository.get_workflow_by_id.return_value = workflow

        result = await service.reject(approval.id, decision_notes="Not sufficient")
        assert result.status == ApprovalStatus.REJECTED
        assert step.status == WorkflowStepStatus.FAILED
        assert workflow.status == WorkflowStatus.FAILED

    async def test_escalate_requires_target(self, service, repository):
        approval = make_approval()
        repository.get_approval_by_id.return_value = approval
        with pytest.raises(ValidationException):
            await service.escalate_approval(approval.id, escalated_to_id=None)

    async def test_escalate_sets_escalation_fields(self, service, repository):
        step = make_step(status=WorkflowStepStatus.BLOCKED)
        approval = make_approval(workflow_step_id=step.id, workflow_id=uuid.uuid4())
        repository.get_approval_by_id.return_value = approval
        repository.get_step_by_id.return_value = step

        result = await service.escalate_approval(approval.id, escalated_to_id=7)
        assert result.status == ApprovalStatus.ESCALATED
        assert result.escalated is True
        assert result.escalated_to_id == 7

    async def test_cancel_approval_unblocks_step(self, service, repository):
        step = make_step(status=WorkflowStepStatus.BLOCKED)
        approval = make_approval(workflow_step_id=step.id, workflow_id=uuid.uuid4())
        repository.get_approval_by_id.return_value = approval
        repository.get_step_by_id.return_value = step
        repository.has_pending_approvals.return_value = False

        result = await service.cancel_approval(approval.id)
        assert result.status == ApprovalStatus.CANCELLED
        assert step.status == WorkflowStepStatus.IN_PROGRESS

    async def test_list_pending_approvals_for_approver_validates_id(self, service):
        with pytest.raises(ValidationException):
            await service.list_pending_approvals_for_approver(0)


# ---------------------------------------------------------------------------
# Statistics / history
# ---------------------------------------------------------------------------
class TestStatisticsAndHistory:
    async def test_get_statistics_combines_repository_calls(
        self, service, repository
    ):
        repository.get_total_count.return_value = 10
        repository.get_status_counts.return_value = {"draft": 5}
        repository.get_priority_counts.return_value = {"normal": 5}
        repository.get_overdue_count.return_value = 2
        repository.get_average_completion_seconds.return_value = 7200.0

        stats = await service.get_statistics()
        assert stats["total"] == 10
        assert stats["average_completion_hours"] == 2.0

    async def test_get_workflow_history_checks_existence_first(
        self, service, repository
    ):
        workflow = make_workflow()
        repository.get_workflow_by_id.return_value = workflow
        repository.get_workflow_history.return_value = []
        result = await service.get_workflow_history(workflow.id)
        assert result == []
        repository.get_workflow_by_id.assert_awaited()