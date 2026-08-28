"""Create audit_logs table.

Revision ID: a1b2c3d4e5f6
Revises: ai_module_migration
Create Date: 2026-08-02 00:00:00.000000

This migration introduces the enterprise Audit Log module. It creates:
    * Three PostgreSQL enum types: ``audit_action_enum``,
      ``audit_severity_enum``, ``audit_status_enum``.
    * The ``audit_logs`` table with a foreign key to ``users.id``.
    * Single-column and composite indexes for common query patterns.
    * CHECK constraints enforcing non-empty ``module``, ``action``, and
      ``description`` values.

.. note::
    Set the ``down_revision`` value below to the identifier of the most
    recent migration already present in this project's Alembic history
    before applying.
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "a1b2c3d4e5f6"
down_revision: Union[str, None] = "ai_module_migration"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


AUDIT_ACTION_VALUES = (
    "CREATE",
    "UPDATE",
    "DELETE",
    "LOGIN",
    "LOGOUT",
    "EXPORT",
    "IMPORT",
    "APPROVE",
    "REJECT",
    "ASSIGN",
    "UNASSIGN",
    "UPLOAD",
    "DOWNLOAD",
    "SEND",
    "GENERATE",
)

AUDIT_SEVERITY_VALUES = ("LOW", "MEDIUM", "HIGH", "CRITICAL")

AUDIT_STATUS_VALUES = ("SUCCESS", "FAILED")


def upgrade() -> None:
    """Applies the audit_logs schema changes.

    Creates the enum types, the ``audit_logs`` table, its foreign key,
    all single-column and composite indexes, and CHECK constraints.
    """
    bind = op.get_bind()

    # NOTE: create_type=False is intentional on all three ENUM
    # definitions below. The types are created explicitly via the
    # `.create(bind, checkfirst=True)` calls immediately following
    # these definitions. Without create_type=False, SQLAlchemy/Alembic
    # will *also* attempt to create the type as a side effect of
    # op.create_table(), which raises psycopg.errors.DuplicateObject
    # ("type ... already exists") once the explicit creation above has
    # already run — or on any database where the type was already
    # created by a prior partial run.
    audit_action_enum = postgresql.ENUM(
        *AUDIT_ACTION_VALUES,
        name="audit_action_enum",
        create_type=False,
    )
    audit_severity_enum = postgresql.ENUM(
        *AUDIT_SEVERITY_VALUES,
        name="audit_severity_enum",
        create_type=False,
    )
    audit_status_enum = postgresql.ENUM(
        *AUDIT_STATUS_VALUES,
        name="audit_status_enum",
        create_type=False,
    )

    audit_action_enum.create(bind, checkfirst=True)
    audit_severity_enum.create(bind, checkfirst=True)
    audit_status_enum.create(bind, checkfirst=True)

    op.create_table(
        "audit_logs",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
            nullable=False,
        ),
        sa.Column(
            "user_id",
            # NOTE: users.id is INTEGER in this database, not UUID.
            # audit_logs.user_id must match that type or the FK below
            # fails with psycopg.errors.DatatypeMismatchError.
            sa.Integer(),
            nullable=True,
        ),
        sa.Column("module", sa.String(length=100), nullable=False),
        sa.Column("entity_type", sa.String(length=100), nullable=True),
        sa.Column("entity_id", sa.String(length=255), nullable=True),
        sa.Column(
            "action",
            audit_action_enum,
            nullable=False,
        ),
        sa.Column("description", sa.Text(), nullable=False),
        sa.Column("old_data", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("new_data", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("ip_address", postgresql.INET(), nullable=True),
        sa.Column("user_agent", sa.Text(), nullable=True),
        sa.Column("request_id", sa.String(length=64), nullable=True),
        sa.Column(
            "status",
            audit_status_enum,
            nullable=False,
            server_default="SUCCESS",
        ),
        sa.Column(
            "severity",
            audit_severity_enum,
            nullable=False,
            server_default="LOW",
        ),
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
            ["user_id"],
            ["users.id"],
            name="fk_audit_logs_user_id_users",
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_audit_logs"),
        sa.CheckConstraint(
            "btrim(module) <> ''", name="ck_audit_logs_module_not_empty"
        ),
        sa.CheckConstraint(
            "action::text <> ''", name="ck_audit_logs_action_not_empty"
        ),
        sa.CheckConstraint(
            "btrim(description) <> ''", name="ck_audit_logs_description_not_empty"
        ),
    )

    # Single-column indexes.
    op.create_index("ix_audit_logs_user_id", "audit_logs", ["user_id"])
    op.create_index("ix_audit_logs_module", "audit_logs", ["module"])
    op.create_index("ix_audit_logs_entity_type", "audit_logs", ["entity_type"])
    op.create_index("ix_audit_logs_entity_id", "audit_logs", ["entity_id"])
    op.create_index("ix_audit_logs_action", "audit_logs", ["action"])
    op.create_index("ix_audit_logs_severity", "audit_logs", ["severity"])
    op.create_index("ix_audit_logs_status", "audit_logs", ["status"])
    op.create_index("ix_audit_logs_created_at", "audit_logs", ["created_at"])

    # Composite indexes for common query patterns.
    op.create_index(
        "ix_audit_logs_module_action", "audit_logs", ["module", "action"]
    )
    op.create_index(
        "ix_audit_logs_entity_type_entity_id",
        "audit_logs",
        ["entity_type", "entity_id"],
    )
    op.create_index(
        "ix_audit_logs_user_id_created_at",
        "audit_logs",
        ["user_id", "created_at"],
    )


def downgrade() -> None:
    """Reverts the audit_logs schema changes.

    Drops all indexes, the ``audit_logs`` table, and the associated enum
    types, in dependency-safe order.
    """
    bind = op.get_bind()

    op.drop_index("ix_audit_logs_user_id_created_at", table_name="audit_logs")
    op.drop_index("ix_audit_logs_entity_type_entity_id", table_name="audit_logs")
    op.drop_index("ix_audit_logs_module_action", table_name="audit_logs")

    op.drop_index("ix_audit_logs_created_at", table_name="audit_logs")
    op.drop_index("ix_audit_logs_status", table_name="audit_logs")
    op.drop_index("ix_audit_logs_severity", table_name="audit_logs")
    op.drop_index("ix_audit_logs_action", table_name="audit_logs")
    op.drop_index("ix_audit_logs_entity_id", table_name="audit_logs")
    op.drop_index("ix_audit_logs_entity_type", table_name="audit_logs")
    op.drop_index("ix_audit_logs_module", table_name="audit_logs")
    op.drop_index("ix_audit_logs_user_id", table_name="audit_logs")

    op.drop_table("audit_logs")

    postgresql.ENUM(name="audit_status_enum").drop(bind, checkfirst=True)
    postgresql.ENUM(name="audit_severity_enum").drop(bind, checkfirst=True)
    postgresql.ENUM(name="audit_action_enum").drop(bind, checkfirst=True)