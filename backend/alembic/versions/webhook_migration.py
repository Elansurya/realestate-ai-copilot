"""webhook module: webhooks + webhook_logs tables

backend/alembic/versions/webhook_migration.py

Creates the Enterprise Webhook module schema:
    - Native PostgreSQL ENUM types:
        * webhook_status_enum
        * webhook_event_enum
        * webhook_delivery_status_enum
        * webhook_authentication_type_enum
    - ``webhooks``      -- registered outbound webhook subscriptions.
    - ``webhook_logs``  -- immutable, append-only delivery attempt audit
      trail for each ``Webhook`` (FK ``ondelete="CASCADE"``).

Mirrors `app/models/webhook.py` exactly: column types/nullability,
server defaults, indexes (single-column + composite), unique
constraints, and check constraints.

Assumes (per `app/models/webhook.py` module docstring):
    - The `pgcrypto` extension is already enabled by an earlier
      migration (required for `gen_random_uuid()`).
    - The `users` table already exists (FK target of
      `webhooks.created_by_id`).

Revision ID: webhook_module_0001
Revises: 20260803_0005
    NOTE: `down_revision` below is a placeholder. Before running,
    set it to the actual current Alembic head of this project, e.g.
    via `alembic heads`, so this migration chains onto it correctly.
Create Date: 2026-08-04
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# ---------------------------------------------------------------------------
# Revision identifiers, used by Alembic.
# ---------------------------------------------------------------------------
revision: str = "webhook_module_0001"
down_revision: Union[str, None] = "20260803_0005"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


# ---------------------------------------------------------------------------
# Native PostgreSQL ENUM type definitions (shared by upgrade/downgrade).
# ---------------------------------------------------------------------------
_webhook_status_enum = postgresql.ENUM(
    "active",
    "inactive",
    "suspended",
    "failed",
    name="webhook_status_enum",
    create_type=False,
)

_webhook_event_enum = postgresql.ENUM(
    "lead_created",
    "lead_updated",
    "lead_converted",
    "deal_created",
    "deal_updated",
    "deal_closed",
    "task_created",
    "task_completed",
    "document_uploaded",
    "payment_received",
    "booking_created",
    "booking_cancelled",
    "user_created",
    "custom",
    name="webhook_event_enum",
    create_type=False,
)

_webhook_delivery_status_enum = postgresql.ENUM(
    "pending",
    "success",
    "failed",
    "retrying",
    "dead_lettered",
    name="webhook_delivery_status_enum",
    create_type=False,
)

_webhook_authentication_type_enum = postgresql.ENUM(
    "none",
    "hmac_signature",
    "bearer_token",
    "api_key",
    "basic_auth",
    name="webhook_authentication_type_enum",
    create_type=False,
)


def upgrade() -> None:
    """Creates the `webhooks` and `webhook_logs` tables, their native
    PostgreSQL enum types, indexes, foreign keys, and check constraints.
    """
    bind = op.get_bind()

    # ------------------------------------------------------------------
    # Enum types (created explicitly, first, so both tables can share
    # `create_type=False` on their columns and avoid double-creation).
    # ------------------------------------------------------------------
    _webhook_status_enum.create(bind, checkfirst=True)
    _webhook_event_enum.create(bind, checkfirst=True)
    _webhook_delivery_status_enum.create(bind, checkfirst=True)
    _webhook_authentication_type_enum.create(bind, checkfirst=True)

    # ------------------------------------------------------------------
    # webhooks
    # ------------------------------------------------------------------
    op.create_table(
        "webhooks",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            server_default=sa.text("gen_random_uuid()"),
            nullable=False,
        ),
        sa.Column("name", sa.String(length=150), nullable=False),
        sa.Column("event", _webhook_event_enum, nullable=False),
        sa.Column("target_url", sa.String(length=2048), nullable=False),
        sa.Column(
            "http_method",
            sa.String(length=10),
            server_default="POST",
            nullable=False,
        ),
        sa.Column(
            "status",
            _webhook_status_enum,
            server_default="active",
            nullable=False,
        ),
        sa.Column(
            "authentication_type",
            _webhook_authentication_type_enum,
            server_default="hmac_signature",
            nullable=False,
        ),
        sa.Column("secret_key", sa.String(length=255), nullable=True),
        sa.Column("custom_headers", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("payload_template", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("retry_count", sa.Integer(), server_default="3", nullable=False),
        sa.Column("timeout_seconds", sa.Integer(), server_default="30", nullable=False),
        sa.Column("rate_limit_per_minute", sa.Integer(), nullable=True),
        sa.Column("enabled", sa.Boolean(), server_default="true", nullable=False),
        sa.Column("last_delivery_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_success_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_failure_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_by_id", sa.Integer(), nullable=True),
        sa.Column("is_deleted", sa.Boolean(), server_default="false", nullable=False),
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
        sa.PrimaryKeyConstraint("id", name=op.f("pk_webhooks")),
        sa.ForeignKeyConstraint(
            ["created_by_id"],
            ["users.id"],
            name=op.f("fk_webhooks_created_by_id_users"),
            ondelete="SET NULL",
        ),
        sa.UniqueConstraint("name", name="uq_webhooks_name"),
        sa.CheckConstraint("btrim(name) <> ''", name="ck_webhooks_name_not_empty"),
        sa.CheckConstraint(
            "btrim(target_url) <> ''", name="ck_webhooks_target_url_not_empty"
        ),
        sa.CheckConstraint(
            "http_method IN ('GET','POST','PUT','PATCH','DELETE')",
            name="ck_webhooks_http_method_valid",
        ),
        sa.CheckConstraint(
            "retry_count >= 0", name="ck_webhooks_retry_count_non_negative"
        ),
        sa.CheckConstraint(
            "timeout_seconds > 0", name="ck_webhooks_timeout_seconds_positive"
        ),
        sa.CheckConstraint(
            "rate_limit_per_minute IS NULL OR rate_limit_per_minute > 0",
            name="ck_webhooks_rate_limit_positive",
        ),
        sa.CheckConstraint(
            "(is_deleted IS FALSE AND deleted_at IS NULL) "
            "OR (is_deleted IS TRUE AND deleted_at IS NOT NULL)",
            name="ck_webhooks_soft_delete_consistency",
        ),
        sa.CheckConstraint(
            "(authentication_type = 'none' AND secret_key IS NULL) "
            "OR (authentication_type <> 'none' AND secret_key IS NOT NULL)",
            name="ck_webhooks_secret_key_required_when_authenticated",
        ),
    )

    op.create_index("ix_webhooks_event", "webhooks", ["event"])
    op.create_index("ix_webhooks_status", "webhooks", ["status"])
    op.create_index(
        "ix_webhooks_authentication_type", "webhooks", ["authentication_type"]
    )
    op.create_index("ix_webhooks_enabled", "webhooks", ["enabled"])
    op.create_index("ix_webhooks_created_by_id", "webhooks", ["created_by_id"])
    op.create_index("ix_webhooks_is_deleted", "webhooks", ["is_deleted"])
    op.create_index("ix_webhooks_created_at", "webhooks", ["created_at"])
    op.create_index(
        "ix_webhooks_last_delivery_at", "webhooks", ["last_delivery_at"]
    )
    op.create_index(
        "ix_webhooks_event_status", "webhooks", ["event", "status"]
    )
    op.create_index(
        "ix_webhooks_event_enabled", "webhooks", ["event", "enabled"]
    )
    op.create_index(
        "ix_webhooks_status_enabled", "webhooks", ["status", "enabled"]
    )

    # ------------------------------------------------------------------
    # webhook_logs
    # ------------------------------------------------------------------
    op.create_table(
        "webhook_logs",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            server_default=sa.text("gen_random_uuid()"),
            nullable=False,
        ),
        sa.Column("webhook_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column(
            "delivery_status",
            _webhook_delivery_status_enum,
            server_default="pending",
            nullable=False,
        ),
        sa.Column("response_code", sa.Integer(), nullable=True),
        sa.Column("response_body", sa.Text(), nullable=True),
        sa.Column("attempt_count", sa.Integer(), server_default="1", nullable=False),
        sa.Column("duration_ms", sa.Numeric(precision=10, scale=3), nullable=True),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column(
            "delivered_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_webhook_logs")),
        sa.ForeignKeyConstraint(
            ["webhook_id"],
            ["webhooks.id"],
            name=op.f("fk_webhook_logs_webhook_id_webhooks"),
            ondelete="CASCADE",
        ),
        sa.CheckConstraint(
            "attempt_count > 0", name="ck_webhook_logs_attempt_count_positive"
        ),
        sa.CheckConstraint(
            "duration_ms IS NULL OR duration_ms >= 0",
            name="ck_webhook_logs_duration_ms_non_negative",
        ),
        sa.CheckConstraint(
            "response_code IS NULL OR (response_code >= 100 AND response_code < 600)",
            name="ck_webhook_logs_response_code_valid_range",
        ),
    )

    op.create_index("ix_webhook_logs_webhook_id", "webhook_logs", ["webhook_id"])
    op.create_index(
        "ix_webhook_logs_delivery_status", "webhook_logs", ["delivery_status"]
    )
    op.create_index("ix_webhook_logs_delivered_at", "webhook_logs", ["delivered_at"])
    op.create_index("ix_webhook_logs_created_at", "webhook_logs", ["created_at"])
    op.create_index(
        "ix_webhook_logs_webhook_id_delivered_at",
        "webhook_logs",
        ["webhook_id", "delivered_at"],
    )
    op.create_index(
        "ix_webhook_logs_webhook_id_delivery_status",
        "webhook_logs",
        ["webhook_id", "delivery_status"],
    )
    op.create_index(
        "ix_webhook_logs_delivery_status_delivered_at",
        "webhook_logs",
        ["delivery_status", "delivered_at"],
    )


def downgrade() -> None:
    """Drops the `webhook_logs` and `webhooks` tables and their
    associated native PostgreSQL enum types, in dependency-safe order.
    """
    bind = op.get_bind()

    # webhook_logs first: it FKs to webhooks.
    op.drop_index(
        "ix_webhook_logs_delivery_status_delivered_at", table_name="webhook_logs"
    )
    op.drop_index(
        "ix_webhook_logs_webhook_id_delivery_status", table_name="webhook_logs"
    )
    op.drop_index(
        "ix_webhook_logs_webhook_id_delivered_at", table_name="webhook_logs"
    )
    op.drop_index("ix_webhook_logs_created_at", table_name="webhook_logs")
    op.drop_index("ix_webhook_logs_delivered_at", table_name="webhook_logs")
    op.drop_index("ix_webhook_logs_delivery_status", table_name="webhook_logs")
    op.drop_index("ix_webhook_logs_webhook_id", table_name="webhook_logs")
    op.drop_table("webhook_logs")

    op.drop_index("ix_webhooks_status_enabled", table_name="webhooks")
    op.drop_index("ix_webhooks_event_enabled", table_name="webhooks")
    op.drop_index("ix_webhooks_event_status", table_name="webhooks")
    op.drop_index("ix_webhooks_last_delivery_at", table_name="webhooks")
    op.drop_index("ix_webhooks_created_at", table_name="webhooks")
    op.drop_index("ix_webhooks_is_deleted", table_name="webhooks")
    op.drop_index("ix_webhooks_created_by_id", table_name="webhooks")
    op.drop_index("ix_webhooks_enabled", table_name="webhooks")
    op.drop_index("ix_webhooks_authentication_type", table_name="webhooks")
    op.drop_index("ix_webhooks_status", table_name="webhooks")
    op.drop_index("ix_webhooks_event", table_name="webhooks")
    op.drop_table("webhooks")

    # Drop enum types last, after all dependent columns are gone.
    _webhook_authentication_type_enum.drop(bind, checkfirst=True)
    _webhook_delivery_status_enum.drop(bind, checkfirst=True)
    _webhook_event_enum.drop(bind, checkfirst=True)
    _webhook_status_enum.drop(bind, checkfirst=True)