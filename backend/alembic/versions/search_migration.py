"""global search module

Revision ID: 20260803_0004
Revises: 20260803_0003
Create Date: 2026-08-03 00:00:00.000000

This migration introduces the enterprise Global Search module. It creates:
    * Two PostgreSQL enum types: ``search_module_enum``,
      ``search_type_enum``.
    * The ``search_history`` table with a foreign key to ``users.id``
      for ``user_id``.
    * Single-column and composite indexes for common query patterns
      (per-user search feeds, module/date dashboards, search-type
      analytics).
    * CHECK constraints enforcing a non-empty ``search_query``,
      non-negative ``result_count``, non-negative
      ``execution_time_ms``, and soft-delete column consistency.

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
revision: str = "20260803_0004"
down_revision: Union[str, None] = "20260803_0003"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


SEARCH_MODULE_VALUES = (
    "customer",
    "lead",
    "property",
    "booking",
    "payment",
    "task",
    "document",
    "workflow",
    "activity",
    "audit_log",
    "notification",
    "report",
)

SEARCH_TYPE_VALUES = ("quick", "advanced", "filtered", "global", "saved")


def upgrade() -> None:
    """Applies the search_history schema changes.

    Creates the enum types, the ``search_history`` table, its foreign
    key, all single-column and composite indexes, and CHECK
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
    search_module_enum = postgresql.ENUM(
        *SEARCH_MODULE_VALUES,
        name="search_module_enum",
        create_type=False,
    )
    search_type_enum = postgresql.ENUM(
        *SEARCH_TYPE_VALUES,
        name="search_type_enum",
        create_type=False,
    )

    search_module_enum.create(bind, checkfirst=True)
    search_type_enum.create(bind, checkfirst=True)

    op.create_table(
        "search_history",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            server_default=sa.text("gen_random_uuid()"),
            primary_key=True,
            nullable=False,
        ),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("search_query", sa.String(length=500), nullable=False),
        sa.Column("module", search_module_enum, nullable=True),
        sa.Column(
            "search_type",
            search_type_enum,
            nullable=False,
            server_default="global",
        ),
        sa.Column(
            "filters", postgresql.JSONB(astext_type=sa.Text()), nullable=True
        ),
        sa.Column(
            "result_count", sa.Integer(), nullable=False, server_default="0"
        ),
        sa.Column(
            "execution_time_ms",
            sa.Numeric(precision=10, scale=3),
            nullable=False,
            server_default="0",
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
            ["user_id"],
            ["users.id"],
            name="fk_search_history_user_id_users",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_search_history"),
        sa.CheckConstraint(
            "btrim(search_query) <> ''",
            name="ck_search_history_query_not_empty",
        ),
        sa.CheckConstraint(
            "result_count >= 0",
            name="ck_search_history_result_count_non_negative",
        ),
        sa.CheckConstraint(
            "execution_time_ms >= 0",
            name="ck_search_history_execution_time_non_negative",
        ),
        sa.CheckConstraint(
            "(is_deleted IS FALSE AND deleted_at IS NULL) "
            "OR (is_deleted IS TRUE AND deleted_at IS NOT NULL)",
            name="ck_search_history_soft_delete_consistency",
        ),
    )

    # Single-column indexes.
    op.create_index(
        "ix_search_history_user_id", "search_history", ["user_id"]
    )
    op.create_index(
        "ix_search_history_module", "search_history", ["module"]
    )
    op.create_index(
        "ix_search_history_search_type", "search_history", ["search_type"]
    )
    op.create_index(
        "ix_search_history_created_at", "search_history", ["created_at"]
    )
    op.create_index(
        "ix_search_history_is_deleted", "search_history", ["is_deleted"]
    )

    # Composite indexes for common query patterns.
    op.create_index(
        "ix_search_history_user_id_created_at",
        "search_history",
        ["user_id", "created_at"],
    )
    op.create_index(
        "ix_search_history_user_id_module",
        "search_history",
        ["user_id", "module"],
    )
    op.create_index(
        "ix_search_history_module_created_at",
        "search_history",
        ["module", "created_at"],
    )
    op.create_index(
        "ix_search_history_search_type_created_at",
        "search_history",
        ["search_type", "created_at"],
    )


def downgrade() -> None:
    """Reverts the search_history schema changes.

    Drops all indexes, the ``search_history`` table, and the
    associated enum types, in dependency-safe order.
    """
    bind = op.get_bind()

    op.drop_index(
        "ix_search_history_search_type_created_at", table_name="search_history"
    )
    op.drop_index(
        "ix_search_history_module_created_at", table_name="search_history"
    )
    op.drop_index(
        "ix_search_history_user_id_module", table_name="search_history"
    )
    op.drop_index(
        "ix_search_history_user_id_created_at", table_name="search_history"
    )

    op.drop_index("ix_search_history_is_deleted", table_name="search_history")
    op.drop_index("ix_search_history_created_at", table_name="search_history")
    op.drop_index("ix_search_history_search_type", table_name="search_history")
    op.drop_index("ix_search_history_module", table_name="search_history")
    op.drop_index("ix_search_history_user_id", table_name="search_history")

    op.drop_table("search_history")

    postgresql.ENUM(name="search_type_enum").drop(bind, checkfirst=True)
    postgresql.ENUM(name="search_module_enum").drop(bind, checkfirst=True)