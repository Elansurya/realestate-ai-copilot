"""integration management module

Revision ID: 20260803_0005
Revises: 20260803_0004
Create Date: 2026-08-03 00:00:00.000000

This migration introduces the enterprise Integration Management
module. It creates:
    * Four PostgreSQL enum types: ``integration_type_enum``,
      ``integration_provider_enum``, ``integration_status_enum``,
      ``authentication_type_enum``.
    * The ``integrations`` table with a foreign key to ``users.id``
      for ``created_by_id``.
    * Single-column and composite indexes for common query patterns
      (per-type dashboards, provider/status lookups, default-integration
      resolution).
    * A uniqueness constraint on ``name``.
    * CHECK constraints enforcing a non-empty ``name``, a positive
      ``timeout_seconds``, a non-negative ``retry_count``, a positive
      (or NULL) ``rate_limit_per_minute``, and soft-delete column
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
revision: str = "20260803_0005"
down_revision: Union[str, None] = "20260803_0004"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


INTEGRATION_TYPE_VALUES = (
    "email",
    "sms",
    "whatsapp",
    "calendar",
    "storage",
    "notification",
    "ai_provider",
    "payment_gateway",
    "webhook",
    "custom_api",
)

INTEGRATION_PROVIDER_VALUES = (
    "smtp",
    "sms_provider",
    "whatsapp_business",
    "google_calendar",
    "google_drive",
    "aws_s3",
    "azure_blob_storage",
    "firebase",
    "openai",
    "anthropic",
    "gemini",
    "hugging_face",
    "razorpay",
    "stripe",
    "webhook_target",
    "custom_rest_api",
)

INTEGRATION_STATUS_VALUES = (
    "active",
    "inactive",
    "pending_verification",
    "failed",
    "disabled",
)

AUTHENTICATION_TYPE_VALUES = (
    "api_key",
    "oauth2",
    "basic_auth",
    "bearer_token",
    "hmac_signature",
    "none",
)


def upgrade() -> None:
    """Applies the integrations schema changes.

    Creates the enum types, the ``integrations`` table, its foreign
    key, all single-column and composite indexes, the uniqueness
    constraint on ``name``, and CHECK constraints.
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
    integration_type_enum = postgresql.ENUM(
        *INTEGRATION_TYPE_VALUES,
        name="integration_type_enum",
        create_type=False,
    )
    integration_provider_enum = postgresql.ENUM(
        *INTEGRATION_PROVIDER_VALUES,
        name="integration_provider_enum",
        create_type=False,
    )
    integration_status_enum = postgresql.ENUM(
        *INTEGRATION_STATUS_VALUES,
        name="integration_status_enum",
        create_type=False,
    )
    authentication_type_enum = postgresql.ENUM(
        *AUTHENTICATION_TYPE_VALUES,
        name="authentication_type_enum",
        create_type=False,
    )

    integration_type_enum.create(bind, checkfirst=True)
    integration_provider_enum.create(bind, checkfirst=True)
    integration_status_enum.create(bind, checkfirst=True)
    authentication_type_enum.create(bind, checkfirst=True)

    op.create_table(
        "integrations",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            server_default=sa.text("gen_random_uuid()"),
            primary_key=True,
            nullable=False,
        ),
        sa.Column("name", sa.String(length=150), nullable=False),
        sa.Column("provider", integration_provider_enum, nullable=False),
        sa.Column("integration_type", integration_type_enum, nullable=False),
        sa.Column(
            "status",
            integration_status_enum,
            nullable=False,
            server_default="pending_verification",
        ),
        sa.Column(
            "authentication_type",
            authentication_type_enum,
            nullable=False,
            server_default="api_key",
        ),
        sa.Column(
            "configuration",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=True,
        ),
        sa.Column(
            "credentials",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=True,
        ),
        sa.Column("base_url", sa.String(length=500), nullable=True),
        sa.Column("api_version", sa.String(length=50), nullable=True),
        sa.Column("webhook_url", sa.String(length=500), nullable=True),
        sa.Column(
            "timeout_seconds", sa.Integer(), nullable=False, server_default="30"
        ),
        sa.Column("retry_count", sa.Integer(), nullable=False, server_default="3"),
        sa.Column("rate_limit_per_minute", sa.Integer(), nullable=True),
        sa.Column(
            "is_default", sa.Boolean(), nullable=False, server_default=sa.false()
        ),
        sa.Column("last_sync_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "last_health_check_at", sa.DateTime(timezone=True), nullable=True
        ),
        sa.Column("created_by_id", sa.Integer(), nullable=True),
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
            ["created_by_id"],
            ["users.id"],
            name="fk_integrations_created_by_id_users",
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_integrations"),
        sa.UniqueConstraint("name", name="uq_integrations_name"),
        sa.CheckConstraint(
            "btrim(name) <> ''",
            name="ck_integrations_name_not_empty",
        ),
        sa.CheckConstraint(
            "timeout_seconds > 0",
            name="ck_integrations_timeout_seconds_positive",
        ),
        sa.CheckConstraint(
            "retry_count >= 0",
            name="ck_integrations_retry_count_non_negative",
        ),
        sa.CheckConstraint(
            "rate_limit_per_minute IS NULL OR rate_limit_per_minute > 0",
            name="ck_integrations_rate_limit_positive",
        ),
        sa.CheckConstraint(
            "(is_deleted IS FALSE AND deleted_at IS NULL) "
            "OR (is_deleted IS TRUE AND deleted_at IS NOT NULL)",
            name="ck_integrations_soft_delete_consistency",
        ),
    )

    # Single-column indexes.
    op.create_index(
        "ix_integrations_provider", "integrations", ["provider"], unique=False
    )
    op.create_index(
        "ix_integrations_integration_type",
        "integrations",
        ["integration_type"],
        unique=False,
    )
    op.create_index(
        "ix_integrations_status", "integrations", ["status"], unique=False
    )
    op.create_index(
        "ix_integrations_authentication_type",
        "integrations",
        ["authentication_type"],
        unique=False,
    )
    op.create_index(
        "ix_integrations_created_by_id",
        "integrations",
        ["created_by_id"],
        unique=False,
    )
    op.create_index(
        "ix_integrations_is_default", "integrations", ["is_default"], unique=False
    )
    op.create_index(
        "ix_integrations_is_deleted", "integrations", ["is_deleted"], unique=False
    )
    op.create_index(
        "ix_integrations_created_at", "integrations", ["created_at"], unique=False
    )
    op.create_index(
        "ix_integrations_last_sync_at",
        "integrations",
        ["last_sync_at"],
        unique=False,
    )
    op.create_index(
        "ix_integrations_last_health_check_at",
        "integrations",
        ["last_health_check_at"],
        unique=False,
    )

    # Composite indexes.
    op.create_index(
        "ix_integrations_integration_type_status",
        "integrations",
        ["integration_type", "status"],
        unique=False,
    )
    op.create_index(
        "ix_integrations_integration_type_provider",
        "integrations",
        ["integration_type", "provider"],
        unique=False,
    )
    op.create_index(
        "ix_integrations_provider_status",
        "integrations",
        ["provider", "status"],
        unique=False,
    )
    op.create_index(
        "ix_integrations_integration_type_is_default",
        "integrations",
        ["integration_type", "is_default"],
        unique=False,
    )


def downgrade() -> None:
    """Reverts the integrations schema changes.

    Drops the ``integrations`` table (which drops its indexes and
    constraints along with it) and the enum types created for it.
    Does not drop the ``pgcrypto`` extension, since other tables in
    the project also depend on it.
    """
    op.drop_index(
        "ix_integrations_integration_type_is_default", table_name="integrations"
    )
    op.drop_index("ix_integrations_provider_status", table_name="integrations")
    op.drop_index(
        "ix_integrations_integration_type_provider", table_name="integrations"
    )
    op.drop_index(
        "ix_integrations_integration_type_status", table_name="integrations"
    )
    op.drop_index(
        "ix_integrations_last_health_check_at", table_name="integrations"
    )
    op.drop_index("ix_integrations_last_sync_at", table_name="integrations")
    op.drop_index("ix_integrations_created_at", table_name="integrations")
    op.drop_index("ix_integrations_is_deleted", table_name="integrations")
    op.drop_index("ix_integrations_is_default", table_name="integrations")
    op.drop_index("ix_integrations_created_by_id", table_name="integrations")
    op.drop_index(
        "ix_integrations_authentication_type", table_name="integrations"
    )
    op.drop_index("ix_integrations_status", table_name="integrations")
    op.drop_index("ix_integrations_integration_type", table_name="integrations")
    op.drop_index("ix_integrations_provider", table_name="integrations")

    op.drop_table("integrations")

    bind = op.get_bind()
    postgresql.ENUM(name="authentication_type_enum").drop(bind, checkfirst=True)
    postgresql.ENUM(name="integration_status_enum").drop(bind, checkfirst=True)
    postgresql.ENUM(name="integration_provider_enum").drop(bind, checkfirst=True)
    postgresql.ENUM(name="integration_type_enum").drop(bind, checkfirst=True)