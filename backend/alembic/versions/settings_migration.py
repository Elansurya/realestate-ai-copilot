"""Create settings table.

Revision ID: settings_module_migration
Revises: 20260802_notification_module
Create Date: 2026-08-02 00:00:00.000000

This migration introduces the enterprise Settings module. It creates:
    * Two PostgreSQL enum types: ``setting_category`` and
      ``setting_data_type``.
    * The ``settings`` table with foreign keys to ``users.id`` for
      ``created_by`` / ``updated_by``.
    * Single-column and composite indexes for common query patterns.
    * A unique constraint on (``category``, ``setting_key``).
    * CHECK constraints enforcing a non-empty ``setting_key`` and that a
      setting cannot be simultaneously encrypted and public.

.. note::
    Set the ``down_revision`` value below to the identifier of the most
    recent migration already present in this project's Alembic history
    (i.e. the current head, obtained via ``alembic heads``) before
    applying.
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "settings_module_migration"
down_revision: Union[str, None] = "20260802_notification_module"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


SETTING_CATEGORY_VALUES = (
    "GENERAL",
    "COMPANY",
    "SECURITY",
    "EMAIL",
    "SMS",
    "WHATSAPP",
    "AI",
    "DASHBOARD",
    "REPORTS",
    "NOTIFICATIONS",
    "STORAGE",
    "BACKUP",
    "THEME",
    "AUDIT",
    "SYSTEM",
)

SETTING_DATA_TYPE_VALUES = (
    "STRING",
    "INTEGER",
    "FLOAT",
    "BOOLEAN",
    "JSON",
    "ARRAY",
    "DATE",
    "DATETIME",
    "EMAIL",
    "URL",
    "PASSWORD",
)


def upgrade() -> None:
    """Applies the settings schema changes.

    Creates the enum types, the ``settings`` table, its foreign keys, all
    single-column and composite indexes, the unique constraint, and CHECK
    constraints.
    """
    bind = op.get_bind()

    op.execute("CREATE EXTENSION IF NOT EXISTS pgcrypto")

    # NOTE: create_type=False is intentional on both ENUM definitions
    # below. The types are created explicitly via the
    # `.create(bind, checkfirst=True)` calls immediately following
    # these definitions. Without create_type=False, SQLAlchemy/Alembic
    # will *also* attempt to create the type as a side effect of
    # op.create_table(), which raises a duplicate-object error
    # ("type ... already exists") once the explicit creation above has
    # already run — or on any database where the type was already
    # created by a prior partial run.
    setting_category_enum = postgresql.ENUM(
        *SETTING_CATEGORY_VALUES,
        name="setting_category",
        create_type=False,
    )
    setting_data_type_enum = postgresql.ENUM(
        *SETTING_DATA_TYPE_VALUES,
        name="setting_data_type",
        create_type=False,
    )

    setting_category_enum.create(bind, checkfirst=True)
    setting_data_type_enum.create(bind, checkfirst=True)

    op.create_table(
        "settings",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
            nullable=False,
        ),
        sa.Column(
            "category",
            setting_category_enum,
            nullable=False,
        ),
        sa.Column("setting_key", sa.String(length=150), nullable=False),
        sa.Column(
            "setting_value", postgresql.JSONB(astext_type=sa.Text()), nullable=True
        ),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column(
            "data_type",
            setting_data_type_enum,
            nullable=False,
            server_default="STRING",
        ),
        sa.Column(
            "validation_rules",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=True,
        ),
        sa.Column(
            "is_public",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("false"),
        ),
        sa.Column(
            "is_editable",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("true"),
        ),
        sa.Column(
            "is_encrypted",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("false"),
        ),
        sa.Column("created_by", sa.Integer(), nullable=True),
        sa.Column("updated_by", sa.Integer(), nullable=True),
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
            ["created_by"],
            ["users.id"],
            name="fk_settings_created_by_users",
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["updated_by"],
            ["users.id"],
            name="fk_settings_updated_by_users",
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_settings"),
        sa.UniqueConstraint(
            "category", "setting_key", name="uq_settings_category_setting_key"
        ),
        sa.CheckConstraint(
            "btrim(setting_key) <> ''", name="ck_settings_setting_key_not_empty"
        ),
        sa.CheckConstraint(
            "NOT (is_encrypted AND is_public)",
            name="ck_settings_encrypted_not_public",
        ),
    )

    # Single-column indexes.
    op.create_index("ix_settings_category", "settings", ["category"])
    op.create_index("ix_settings_setting_key", "settings", ["setting_key"])
    op.create_index("ix_settings_is_public", "settings", ["is_public"])
    op.create_index("ix_settings_is_editable", "settings", ["is_editable"])
    op.create_index("ix_settings_created_by", "settings", ["created_by"])
    op.create_index("ix_settings_updated_by", "settings", ["updated_by"])

    # Composite indexes for common query patterns.
    op.create_index(
        "ix_settings_category_is_public", "settings", ["category", "is_public"]
    )
    op.create_index(
        "ix_settings_category_is_editable", "settings", ["category", "is_editable"]
    )


def downgrade() -> None:
    """Reverts the settings schema changes.

    Drops all indexes, the ``settings`` table, and the associated enum
    types, in dependency-safe order.
    """
    bind = op.get_bind()

    op.drop_index("ix_settings_category_is_editable", table_name="settings")
    op.drop_index("ix_settings_category_is_public", table_name="settings")

    op.drop_index("ix_settings_updated_by", table_name="settings")
    op.drop_index("ix_settings_created_by", table_name="settings")
    op.drop_index("ix_settings_is_editable", table_name="settings")
    op.drop_index("ix_settings_is_public", table_name="settings")
    op.drop_index("ix_settings_setting_key", table_name="settings")
    op.drop_index("ix_settings_category", table_name="settings")

    op.drop_table("settings")

    postgresql.ENUM(name="setting_data_type").drop(bind, checkfirst=True)
    postgresql.ENUM(name="setting_category").drop(bind, checkfirst=True)