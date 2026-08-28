"""
backend/app/utils/workflow_engine.py

Workflow Engine + Approval Engine for the Workflow module.

Two cooperating engines, both operating purely through
`WorkflowRepository` (no direct DB/session access) and validated by
`WorkflowValidator`:

    WorkflowEngine
        - Workflow / WorkflowStep CRUD orchestration.
        - Workflow execution (start / advance / complete / fail /
          retry / skip / hold / resume / cancel).
        - Assignment (workflow-level and step-level).
        - Comments (workflow-level and step-level), persisted inside
          the existing JSONB columns (`workflows.metadata` and
          `workflow_steps.input_data`) under a `"comments"` key, since
          the Workflow module's schema has no dedicated comment table.
        - History (delegates to the repository's merged step/approval
          timeline).

    ApprovalEngine
        - Approval flow orchestration (request / approve / reject /
          escalate / cancel) and the side effects each decision has on
          the gated WorkflowStep and its parent Workflow.

Only project domain exceptions are raised (see `app.core.exceptions`):

    NotFoundException(message: str)
    ConflictException(message: str)
    BusinessRuleException(message: str)
    ValidationException(message: str)
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Any, Optional

from app.core.exceptions import (
    BusinessRuleException,
    ConflictException,
    NotFoundException,
)
from app.models.workflow import (
    ApprovalStatus,
    Workflow,
    WorkflowApproval,
    WorkflowStep,
    WorkflowStepStatus,
    WorkflowStatus,
)
from app.repositories.workflow_repository import WorkflowRepository
from app.utils.workflow_validator import WorkflowValidator


class WorkflowEngine:
    """Orchestrates Workflow / WorkflowStep execution, assignment,
    comments, and history."""

    def __init__(
        self,
        repository: WorkflowRepository,
        validator: Optional[WorkflowValidator] = None,
    ) -> None:
        self.repository = repository
        self.validator = validator or WorkflowValidator()

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------
    async def _get_workflow_or_raise(
        self, workflow_id: uuid.UUID, include_deleted: bool = False
    ) -> Workflow:
        workflow = await self.repository.get_workflow_by_id(
            workflow_id, include_deleted=include_deleted
        )
        if workflow is None:
            raise NotFoundException(f"Workflow '{workflow_id}' was not found.")
        return workflow

    async def _get_step_or_raise(
        self, workflow_id: uuid.UUID, step_id: uuid.UUID
    ) -> WorkflowStep:
        step = await self.repository.get_step_by_id(step_id)
        if step is None or step.workflow_id != workflow_id:
            raise NotFoundException(f"WorkflowStep '{step_id}' was not found.")
        return step

    def _ensure_not_deleted(self, workflow: Workflow) -> None:
        if workflow.is_deleted:
            raise ConflictException(
                f"Workflow '{workflow.id}' has been deleted and cannot be modified."
            )

    # ------------------------------------------------------------------
    # Workflow execution
    # ------------------------------------------------------------------
    async def start_workflow(
        self, workflow_id: uuid.UUID, actor_id: Optional[int] = None
    ) -> Workflow:
        workflow = await self.repository.get_workflow_with_steps(workflow_id)
        if workflow is None:
            raise NotFoundException(f"Workflow '{workflow_id}' was not found.")
        self._ensure_not_deleted(workflow)

        if not workflow.steps:
            raise BusinessRuleException(
                "Cannot start a workflow that has no steps defined."
            )

        self.validator.validate_workflow_transition(
            workflow.status, WorkflowStatus.ACTIVE
        )

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
        self.validator.validate_step_transition(
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

    async def _advance(
        self, workflow: Workflow, actor_id: Optional[int] = None
    ) -> Workflow:
        next_step = await self.repository.get_next_pending_step(workflow.id)
        now = datetime.now(timezone.utc)

        if next_step is None:
            self.validator.validate_workflow_transition(
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

        self.validator.validate_step_transition(
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

    async def complete_step(
        self,
        workflow_id: uuid.UUID,
        step_id: uuid.UUID,
        output_data: Optional[dict[str, Any]] = None,
        actor_id: Optional[int] = None,
    ) -> WorkflowStep:
        workflow = await self._get_workflow_or_raise(workflow_id)
        self._ensure_not_deleted(workflow)
        step = await self._get_step_or_raise(workflow_id, step_id)

        if step.is_approval_required and await self.repository.has_pending_approvals(
            step.id
        ):
            raise BusinessRuleException(
                f"WorkflowStep '{step.id}' has pending approvals and cannot be "
                "completed until they are resolved."
            )

        self.validator.validate_step_transition(
            step.status, WorkflowStepStatus.COMPLETED
        )
        now = datetime.now(timezone.utc)
        merged_output = dict(step.output_data or {})
        if output_data:
            merged_output.update(output_data)

        step = await self.repository.update_step(
            step,
            {
                "status": WorkflowStepStatus.COMPLETED,
                "completed_at": now,
                "output_data": merged_output or None,
                "updated_by_id": actor_id,
            },
        )
        await self._advance(workflow, actor_id=actor_id)
        return step

    async def fail_step(
        self,
        workflow_id: uuid.UUID,
        step_id: uuid.UUID,
        reason: Optional[str] = None,
        actor_id: Optional[int] = None,
    ) -> WorkflowStep:
        workflow = await self._get_workflow_or_raise(workflow_id)
        self._ensure_not_deleted(workflow)
        step = await self._get_step_or_raise(workflow_id, step_id)

        self.validator.validate_step_transition(step.status, WorkflowStepStatus.FAILED)
        merged_output = dict(step.output_data or {})
        if reason:
            merged_output["error"] = reason

        step = await self.repository.update_step(
            step,
            {
                "status": WorkflowStepStatus.FAILED,
                "output_data": merged_output or None,
                "updated_by_id": actor_id,
            },
        )

        self.validator.validate_workflow_transition(
            workflow.status, WorkflowStatus.FAILED
        )
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
        workflow = await self._get_workflow_or_raise(workflow_id)
        self._ensure_not_deleted(workflow)
        step = await self._get_step_or_raise(workflow_id, step_id)

        self.validator.validate_step_transition(
            step.status, WorkflowStepStatus.IN_PROGRESS
        )
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
            self.validator.validate_workflow_transition(
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
        workflow = await self._get_workflow_or_raise(workflow_id)
        self._ensure_not_deleted(workflow)
        step = await self._get_step_or_raise(workflow_id, step_id)

        self.validator.validate_step_transition(
            step.status, WorkflowStepStatus.SKIPPED
        )
        step = await self.repository.update_step(
            step,
            {
                "status": WorkflowStepStatus.SKIPPED,
                "completed_at": datetime.now(timezone.utc),
                "updated_by_id": actor_id,
            },
        )
        await self._advance(workflow, actor_id=actor_id)
        return step

    async def hold_workflow(
        self, workflow_id: uuid.UUID, actor_id: Optional[int] = None
    ) -> Workflow:
        workflow = await self._get_workflow_or_raise(workflow_id)
        self._ensure_not_deleted(workflow)
        self.validator.validate_workflow_transition(
            workflow.status, WorkflowStatus.ON_HOLD
        )
        return await self.repository.update_workflow(
            workflow, {"status": WorkflowStatus.ON_HOLD, "updated_by_id": actor_id}
        )

    async def resume_workflow(
        self, workflow_id: uuid.UUID, actor_id: Optional[int] = None
    ) -> Workflow:
        workflow = await self._get_workflow_or_raise(workflow_id)
        self._ensure_not_deleted(workflow)
        target = (
            WorkflowStatus.IN_PROGRESS
            if workflow.current_step_order
            else WorkflowStatus.ACTIVE
        )
        self.validator.validate_workflow_transition(workflow.status, target)
        return await self.repository.update_workflow(
            workflow, {"status": target, "updated_by_id": actor_id}
        )

    async def cancel_workflow(
        self,
        workflow_id: uuid.UUID,
        reason: str,
        actor_id: Optional[int] = None,
    ) -> Workflow:
        self.validator.validate_cancellation_reason(reason)
        workflow = await self._get_workflow_or_raise(workflow_id)
        self._ensure_not_deleted(workflow)
        self.validator.validate_workflow_transition(
            workflow.status, WorkflowStatus.CANCELLED
        )
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
    # Assignment
    # ------------------------------------------------------------------
    async def assign_workflow(
        self,
        workflow_id: uuid.UUID,
        assignee_id: int,
        actor_id: Optional[int] = None,
    ) -> Workflow:
        self.validator.validate_assignee_id(assignee_id)
        workflow = await self._get_workflow_or_raise(workflow_id)
        self._ensure_not_deleted(workflow)
        return await self.repository.update_workflow(
            workflow, {"assigned_to_id": assignee_id, "updated_by_id": actor_id}
        )

    async def assign_step(
        self,
        workflow_id: uuid.UUID,
        step_id: uuid.UUID,
        assignee_id: int,
        actor_id: Optional[int] = None,
    ) -> WorkflowStep:
        self.validator.validate_assignee_id(assignee_id)
        workflow = await self._get_workflow_or_raise(workflow_id)
        self._ensure_not_deleted(workflow)
        step = await self._get_step_or_raise(workflow_id, step_id)
        return await self.repository.update_step(
            step, {"assigned_to_id": assignee_id, "updated_by_id": actor_id}
        )

    # ------------------------------------------------------------------
    # Comments (stored inside existing JSONB columns; no dedicated table
    # exists in the Workflow module's schema)
    # ------------------------------------------------------------------
    @staticmethod
    def _new_comment(author_id: int, message: str) -> dict[str, Any]:
        return {
            "id": str(uuid.uuid4()),
            "author_id": author_id,
            "message": message.strip(),
            "created_at": datetime.now(timezone.utc).isoformat(),
        }

    async def add_workflow_comment(
        self, workflow_id: uuid.UUID, author_id: int, message: str
    ) -> dict[str, Any]:
        self.validator.validate_comment_text(message)
        workflow = await self._get_workflow_or_raise(workflow_id)
        self._ensure_not_deleted(workflow)

        meta = dict(workflow.meta_data or {})
        comments: list[dict[str, Any]] = list(meta.get("comments", []))
        comment = self._new_comment(author_id, message)
        comments.append(comment)
        meta["comments"] = comments

        await self.repository.update_workflow(workflow, {"meta_data": meta})
        return comment

    async def list_workflow_comments(
        self, workflow_id: uuid.UUID
    ) -> list[dict[str, Any]]:
        workflow = await self._get_workflow_or_raise(workflow_id)
        meta = workflow.meta_data or {}
        return list(meta.get("comments", []))

    async def add_step_comment(
        self,
        workflow_id: uuid.UUID,
        step_id: uuid.UUID,
        author_id: int,
        message: str,
    ) -> dict[str, Any]:
        self.validator.validate_comment_text(message)
        await self._get_workflow_or_raise(workflow_id)
        step = await self._get_step_or_raise(workflow_id, step_id)

        payload = dict(step.input_data or {})
        comments: list[dict[str, Any]] = list(payload.get("comments", []))
        comment = self._new_comment(author_id, message)
        comments.append(comment)
        payload["comments"] = comments

        await self.repository.update_step(step, {"input_data": payload})
        return comment

    async def list_step_comments(
        self, workflow_id: uuid.UUID, step_id: uuid.UUID
    ) -> list[dict[str, Any]]:
        step = await self._get_step_or_raise(workflow_id, step_id)
        payload = step.input_data or {}
        return list(payload.get("comments", []))

    # ------------------------------------------------------------------
    # History
    # ------------------------------------------------------------------
    async def get_history(self, workflow_id: uuid.UUID) -> list[dict[str, Any]]:
        await self._get_workflow_or_raise(workflow_id)
        return await self.repository.get_workflow_history(workflow_id)


class ApprovalEngine:
    """Orchestrates the approval flow (request / approve / reject /
    escalate / cancel) and its side effects on the gated step and
    parent workflow."""

    def __init__(
        self,
        repository: WorkflowRepository,
        validator: Optional[WorkflowValidator] = None,
        workflow_engine: Optional[WorkflowEngine] = None,
    ) -> None:
        self.repository = repository
        self.validator = validator or WorkflowValidator()
        self.workflow_engine = workflow_engine or WorkflowEngine(repository, self.validator)

    async def _get_approval_or_raise(
        self, approval_id: uuid.UUID
    ) -> WorkflowApproval:
        approval = await self.repository.get_approval_by_id(approval_id)
        if approval is None:
            raise NotFoundException(f"WorkflowApproval '{approval_id}' was not found.")
        return approval

    async def request_approval(
        self,
        workflow_id: uuid.UUID,
        step_id: uuid.UUID,
        approver_id: int,
        actor_id: Optional[int] = None,
    ) -> WorkflowApproval:
        self.validator.validate_approver_id(approver_id)

        workflow = await self.workflow_engine._get_workflow_or_raise(workflow_id)
        self.workflow_engine._ensure_not_deleted(workflow)
        step = await self.workflow_engine._get_step_or_raise(workflow_id, step_id)

        self.validator.validate_approval_requestable(
            step.status, step.is_approval_required
        )

        approval = await self.repository.create_approval(
            {
                "workflow_step_id": step.id,
                "workflow_id": workflow.id,
                "approver_id": approver_id,
            }
        )

        if step.status == WorkflowStepStatus.PENDING:
            self.validator.validate_step_transition(
                step.status, WorkflowStepStatus.BLOCKED
            )
            await self.repository.update_step(
                step,
                {"status": WorkflowStepStatus.BLOCKED, "updated_by_id": actor_id},
            )
        return approval

    async def _unblock_step_if_resolved(
        self, step: WorkflowStep, actor_id: Optional[int]
    ) -> None:
        if step.status != WorkflowStepStatus.BLOCKED:
            return
        if await self.repository.has_pending_approvals(step.id):
            return
        self.validator.validate_step_transition(
            step.status, WorkflowStepStatus.IN_PROGRESS
        )
        await self.repository.update_step(
            step,
            {
                "status": WorkflowStepStatus.IN_PROGRESS,
                "started_at": step.started_at or datetime.now(timezone.utc),
                "updated_by_id": actor_id,
            },
        )

    async def _decide(
        self,
        approval_id: uuid.UUID,
        target_status: ApprovalStatus,
        decision_notes: Optional[str] = None,
        escalated_to_id: Optional[int] = None,
        actor_id: Optional[int] = None,
    ) -> WorkflowApproval:
        approval = await self._get_approval_or_raise(approval_id)
        self.validator.validate_approval_transition(approval.status, target_status)

        update_data: dict[str, Any] = {
            "status": target_status,
            "decision_notes": decision_notes,
        }

        if target_status in (ApprovalStatus.APPROVED, ApprovalStatus.REJECTED):
            update_data["decided_at"] = datetime.now(timezone.utc)

        if target_status == ApprovalStatus.ESCALATED:
            self.validator.validate_escalation_target(escalated_to_id)
            update_data["escalated"] = True
            update_data["escalated_to_id"] = escalated_to_id

        approval = await self.repository.update_approval(approval, update_data)

        step = await self.repository.get_step_by_id(approval.workflow_step_id)
        if step is not None:
            if target_status == ApprovalStatus.APPROVED:
                await self._unblock_step_if_resolved(step, actor_id)
            elif target_status == ApprovalStatus.REJECTED:
                self.validator.validate_step_transition(
                    step.status, WorkflowStepStatus.FAILED
                )
                merged_output = dict(step.output_data or {})
                if decision_notes:
                    merged_output["rejection_reason"] = decision_notes
                await self.repository.update_step(
                    step,
                    {
                        "status": WorkflowStepStatus.FAILED,
                        "output_data": merged_output or None,
                        "updated_by_id": actor_id,
                    },
                )
                workflow = await self.workflow_engine._get_workflow_or_raise(
                    approval.workflow_id
                )
                self.validator.validate_workflow_transition(
                    workflow.status, WorkflowStatus.FAILED
                )
                await self.repository.update_workflow(
                    workflow,
                    {"status": WorkflowStatus.FAILED, "updated_by_id": actor_id},
                )
            elif target_status == ApprovalStatus.CANCELLED:
                await self._unblock_step_if_resolved(step, actor_id)

        return approval

    async def approve(
        self,
        approval_id: uuid.UUID,
        decision_notes: Optional[str] = None,
        actor_id: Optional[int] = None,
    ) -> WorkflowApproval:
        return await self._decide(
            approval_id, ApprovalStatus.APPROVED, decision_notes, actor_id=actor_id
        )

    async def reject(
        self,
        approval_id: uuid.UUID,
        decision_notes: Optional[str] = None,
        actor_id: Optional[int] = None,
    ) -> WorkflowApproval:
        return await self._decide(
            approval_id, ApprovalStatus.REJECTED, decision_notes, actor_id=actor_id
        )

    async def escalate(
        self,
        approval_id: uuid.UUID,
        escalated_to_id: int,
        decision_notes: Optional[str] = None,
        actor_id: Optional[int] = None,
    ) -> WorkflowApproval:
        return await self._decide(
            approval_id,
            ApprovalStatus.ESCALATED,
            decision_notes,
            escalated_to_id=escalated_to_id,
            actor_id=actor_id,
        )

    async def cancel(
        self,
        approval_id: uuid.UUID,
        decision_notes: Optional[str] = None,
        actor_id: Optional[int] = None,
    ) -> WorkflowApproval:
        return await self._decide(
            approval_id, ApprovalStatus.CANCELLED, decision_notes, actor_id=actor_id
        )

    async def list_step_approvals(
        self, step_id: uuid.UUID
    ) -> list[WorkflowApproval]:
        return await self.repository.list_approvals_by_step(step_id)

    async def list_pending_for_approver(
        self, approver_id: int, pagination
    ):
        self.validator.validate_approver_id(approver_id)
        return await self.repository.list_pending_approvals_for_approver(
            approver_id, pagination
        )