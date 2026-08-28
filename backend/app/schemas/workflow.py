"""
backend/app/schemas/workflow.py

Pydantic v2 schemas for the Workflow module of the Enterprise Real
Estate AI Copilot CRM.

Mirrors the shape of `app/models/workflow.py`:
    Workflow (1) ---- (N) WorkflowStep (1) ---- (N) WorkflowApproval

Naming convention:
    - `*Base`   -> shared/common fields.
    - `*Create` -> payload accepted on creation.
    - `*Update` -> payload accepted on partial update (PATCH-style, all
      fields optional).
    - `*Read`   -> full representation returned by the API, including
      server-generated/audit fields.
    - `*WithChildren` -> `*Read` variant that nests its child collection,
      for endpoints that return an aggregate view.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from enum import Enum
from typing import List, Optional

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


# ---------------------------------------------------------------------------
# Enumerations (mirror app.models.workflow)
# ---------------------------------------------------------------------------
class WorkflowStatus(str, Enum):
    """Lifecycle status of a Workflow instance as a whole."""

    DRAFT = "draft"
    ACTIVE = "active"
    IN_PROGRESS = "in_progress"
    ON_HOLD = "on_hold"
    COMPLETED = "completed"
    CANCELLED = "cancelled"
    FAILED = "failed"


class WorkflowStepStatus(str, Enum):
    """Lifecycle status of an individual WorkflowStep."""

    PENDING = "pending"
    IN_PROGRESS = "in_progress"
    COMPLETED = "completed"
    SKIPPED = "skipped"
    BLOCKED = "blocked"
    FAILED = "failed"


class ApprovalStatus(str, Enum):
    """Decision status of a WorkflowApproval request."""

    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"
    ESCALATED = "escalated"
    CANCELLED = "cancelled"


class WorkflowPriority(str, Enum):
    """Allowed priority labels for a Workflow (mirrors the DB check constraint)."""

    LOW = "low"
    NORMAL = "normal"
    HIGH = "high"
    URGENT = "urgent"


# ---------------------------------------------------------------------------
# WorkflowApproval schemas
# ---------------------------------------------------------------------------
class WorkflowApprovalBase(BaseModel):
    """Fields shared across WorkflowApproval create/update/read schemas."""

    model_config = ConfigDict(from_attributes=True)

    approver_id: int = Field(..., gt=0, description="User ID asked to decide.")
    decision_notes: Optional[str] = Field(
        None, max_length=10_000, description="Free-text notes accompanying the decision."
    )


class WorkflowApprovalCreate(WorkflowApprovalBase):
    """Payload to request a new approval against a WorkflowStep."""

    workflow_step_id: uuid.UUID = Field(..., description="Step this approval gates.")
    workflow_id: uuid.UUID = Field(..., description="Parent workflow (denormalized).")


class WorkflowApprovalUpdate(BaseModel):
    """Payload to record/update an approval decision. All fields optional."""

    model_config = ConfigDict(from_attributes=True)

    status: Optional[ApprovalStatus] = None
    decision_notes: Optional[str] = Field(None, max_length=10_000)
    escalated: Optional[bool] = None
    escalated_to_id: Optional[int] = Field(None, gt=0)

    @model_validator(mode="after")
    def _validate_escalation(self) -> "WorkflowApprovalUpdate":
        """Ensure escalated_to_id is present whenever escalated is set True."""
        if self.escalated is True and self.escalated_to_id is None:
            raise ValueError("escalated_to_id is required when escalated is True.")
        return self


class WorkflowApprovalRead(WorkflowApprovalBase):
    """Full representation of a WorkflowApproval, as returned by the API."""

    id: uuid.UUID
    workflow_step_id: uuid.UUID
    workflow_id: uuid.UUID
    status: ApprovalStatus
    requested_at: datetime
    decided_at: Optional[datetime] = None
    escalated: bool
    escalated_to_id: Optional[int] = None
    created_at: datetime
    updated_at: datetime


# ---------------------------------------------------------------------------
# WorkflowStep schemas
# ---------------------------------------------------------------------------
class WorkflowStepBase(BaseModel):
    """Fields shared across WorkflowStep create/update/read schemas."""

    model_config = ConfigDict(from_attributes=True)

    step_name: str = Field(..., min_length=1, max_length=255)
    step_type: str = Field(..., min_length=1, max_length=100)
    assigned_to_id: Optional[int] = Field(None, gt=0)
    is_approval_required: bool = False
    instructions: Optional[str] = Field(None, max_length=10_000)
    input_data: Optional[dict] = None
    due_date: Optional[datetime] = None
    sla_hours: Optional[int] = Field(None, gt=0)


class WorkflowStepCreate(WorkflowStepBase):
    """Payload to add a new step to a workflow."""

    workflow_id: uuid.UUID = Field(..., description="Parent workflow this step belongs to.")
    step_order: int = Field(..., gt=0, description="1-based position within the workflow.")


class WorkflowStepUpdate(BaseModel):
    """Payload to partially update a WorkflowStep. All fields optional."""

    model_config = ConfigDict(from_attributes=True)

    step_name: Optional[str] = Field(None, min_length=1, max_length=255)
    step_order: Optional[int] = Field(None, gt=0)
    status: Optional[WorkflowStepStatus] = None
    assigned_to_id: Optional[int] = Field(None, gt=0)
    is_approval_required: Optional[bool] = None
    instructions: Optional[str] = Field(None, max_length=10_000)
    input_data: Optional[dict] = None
    output_data: Optional[dict] = None
    due_date: Optional[datetime] = None
    sla_hours: Optional[int] = Field(None, gt=0)
    retry_count: Optional[int] = Field(None, ge=0)


class WorkflowStepRead(WorkflowStepBase):
    """Full representation of a WorkflowStep, as returned by the API."""

    id: uuid.UUID
    workflow_id: uuid.UUID
    step_order: int
    status: WorkflowStepStatus
    output_data: Optional[dict] = None
    started_at: Optional[datetime] = None
    completed_at: Optional[datetime] = None
    retry_count: int
    created_at: datetime
    updated_at: datetime
    created_by_id: Optional[int] = None
    updated_by_id: Optional[int] = None


class WorkflowStepWithApprovals(WorkflowStepRead):
    """WorkflowStepRead variant that nests its approval decisions."""

    approvals: List[WorkflowApprovalRead] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# Workflow schemas
# ---------------------------------------------------------------------------
class WorkflowBase(BaseModel):
    """Fields shared across Workflow create/update/read schemas."""

    model_config = ConfigDict(from_attributes=True)

    name: str = Field(..., min_length=1, max_length=255)
    description: Optional[str] = Field(None, max_length=10_000)
    workflow_type: str = Field(..., min_length=1, max_length=100)
    entity_type: str = Field(..., min_length=1, max_length=50)
    entity_id: str = Field(..., min_length=1, max_length=64)
    priority: WorkflowPriority = WorkflowPriority.NORMAL
    due_date: Optional[datetime] = None
    meta_data: Optional[dict] = None


class WorkflowCreate(WorkflowBase):
    """Payload to create a new Workflow."""

    initiated_by_id: int = Field(..., gt=0)
    assigned_to_id: Optional[int] = Field(None, gt=0)
    steps: Optional[List[WorkflowStepCreate]] = Field(
        default=None,
        description="Optional initial set of steps to create alongside the workflow.",
    )


class WorkflowUpdate(BaseModel):
    """Payload to partially update a Workflow. All fields optional."""

    model_config = ConfigDict(from_attributes=True)

    name: Optional[str] = Field(None, min_length=1, max_length=255)
    description: Optional[str] = Field(None, max_length=10_000)
    status: Optional[WorkflowStatus] = None
    assigned_to_id: Optional[int] = Field(None, gt=0)
    current_step_order: Optional[int] = Field(None, gt=0)
    priority: Optional[WorkflowPriority] = None
    due_date: Optional[datetime] = None
    cancellation_reason: Optional[str] = Field(None, max_length=10_000)
    meta_data: Optional[dict] = None

    @model_validator(mode="after")
    def _validate_cancellation(self) -> "WorkflowUpdate":
        """Require a cancellation_reason whenever status is set to CANCELLED."""
        if self.status == WorkflowStatus.CANCELLED and not self.cancellation_reason:
            raise ValueError("cancellation_reason is required when status is 'cancelled'.")
        return self


class WorkflowRead(WorkflowBase):
    """Full representation of a Workflow, as returned by the API."""

    id: uuid.UUID
    status: WorkflowStatus
    initiated_by_id: int
    assigned_to_id: Optional[int] = None
    current_step_order: Optional[int] = None
    started_at: Optional[datetime] = None
    completed_at: Optional[datetime] = None
    cancelled_at: Optional[datetime] = None
    cancellation_reason: Optional[str] = None
    is_deleted: bool
    created_at: datetime
    updated_at: datetime
    created_by_id: Optional[int] = None
    updated_by_id: Optional[int] = None

    @field_validator("entity_id")
    @classmethod
    def _entity_id_not_blank(cls, value: str) -> str:
        """Guard against blank entity_id values slipping through as whitespace."""
        if not value.strip():
            raise ValueError("entity_id must not be blank.")
        return value


class WorkflowWithSteps(WorkflowRead):
    """WorkflowRead variant that nests its ordered steps (and their approvals)."""

    steps: List[WorkflowStepWithApprovals] = Field(default_factory=list)