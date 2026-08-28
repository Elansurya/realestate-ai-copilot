"""workflow module

Revision ID: 20260803_0001
Revises: settings_module_migration
Create Date: 2026-08-03 00:00:00.000000
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

# NOTE: Replace the placeholder below with the actual revision id of the
# current migration head before running. Determine it via
# `alembic heads` or by inspecting backend/alembic/versions/ -- do not
# guess this value.
revision = "20260803_0001"
down_revision = "settings_module_migration"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("CREATE EXTENSION IF NOT EXISTS pgcrypto")

    # NOTE: create_type=False is intentional on all three ENUM
    # definitions below. The types are created explicitly via the
    # `.create(bind, checkfirst=True)` calls immediately following
    # these definitions. Without create_type=False, SQLAlchemy/Alembic
    # will *also* attempt to create the type as a side effect of
    # op.create_table(), which raises a duplicate-object error
    # ("type ... already exists") once the explicit creation above has
    # already run — or on any database where the type was already
    # created by a prior partial run.
    workflow_status_enum = postgresql.ENUM(
        "draft",
        "active",
        "in_progress",
        "on_hold",
        "completed",
        "cancelled",
        "failed",
        name="workflow_status_enum",
        create_type=False,
    )
    workflow_step_status_enum = postgresql.ENUM(
        "pending",
        "in_progress",
        "completed",
        "skipped",
        "blocked",
        "failed",
        name="workflow_step_status_enum",
        create_type=False,
    )
    approval_status_enum = postgresql.ENUM(
        "pending",
        "approved",
        "rejected",
        "escalated",
        "cancelled",
        name="approval_status_enum",
        create_type=False,
    )

    bind = op.get_bind()
    workflow_status_enum.create(bind, checkfirst=True)
    workflow_step_status_enum.create(bind, checkfirst=True)
    approval_status_enum.create(bind, checkfirst=True)

    # -----------------------------------------------------------------
    # workflows
    # -----------------------------------------------------------------
    op.create_table(
        "workflows",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            server_default=sa.text("gen_random_uuid()"),
            primary_key=True,
        ),
        sa.Column("name", sa.String(length=255), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("workflow_type", sa.String(length=100), nullable=False),
        sa.Column(
            "status",
            workflow_status_enum,
            nullable=False,
            server_default="draft",
        ),
        sa.Column("entity_type", sa.String(length=50), nullable=False),
        sa.Column("entity_id", sa.String(length=64), nullable=False),
        sa.Column("initiated_by_id", sa.Integer(), nullable=False),
        sa.Column("assigned_to_id", sa.Integer(), nullable=True),
        sa.Column("current_step_order", sa.Integer(), nullable=True),
        sa.Column(
            "priority", sa.String(length=20), nullable=False, server_default="normal"
        ),
        sa.Column("due_date", sa.DateTime(timezone=True), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("cancelled_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("cancellation_reason", sa.Text(), nullable=True),
        sa.Column("metadata", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column(
            "is_deleted", sa.Boolean(), nullable=False, server_default=sa.false()
        ),
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column("created_by_id", sa.Integer(), nullable=True),
        sa.Column("updated_by_id", sa.Integer(), nullable=True),
        sa.ForeignKeyConstraint(
            ["initiated_by_id"],
            ["users.id"],
            name="fk_workflows_initiated_by_id_users",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["assigned_to_id"],
            ["users.id"],
            name="fk_workflows_assigned_to_id_users",
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["created_by_id"],
            ["users.id"],
            name="fk_workflows_created_by_id_users",
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["updated_by_id"],
            ["users.id"],
            name="fk_workflows_updated_by_id_users",
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_workflows"),
        sa.CheckConstraint(
            "priority IN ('low', 'normal', 'high', 'urgent')",
            name="ck_workflows_priority_valid",
        ),
        sa.CheckConstraint(
            "cancelled_at IS NULL OR status = 'cancelled'",
            name="ck_workflows_cancelled_consistency",
        ),
        sa.CheckConstraint(
            "completed_at IS NULL OR status = 'completed'",
            name="ck_workflows_completed_consistency",
        ),
    )
    op.create_index(
        "ix_workflows_entity", "workflows", ["entity_type", "entity_id"]
    )
    op.create_index("ix_workflows_status", "workflows", ["status"])
    op.create_index(
        "ix_workflows_assigned_to_id", "workflows", ["assigned_to_id"]
    )

    # -----------------------------------------------------------------
    # workflow_steps
    # -----------------------------------------------------------------
    op.create_table(
        "workflow_steps",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            server_default=sa.text("gen_random_uuid()"),
            primary_key=True,
        ),
        sa.Column("workflow_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("step_order", sa.Integer(), nullable=False),
        sa.Column("step_name", sa.String(length=255), nullable=False),
        sa.Column("step_type", sa.String(length=100), nullable=False),
        sa.Column(
            "status",
            workflow_step_status_enum,
            nullable=False,
            server_default="pending",
        ),
        sa.Column("assigned_to_id", sa.Integer(), nullable=True),
        sa.Column(
            "is_approval_required",
            sa.Boolean(),
            nullable=False,
            server_default=sa.false(),
        ),
        sa.Column("instructions", sa.Text(), nullable=True),
        sa.Column("input_data", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("output_data", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("due_date", sa.DateTime(timezone=True), nullable=True),
        sa.Column("sla_hours", sa.Integer(), nullable=True),
        sa.Column(
            "retry_count", sa.Integer(), nullable=False, server_default="0"
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column("created_by_id", sa.Integer(), nullable=True),
        sa.Column("updated_by_id", sa.Integer(), nullable=True),
        sa.ForeignKeyConstraint(
            ["workflow_id"],
            ["workflows.id"],
            name="fk_workflow_steps_workflow_id_workflows",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["assigned_to_id"],
            ["users.id"],
            name="fk_workflow_steps_assigned_to_id_users",
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["created_by_id"],
            ["users.id"],
            name="fk_workflow_steps_created_by_id_users",
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["updated_by_id"],
            ["users.id"],
            name="fk_workflow_steps_updated_by_id_users",
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_workflow_steps"),
        sa.UniqueConstraint(
            "workflow_id",
            "step_order",
            name="uq_workflow_steps_workflow_id_step_order",
        ),
        sa.CheckConstraint(
            "step_order > 0", name="ck_workflow_steps_step_order_positive"
        ),
        sa.CheckConstraint(
            "retry_count >= 0", name="ck_workflow_steps_retry_count_non_negative"
        ),
        sa.CheckConstraint(
            "completed_at IS NULL OR status = 'completed'",
            name="ck_workflow_steps_completed_consistency",
        ),
    )
    op.create_index(
        "ix_workflow_steps_workflow_id_status",
        "workflow_steps",
        ["workflow_id", "status"],
    )
    op.create_index(
        "ix_workflow_steps_assigned_to_id", "workflow_steps", ["assigned_to_id"]
    )

    # -----------------------------------------------------------------
    # workflow_approvals
    # -----------------------------------------------------------------
    op.create_table(
        "workflow_approvals",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            server_default=sa.text("gen_random_uuid()"),
            primary_key=True,
        ),
        sa.Column("workflow_step_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("workflow_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("approver_id", sa.Integer(), nullable=False),
        sa.Column(
            "status",
            approval_status_enum,
            nullable=False,
            server_default="pending",
        ),
        sa.Column("decision_notes", sa.Text(), nullable=True),
        sa.Column(
            "requested_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column("decided_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "escalated", sa.Boolean(), nullable=False, server_default=sa.false()
        ),
        sa.Column("escalated_to_id", sa.Integer(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.ForeignKeyConstraint(
            ["workflow_step_id"],
            ["workflow_steps.id"],
            name="fk_workflow_approvals_workflow_step_id_workflow_steps",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["workflow_id"],
            ["workflows.id"],
            name="fk_workflow_approvals_workflow_id_workflows",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["approver_id"],
            ["users.id"],
            name="fk_workflow_approvals_approver_id_users",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["escalated_to_id"],
            ["users.id"],
            name="fk_workflow_approvals_escalated_to_id_users",
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_workflow_approvals"),
        sa.CheckConstraint(
            "(status IN ('approved', 'rejected') AND decided_at IS NOT NULL) "
            "OR (status NOT IN ('approved', 'rejected'))",
            name="ck_workflow_approvals_decided_at_consistency",
        ),
        sa.CheckConstraint(
            "(escalated IS TRUE AND escalated_to_id IS NOT NULL) "
            "OR (escalated IS FALSE)",
            name="ck_workflow_approvals_escalation_consistency",
        ),
    )
    op.create_index(
        "ix_workflow_approvals_step_id_status",
        "workflow_approvals",
        ["workflow_step_id", "status"],
    )
    op.create_index(
        "ix_workflow_approvals_approver_id_status",
        "workflow_approvals",
        ["approver_id", "status"],
    )
    op.create_index(
        "ix_workflow_approvals_workflow_id", "workflow_approvals", ["workflow_id"]
    )


def downgrade() -> None:
    op.drop_index(
        "ix_workflow_approvals_workflow_id", table_name="workflow_approvals"
    )
    op.drop_index(
        "ix_workflow_approvals_approver_id_status", table_name="workflow_approvals"
    )
    op.drop_index(
        "ix_workflow_approvals_step_id_status", table_name="workflow_approvals"
    )
    op.drop_table("workflow_approvals")

    op.drop_index(
        "ix_workflow_steps_assigned_to_id", table_name="workflow_steps"
    )
    op.drop_index(
        "ix_workflow_steps_workflow_id_status", table_name="workflow_steps"
    )
    op.drop_table("workflow_steps")

    op.drop_index("ix_workflows_assigned_to_id", table_name="workflows")
    op.drop_index("ix_workflows_status", table_name="workflows")
    op.drop_index("ix_workflows_entity", table_name="workflows")
    op.drop_table("workflows")

    bind = op.get_bind()
    postgresql.ENUM(name="approval_status_enum").drop(bind, checkfirst=True)
    postgresql.ENUM(name="workflow_step_status_enum").drop(bind, checkfirst=True)
    postgresql.ENUM(name="workflow_status_enum").drop(bind, checkfirst=True)