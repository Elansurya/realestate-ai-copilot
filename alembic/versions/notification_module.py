"""Notification module - Phase 4 schema.

Creates the full notification subsystem schema:
    * notifications
    * notification_templates
    * notification_logs
    * notification_queue
    * email_notifications
    * sms_notifications
    * whatsapp_notifications
    * push_notifications
    * in_app_notifications

Revision ID: 20260802_notification_module
Revises: 20260802_0002
Create Date: 2026-08-02 00:00:00.000000
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# --------------------------------------------------------------------------- #
# Revision identifiers, used by Alembic.
# --------------------------------------------------------------------------- #
revision: str = "20260802_notification_module"
down_revision: Union[str, None] = "20260802_0002"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


# --------------------------------------------------------------------------- #
# Enum definitions (created once, reused across columns)
# --------------------------------------------------------------------------- #

notification_channel_enum = postgresql.ENUM(
    "EMAIL",
    "SMS",
    "WHATSAPP",
    "PUSH",
    "IN_APP",
    name="notification_channel_enum",
    create_type=False,
)

notification_status_enum = postgresql.ENUM(
    "PENDING",
    "QUEUED",
    "SENDING",
    "SENT",
    "DELIVERED",
    "FAILED",
    "RETRY_SCHEDULED",
    "DEAD_LETTER",
    "CANCELLED",
    "SCHEDULED",
    name="notification_status_enum",
    create_type=False,
)

notification_priority_enum = postgresql.ENUM(
    "LOW",
    "NORMAL",
    "HIGH",
    "CRITICAL",
    name="notification_priority_enum",
    create_type=False,
)

notification_type_enum = postgresql.ENUM(
    "SYSTEM",
    "MARKETING",
    "TRANSACTIONAL",
    "REMINDER",
    "ALERT",
    "LEAD_UPDATE",
    "TASK_ASSIGNMENT",
    name="notification_type_enum",
    create_type=False,
)

queue_status_enum = postgresql.ENUM(
    "PENDING",
    "DISPATCHING",
    "COMPLETED",
    "FAILED",
    "DEAD_LETTER",
    name="notification_queue_status_enum",
    create_type=False,
)

delivery_provider_status_enum = postgresql.ENUM(
    "PENDING",
    "SENT",
    "DELIVERED",
    "BOUNCED",
    "FAILED",
    "READ",
    name="delivery_provider_status_enum",
    create_type=False,
)


def upgrade() -> None:
    """Apply the notification module schema."""
    bind = op.get_bind()

    # ---- Enum types --------------------------------------------------- #
    notification_channel_enum.create(bind, checkfirst=True)
    notification_status_enum.create(bind, checkfirst=True)
    notification_priority_enum.create(bind, checkfirst=True)
    notification_type_enum.create(bind, checkfirst=True)
    queue_status_enum.create(bind, checkfirst=True)
    delivery_provider_status_enum.create(bind, checkfirst=True)

    # ---- notification_templates ---------------------------------------- #
    op.create_table(
        "notification_templates",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            server_default=sa.text("gen_random_uuid()"),
            primary_key=True,
            nullable=False,
        ),
        sa.Column("code", sa.String(length=100), nullable=False),
        sa.Column("name", sa.String(length=255), nullable=False),
        sa.Column(
            "channel",
            notification_channel_enum,
            nullable=False,
        ),
        sa.Column(
            "notification_type",
            notification_type_enum,
            nullable=False,
        ),
        sa.Column("subject_template", sa.Text(), nullable=True),
        sa.Column("body_template", sa.Text(), nullable=False),
        sa.Column(
            "variables",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.text("true")),
        sa.Column("version", sa.Integer(), nullable=False, server_default=sa.text("1")),
        sa.Column("created_by", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("updated_by", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("is_deleted", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("deleted_by", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.CheckConstraint("version > 0", name="ck_notification_templates_version_positive"),
    )
    op.create_index(
        "ix_notification_templates_code",
        "notification_templates",
        ["code"],
        unique=True,
        postgresql_where=sa.text("is_deleted = false"),
    )
    op.create_index(
        "ix_notification_templates_channel_active",
        "notification_templates",
        ["channel", "is_active"],
    )

    # ---- notifications --------------------------------------------------- #
    op.create_table(
        "notifications",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            server_default=sa.text("gen_random_uuid()"),
            primary_key=True,
            nullable=False,
        ),
        sa.Column(
            "recipient_id",
            # NOTE: users.id is INTEGER in this database, not UUID.
            # This column has an FK to users.id, so it must match that
            # type or the constraint fails with
            # asyncpg.exceptions.DatatypeMismatchError.
            sa.Integer(),
            nullable=False,
        ),
        sa.Column(
            "sender_id",
            # NOTE: same reasoning as recipient_id above -- FK targets
            # users.id, which is INTEGER.
            sa.Integer(),
            nullable=True,
        ),
        sa.Column(
            "template_id",
            postgresql.UUID(as_uuid=True),
            nullable=True,
        ),
        sa.Column("channel", notification_channel_enum, nullable=False),
        sa.Column("notification_type", notification_type_enum, nullable=False),
        sa.Column(
            "priority",
            notification_priority_enum,
            nullable=False,
            server_default=sa.text("'NORMAL'"),
        ),
        sa.Column(
            "status",
            notification_status_enum,
            nullable=False,
            server_default=sa.text("'PENDING'"),
        ),
        sa.Column("subject", sa.String(length=500), nullable=True),
        sa.Column("body", sa.Text(), nullable=False),
        sa.Column(
            "payload",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
        sa.Column("is_read", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column("read_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("scheduled_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("sent_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("delivered_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("failed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("cancelled_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("retry_count", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("max_retries", sa.Integer(), nullable=False, server_default=sa.text("5")),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column("created_by", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("updated_by", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("is_deleted", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("deleted_by", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.ForeignKeyConstraint(
            ["recipient_id"], ["users.id"], name="fk_notifications_recipient_id", ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["sender_id"], ["users.id"], name="fk_notifications_sender_id", ondelete="SET NULL"
        ),
        sa.ForeignKeyConstraint(
            ["template_id"],
            ["notification_templates.id"],
            name="fk_notifications_template_id",
            ondelete="SET NULL",
        ),
        sa.CheckConstraint("retry_count >= 0", name="ck_notifications_retry_count_non_negative"),
        sa.CheckConstraint("max_retries >= 0", name="ck_notifications_max_retries_non_negative"),
        sa.CheckConstraint(
            "(read_at IS NULL) OR (is_read = true)", name="ck_notifications_read_at_consistency"
        ),
        sa.CheckConstraint(
            "(status <> 'SCHEDULED') OR (scheduled_at IS NOT NULL)",
            name="ck_notifications_scheduled_requires_timestamp",
        ),
    )
    op.create_index("ix_notifications_recipient_id", "notifications", ["recipient_id"])
    op.create_index("ix_notifications_status", "notifications", ["status"])
    op.create_index("ix_notifications_channel", "notifications", ["channel"])
    op.create_index("ix_notifications_priority", "notifications", ["priority"])
    op.create_index("ix_notifications_scheduled_at", "notifications", ["scheduled_at"])
    op.create_index("ix_notifications_created_at", "notifications", ["created_at"])
    op.create_index(
        "ix_notifications_recipient_status",
        "notifications",
        ["recipient_id", "status"],
    )
    op.create_index(
        "ix_notifications_recipient_unread",
        "notifications",
        ["recipient_id", "is_read"],
        postgresql_where=sa.text("is_deleted = false"),
    )
    op.create_index(
        "ix_notifications_channel_status_priority",
        "notifications",
        ["channel", "status", "priority"],
    )

    # ---- notification_queue --------------------------------------------- #
    op.create_table(
        "notification_queue",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            server_default=sa.text("gen_random_uuid()"),
            primary_key=True,
            nullable=False,
        ),
        sa.Column("notification_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("channel", notification_channel_enum, nullable=False),
        sa.Column(
            "priority",
            notification_priority_enum,
            nullable=False,
            server_default=sa.text("'NORMAL'"),
        ),
        sa.Column(
            "queue_status",
            queue_status_enum,
            nullable=False,
            server_default=sa.text("'PENDING'"),
        ),
        sa.Column("attempt_count", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column("locked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("locked_by", sa.String(length=255), nullable=True),
        sa.Column("available_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("dead_letter_reason", sa.Text(), nullable=True),
        sa.Column("is_deleted", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("deleted_by", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.ForeignKeyConstraint(
            ["notification_id"],
            ["notifications.id"],
            name="fk_notification_queue_notification_id",
            ondelete="CASCADE",
        ),
        sa.CheckConstraint(
            "attempt_count >= 0", name="ck_notification_queue_attempt_count_non_negative"
        ),
    )
    op.create_index(
        "ix_notification_queue_notification_id", "notification_queue", ["notification_id"]
    )
    op.create_index("ix_notification_queue_status", "notification_queue", ["queue_status"])
    op.create_index(
        "ix_notification_queue_status_priority",
        "notification_queue",
        ["queue_status", "priority"],
    )
    op.create_index(
        "ix_notification_queue_channel_status",
        "notification_queue",
        ["channel", "queue_status"],
    )

    # ---- notification_logs ------------------------------------------------ #
    op.create_table(
        "notification_logs",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            server_default=sa.text("gen_random_uuid()"),
            primary_key=True,
            nullable=False,
        ),
        sa.Column("notification_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("event", sa.String(length=100), nullable=False),
        sa.Column("message", sa.Text(), nullable=True),
        sa.Column(
            "metadata",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.ForeignKeyConstraint(
            ["notification_id"],
            ["notifications.id"],
            name="fk_notification_logs_notification_id",
            ondelete="CASCADE",
        ),
    )
    op.create_index("ix_notification_logs_notification_id", "notification_logs", ["notification_id"])
    op.create_index("ix_notification_logs_event", "notification_logs", ["event"])
    op.create_index(
        "ix_notification_logs_notification_created",
        "notification_logs",
        ["notification_id", "created_at"],
    )

    # ---- Channel-specific detail tables ------------------------------ #

    op.create_table(
        "email_notifications",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            server_default=sa.text("gen_random_uuid()"),
            primary_key=True,
            nullable=False,
        ),
        sa.Column("notification_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("from_address", sa.String(length=320), nullable=False),
        sa.Column("to_address", sa.String(length=320), nullable=False),
        sa.Column("cc_addresses", postgresql.ARRAY(sa.String(length=320)), nullable=True),
        sa.Column("bcc_addresses", postgresql.ARRAY(sa.String(length=320)), nullable=True),
        sa.Column("html_body", sa.Text(), nullable=True),
        sa.Column("provider", sa.String(length=100), nullable=True),
        sa.Column("provider_message_id", sa.String(length=255), nullable=True),
        sa.Column(
            "provider_status",
            delivery_provider_status_enum,
            nullable=False,
            server_default=sa.text("'PENDING'"),
        ),
        sa.Column("opened_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("bounced_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("is_deleted", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.ForeignKeyConstraint(
            ["notification_id"],
            ["notifications.id"],
            name="fk_email_notifications_notification_id",
            ondelete="CASCADE",
        ),
        sa.CheckConstraint(
            "position('@' in to_address) > 1", name="ck_email_notifications_to_address_format"
        ),
    )
    op.create_index(
        "ix_email_notifications_notification_id",
        "email_notifications",
        ["notification_id"],
        unique=True,
    )
    op.create_index(
        "ix_email_notifications_provider_message_id",
        "email_notifications",
        ["provider_message_id"],
    )

    op.create_table(
        "sms_notifications",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            server_default=sa.text("gen_random_uuid()"),
            primary_key=True,
            nullable=False,
        ),
        sa.Column("notification_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("from_number", sa.String(length=20), nullable=False),
        sa.Column("to_number", sa.String(length=20), nullable=False),
        sa.Column("segment_count", sa.Integer(), nullable=False, server_default=sa.text("1")),
        sa.Column("provider", sa.String(length=100), nullable=True),
        sa.Column("provider_message_id", sa.String(length=255), nullable=True),
        sa.Column(
            "provider_status",
            delivery_provider_status_enum,
            nullable=False,
            server_default=sa.text("'PENDING'"),
        ),
        sa.Column("is_deleted", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.ForeignKeyConstraint(
            ["notification_id"],
            ["notifications.id"],
            name="fk_sms_notifications_notification_id",
            ondelete="CASCADE",
        ),
        sa.CheckConstraint("segment_count > 0", name="ck_sms_notifications_segment_count_positive"),
        sa.CheckConstraint(
            "char_length(to_number) >= 8", name="ck_sms_notifications_to_number_length"
        ),
    )
    op.create_index(
        "ix_sms_notifications_notification_id", "sms_notifications", ["notification_id"], unique=True
    )
    op.create_index("ix_sms_notifications_to_number", "sms_notifications", ["to_number"])

    op.create_table(
        "whatsapp_notifications",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            server_default=sa.text("gen_random_uuid()"),
            primary_key=True,
            nullable=False,
        ),
        sa.Column("notification_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("from_number", sa.String(length=20), nullable=False),
        sa.Column("to_number", sa.String(length=20), nullable=False),
        sa.Column("wa_template_name", sa.String(length=255), nullable=True),
        sa.Column(
            "wa_template_params",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
        sa.Column("provider", sa.String(length=100), nullable=True),
        sa.Column("provider_message_id", sa.String(length=255), nullable=True),
        sa.Column(
            "provider_status",
            delivery_provider_status_enum,
            nullable=False,
            server_default=sa.text("'PENDING'"),
        ),
        sa.Column("is_deleted", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.ForeignKeyConstraint(
            ["notification_id"],
            ["notifications.id"],
            name="fk_whatsapp_notifications_notification_id",
            ondelete="CASCADE",
        ),
        sa.CheckConstraint(
            "char_length(to_number) >= 8", name="ck_whatsapp_notifications_to_number_length"
        ),
    )
    op.create_index(
        "ix_whatsapp_notifications_notification_id",
        "whatsapp_notifications",
        ["notification_id"],
        unique=True,
    )

    op.create_table(
        "push_notifications",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            server_default=sa.text("gen_random_uuid()"),
            primary_key=True,
            nullable=False,
        ),
        sa.Column("notification_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("device_token", sa.String(length=500), nullable=False),
        sa.Column("platform", sa.String(length=20), nullable=False),
        sa.Column("title", sa.String(length=255), nullable=False),
        sa.Column(
            "data_payload",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
        sa.Column("provider", sa.String(length=100), nullable=True),
        sa.Column("provider_message_id", sa.String(length=255), nullable=True),
        sa.Column(
            "provider_status",
            delivery_provider_status_enum,
            nullable=False,
            server_default=sa.text("'PENDING'"),
        ),
        sa.Column("clicked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("is_deleted", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.ForeignKeyConstraint(
            ["notification_id"],
            ["notifications.id"],
            name="fk_push_notifications_notification_id",
            ondelete="CASCADE",
        ),
        sa.CheckConstraint(
            "platform in ('IOS','ANDROID','WEB')", name="ck_push_notifications_platform_valid"
        ),
    )
    op.create_index(
        "ix_push_notifications_notification_id", "push_notifications", ["notification_id"], unique=True
    )
    op.create_index("ix_push_notifications_platform", "push_notifications", ["platform"])

    op.create_table(
        "in_app_notifications",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            server_default=sa.text("gen_random_uuid()"),
            primary_key=True,
            nullable=False,
        ),
        sa.Column("notification_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("icon", sa.String(length=100), nullable=True),
        sa.Column("action_url", sa.String(length=1000), nullable=True),
        sa.Column("action_label", sa.String(length=100), nullable=True),
        sa.Column("is_dismissed", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column("dismissed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("is_deleted", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.ForeignKeyConstraint(
            ["notification_id"],
            ["notifications.id"],
            name="fk_in_app_notifications_notification_id",
            ondelete="CASCADE",
        ),
    )
    op.create_index(
        "ix_in_app_notifications_notification_id",
        "in_app_notifications",
        ["notification_id"],
        unique=True,
    )
    op.create_index(
        "ix_in_app_notifications_dismissed", "in_app_notifications", ["is_dismissed"]
    )


def downgrade() -> None:
    """Revert the notification module schema."""
    bind = op.get_bind()

    op.drop_index("ix_in_app_notifications_dismissed", table_name="in_app_notifications")
    op.drop_index("ix_in_app_notifications_notification_id", table_name="in_app_notifications")
    op.drop_table("in_app_notifications")

    op.drop_index("ix_push_notifications_platform", table_name="push_notifications")
    op.drop_index("ix_push_notifications_notification_id", table_name="push_notifications")
    op.drop_table("push_notifications")

    op.drop_index(
        "ix_whatsapp_notifications_notification_id", table_name="whatsapp_notifications"
    )
    op.drop_table("whatsapp_notifications")

    op.drop_index("ix_sms_notifications_to_number", table_name="sms_notifications")
    op.drop_index("ix_sms_notifications_notification_id", table_name="sms_notifications")
    op.drop_table("sms_notifications")

    op.drop_index(
        "ix_email_notifications_provider_message_id", table_name="email_notifications"
    )
    op.drop_index("ix_email_notifications_notification_id", table_name="email_notifications")
    op.drop_table("email_notifications")

    op.drop_index(
        "ix_notification_logs_notification_created", table_name="notification_logs"
    )
    op.drop_index("ix_notification_logs_event", table_name="notification_logs")
    op.drop_index("ix_notification_logs_notification_id", table_name="notification_logs")
    op.drop_table("notification_logs")

    op.drop_index(
        "ix_notification_queue_channel_status", table_name="notification_queue"
    )
    op.drop_index(
        "ix_notification_queue_status_priority", table_name="notification_queue"
    )
    op.drop_index("ix_notification_queue_status", table_name="notification_queue")
    op.drop_index(
        "ix_notification_queue_notification_id", table_name="notification_queue"
    )
    op.drop_table("notification_queue")

    op.drop_index(
        "ix_notifications_channel_status_priority", table_name="notifications"
    )
    op.drop_index("ix_notifications_recipient_unread", table_name="notifications")
    op.drop_index("ix_notifications_recipient_status", table_name="notifications")
    op.drop_index("ix_notifications_created_at", table_name="notifications")
    op.drop_index("ix_notifications_scheduled_at", table_name="notifications")
    op.drop_index("ix_notifications_priority", table_name="notifications")
    op.drop_index("ix_notifications_channel", table_name="notifications")
    op.drop_index("ix_notifications_status", table_name="notifications")
    op.drop_index("ix_notifications_recipient_id", table_name="notifications")
    op.drop_table("notifications")

    op.drop_index(
        "ix_notification_templates_channel_active", table_name="notification_templates"
    )
    op.drop_index("ix_notification_templates_code", table_name="notification_templates")
    op.drop_table("notification_templates")

    delivery_provider_status_enum.drop(bind, checkfirst=True)
    queue_status_enum.drop(bind, checkfirst=True)
    notification_type_enum.drop(bind, checkfirst=True)
    notification_priority_enum.drop(bind, checkfirst=True)
    notification_status_enum.drop(bind, checkfirst=True)
    notification_channel_enum.drop(bind, checkfirst=True)