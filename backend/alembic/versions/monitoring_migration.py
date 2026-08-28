"""monitoring module: system_health table

Revision ID: 20260804_0002
Revises: webhook_module_0001
Create Date: 2026-08-04 00:00:00.000000

This migration introduces the Enterprise Monitoring & Health module.
It creates:
    * Two PostgreSQL enum types: ``health_status_enum``,
      ``component_type_enum``.
    * The ``system_health`` table, including a self-referential
      foreign key (``parent_component_id`` -> ``system_health.id``)
      and foreign keys to ``users.id`` for ``created_by_id`` /
      ``updated_by_id`` / ``deleted_by_id``.
    * Single-column and composite indexes for common query patterns
      (per-type/status dashboards, active/deleted filtering, latest
      health-check lookups).
    * A uniqueness constraint on (``component_name``, ``component_type``).
    * CHECK constraints enforcing a non-empty ``component_name``,
      bounded percentage metrics (0-100), a non-negative
      ``response_time_ms``, non-negative error/warning counters,
      soft-delete column consistency, and that a component cannot be
      its own parent.

Mirrors `app/models/monitoring.py` exactly: column types/nullability,
server defaults, indexes (single-column + composite), unique
constraints, and check constraints.

Assumes (per `app/models/monitoring.py` module docstring):
    - The `pgcrypto` extension is already enabled by an earlier
      migration (required for `gen_random_uuid()`).
    - The `users` table already exists (FK target of
      `system_health.created_by_id` / `updated_by_id` / `deleted_by_id`).
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "20260804_0002"
down_revision: Union[str, None] = "webhook_module_0001"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


HEALTH_STATUS_VALUES = (
    "HEALTHY",
    "DEGRADED",
    "UNHEALTHY",
    "DOWN",
    "MAINTENANCE",
    "UNKNOWN",
)

COMPONENT_TYPE_VALUES = (
    "APPLICATION",
    "DATABASE",
    "STORAGE",
    "AI_PROVIDER",
    "EXTERNAL_INTEGRATION",
    "NOTIFICATION_SERVICE",
    "DOCUMENT_STORAGE",
    "WORKFLOW_ENGINE",
    "SEARCH_ENGINE",
)


def upgrade() -> None:
    """Applies the system_health schema changes.

    Creates the enum types, the ``system_health`` table, its foreign
    keys (including the self-referential parent/child relationship),
    all single-column and composite indexes, the uniqueness
    constraint on (``component_name``, ``component_type``), and CHECK
    constraints.
    """
    op.execute("CREATE EXTENSION IF NOT EXISTS pgcrypto")

    bind = op.get_bind()

    # NOTE: create_type=False is intentional on both ENUM definitions
    # below. The types are created explicitly via the
    # `.create(bind, checkfirst=True)` calls immediately following
    # these definitions. Without create_type=False, SQLAlchemy/Alembic
    # will *also* attempt to create the type as a side effect of
    # op.create_table(), which raises a duplicate-object error
    # ("type ... already exists") once the explicit creation above has
    # already run — or on any database where the type was already
    # created by a prior partial run.
    health_status_enum = postgresql.ENUM(
        *HEALTH_STATUS_VALUES,
        name="health_status_enum",
        create_type=False,
    )
    component_type_enum = postgresql.ENUM(
        *COMPONENT_TYPE_VALUES,
        name="component_type_enum",
        create_type=False,
    )

    health_status_enum.create(bind, checkfirst=True)
    component_type_enum.create(bind, checkfirst=True)

    op.create_table(
        "system_health",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            server_default=sa.text("gen_random_uuid()"),
            primary_key=True,
            nullable=False,
        ),
        sa.Column(
            "parent_component_id",
            postgresql.UUID(as_uuid=True),
            nullable=True,
        ),
        sa.Column("component_name", sa.String(length=150), nullable=False),
        sa.Column("component_type", component_type_enum, nullable=False),
        sa.Column(
            "status",
            health_status_enum,
            nullable=False,
            server_default="UNKNOWN",
        ),
        sa.Column("cpu_usage_percent", sa.Numeric(5, 2), nullable=True),
        sa.Column("memory_usage_percent", sa.Numeric(5, 2), nullable=True),
        sa.Column("disk_usage_percent", sa.Numeric(5, 2), nullable=True),
        sa.Column("response_time_ms", sa.Numeric(10, 3), nullable=True),
        sa.Column(
            "error_count", sa.Integer(), nullable=False, server_default="0"
        ),
        sa.Column(
            "warning_count", sa.Integer(), nullable=False, server_default="0"
        ),
        sa.Column(
            "last_health_check_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("last_success_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_failure_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("status_message", sa.Text(), nullable=True),
        sa.Column(
            "metadata",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=True,
        ),
        sa.Column(
            "is_active", sa.Boolean(), nullable=False, server_default=sa.true()
        ),
        sa.Column(
            "is_deleted", sa.Boolean(), nullable=False, server_default=sa.false()
        ),
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("deleted_by_id", sa.Integer(), nullable=True),
        sa.Column("created_by_id", sa.Integer(), nullable=True),
        sa.Column("updated_by_id", sa.Integer(), nullable=True),
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
            ["parent_component_id"],
            ["system_health.id"],
            name="fk_system_health_parent_component_id_system_health",
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["deleted_by_id"],
            ["users.id"],
            name="fk_system_health_deleted_by_id_users",
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["created_by_id"],
            ["users.id"],
            name="fk_system_health_created_by_id_users",
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["updated_by_id"],
            ["users.id"],
            name="fk_system_health_updated_by_id_users",
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_system_health"),
        sa.UniqueConstraint(
            "component_name",
            "component_type",
            name="uq_system_health_component_name_component_type",
        ),
        sa.CheckConstraint(
            "length(trim(component_name)) > 0",
            name="ck_system_health_component_name_not_blank",
        ),
        sa.CheckConstraint(
            "cpu_usage_percent IS NULL OR "
            "(cpu_usage_percent >= 0 AND cpu_usage_percent <= 100)",
            name="ck_system_health_cpu_usage_range",
        ),
        sa.CheckConstraint(
            "memory_usage_percent IS NULL OR "
            "(memory_usage_percent >= 0 AND memory_usage_percent <= 100)",
            name="ck_system_health_memory_usage_range",
        ),
        sa.CheckConstraint(
            "disk_usage_percent IS NULL OR "
            "(disk_usage_percent >= 0 AND disk_usage_percent <= 100)",
            name="ck_system_health_disk_usage_range",
        ),
        sa.CheckConstraint(
            "response_time_ms IS NULL OR response_time_ms >= 0",
            name="ck_system_health_response_time_non_negative",
        ),
        sa.CheckConstraint(
            "error_count >= 0", name="ck_system_health_error_count_non_negative"
        ),
        sa.CheckConstraint(
            "warning_count >= 0",
            name="ck_system_health_warning_count_non_negative",
        ),
        sa.CheckConstraint(
            "(is_deleted = false AND deleted_at IS NULL AND deleted_by_id IS NULL) "
            "OR (is_deleted = true AND deleted_at IS NOT NULL)",
            name="ck_system_health_soft_delete_consistency",
        ),
        sa.CheckConstraint(
            "parent_component_id IS NULL OR parent_component_id != id",
            name="ck_system_health_parent_not_self",
        ),
    )

    # Single-column indexes.
    op.create_index(
        "ix_system_health_parent_component_id",
        "system_health",
        ["parent_component_id"],
        unique=False,
    )
    op.create_index(
        "ix_system_health_component_name",
        "system_health",
        ["component_name"],
        unique=False,
    )
    op.create_index(
        "ix_system_health_component_type",
        "system_health",
        ["component_type"],
        unique=False,
    )
    op.create_index(
        "ix_system_health_status", "system_health", ["status"], unique=False
    )
    op.create_index(
        "ix_system_health_response_time_ms",
        "system_health",
        ["response_time_ms"],
        unique=False,
    )
    op.create_index(
        "ix_system_health_last_health_check_at",
        "system_health",
        ["last_health_check_at"],
        unique=False,
    )
    op.create_index(
        "ix_system_health_is_deleted", "system_health", ["is_deleted"], unique=False
    )
    op.create_index(
        "ix_system_health_deleted_by_id",
        "system_health",
        ["deleted_by_id"],
        unique=False,
    )
    op.create_index(
        "ix_system_health_created_by_id",
        "system_health",
        ["created_by_id"],
        unique=False,
    )
    op.create_index(
        "ix_system_health_updated_by_id",
        "system_health",
        ["updated_by_id"],
        unique=False,
    )

    # Composite indexes.
    op.create_index(
        "ix_system_health_type_status",
        "system_health",
        ["component_type", "status"],
        unique=False,
    )
    op.create_index(
        "ix_system_health_active_deleted",
        "system_health",
        ["is_active", "is_deleted"],
        unique=False,
    )
    op.create_index(
        "ix_system_health_status_last_check",
        "system_health",
        ["status", "last_health_check_at"],
        unique=False,
    )


def downgrade() -> None:
    """Reverts the system_health schema changes.

    Drops the ``system_health`` table (which drops its indexes and
    constraints along with it) and the enum types created for it.
    Does not drop the ``pgcrypto`` extension, since other tables in
    the project also depend on it.
    """
    op.drop_index("ix_system_health_status_last_check", table_name="system_health")
    op.drop_index("ix_system_health_active_deleted", table_name="system_health")
    op.drop_index("ix_system_health_type_status", table_name="system_health")
    op.drop_index("ix_system_health_updated_by_id", table_name="system_health")
    op.drop_index("ix_system_health_created_by_id", table_name="system_health")
    op.drop_index("ix_system_health_deleted_by_id", table_name="system_health")
    op.drop_index("ix_system_health_is_deleted", table_name="system_health")
    op.drop_index(
        "ix_system_health_last_health_check_at", table_name="system_health"
    )
    op.drop_index("ix_system_health_response_time_ms", table_name="system_health")
    op.drop_index("ix_system_health_status", table_name="system_health")
    op.drop_index("ix_system_health_component_type", table_name="system_health")
    op.drop_index("ix_system_health_component_name", table_name="system_health")
    op.drop_index(
        "ix_system_health_parent_component_id", table_name="system_health"
    )

    op.drop_table("system_health")

    bind = op.get_bind()
    postgresql.ENUM(name="component_type_enum").drop(bind, checkfirst=True)
    postgresql.ENUM(name="health_status_enum").drop(bind, checkfirst=True)