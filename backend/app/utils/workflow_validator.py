"""
backend/app/utils/workflow_validator.py

Validation Engine for the Workflow module.

Pure, side-effect-free validation logic shared by `workflow_engine.py`
and (optionally) the API layer. This module owns:
    - The three state machines (Workflow / WorkflowStep /
      WorkflowApproval) as data, so engine code never hard-codes
      "allowed next status" rules inline.
    - Input-shape validation (names, ids, pagination, sort fields,
      comment text, step ordering, etc).
    - Cross-field / business-rule validation that does not require a
      database round trip (DB-dependent checks such as "does a step
      with this order already exist" stay in the engine, which has
      repository access).

Only project domain exceptions are raised (see `app.core.exceptions`):

    ValidationException(message: str)
    BusinessRuleException(message: str)
"""

from __future__ import annotations

from typing import Iterable, Mapping, Optional, Sequence

from app.core.exceptions import BusinessRuleException, ValidationException
from app.models.workflow import ApprovalStatus, WorkflowStatus, WorkflowStepStatus

# ---------------------------------------------------------------------------
# State machines
# ---------------------------------------------------------------------------
WORKFLOW_TRANSITIONS: Mapping[WorkflowStatus, frozenset[WorkflowStatus]] = {
    WorkflowStatus.DRAFT: frozenset({WorkflowStatus.ACTIVE, WorkflowStatus.CANCELLED}),
    WorkflowStatus.ACTIVE: frozenset(
        {
            WorkflowStatus.IN_PROGRESS,
            WorkflowStatus.ON_HOLD,
            WorkflowStatus.CANCELLED,
            WorkflowStatus.FAILED,
        }
    ),
    WorkflowStatus.IN_PROGRESS: frozenset(
        {
            WorkflowStatus.ON_HOLD,
            WorkflowStatus.COMPLETED,
            WorkflowStatus.CANCELLED,
            WorkflowStatus.FAILED,
        }
    ),
    WorkflowStatus.ON_HOLD: frozenset(
        {
            WorkflowStatus.ACTIVE,
            WorkflowStatus.IN_PROGRESS,
            WorkflowStatus.CANCELLED,
            WorkflowStatus.FAILED,
        }
    ),
    WorkflowStatus.FAILED: frozenset(
        {WorkflowStatus.IN_PROGRESS, WorkflowStatus.CANCELLED}
    ),
    WorkflowStatus.COMPLETED: frozenset(),
    WorkflowStatus.CANCELLED: frozenset(),
}

STEP_TRANSITIONS: Mapping[WorkflowStepStatus, frozenset[WorkflowStepStatus]] = {
    WorkflowStepStatus.PENDING: frozenset(
        {
            WorkflowStepStatus.IN_PROGRESS,
            WorkflowStepStatus.SKIPPED,
            WorkflowStepStatus.BLOCKED,
        }
    ),
    WorkflowStepStatus.IN_PROGRESS: frozenset(
        {
            WorkflowStepStatus.COMPLETED,
            WorkflowStepStatus.FAILED,
            WorkflowStepStatus.BLOCKED,
        }
    ),
    WorkflowStepStatus.BLOCKED: frozenset(
        {
            WorkflowStepStatus.PENDING,
            WorkflowStepStatus.IN_PROGRESS,
            WorkflowStepStatus.FAILED,
        }
    ),
    WorkflowStepStatus.FAILED: frozenset(
        {WorkflowStepStatus.PENDING, WorkflowStepStatus.IN_PROGRESS}
    ),
    WorkflowStepStatus.COMPLETED: frozenset(),
    WorkflowStepStatus.SKIPPED: frozenset(),
}

APPROVAL_TRANSITIONS: Mapping[ApprovalStatus, frozenset[ApprovalStatus]] = {
    ApprovalStatus.PENDING: frozenset(
        {
            ApprovalStatus.APPROVED,
            ApprovalStatus.REJECTED,
            ApprovalStatus.ESCALATED,
            ApprovalStatus.CANCELLED,
        }
    ),
    ApprovalStatus.ESCALATED: frozenset(
        {ApprovalStatus.APPROVED, ApprovalStatus.REJECTED, ApprovalStatus.CANCELLED}
    ),
    ApprovalStatus.APPROVED: frozenset(),
    ApprovalStatus.REJECTED: frozenset(),
    ApprovalStatus.CANCELLED: frozenset(),
}

TERMINAL_WORKFLOW_STATUSES = frozenset({WorkflowStatus.COMPLETED, WorkflowStatus.CANCELLED})
TERMINAL_STEP_STATUSES = frozenset(
    {WorkflowStepStatus.COMPLETED, WorkflowStepStatus.SKIPPED}
)

_VALID_PRIORITIES = frozenset({"low", "normal", "high", "urgent"})
_SORTABLE_WORKFLOW_FIELDS = frozenset(
    {
        "created_at",
        "updated_at",
        "due_date",
        "priority",
        "status",
        "name",
        "started_at",
        "completed_at",
    }
)
_MAX_PAGE_SIZE = 200
_MAX_COMMENT_LENGTH = 5_000


class WorkflowValidator:
    """Stateless validation engine for the Workflow module."""

    # ------------------------------------------------------------------
    # State transitions
    # ------------------------------------------------------------------
    @staticmethod
    def validate_workflow_transition(
        current: WorkflowStatus, target: WorkflowStatus
    ) -> None:
        allowed = WORKFLOW_TRANSITIONS.get(current, frozenset())
        if target not in allowed:
            raise BusinessRuleException(
                f"Cannot transition workflow from '{current.value}' to "
                f"'{target.value}'."
            )

    @staticmethod
    def validate_step_transition(
        current: WorkflowStepStatus, target: WorkflowStepStatus
    ) -> None:
        allowed = STEP_TRANSITIONS.get(current, frozenset())
        if target not in allowed:
            raise BusinessRuleException(
                f"Cannot transition workflow step from '{current.value}' to "
                f"'{target.value}'."
            )

    @staticmethod
    def validate_approval_transition(
        current: ApprovalStatus, target: ApprovalStatus
    ) -> None:
        allowed = APPROVAL_TRANSITIONS.get(current, frozenset())
        if target not in allowed:
            raise BusinessRuleException(
                f"Cannot transition approval from '{current.value}' to "
                f"'{target.value}'."
            )

    @staticmethod
    def ensure_workflow_not_terminal(status: WorkflowStatus, action: str) -> None:
        if status in TERMINAL_WORKFLOW_STATUSES:
            raise BusinessRuleException(
                f"Cannot {action}: workflow is in terminal status '{status.value}'."
            )

    # ------------------------------------------------------------------
    # Workflow input validation
    # ------------------------------------------------------------------
    @staticmethod
    def validate_workflow_name(name: str) -> None:
        if not name or not name.strip():
            raise ValidationException("Workflow name must not be blank.")
        if len(name) > 255:
            raise ValidationException("Workflow name must not exceed 255 characters.")

    @staticmethod
    def validate_entity_reference(entity_type: str, entity_id: str) -> None:
        if not entity_type or not entity_type.strip():
            raise ValidationException("entity_type must not be blank.")
        if not entity_id or not entity_id.strip():
            raise ValidationException("entity_id must not be blank.")

    @staticmethod
    def validate_priority(priority: str) -> None:
        if priority not in _VALID_PRIORITIES:
            raise ValidationException(
                f"priority must be one of {sorted(_VALID_PRIORITIES)}."
            )

    @staticmethod
    def validate_step_order_sequence(orders: Sequence[int]) -> None:
        if not orders:
            return
        if len(orders) != len(set(orders)):
            raise ValidationException("Duplicate step_order values are not allowed.")
        if sorted(orders) != list(range(1, len(orders) + 1)):
            raise ValidationException(
                "step_order values must form a contiguous 1-based sequence."
            )

    @staticmethod
    def validate_step_name(name: str) -> None:
        if not name or not name.strip():
            raise ValidationException("Step name must not be blank.")

    @staticmethod
    def validate_step_can_be_added(workflow_status: WorkflowStatus) -> None:
        if workflow_status in TERMINAL_WORKFLOW_STATUSES:
            raise BusinessRuleException(
                f"Cannot add steps to a workflow in terminal status "
                f"'{workflow_status.value}'."
            )

    @staticmethod
    def validate_step_can_be_removed(workflow_status: WorkflowStatus) -> None:
        if workflow_status != WorkflowStatus.DRAFT:
            raise BusinessRuleException(
                "Steps can only be removed while the workflow is in "
                "'draft' status."
            )

    # ------------------------------------------------------------------
    # Approval input validation
    # ------------------------------------------------------------------
    @staticmethod
    def validate_approver_id(approver_id: int) -> None:
        if approver_id is None or approver_id <= 0:
            raise ValidationException("approver_id must be a positive integer.")

    @staticmethod
    def validate_approval_requestable(
        step_status: WorkflowStepStatus, is_approval_required: bool
    ) -> None:
        if not is_approval_required:
            raise BusinessRuleException(
                "This step does not require approval."
            )
        if step_status not in (
            WorkflowStepStatus.PENDING,
            WorkflowStepStatus.IN_PROGRESS,
            WorkflowStepStatus.BLOCKED,
        ):
            raise BusinessRuleException(
                f"Cannot request approval for a step in status "
                f"'{step_status.value}'."
            )

    @staticmethod
    def validate_escalation_target(escalated_to_id: Optional[int]) -> None:
        if escalated_to_id is None or escalated_to_id <= 0:
            raise ValidationException(
                "escalated_to_id must be a positive integer when escalating."
            )

    # ------------------------------------------------------------------
    # Assignment / comment validation
    # ------------------------------------------------------------------
    @staticmethod
    def validate_assignee_id(assignee_id: int) -> None:
        if assignee_id is None or assignee_id <= 0:
            raise ValidationException("assignee_id must be a positive integer.")

    @staticmethod
    def validate_comment_text(message: str) -> None:
        if not message or not message.strip():
            raise ValidationException("Comment message must not be blank.")
        if len(message) > _MAX_COMMENT_LENGTH:
            raise ValidationException(
                f"Comment message must not exceed {_MAX_COMMENT_LENGTH} characters."
            )

    # ------------------------------------------------------------------
    # Query / pagination validation
    # ------------------------------------------------------------------
    @staticmethod
    def validate_pagination(page: int, page_size: int) -> None:
        if page < 1:
            raise ValidationException("page must be >= 1.")
        if not (1 <= page_size <= _MAX_PAGE_SIZE):
            raise ValidationException(
                f"page_size must be between 1 and {_MAX_PAGE_SIZE}."
            )

    @staticmethod
    def validate_sort_field(field: str) -> None:
        if field not in _SORTABLE_WORKFLOW_FIELDS:
            raise ValidationException(
                f"Cannot sort by '{field}'. Allowed fields: "
                f"{sorted(_SORTABLE_WORKFLOW_FIELDS)}."
            )

    @staticmethod
    def validate_sort_direction(direction: str) -> None:
        if direction.lower() not in ("asc", "desc"):
            raise ValidationException("sort direction must be 'asc' or 'desc'.")

    @staticmethod
    def validate_search_term(search_term: str) -> None:
        if not search_term or not search_term.strip():
            raise ValidationException("search_term must not be blank.")

    @staticmethod
    def validate_cancellation_reason(reason: str) -> None:
        if not reason or not reason.strip():
            raise ValidationException(
                "cancellation_reason is required to cancel a workflow."
            )