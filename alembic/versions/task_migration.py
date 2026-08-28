"""task management module

Revision ID: 20260803_0003
Revises: 20260803_0002
Create Date: 2026-08-03 00:00:00.000000

This migration introduces the enterprise Task Management module. It creates:
    * Three PostgreSQL enum types: ``task_status_enum``,
      ``task_priority_enum``, ``task_type_enum``.
    * The ``tasks`` table with foreign keys to ``users.id`` for
      ``assigned_to_id``, ``created_by_id``, and ``completed_by_id``.
    * Single-column and composite indexes for common query patterns
      (assignment feeds, due-date/status dashboards, related-entity
      lookups).
    * CHECK constraints enforcing a non-empty ``title``, non-negative
      comment/attachment counters, a consistent
      ``related_module``/``related_entity_id`` pair, a consistent
      ``completed_at``/``status`` relationship, a consistent
      ``completed_by_id``/``status`` relationship, a sane
      ``reminder_time``/``due_date`` ordering, and soft-delete column
      consistency.

.. note::
    Replace the ``down_revision`` placeholder below with the actual
    revision id of the current migration head (via ``alembic heads``
    or by inspecting ``backend/alembic/versions/``) before applying --
    do not guess this value.
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "20260803_0003"
down_revision: Union[str, None] = "20260803_0002"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


TASK_STATUS_VALUES = (
    "pending",
    "in_progress",
    "on_hold",
    "completed",
    "cancelled",
)

TASK_PRIORITY_VALUES = ("low", "normal", "high", "urgent")

TASK_TYPE_VALUES = (
    "general",
    "follow_up",
    "call",
    "email",
    "meeting",
    "site_visit",
    "document_review",
    "payment_follow_up",
    "approval",
    "other",
)


def upgrade() -> None:
    """Applies the tasks schema changes.

    Creates the enum types, the ``tasks`` table, its foreign keys, all
    single-column and composite indexes, and CHECK constraints.
    """
    op.execute("CREATE EXTENSION IF NOT EXISTS pgcrypto")

    bind = op.get_bind()

    # NOTE: create_type=False is intentional on all three ENUM
    # definitions below. The types are created explicitly via the
    # `.create(bind, checkfirst=True)` calls immediately following
    # these definitions. Without create_type=False, SQLAlchemy/Alembic
    # will *also* attempt to create the type as a side effect of
    # op.create_table(), which raises a duplicate-object error
    # ("type ... already exists") once the explicit creation above has
    # already run — or on any database where the type was already
    # created by a prior partial run.
    task_status_enum = postgresql.ENUM(
        *TASK_STATUS_VALUES,
        name="task_status_enum",
        create_type=False,
    )
    task_priority_enum = postgresql.ENUM(
        *TASK_PRIORITY_VALUES,
        name="task_priority_enum",
        create_type=False,
    )
    task_type_enum = postgresql.ENUM(
        *TASK_TYPE_VALUES,
        name="task_type_enum",
        create_type=False,
    )

    task_status_enum.create(bind, checkfirst=True)
    task_priority_enum.create(bind, checkfirst=True)
    task_type_enum.create(bind, checkfirst=True)

    op.create_table(
        "tasks",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            server_default=sa.text("gen_random_uuid()"),
            primary_key=True,
            nullable=False,
        ),
        sa.Column("title", sa.String(length=255), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column(
            "task_type",
            task_type_enum,
            nullable=False,
            server_default="general",
        ),
        sa.Column(
            "status",
            task_status_enum,
            nullable=False,
            server_default="pending",
        ),
        sa.Column(
            "priority",
            task_priority_enum,
            nullable=False,
            server_default="normal",
        ),
        sa.Column("due_date", sa.DateTime(timezone=True), nullable=True),
        sa.Column("reminder_time", sa.DateTime(timezone=True), nullable=True),
        sa.Column("assigned_to_id", sa.Integer(), nullable=True),
        sa.Column("created_by_id", sa.Integer(), nullable=True),
        sa.Column("related_module", sa.String(length=50), nullable=True),
        sa.Column("related_entity_id", sa.String(length=64), nullable=True),
        sa.Column(
            "comments_count", sa.Integer(), nullable=False, server_default="0"
        ),
        sa.Column(
            "attachments_count", sa.Integer(), nullable=False, server_default="0"
        ),
        sa.Column("metadata", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("completed_by_id", sa.Integer(), nullable=True),
        sa.Column(
            "is_deleted", sa.Boolean(), nullable=False, server_default=sa.false()
        ),
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["assigned_to_id"],
            ["users.id"],
            name="fk_tasks_assigned_to_id_users",
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["created_by_id"],
            ["users.id"],
            name="fk_tasks_created_by_id_users",
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["completed_by_id"],
            ["users.id"],
            name="fk_tasks_completed_by_id_users",
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_tasks"),
        sa.CheckConstraint("btrim(title) <> ''", name="ck_tasks_title_not_empty"),
        sa.CheckConstraint(
            "comments_count >= 0", name="ck_tasks_comments_count_non_negative"
        ),
        sa.CheckConstraint(
            "attachments_count >= 0",
            name="ck_tasks_attachments_count_non_negative",
        ),
        sa.CheckConstraint(
            "(related_module IS NULL AND related_entity_id IS NULL) "
            "OR (related_module IS NOT NULL AND related_entity_id IS NOT NULL)",
            name="ck_tasks_related_entity_pair_consistency",
        ),
        sa.CheckConstraint(
            "(status = 'completed' AND completed_at IS NOT NULL) "
            "OR (status <> 'completed' AND completed_at IS NULL)",
            name="ck_tasks_completed_at_consistency",
        ),
        sa.CheckConstraint(
            "completed_by_id IS NULL OR status = 'completed'",
            name="ck_tasks_completed_by_consistency",
        ),
        sa.CheckConstraint(
            "reminder_time IS NULL OR due_date IS NULL "
            "OR reminder_time <= due_date",
            name="ck_tasks_reminder_before_due_date",
        ),
        sa.CheckConstraint(
            "(is_deleted IS FALSE AND deleted_at IS NULL) "
            "OR (is_deleted IS TRUE AND deleted_at IS NOT NULL)",
            name="ck_tasks_soft_delete_consistency",
        ),
    )

    # Single-column indexes.
    op.create_index("ix_tasks_status", "tasks", ["status"])
    op.create_index("ix_tasks_priority", "tasks", ["priority"])
    op.create_index("ix_tasks_task_type", "tasks", ["task_type"])
    op.create_index("ix_tasks_assigned_to_id", "tasks", ["assigned_to_id"])
    op.create_index("ix_tasks_created_by_id", "tasks", ["created_by_id"])
    op.create_index("ix_tasks_completed_by_id", "tasks", ["completed_by_id"])
    op.create_index("ix_tasks_due_date", "tasks", ["due_date"])
    op.create_index("ix_tasks_reminder_time", "tasks", ["reminder_time"])
    op.create_index("ix_tasks_related_module", "tasks", ["related_module"])
    op.create_index("ix_tasks_related_entity_id", "tasks", ["related_entity_id"])
    op.create_index("ix_tasks_created_at", "tasks", ["created_at"])

    # Composite indexes for common query patterns.
    op.create_index(
        "ix_tasks_related_module_related_entity_id",
        "tasks",
        ["related_module", "related_entity_id"],
    )
    op.create_index(
        "ix_tasks_assigned_to_id_status", "tasks", ["assigned_to_id", "status"]
    )
    op.create_index(
        "ix_tasks_assigned_to_id_due_date",
        "tasks",
        ["assigned_to_id", "due_date"],
    )
    op.create_index("ix_tasks_status_due_date", "tasks", ["status", "due_date"])
    op.create_index("ix_tasks_status_priority", "tasks", ["status", "priority"])


def downgrade() -> None:
    """Reverts the tasks schema changes.

    Drops all indexes, the ``tasks`` table, and the associated enum
    types, in dependency-safe order.
    """
    bind = op.get_bind()

    op.drop_index("ix_tasks_status_priority", table_name="tasks")
    op.drop_index("ix_tasks_status_due_date", table_name="tasks")
    op.drop_index("ix_tasks_assigned_to_id_due_date", table_name="tasks")
    op.drop_index("ix_tasks_assigned_to_id_status", table_name="tasks")
    op.drop_index(
        "ix_tasks_related_module_related_entity_id", table_name="tasks"
    )

    op.drop_index("ix_tasks_created_at", table_name="tasks")
    op.drop_index("ix_tasks_related_entity_id", table_name="tasks")
    op.drop_index("ix_tasks_related_module", table_name="tasks")
    op.drop_index("ix_tasks_reminder_time", table_name="tasks")
    op.drop_index("ix_tasks_due_date", table_name="tasks")
    op.drop_index("ix_tasks_completed_by_id", table_name="tasks")
    op.drop_index("ix_tasks_created_by_id", table_name="tasks")
    op.drop_index("ix_tasks_assigned_to_id", table_name="tasks")
    op.drop_index("ix_tasks_task_type", table_name="tasks")
    op.drop_index("ix_tasks_priority", table_name="tasks")
    op.drop_index("ix_tasks_status", table_name="tasks")

    op.drop_table("tasks")

    postgresql.ENUM(name="task_type_enum").drop(bind, checkfirst=True)
    postgresql.ENUM(name="task_priority_enum").drop(bind, checkfirst=True)
    postgresql.ENUM(name="task_status_enum").drop(bind, checkfirst=True)