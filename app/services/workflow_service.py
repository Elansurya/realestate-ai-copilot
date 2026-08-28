"""
backend/app/services/workflow_service.py

Business logic / orchestration layer for the Workflow module.

Responsibilities:
    - Enforce the Workflow / WorkflowStep / WorkflowApproval state
      machines (transition validation).
    - Orchestrate workflow execution (start, advance, complete, fail,
      cancel, hold/resume, retry).
    - Orchestrate the approval flow (request, approve, reject,
      escalate, cancel).
    - Provide search, pagination, sorting, filtering and statistics on
      top of the repository.
    - Own soft delete / restore semantics.
    - Translate "not found" / "invalid state" / "invalid input"
      situations into project domain exceptions -- never raw
      SQLAlchemy or framework exceptions.

NOTE: This module imports domain exceptions from `app.core.exceptions`.
That module is assumed to already exist in the project (it is not
part of the Workflow module) and to expose the following exception
types, all deriving from a common base domain exception:

    NotFoundException(resource: str, identifier: Any)
    ValidationException(message: str)
    ConflictException(message: str)
    BusinessRuleViolationException(message: str)

If the actual names/signatures in `app.core.exceptions` differ, only
the small `_exceptions` adapter section below needs to change -- the
rest of this file only ever raises through those four calls.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Any, Optional

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
from app.repositories.workflow_repository import (
    PageResult,
    PaginationParams,
    SortParams,
    WorkflowFilterParams,
    WorkflowRepository,
)
from app.schemas.workflow import (
    WorkflowApprovalUpdate,
    WorkflowCreate,
    WorkflowStepCreate,
    WorkflowStepUpdate,
    WorkflowUpdate,
)

# ---------------------------------------------------------------------------
# State machines
# ---------------------------------------------------------------------------
_WORKFLOW_TRANSITIONS: dict[WorkflowStatus, set[WorkflowStatus]] = {
    WorkflowStatus.DRAFT: {WorkflowStatus.ACTIVE, WorkflowStatus.CANCELLED},
    WorkflowStatus.ACTIVE: {
        WorkflowStatus.IN_PROGRESS,
        WorkflowStatus.ON_HOLD,
        WorkflowStatus.CANCELLED,
        WorkflowStatus.FAILED,
    },
    WorkflowStatus.IN_PROGRESS: {
        WorkflowStatus.ON_HOLD,
        WorkflowStatus.COMPLETED,
        WorkflowStatus.CANCELLED,
        WorkflowStatus.FAILED,
    },
    WorkflowStatus.ON_HOLD: {
        WorkflowStatus.ACTIVE,
        WorkflowStatus.IN_PROGRESS,
        WorkflowStatus.CANCELLED,
        WorkflowStatus.FAILED,
    },
    WorkflowStatus.FAILED: {WorkflowStatus.IN_PROGRESS, WorkflowStatus.CANCELLED},
    WorkflowStatus.COMPLETED: set(),
    WorkflowStatus.CANCELLED: set(),
}

_STEP_TRANSITIONS: dict[WorkflowStepStatus, set[WorkflowStepStatus]] = {
    WorkflowStepStatus.PENDING: {
        WorkflowStepStatus.IN_PROGRESS,
        WorkflowStepStatus.SKIPPED,
        WorkflowStepStatus.BLOCKED,
    },
    WorkflowStepStatus.IN_PROGRESS: {
        WorkflowStepStatus.COMPLETED,
        WorkflowStepStatus.FAILED,
        WorkflowStepStatus.BLOCKED,
    },
    WorkflowStepStatus.BLOCKED: {
        WorkflowStepStatus.PENDING,
        WorkflowStepStatus.IN_PROGRESS,
        WorkflowStepStatus.FAILED,
    },
    WorkflowStepStatus.FAILED: {
        WorkflowStepStatus.PENDING,
        WorkflowStepStatus.IN_PROGRESS,
    },
    WorkflowStepStatus.COMPLETED: set(),
    WorkflowStepStatus.SKIPPED: set(),
}

_APPROVAL_TRANSITIONS: dict[ApprovalStatus, set[ApprovalStatus]] = {
    ApprovalStatus.PENDING: {
        ApprovalStatus.APPROVED,
        ApprovalStatus.REJECTED,
        ApprovalStatus.ESCALATED,
        ApprovalStatus.CANCELLED,
    },
    ApprovalStatus.ESCALATED: {
        ApprovalStatus.APPROVED,
        ApprovalStatus.REJECTED,
        ApprovalStatus.CANCELLED,
    },
    ApprovalStatus.APPROVED: set(),
    ApprovalStatus.REJECTED: set(),
    ApprovalStatus.CANCELLED: set(),
}

_TERMINAL_WORKFLOW_STATUSES = {WorkflowStatus.COMPLETED, WorkflowStatus.CANCELLED}


class WorkflowService:
    """Business logic layer for the Workflow module."""

    def __init__(self, repository: WorkflowRepository) -> None:
        self.repository = repository

    # ------------------------------------------------------------------
    # Internal validation helpers
    # ------------------------------------------------------------------
    def _validate_workflow_transition(
        self, current: WorkflowStatus, target: WorkflowStatus
    ) -> None:
        allowed = _WORKFLOW_TRANSITIONS.get(current, set())
        if target not in allowed:
            raise BusinessRuleViolationException(
                f"Cannot transition workflow from '{current.value}' to "
                f"'{target.value}'."
            )

    def _validate_step_transition(
        self, current: WorkflowStepStatus, target: WorkflowStepStatus
    ) -> None:
        allowed = _STEP_TRANSITIONS.get(current, set())
        if target not in allowed:
            raise BusinessRuleViolationException(
                f"Cannot transition workflow step from '{current.value}' to "
                f"'{target.value}'."
            )

    def _validate_approval_transition(
        self, current: ApprovalStatus, target: ApprovalStatus
    ) -> None:
        allowed = _APPROVAL_TRANSITIONS.get(current, set())
        if target not in allowed:
            raise BusinessRuleViolationException(
                f"Cannot transition approval from '{current.value}' to "
                f"'{target.value}'."
            )

    def _validate_workflow_payload(self, payload: WorkflowCreate) -> None:
        if not payload.name.strip():
            raise ValidationException("Workflow name must not be blank.")
        if not payload.entity_id.strip():
            raise ValidationException("Workflow entity_id must not be blank.")
        if payload.steps:
            orders = [step.step_order for step in payload.steps]
            if len(orders) != len(set(orders)):
                raise ValidationException(
                    "Duplicate step_order values found in initial steps payload."
                )
            if sorted(orders) != list(range(1, len(orders) + 1)):
                raise ValidationException(
                    "Initial step_order values must form a contiguous "
                    "1-based sequence."
                )

    def _validate_step_payload(
        self, workflow: Workflow, payload: WorkflowStepCreate
    ) -> None:
        if not payload.step_name.strip():
            raise ValidationException("Step name must not be blank.")
        if workflow.status in _TERMINAL_WORKFLOW_STATUSES:
            raise BusinessRuleViolationException(
                f"Cannot add steps to a workflow in terminal status "
                f"'{workflow.status.value}'."
            )

    def _ensure_not_deleted(self, workflow: Workflow) -> None:
        if workflow.is_deleted:
            raise ConflictException(
                f"Workflow '{workflow.id}' has been deleted and cannot be modified."
            )

    # ------------------------------------------------------------------
    # Workflow: CRUD
    # ------------------------------------------------------------------
    async def create_workflow(
        self, payload: WorkflowCreate, actor_id: Optional[int] = None
    ) -> Workflow:
        self._validate_workflow_payload(payload)

        values = payload.model_dump(exclude={"steps", "meta_data"})
        values["metadata"] = payload.meta_data
        values["priority"] = payload.priority.value
        values["created_by_id"] = actor_id
        values["updated_by_id"] = actor_id

        workflow = await self.repository.create_workflow(values)

        if payload.steps:
            step_values = []
            for step_payload in payload.steps:
                step_dict = step_payload.model_dump(exclude={"workflow_id"})
                step_dict["workflow_id"] = workflow.id
                step_dict["created_by_id"] = actor_id
                step_dict["updated_by_id"] = actor_id
                step_values.append(step_dict)
            await self.repository.bulk_create_steps(step_values)

        result = await self.repository.get_workflow_with_steps(workflow.id)
        assert result is not None
        return result

    async def get_workflow(
        self, workflow_id: uuid.UUID, include_deleted: bool = False
    ) -> Workflow:
        workflow = await self.repository.get_workflow_by_id(
            workflow_id, include_deleted=include_deleted
        )
        if workflow is None:
            raise NotFoundException("Workflow", workflow_id)
        return workflow

    async def get_workflow_with_steps(
        self, workflow_id: uuid.UUID, include_deleted: bool = False
    ) -> Workflow:
        workflow = await self.repository.get_workflow_with_steps(
            workflow_id, include_deleted=include_deleted
        )
        if workflow is None:
            raise NotFoundException("Workflow", workflow_id)
        return workflow

    async def update_workflow(
        self,
        workflow_id: uuid.UUID,
        payload: WorkflowUpdate,
        actor_id: Optional[int] = None,
    ) -> Workflow:
        workflow = await self.get_workflow(workflow_id)
        self._ensure_not_deleted(workflow)

        update_data = payload.model_dump(exclude_unset=True)

        if "status" in update_data and update_data["status"] is not None:
            target_status = WorkflowStatus(update_data["status"])
            self._validate_workflow_transition(workflow.status, target_status)
            self._apply_status_side_effects(update_data, target_status)

        if "meta_data" in update_data:
            update_data["metadata"] = update_data.pop("meta_data")
        if "priority" in update_data and update_data["priority"] is not None:
            update_data["priority"] = update_data["priority"].value

        update_data["updated_by_id"] = actor_id
        return await self.repository.update_workflow(workflow, update_data)

    def _apply_status_side_effects(
        self, update_data: dict[str, Any], target_status: WorkflowStatus
    ) -> None:
        now = datetime.now(timezone.utc)
        if target_status == WorkflowStatus.CANCELLED:
            update_data.setdefault("cancelled_at", now)
        if target_status == WorkflowStatus.COMPLETED:
            update_data.setdefault("completed_at", now)
        if target_status in (WorkflowStatus.ACTIVE, WorkflowStatus.IN_PROGRESS):
            update_data.setdefault("started_at", now)

    async def delete_workflow(
        self, workflow_id: uuid.UUID, actor_id: Optional[int] = None
    ) -> Workflow:
        workflow = await self.get_workflow(workflow_id)
        if workflow.is_deleted:
            raise ConflictException(f"Workflow '{workflow_id}' is already deleted.")
        workflow.updated_by_id = actor_id
        return await self.repository.soft_delete_workflow(workflow)

    async def restore_workflow(
        self, workflow_id: uuid.UUID, actor_id: Optional[int] = None
    ) -> Workflow:
        workflow = await self.repository.get_workflow_by_id(
            workflow_id, include_deleted=True
        )
        if workflow is None:
            raise NotFoundException("Workflow", workflow_id)
        if not workflow.is_deleted:
            raise ConflictException(f"Workflow '{workflow_id}' is not deleted.")
        workflow.updated_by_id = actor_id
        return await self.repository.restore_workflow(workflow)

    # ------------------------------------------------------------------
    # Workflow: search / list / pagination / sorting / filtering
    # ------------------------------------------------------------------
    async def list_workflows(
        self,
        filters: Optional[WorkflowFilterParams] = None,
        pagination: Optional[PaginationParams] = None,
        sort: Optional[SortParams] = None,
    ) -> PageResult:
        filters = filters or WorkflowFilterParams()
        pagination = pagination or PaginationParams()
        sort = sort or SortParams()

        if pagination.page < 1:
            raise ValidationException("page must be >= 1.")
        if not (1 <= pagination.page_size <= 200):
            raise ValidationException("page_size must be between 1 and 200.")

        return await self.repository.list_workflows(filters, pagination, sort)

    async def search_workflows(
        self,
        search_term: str,
        filters: Optional[WorkflowFilterParams] = None,
        pagination: Optional[PaginationParams] = None,
        sort: Optional[SortParams] = None,
    ) -> PageResult:
        if not search_term or not search_term.strip():
            raise ValidationException("search_term must not be blank.")

        filters = filters or WorkflowFilterParams()
        pagination = pagination or PaginationParams()
        sort = sort or SortParams()

        return await self.repository.search_workflows(
            search_term.strip(), filters, pagination, sort
        )

    # ------------------------------------------------------------------
    # Workflow: statistics
    # ------------------------------------------------------------------
    async def get_statistics(
        self, filters: Optional[WorkflowFilterParams] = None
    ) -> dict[str, Any]:
        filters = filters or WorkflowFilterParams()

        total = await self.repository.get_total_count(filters)
        by_status = await self.repository.get_status_counts(filters)
        by_priority = await self.repository.get_priority_counts(filters)
        overdue = await self.repository.get_overdue_count(filters)
        avg_completion_seconds = await self.repository.get_average_completion_seconds(
            filters
        )

        return {
            "total": total,
            "by_status": by_status,
            "by_priority": by_priority,
            "overdue_count": overdue,
            "average_completion_seconds": avg_completion_seconds,
            "average_completion_hours": (
                round(avg_completion_seconds / 3600, 2)
                if avg_completion_seconds is not None
                else None
            ),
        }

    async def get_workflow_step_statistics(
        self, workflow_id: uuid.UUID
    ) -> dict[str, int]:
        await self.get_workflow(workflow_id)
        return await self.repository.count_steps_by_status(workflow_id)

    async def get_approval_statistics(
        self, workflow_id: Optional[uuid.UUID] = None
    ) -> dict[str, int]:
        if workflow_id is not None:
            await self.get_workflow(workflow_id)
        return await self.repository.get_approval_counts_by_status(workflow_id)

    # ------------------------------------------------------------------
    # Workflow: history
    # ------------------------------------------------------------------
    async def get_workflow_history(
        self, workflow_id: uuid.UUID
    ) -> list[dict[str, Any]]:
        await self.get_workflow(workflow_id)
        return await self.repository.get_workflow_history(workflow_id)

    # ------------------------------------------------------------------
    # Workflow execution
    # ------------------------------------------------------------------
    async def start_workflow(
        self, workflow_id: uuid.UUID, actor_id: Optional[int] = None
    ) -> Workflow:
        workflow = await self.get_workflow_with_steps(workflow_id)
        self._ensure_not_deleted(workflow)

        if not workflow.steps:
            raise BusinessRuleViolationException(
                "Cannot start a workflow that has no steps defined."
            )

        self._validate_workflow_transition(workflow.status, WorkflowStatus.ACTIVE)

        now = datetime.now(timezone.utc)
        workflow = await self.repository.update_workflow(
            workflow,
            {
                "status": WorkflowStatus.ACTIVE,
                "started_at": now,
                "current_step_order": workflow.steps[0].step_order,
                "updated_by_id": actor_id,
            },
        )

        first_step = workflow.steps[0]
        self._validate_step_transition(
            first_step.status, WorkflowStepStatus.IN_PROGRESS
        )
        await self.repository.update_step(
            first_step,
            {
                "status": WorkflowStepStatus.IN_PROGRESS,
                "started_at": now,
                "updated_by_id": actor_id,
            },
        )

        return await self.repository.update_workflow(
            workflow, {"status": WorkflowStatus.IN_PROGRESS}
        )

    async def complete_step(
        self,
        workflow_id: uuid.UUID,
        step_id: uuid.UUID,
        output_data: Optional[dict[str, Any]] = None,
        actor_id: Optional[int] = None,
    ) -> WorkflowStep:
        workflow = await self.get_workflow(workflow_id)
        self._ensure_not_deleted(workflow)

        step = await self.repository.get_step_by_id(step_id)
        if step is None or step.workflow_id != workflow.id:
            raise NotFoundException("WorkflowStep", step_id)

        if step.is_approval_required:
            if await self.repository.has_pending_approvals(step.id):
                raise BusinessRuleViolationException(
                    f"WorkflowStep '{step.id}' has pending approvals and "
                    "cannot be completed until they are resolved."
                )

        self._validate_step_transition(step.status, WorkflowStepStatus.COMPLETED)

        now = datetime.now(timezone.utc)
        step = await self.repository.update_step(
            step,
            {
                "status": WorkflowStepStatus.COMPLETED,
                "completed_at": now,
                "output_data": output_data,
                "updated_by_id": actor_id,
            },
        )

        await self._advance_workflow(workflow, actor_id=actor_id)
        return step

    async def _advance_workflow(
        self, workflow: Workflow, actor_id: Optional[int] = None
    ) -> Workflow:
        """Move the workflow pointer to the next pending step, or mark
        the workflow COMPLETED if none remain."""
        next_step = await self.repository.get_next_pending_step(workflow.id)
        now = datetime.now(timezone.utc)

        if next_step is None:
            self._validate_workflow_transition(
                workflow.status, WorkflowStatus.COMPLETED
            )
            return await self.repository.update_workflow(
                workflow,
                {
                    "status": WorkflowStatus.COMPLETED,
                    "completed_at": now,
                    "updated_by_id": actor_id,
                },
            )

        self._validate_step_transition(
            next_step.status, WorkflowStepStatus.IN_PROGRESS
        )
        await self.repository.update_step(
            next_step,
            {
                "status": WorkflowStepStatus.IN_PROGRESS,
                "started_at": now,
                "updated_by_id": actor_id,
            },
        )
        return await self.repository.update_workflow(
            workflow,
            {"current_step_order": next_step.step_order, "updated_by_id": actor_id},
        )

    async def fail_step(
        self,
        workflow_id: uuid.UUID,
        step_id: uuid.UUID,
        reason: Optional[str] = None,
        actor_id: Optional[int] = None,
    ) -> WorkflowStep:
        workflow = await self.get_workflow(workflow_id)
        self._ensure_not_deleted(workflow)

        step = await self.repository.get_step_by_id(step_id)
        if step is None or step.workflow_id != workflow.id:
            raise NotFoundException("WorkflowStep", step_id)

        self._validate_step_transition(step.status, WorkflowStepStatus.FAILED)
        step = await self.repository.update_step(
            step,
            {
                "status": WorkflowStepStatus.FAILED,
                "retry_count": step.retry_count,
                "output_data": {"error": reason} if reason else step.output_data,
                "updated_by_id": actor_id,
            },
        )

        self._validate_workflow_transition(workflow.status, WorkflowStatus.FAILED)
        await self.repository.update_workflow(
            workflow, {"status": WorkflowStatus.FAILED, "updated_by_id": actor_id}
        )
        return step

    async def retry_step(
        self,
        workflow_id: uuid.UUID,
        step_id: uuid.UUID,
        actor_id: Optional[int] = None,
    ) -> WorkflowStep:
        workflow = await self.get_workflow(workflow_id)
        self._ensure_not_deleted(workflow)

        step = await self.repository.get_step_by_id(step_id)
        if step is None or step.workflow_id != workflow.id:
            raise NotFoundException("WorkflowStep", step_id)

        self._validate_step_transition(step.status, WorkflowStepStatus.IN_PROGRESS)
        step = await self.repository.update_step(
            step,
            {
                "status": WorkflowStepStatus.IN_PROGRESS,
                "retry_count": step.retry_count + 1,
                "started_at": datetime.now(timezone.utc),
                "completed_at": None,
                "updated_by_id": actor_id,
            },
        )

        if workflow.status == WorkflowStatus.FAILED:
            self._validate_workflow_transition(
                workflow.status, WorkflowStatus.IN_PROGRESS
            )
            await self.repository.update_workflow(
                workflow,
                {"status": WorkflowStatus.IN_PROGRESS, "updated_by_id": actor_id},
            )
        return step

    async def skip_step(
        self,
        workflow_id: uuid.UUID,
        step_id: uuid.UUID,
        actor_id: Optional[int] = None,
    ) -> WorkflowStep:
        workflow = await self.get_workflow(workflow_id)
        self._ensure_not_deleted(workflow)

        step = await self.repository.get_step_by_id(step_id)
        if step is None or step.workflow_id != workflow.id:
            raise NotFoundException("WorkflowStep", step_id)

        self._validate_step_transition(step.status, WorkflowStepStatus.SKIPPED)
        step = await self.repository.update_step(
            step,
            {
                "status": WorkflowStepStatus.SKIPPED,
                "completed_at": datetime.now(timezone.utc),
                "updated_by_id": actor_id,
            },
        )
        await self._advance_workflow(workflow, actor_id=actor_id)
        return step

    async def hold_workflow(
        self, workflow_id: uuid.UUID, actor_id: Optional[int] = None
    ) -> Workflow:
        workflow = await self.get_workflow(workflow_id)
        self._ensure_not_deleted(workflow)
        self._validate_workflow_transition(workflow.status, WorkflowStatus.ON_HOLD)
        return await self.repository.update_workflow(
            workflow, {"status": WorkflowStatus.ON_HOLD, "updated_by_id": actor_id}
        )

    async def resume_workflow(
        self, workflow_id: uuid.UUID, actor_id: Optional[int] = None
    ) -> Workflow:
        workflow = await self.get_workflow(workflow_id)
        self._ensure_not_deleted(workflow)
        target = (
            WorkflowStatus.IN_PROGRESS
            if workflow.current_step_order
            else WorkflowStatus.ACTIVE
        )
        self._validate_workflow_transition(workflow.status, target)
        return await self.repository.update_workflow(
            workflow, {"status": target, "updated_by_id": actor_id}
        )

    async def cancel_workflow(
        self,
        workflow_id: uuid.UUID,
        reason: str,
        actor_id: Optional[int] = None,
    ) -> Workflow:
        if not reason or not reason.strip():
            raise ValidationException(
                "cancellation_reason is required to cancel a workflow."
            )

        workflow = await self.get_workflow(workflow_id)
        self._ensure_not_deleted(workflow)
        self._validate_workflow_transition(workflow.status, WorkflowStatus.CANCELLED)

        return await self.repository.update_workflow(
            workflow,
            {
                "status": WorkflowStatus.CANCELLED,
                "cancelled_at": datetime.now(timezone.utc),
                "cancellation_reason": reason.strip(),
                "updated_by_id": actor_id,
            },
        )

    # ------------------------------------------------------------------
    # WorkflowStep: standalone CRUD (outside execution flow)
    # ------------------------------------------------------------------
    async def add_step(
        self,
        payload: WorkflowStepCreate,
        actor_id: Optional[int] = None,
    ) -> WorkflowStep:
        workflow = await self.get_workflow(payload.workflow_id)
        self._ensure_not_deleted(workflow)
        self._validate_step_payload(workflow, payload)

        existing = await self.repository.get_step_by_order(
            workflow.id, payload.step_order
        )
        if existing is not None:
            raise ConflictException(
                f"A step with step_order={payload.step_order} already exists "
                f"for workflow '{workflow.id}'."
            )

        values = payload.model_dump()
        values["created_by_id"] = actor_id
        values["updated_by_id"] = actor_id
        return await self.repository.create_step(values)

    async def get_step(self, step_id: uuid.UUID) -> WorkflowStep:
        step = await self.repository.get_step_by_id(step_id)
        if step is None:
            raise NotFoundException("WorkflowStep", step_id)
        return step

    async def list_workflow_steps(
        self, workflow_id: uuid.UUID
    ) -> list[WorkflowStep]:
        await self.get_workflow(workflow_id)
        return await self.repository.list_steps_by_workflow(workflow_id)

    async def update_step_details(
        self,
        step_id: uuid.UUID,
        payload: WorkflowStepUpdate,
        actor_id: Optional[int] = None,
    ) -> WorkflowStep:
        step = await self.get_step(step_id)
        update_data = payload.model_dump(exclude_unset=True)

        if "status" in update_data and update_data["status"] is not None:
            target_status = WorkflowStepStatus(update_data["status"])
            self._validate_step_transition(step.status, target_status)

        update_data["updated_by_id"] = actor_id
        return await self.repository.update_step(step, update_data)

    async def remove_step(
        self, step_id: uuid.UUID, actor_id: Optional[int] = None
    ) -> None:
        step = await self.get_step(step_id)
        workflow = await self.get_workflow(step.workflow_id)
        if workflow.status not in (WorkflowStatus.DRAFT,):
            raise BusinessRuleViolationException(
                "Steps can only be removed while the workflow is in "
                "'draft' status."
            )
        await self.repository.delete_step(step)

    # ------------------------------------------------------------------
    # Approval flow
    # ------------------------------------------------------------------
    async def request_approval(
        self,
        workflow_id: uuid.UUID,
        step_id: uuid.UUID,
        approver_id: int,
        actor_id: Optional[int] = None,
    ) -> WorkflowApproval:
        workflow = await self.get_workflow(workflow_id)
        self._ensure_not_deleted(workflow)

        step = await self.repository.get_step_by_id(step_id)
        if step is None or step.workflow_id != workflow.id:
            raise NotFoundException("WorkflowStep", step_id)

        if not step.is_approval_required:
            raise BusinessRuleViolationException(
                f"WorkflowStep '{step.id}' does not require approval."
            )
        if step.status not in (
            WorkflowStepStatus.PENDING,
            WorkflowStepStatus.IN_PROGRESS,
            WorkflowStepStatus.BLOCKED,
        ):
            raise BusinessRuleViolationException(
                f"Cannot request approval for a step in status "
                f"'{step.status.value}'."
            )
        if approver_id <= 0:
            raise ValidationException("approver_id must be a positive integer.")

        values = {
            "workflow_step_id": step.id,
            "workflow_id": workflow.id,
            "approver_id": approver_id,
        }
        approval = await self.repository.create_approval(values)

        if step.status == WorkflowStepStatus.PENDING:
            self._validate_step_transition(step.status, WorkflowStepStatus.BLOCKED)
            await self.repository.update_step(
                step,
                {"status": WorkflowStepStatus.BLOCKED, "updated_by_id": actor_id},
            )
        return approval

    async def get_approval(self, approval_id: uuid.UUID) -> WorkflowApproval:
        approval = await self.repository.get_approval_by_id(approval_id)
        if approval is None:
            raise NotFoundException("WorkflowApproval", approval_id)
        return approval

    async def list_step_approvals(
        self, step_id: uuid.UUID
    ) -> list[WorkflowApproval]:
        await self.get_step(step_id)
        return await self.repository.list_approvals_by_step(step_id)

    async def list_pending_approvals_for_approver(
        self, approver_id: int, pagination: Optional[PaginationParams] = None
    ) -> PageResult:
        if approver_id <= 0:
            raise ValidationException("approver_id must be a positive integer.")
        pagination = pagination or PaginationParams()
        return await self.repository.list_pending_approvals_for_approver(
            approver_id, pagination
        )

    async def _unblock_step_if_resolved(
        self, step: WorkflowStep, actor_id: Optional[int]
    ) -> None:
        if step.status != WorkflowStepStatus.BLOCKED:
            return
        if await self.repository.has_pending_approvals(step.id):
            return
        self._validate_step_transition(step.status, WorkflowStepStatus.IN_PROGRESS)
        await self.repository.update_step(
            step,
            {
                "status": WorkflowStepStatus.IN_PROGRESS,
                "started_at": step.started_at or datetime.now(timezone.utc),
                "updated_by_id": actor_id,
            },
        )

    async def decide_approval(
        self,
        approval_id: uuid.UUID,
        payload: WorkflowApprovalUpdate,
        actor_id: Optional[int] = None,
    ) -> WorkflowApproval:
        approval = await self.get_approval(approval_id)

        if payload.status is None:
            raise ValidationException("status is required to decide an approval.")

        target_status = ApprovalStatus(payload.status)
        self._validate_approval_transition(approval.status, target_status)

        update_data: dict[str, Any] = {
            "status": target_status,
            "decision_notes": payload.decision_notes,
        }

        if target_status in (ApprovalStatus.APPROVED, ApprovalStatus.REJECTED):
            update_data["decided_at"] = datetime.now(timezone.utc)

        if target_status == ApprovalStatus.ESCALATED:
            if payload.escalated_to_id is None:
                raise ValidationException(
                    "escalated_to_id is required when escalating an approval."
                )
            update_data["escalated"] = True
            update_data["escalated_to_id"] = payload.escalated_to_id

        approval = await self.repository.update_approval(approval, update_data)

        step = await self.repository.get_step_by_id(approval.workflow_step_id)
        if step is not None:
            if target_status == ApprovalStatus.APPROVED:
                await self._unblock_step_if_resolved(step, actor_id)
            elif target_status == ApprovalStatus.REJECTED:
                self._validate_step_transition(
                    step.status, WorkflowStepStatus.FAILED
                )
                await self.repository.update_step(
                    step,
                    {
                        "status": WorkflowStepStatus.FAILED,
                        "output_data": {
                            "rejection_reason": payload.decision_notes
                        },
                        "updated_by_id": actor_id,
                    },
                )
                workflow = await self.get_workflow(approval.workflow_id)
                self._validate_workflow_transition(
                    workflow.status, WorkflowStatus.FAILED
                )
                await self.repository.update_workflow(
                    workflow,
                    {"status": WorkflowStatus.FAILED, "updated_by_id": actor_id},
                )

        return approval

    async def approve(
        self,
        approval_id: uuid.UUID,
        decision_notes: Optional[str] = None,
        actor_id: Optional[int] = None,
    ) -> WorkflowApproval:
        payload = WorkflowApprovalUpdate(
            status=ApprovalStatus.APPROVED, decision_notes=decision_notes
        )
        return await self.decide_approval(approval_id, payload, actor_id)

    async def reject(
        self,
        approval_id: uuid.UUID,
        decision_notes: Optional[str] = None,
        actor_id: Optional[int] = None,
    ) -> WorkflowApproval:
        payload = WorkflowApprovalUpdate(
            status=ApprovalStatus.REJECTED, decision_notes=decision_notes
        )
        return await self.decide_approval(approval_id, payload, actor_id)

    async def escalate_approval(
        self,
        approval_id: uuid.UUID,
        escalated_to_id: int,
        decision_notes: Optional[str] = None,
        actor_id: Optional[int] = None,
    ) -> WorkflowApproval:
        if escalated_to_id is None:
            raise ValidationException(
                "escalated_to_id is required when escalating an approval."
            )
        payload = WorkflowApprovalUpdate(
            status=ApprovalStatus.ESCALATED,
            decision_notes=decision_notes,
            escalated=True,
            escalated_to_id=escalated_to_id,
        )
        return await self.decide_approval(approval_id, payload, actor_id)

    async def cancel_approval(
        self,
        approval_id: uuid.UUID,
        decision_notes: Optional[str] = None,
        actor_id: Optional[int] = None,
    ) -> WorkflowApproval:
        payload = WorkflowApprovalUpdate(
            status=ApprovalStatus.CANCELLED, decision_notes=decision_notes
        )
        approval = await self.decide_approval(approval_id, payload, actor_id)

        step = await self.repository.get_step_by_id(approval.workflow_step_id)
        if step is not None:
            await self._unblock_step_if_resolved(step, actor_id)
        return approval