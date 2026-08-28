"""activity timeline module

Revision ID: 20260803_0002
Revises: 20260803_0001
Create Date: 2026-08-03 00:00:00.000000

This migration introduces the enterprise Activity Timeline module. It creates:
    * Four PostgreSQL enum types: ``activity_module_enum``,
      ``activity_type_enum``, ``activity_priority_enum``,
      ``activity_status_enum``.
    * The ``activities`` table with foreign keys to ``users.id`` for
      ``performed_by_id`` and ``assigned_to_id``.
    * Single-column and composite indexes for common query patterns.
    * CHECK constraints enforcing non-empty ``title``, ``entity_type``,
      ``entity_id`` values and soft-delete column consistency.

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
revision: str = "20260803_0002"
down_revision: Union[str, None] = "20260803_0001"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


ACTIVITY_MODULE_VALUES = (
    "customer",
    "lead",
    "property",
    "booking",
    "payment",
    "workflow",
    "notification",
    "ai",
    "audit",
    "document",
    "settings",
)

ACTIVITY_TYPE_VALUES = (
    "created",
    "updated",
    "deleted",
    "restored",
    "archived",
    "status_changed",
    "assigned",
    "unassigned",
    "approved",
    "rejected",
    "uploaded",
    "downloaded",
    "viewed",
    "commented",
    "scheduled",
    "cancelled",
    "completed",
    "payment_received",
    "payment_failed",
    "workflow_started",
    "workflow_completed",
    "notification_sent",
    "ai_generated",
    "login",
    "logout",
    "exported",
    "imported",
)

ACTIVITY_PRIORITY_VALUES = ("low", "normal", "high", "urgent")

ACTIVITY_STATUS_VALUES = (
    "pending",
    "active",
    "completed",
    "archived",
    "failed",
    "cancelled",
)


def upgrade() -> None:
    """Applies the activities schema changes.

    Creates the enum types, the ``activities`` table, its foreign
    keys, all single-column and composite indexes, and CHECK
    constraints.
    """
    op.execute("CREATE EXTENSION IF NOT EXISTS pgcrypto")

    bind = op.get_bind()

    # NOTE: create_type=False is intentional on all four ENUM
    # definitions below. The types are created explicitly via the
    # `.create(bind, checkfirst=True)` calls immediately following
    # these definitions. Without create_type=False, SQLAlchemy/Alembic
    # will *also* attempt to create the type as a side effect of
    # op.create_table(), which raises a duplicate-object error
    # ("type ... already exists") once the explicit creation above has
    # already run — or on any database where the type was already
    # created by a prior partial run.
    activity_module_enum = postgresql.ENUM(
        *ACTIVITY_MODULE_VALUES,
        name="activity_module_enum",
        create_type=False,
    )
    activity_type_enum = postgresql.ENUM(
        *ACTIVITY_TYPE_VALUES,
        name="activity_type_enum",
        create_type=False,
    )
    activity_priority_enum = postgresql.ENUM(
        *ACTIVITY_PRIORITY_VALUES,
        name="activity_priority_enum",
        create_type=False,
    )
    activity_status_enum = postgresql.ENUM(
        *ACTIVITY_STATUS_VALUES,
        name="activity_status_enum",
        create_type=False,
    )

    activity_module_enum.create(bind, checkfirst=True)
    activity_type_enum.create(bind, checkfirst=True)
    activity_priority_enum.create(bind, checkfirst=True)
    activity_status_enum.create(bind, checkfirst=True)

    op.create_table(
        "activities",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            server_default=sa.text("gen_random_uuid()"),
            primary_key=True,
            nullable=False,
        ),
        sa.Column("module", activity_module_enum, nullable=False),
        sa.Column("entity_type", sa.String(length=100), nullable=False),
        sa.Column("entity_id", sa.String(length=64), nullable=False),
        sa.Column("action", activity_type_enum, nullable=False),
        sa.Column("title", sa.String(length=255), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("old_value", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("new_value", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("metadata", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column(
            "priority",
            activity_priority_enum,
            nullable=False,
            server_default="normal",
        ),
        sa.Column(
            "status",
            activity_status_enum,
            nullable=False,
            server_default="active",
        ),
        sa.Column("performed_by_id", sa.Integer(), nullable=True),
        sa.Column("assigned_to_id", sa.Integer(), nullable=True),
        sa.Column("ip_address", postgresql.INET(), nullable=True),
        sa.Column("user_agent", sa.Text(), nullable=True),
        sa.Column(
            "source", sa.String(length=50), nullable=True, server_default="system"
        ),
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
            ["performed_by_id"],
            ["users.id"],
            name="fk_activities_performed_by_id_users",
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["assigned_to_id"],
            ["users.id"],
            name="fk_activities_assigned_to_id_users",
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_activities"),
        sa.CheckConstraint(
            "btrim(title) <> ''", name="ck_activities_title_not_empty"
        ),
        sa.CheckConstraint(
            "btrim(entity_type) <> ''", name="ck_activities_entity_type_not_empty"
        ),
        sa.CheckConstraint(
            "btrim(entity_id) <> ''", name="ck_activities_entity_id_not_empty"
        ),
        sa.CheckConstraint(
            "(is_deleted IS FALSE AND deleted_at IS NULL) "
            "OR (is_deleted IS TRUE AND deleted_at IS NOT NULL)",
            name="ck_activities_soft_delete_consistency",
        ),
    )

    # Single-column indexes.
    op.create_index("ix_activities_module", "activities", ["module"])
    op.create_index("ix_activities_entity_type", "activities", ["entity_type"])
    op.create_index("ix_activities_entity_id", "activities", ["entity_id"])
    op.create_index("ix_activities_action", "activities", ["action"])
    op.create_index("ix_activities_priority", "activities", ["priority"])
    op.create_index("ix_activities_status", "activities", ["status"])
    op.create_index(
        "ix_activities_performed_by_id", "activities", ["performed_by_id"]
    )
    op.create_index(
        "ix_activities_assigned_to_id", "activities", ["assigned_to_id"]
    )
    op.create_index("ix_activities_source", "activities", ["source"])
    op.create_index("ix_activities_created_at", "activities", ["created_at"])

    # Composite indexes for common query patterns.
    op.create_index(
        "ix_activities_entity_type_entity_id",
        "activities",
        ["entity_type", "entity_id"],
    )
    op.create_index(
        "ix_activities_module_action", "activities", ["module", "action"]
    )
    op.create_index(
        "ix_activities_module_created_at", "activities", ["module", "created_at"]
    )
    op.create_index(
        "ix_activities_performed_by_id_created_at",
        "activities",
        ["performed_by_id", "created_at"],
    )
    op.create_index(
        "ix_activities_assigned_to_id_status",
        "activities",
        ["assigned_to_id", "status"],
    )


def downgrade() -> None:
    """Reverts the activities schema changes.

    Drops all indexes, the ``activities`` table, and the associated
    enum types, in dependency-safe order.
    """
    bind = op.get_bind()

    op.drop_index(
        "ix_activities_assigned_to_id_status", table_name="activities"
    )
    op.drop_index(
        "ix_activities_performed_by_id_created_at", table_name="activities"
    )
    op.drop_index("ix_activities_module_created_at", table_name="activities")
    op.drop_index("ix_activities_module_action", table_name="activities")
    op.drop_index(
        "ix_activities_entity_type_entity_id", table_name="activities"
    )

    op.drop_index("ix_activities_created_at", table_name="activities")
    op.drop_index("ix_activities_source", table_name="activities")
    op.drop_index("ix_activities_assigned_to_id", table_name="activities")
    op.drop_index("ix_activities_performed_by_id", table_name="activities")
    op.drop_index("ix_activities_status", table_name="activities")
    op.drop_index("ix_activities_priority", table_name="activities")
    op.drop_index("ix_activities_action", table_name="activities")
    op.drop_index("ix_activities_entity_id", table_name="activities")
    op.drop_index("ix_activities_entity_type", table_name="activities")
    op.drop_index("ix_activities_module", table_name="activities")

    op.drop_table("activities")

    postgresql.ENUM(name="activity_status_enum").drop(bind, checkfirst=True)
    postgresql.ENUM(name="activity_priority_enum").drop(bind, checkfirst=True)
    postgresql.ENUM(name="activity_type_enum").drop(bind, checkfirst=True)
    postgresql.ENUM(name="activity_module_enum").drop(bind, checkfirst=True)