"""
backend/app/models/workflow.py

SQLAlchemy 2.x (async) ORM models for the Workflow module of the
Enterprise Real Estate AI Copilot CRM.

The Workflow module models a generic, ordered process (a "Workflow")
made up of discrete, sequenced units of work ("WorkflowStep"), some of
which may require a human decision ("WorkflowApproval") before the
workflow can progress.

Domain shape:
    Workflow (1) ---- (N) WorkflowStep (1) ---- (N) WorkflowApproval

A Workflow is polymorphically attached to the entity it governs (a
Lead, Customer, Property, or Booking) via `entity_type` / `entity_id`
rather than a hard foreign key, since those entities use different
primary key types (UUID vs. Integer) across the project -- mirroring
the same "no dedicated association table required" tradeoff already
accepted elsewhere in this codebase (see `app/models/document.py`).

Conventions (mirrors `app/models/document.py` / `app/models/message.py`):
    - `Base` comes from `app.db.base`.
    - Primary keys are server-generated PostgreSQL UUIDs via
      `func.gen_random_uuid()` (requires the `pgcrypto` extension,
      already enabled by earlier migrations in this project).
    - `*_by_id` audit/actor columns are `Integer` FKs to `users.id`,
      matching `User.id`'s actual type (see `app/models/user.py`).
    - Enums are native PostgreSQL ENUM types (via SQLAlchemy's
      `Enum`) for strong data-integrity at the database level.
    - Timestamps are timezone-aware (UTC).
    - `User` is imported only under `TYPE_CHECKING` to avoid a
      runtime circular-import surface.
"""

from __future__ import annotations

import enum
import uuid
from datetime import datetime
from typing import TYPE_CHECKING, List, Optional

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy import Enum as SAEnum
from sqlalchemy.dialects.postgresql import JSONB, UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base

if TYPE_CHECKING:
    from app.models.user import User


# ---------------------------------------------------------------------------
# Enumerations
# ---------------------------------------------------------------------------
class WorkflowStatus(str, enum.Enum):
    """Lifecycle status of a Workflow instance as a whole."""

    DRAFT = "draft"
    ACTIVE = "active"
    IN_PROGRESS = "in_progress"
    ON_HOLD = "on_hold"
    COMPLETED = "completed"
    CANCELLED = "cancelled"
    FAILED = "failed"


class WorkflowStepStatus(str, enum.Enum):
    """Lifecycle status of an individual WorkflowStep."""

    PENDING = "pending"
    IN_PROGRESS = "in_progress"
    COMPLETED = "completed"
    SKIPPED = "skipped"
    BLOCKED = "blocked"
    FAILED = "failed"


class ApprovalStatus(str, enum.Enum):
    """Decision status of a WorkflowApproval request."""

    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"
    ESCALATED = "escalated"
    CANCELLED = "cancelled"


# ---------------------------------------------------------------------------
# Workflow
# ---------------------------------------------------------------------------
class Workflow(Base):
    """Represents a single, end-to-end orchestrated business process.

    Attributes:
        id: Primary key UUID.
        name: Human-readable workflow name.
        description: Optional long-form description of the workflow's purpose.
        workflow_type: Free-form category of workflow (e.g. "lead_conversion",
            "booking_approval", "document_verification").
        status: Current overall status of the workflow.
        entity_type: Type of the domain entity this workflow governs
            (e.g. "lead", "customer", "property", "booking").
        entity_id: Identifier of the governed entity, stored as text to
            remain agnostic of the referenced entity's PK type (UUID or
            Integer) across the project.
        initiated_by_id: FK to the user who started the workflow.
        assigned_to_id: FK to the user currently responsible for it.
        current_step_order: Convenience pointer to the active step's order.
        priority: Priority label ("low", "normal", "high", "urgent").
        due_date: Optional target completion timestamp.
        started_at: Timestamp the workflow moved out of DRAFT.
        completed_at: Timestamp the workflow reached a terminal COMPLETED state.
        cancelled_at: Timestamp the workflow was cancelled, if applicable.
        cancellation_reason: Free-text reason for cancellation.
        meta_data: Arbitrary JSONB payload for workflow-specific context.
        is_deleted: Soft-delete flag.
        deleted_at: Timestamp of soft deletion, if any.
        created_at: Record creation timestamp.
        updated_at: Record last-update timestamp.
        created_by_id: FK to the user who created the record.
        updated_by_id: FK to the user who last updated the record.
        steps: Ordered collection of this workflow's WorkflowStep rows.
    """

    __tablename__ = "workflows"
    __table_args__ = (
        CheckConstraint(
            "priority IN ('low', 'normal', 'high', 'urgent')",
            name="ck_workflows_priority_valid",
        ),
        CheckConstraint(
            "cancelled_at IS NULL OR status = 'cancelled'",
            name="ck_workflows_cancelled_consistency",
        ),
        CheckConstraint(
            "completed_at IS NULL OR status = 'completed'",
            name="ck_workflows_completed_consistency",
        ),
        Index("ix_workflows_entity", "entity_type", "entity_id"),
        Index("ix_workflows_status", "status"),
        Index("ix_workflows_assigned_to_id", "assigned_to_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        primary_key=True,
        server_default=func.gen_random_uuid(),
    )

    name: Mapped[str] = mapped_column(String(255), nullable=False)
    description: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    workflow_type: Mapped[str] = mapped_column(String(100), nullable=False)

    status: Mapped[WorkflowStatus] = mapped_column(
        SAEnum(
            WorkflowStatus,
            name="workflow_status_enum",
            native_enum=True,
            validate_strings=True,
            values_callable=lambda enum_cls: [member.value for member in enum_cls],
        ),
        nullable=False,
        default=WorkflowStatus.DRAFT,
        server_default=WorkflowStatus.DRAFT.value,
    )

    entity_type: Mapped[str] = mapped_column(String(50), nullable=False)
    entity_id: Mapped[str] = mapped_column(String(64), nullable=False)

    initiated_by_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("users.id", ondelete="RESTRICT"), nullable=False
    )
    assigned_to_id: Mapped[Optional[int]] = mapped_column(
        Integer, ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )

    current_step_order: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    priority: Mapped[str] = mapped_column(
        String(20), nullable=False, default="normal", server_default="normal"
    )

    due_date: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    started_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    completed_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    cancelled_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    cancellation_reason: Mapped[Optional[str]] = mapped_column(Text, nullable=True)

    meta_data: Mapped[Optional[dict]] = mapped_column("metadata", JSONB, nullable=True)

    is_deleted: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default="false"
    )
    deleted_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        onupdate=func.now(),
    )
    created_by_id: Mapped[Optional[int]] = mapped_column(
        Integer, ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    updated_by_id: Mapped[Optional[int]] = mapped_column(
        Integer, ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )

    steps: Mapped[List["WorkflowStep"]] = relationship(
        "WorkflowStep",
        back_populates="workflow",
        cascade="all, delete-orphan",
        order_by="WorkflowStep.step_order",
    )

    def __repr__(self) -> str:
        """Return a debug-friendly representation of the workflow."""
        return f"<Workflow id={self.id} name={self.name!r} status={self.status}>"


# ---------------------------------------------------------------------------
# WorkflowStep
# ---------------------------------------------------------------------------
class WorkflowStep(Base):
    """Represents a single, ordered unit of work within a Workflow.

    Attributes:
        id: Primary key UUID.
        workflow_id: FK to the parent Workflow.
        step_order: 1-based position of this step within its workflow.
        step_name: Human-readable step name.
        step_type: Free-form category of step (e.g. "document_verification",
            "approval", "payment_confirmation").
        status: Current status of this step.
        assigned_to_id: FK to the user responsible for completing this step.
        is_approval_required: Whether this step must be gated by a
            WorkflowApproval before it can be marked COMPLETED.
        instructions: Optional free-text guidance for whoever executes the step.
        input_data: Arbitrary JSONB input payload consumed by this step.
        output_data: Arbitrary JSONB result payload produced by this step.
        started_at: Timestamp the step moved into IN_PROGRESS.
        completed_at: Timestamp the step reached a terminal COMPLETED state.
        due_date: Optional target completion timestamp for this step.
        sla_hours: Optional service-level agreement window, in hours.
        retry_count: Number of times this step has been retried after failure.
        created_at: Record creation timestamp.
        updated_at: Record last-update timestamp.
        created_by_id: FK to the user who created the record.
        updated_by_id: FK to the user who last updated the record.
        workflow: The parent Workflow record.
        approvals: Approval decisions requested against this step.
    """

    __tablename__ = "workflow_steps"
    __table_args__ = (
        UniqueConstraint(
            "workflow_id", "step_order", name="uq_workflow_steps_workflow_id_step_order"
        ),
        CheckConstraint("step_order > 0", name="ck_workflow_steps_step_order_positive"),
        CheckConstraint(
            "retry_count >= 0", name="ck_workflow_steps_retry_count_non_negative"
        ),
        CheckConstraint(
            "completed_at IS NULL OR status = 'completed'",
            name="ck_workflow_steps_completed_consistency",
        ),
        Index("ix_workflow_steps_workflow_id_status", "workflow_id", "status"),
        Index("ix_workflow_steps_assigned_to_id", "assigned_to_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        primary_key=True,
        server_default=func.gen_random_uuid(),
    )

    workflow_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("workflows.id", ondelete="CASCADE"),
        nullable=False,
    )

    step_order: Mapped[int] = mapped_column(Integer, nullable=False)
    step_name: Mapped[str] = mapped_column(String(255), nullable=False)
    step_type: Mapped[str] = mapped_column(String(100), nullable=False)

    status: Mapped[WorkflowStepStatus] = mapped_column(
        SAEnum(
            WorkflowStepStatus,
            name="workflow_step_status_enum",
            native_enum=True,
            validate_strings=True,
            values_callable=lambda enum_cls: [member.value for member in enum_cls],
        ),
        nullable=False,
        default=WorkflowStepStatus.PENDING,
        server_default=WorkflowStepStatus.PENDING.value,
    )

    assigned_to_id: Mapped[Optional[int]] = mapped_column(
        Integer, ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )

    is_approval_required: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default="false"
    )

    instructions: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    input_data: Mapped[Optional[dict]] = mapped_column(JSONB, nullable=True)
    output_data: Mapped[Optional[dict]] = mapped_column(JSONB, nullable=True)

    started_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    completed_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    due_date: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    sla_hours: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    retry_count: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        onupdate=func.now(),
    )
    created_by_id: Mapped[Optional[int]] = mapped_column(
        Integer, ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    updated_by_id: Mapped[Optional[int]] = mapped_column(
        Integer, ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )

    workflow: Mapped["Workflow"] = relationship("Workflow", back_populates="steps")
    approvals: Mapped[List["WorkflowApproval"]] = relationship(
        "WorkflowApproval",
        back_populates="step",
        cascade="all, delete-orphan",
        order_by="WorkflowApproval.requested_at",
    )

    def __repr__(self) -> str:
        """Return a debug-friendly representation of the workflow step."""
        return (
            f"<WorkflowStep id={self.id} workflow_id={self.workflow_id} "
            f"order={self.step_order} status={self.status}>"
        )


# ---------------------------------------------------------------------------
# WorkflowApproval
# ---------------------------------------------------------------------------
class WorkflowApproval(Base):
    """Represents a single approval decision requested against a WorkflowStep.

    Attributes:
        id: Primary key UUID.
        workflow_step_id: FK to the WorkflowStep this approval gates.
        workflow_id: Denormalized FK to the parent Workflow (query convenience).
        approver_id: FK to the user asked to make the decision.
        status: Current decision status.
        decision_notes: Optional free-text notes accompanying the decision.
        requested_at: Timestamp the approval was requested.
        decided_at: Timestamp the approver made a decision, if any.
        escalated: Whether this approval has been escalated.
        escalated_to_id: FK to the user this approval was escalated to.
        created_at: Record creation timestamp.
        updated_at: Record last-update timestamp.
        step: The parent WorkflowStep record.
    """

    __tablename__ = "workflow_approvals"
    __table_args__ = (
        CheckConstraint(
            "(status IN ('approved', 'rejected') AND decided_at IS NOT NULL) "
            "OR (status NOT IN ('approved', 'rejected'))",
            name="ck_workflow_approvals_decided_at_consistency",
        ),
        CheckConstraint(
            "(escalated IS TRUE AND escalated_to_id IS NOT NULL) "
            "OR (escalated IS FALSE)",
            name="ck_workflow_approvals_escalation_consistency",
        ),
        Index("ix_workflow_approvals_step_id_status", "workflow_step_id", "status"),
        Index("ix_workflow_approvals_approver_id_status", "approver_id", "status"),
        Index("ix_workflow_approvals_workflow_id", "workflow_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        primary_key=True,
        server_default=func.gen_random_uuid(),
    )

    workflow_step_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("workflow_steps.id", ondelete="CASCADE"),
        nullable=False,
    )
    workflow_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("workflows.id", ondelete="CASCADE"),
        nullable=False,
    )

    approver_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("users.id", ondelete="RESTRICT"), nullable=False
    )

    status: Mapped[ApprovalStatus] = mapped_column(
        SAEnum(
            ApprovalStatus,
            name="approval_status_enum",
            native_enum=True,
            validate_strings=True,
            values_callable=lambda enum_cls: [member.value for member in enum_cls],
        ),
        nullable=False,
        default=ApprovalStatus.PENDING,
        server_default=ApprovalStatus.PENDING.value,
    )

    decision_notes: Mapped[Optional[str]] = mapped_column(Text, nullable=True)

    requested_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    decided_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    escalated: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default="false"
    )
    escalated_to_id: Mapped[Optional[int]] = mapped_column(
        Integer, ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        onupdate=func.now(),
    )

    step: Mapped["WorkflowStep"] = relationship("WorkflowStep", back_populates="approvals")

    def __repr__(self) -> str:
        """Return a debug-friendly representation of the approval."""
        return (
            f"<WorkflowApproval id={self.id} step_id={self.workflow_step_id} "
            f"status={self.status}>"
        )