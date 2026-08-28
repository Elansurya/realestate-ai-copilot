"""Reconcile the notification database with the current ORM contract.

This migration is intentionally additive/idempotent and repairs the schema drift
that caused the notification repository/service tests to fail:

* users.id is INTEGER while notification recipient/sender identifiers are the
  public UUIDs used by the current API/model. Existing integer references are
  mapped through users.uuid before the columns are converted to UUID.
* notification category/priority/status enum labels are reconciled with the
  SQLAlchemy enum names used by the current models.
* notification_templates.locale is added.
* queue/log compatibility columns used by the current ORM are added.
* notification_queue.channel is made nullable because channel is authoritative
  on notifications and the current ORM deliberately treats it as redundant.

No notification rows are intentionally deleted. Orphaned legacy recipient IDs
cause the migration to stop rather than silently inventing a recipient.
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "20260825_0001"
down_revision = "20260824_0001"
branch_labels = None
depends_on = None


def _columns(bind, table: str) -> dict[str, dict]:
    insp = sa.inspect(bind)
    if not insp.has_table(table):
        return {}
    return {c["name"]: c for c in insp.get_columns(table)}


def _column_type(bind, table: str, column: str) -> str | None:
    col = _columns(bind, table).get(column)
    return str(col["type"]).upper() if col else None


def _add_column_if_missing(bind, table: str, column: sa.Column) -> None:
    if column.name not in _columns(bind, table):
        op.add_column(table, column)


def _enum_labels(bind, enum_name: str) -> set[str]:
    rows = bind.execute(
        sa.text(
            "SELECT e.enumlabel "
            "FROM pg_enum e JOIN pg_type t ON t.oid=e.enumtypid "
            "WHERE t.typname=:name"
        ),
        {"name": enum_name},
    )
    return {row[0] for row in rows}


def _ensure_enum_value(bind, enum_name: str, value: str) -> None:
    labels = _enum_labels(bind, enum_name)
    if value not in labels:
        safe = value.replace("'", "''")
        op.execute(sa.text(f"ALTER TYPE {enum_name} ADD VALUE IF NOT EXISTS '{safe}'"))


def _rename_enum_value_if_possible(bind, enum_name: str, old: str, new: str) -> None:
    labels = _enum_labels(bind, enum_name)
    if old in labels and new not in labels:
        old_safe = old.replace("'", "''")
        new_safe = new.replace("'", "''")
        op.execute(sa.text(f"ALTER TYPE {enum_name} RENAME VALUE '{old_safe}' TO '{new_safe}'"))


def _drop_indexes(bind, names: list[str]) -> None:
    insp = sa.inspect(bind)
    for name in names:
        for table in insp.get_table_names():
            if any(idx["name"] == name for idx in insp.get_indexes(table)):
                op.drop_index(name, table_name=table)
                break


def _reconcile_user_reference_column(bind, column: str, nullable: bool) -> None:
    """Convert legacy INTEGER user IDs to the public UUID stored in users.uuid."""
    if _column_type(bind, "notifications", column) != "INTEGER":
        return

    tmp = f"{column}_uuid_tmp"
    if tmp not in _columns(bind, "notifications"):
        op.add_column(
            "notifications",
            sa.Column(tmp, postgresql.UUID(as_uuid=True), nullable=True),
        )

    # Existing notification rows use users.id. The current API/model uses
    # users.uuid instead, so preserve the logical recipient identity.
    op.execute(
        sa.text(
            f"UPDATE notifications n "
            f"SET {tmp}=u.uuid::uuid "
            f"FROM users u WHERE u.id=n.{column}"
        )
    )

    if not nullable:
        missing = bind.execute(
            sa.text(
                f"SELECT count(*) FROM notifications "
                f"WHERE {column} IS NOT NULL AND {tmp} IS NULL"
            )
        ).scalar_one()
        if missing:
            raise RuntimeError(
                f"Cannot migrate notifications.{column}: {missing} legacy user IDs "
                "do not have a matching users.uuid value."
            )

    # Drop every FK/index tied to the old integer column before replacing it.
    insp = sa.inspect(bind)
    for fk in insp.get_foreign_keys("notifications"):
        if column in (fk.get("constrained_columns") or []):
            name = fk.get("name")
            if name:
                op.drop_constraint(name, "notifications", type_="foreignkey")

    _drop_indexes(
        bind,
        [
            "ix_notifications_recipient_id",
            "ix_notifications_recipient_status",
            "ix_notifications_recipient_unread",
        ],
    )

    op.drop_column("notifications", column)
    op.alter_column("notifications", tmp, new_column_name=column)
    op.alter_column("notifications", column, nullable=nullable)

    # Recreate the indexes expected by the current ORM.
    if column == "recipient_id":
        op.create_index("ix_notifications_recipient_id", "notifications", [column])
        op.create_index("ix_notifications_recipient_status", "notifications", [column, "status"])
        op.create_index(
            "ix_notifications_recipient_unread",
            "notifications",
            [column, "is_read"],
            postgresql_where=sa.text("is_deleted = false"),
        )


def upgrade() -> None:
    bind = op.get_bind()

    # ------------------------------------------------------------------
    # Enum compatibility
    # ------------------------------------------------------------------
    if not _enum_labels(bind, "notification_category_enum"):
        category_enum = postgresql.ENUM(
            "LEAD", "DEAL", "TASK", "PROPERTY", "PAYMENT", "APPOINTMENT", "SYSTEM", "MARKETING",
            name="notification_category_enum", create_type=False,
        )
        category_enum.create(bind, checkfirst=True)
    if _enum_labels(bind, "notification_category_enum"):
        for lower, upper in (
            ("lead", "LEAD"),
            ("deal", "DEAL"),
            ("task", "TASK"),
            ("property", "PROPERTY"),
            ("payment", "PAYMENT"),
            ("appointment", "APPOINTMENT"),
            ("system", "SYSTEM"),
            ("marketing", "MARKETING"),
        ):
            _rename_enum_value_if_possible(bind, "notification_category_enum", lower, upper)

    if _enum_labels(bind, "notification_priority_enum"):
        _rename_enum_value_if_possible(bind, "notification_priority_enum", "CRITICAL", "URGENT")
        _ensure_enum_value(bind, "notification_priority_enum", "URGENT")

    if _enum_labels(bind, "notification_status_enum"):
        for value in ("READ", "RETRYING", "SCHEDULED"):
            _ensure_enum_value(bind, "notification_status_enum", value)

    # ------------------------------------------------------------------
    # notifications: INTEGER user FK -> public UUID identifiers
    # ------------------------------------------------------------------
    if _columns(bind, "notifications"):
        _reconcile_user_reference_column(bind, "recipient_id", nullable=False)
        _reconcile_user_reference_column(bind, "sender_id", nullable=True)

        # Current ORM owns the category column; older schemas may have
        # notification_type but category was added by 20260820_0001.
        if "category" not in _columns(bind, "notifications") and "notification_type" in _columns(bind, "notifications"):
            category_enum = postgresql.ENUM(
                "LEAD", "DEAL", "TASK", "PROPERTY", "PAYMENT", "APPOINTMENT", "SYSTEM", "MARKETING",
                name="notification_category_enum", create_type=False,
            )
            category_enum.create(bind, checkfirst=True)
            op.add_column("notifications", sa.Column("category", category_enum, nullable=True))
            op.execute(
                "UPDATE notifications SET category = CASE upper(notification_type::text) "
                "WHEN 'LEAD_UPDATE' THEN 'LEAD' WHEN 'TASK_ASSIGNMENT' THEN 'TASK' "
                "WHEN 'TRANSACTIONAL' THEN 'DEAL' WHEN 'REMINDER' THEN 'APPOINTMENT' "
                "WHEN 'ALERT' THEN 'SYSTEM' WHEN 'MARKETING' THEN 'MARKETING' ELSE 'SYSTEM' END::notification_category_enum"
            )
            op.alter_column("notifications", "category", nullable=False)

    # ------------------------------------------------------------------
    # Queue compatibility columns
    # ------------------------------------------------------------------
    if _columns(bind, "notification_queue"):
        _add_column_if_missing(
            bind, "notification_queue",
            sa.Column("status", sa.String(30), nullable=False, server_default="waiting"),
        )
        _add_column_if_missing(
            bind, "notification_queue",
            sa.Column("retry_count", sa.Integer(), nullable=False, server_default="0"),
        )
        _add_column_if_missing(
            bind, "notification_queue",
            sa.Column("max_retries", sa.Integer(), nullable=False, server_default="3"),
        )
        _add_column_if_missing(
            bind, "notification_queue",
            sa.Column("scheduled_at", sa.DateTime(timezone=True), nullable=True),
        )
        _add_column_if_missing(
            bind, "notification_queue",
            sa.Column("next_retry_at", sa.DateTime(timezone=True), nullable=True),
        )
        # Channel is duplicated from notifications.channel and is not part of
        # the current ORM contract. Allow ORM-created queue rows without it.
        if "channel" in _columns(bind, "notification_queue"):
            op.alter_column("notification_queue", "channel", nullable=True)

    # ------------------------------------------------------------------
    # Notification log compatibility
    # ------------------------------------------------------------------
    if _columns(bind, "notification_logs"):
        op.execute(
            "DO $$ BEGIN "
            "IF NOT EXISTS (SELECT 1 FROM pg_type WHERE typname='notification_event_type_enum') THEN "
            "CREATE TYPE notification_event_type_enum AS ENUM "
            "('CREATED','QUEUED','DISPATCHED','SENT','DELIVERED','FAILED','RETRIED','READ','CANCELLED','SCHEDULED'); "
            "END IF; END $$;"
        )
        event_enum = postgresql.ENUM(
            "CREATED", "QUEUED", "DISPATCHED", "SENT", "DELIVERED", "FAILED", "RETRIED", "READ", "CANCELLED", "SCHEDULED",
            name="notification_event_type_enum", create_type=False,
        )
        _add_column_if_missing(bind, "notification_logs", sa.Column("event_type", event_enum, nullable=True))
        status_enum = postgresql.ENUM(
            "PENDING", "QUEUED", "SENDING", "SENT", "DELIVERED", "FAILED", "RETRY_SCHEDULED", "DEAD_LETTER", "CANCELLED", "SCHEDULED", "READ", "RETRYING",
            name="notification_status_enum", create_type=False,
        )
        _add_column_if_missing(bind, "notification_logs", sa.Column("status", status_enum, nullable=True))
        _add_column_if_missing(bind, "notification_logs", sa.Column("attempt_number", sa.Integer(), nullable=True, server_default="1"))
        _add_column_if_missing(bind, "notification_logs", sa.Column("provider_response", postgresql.JSONB(), nullable=True))
        _add_column_if_missing(bind, "notification_logs", sa.Column("error_message", sa.Text(), nullable=True))
        _add_column_if_missing(bind, "notification_logs", sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=True, server_default=sa.text("now()")))

    # ------------------------------------------------------------------
    # Template locale + audit columns
    # ------------------------------------------------------------------
    if _columns(bind, "notification_templates"):
        op.execute(
            "DO $$ BEGIN "
            "IF NOT EXISTS (SELECT 1 FROM pg_type WHERE typname='template_locale_enum') THEN "
            "CREATE TYPE template_locale_enum AS ENUM ('EN_US','EN_IN','HI_IN','AR_AE'); "
            "END IF; END $$;"
        )
        locale_enum = postgresql.ENUM(
            "EN_US", "EN_IN", "HI_IN", "AR_AE", name="template_locale_enum", create_type=False
        )
        _add_column_if_missing(bind, "notification_templates", sa.Column("locale", locale_enum, nullable=False, server_default="EN_US"))
        _add_column_if_missing(bind, "notification_templates", sa.Column("created_by", postgresql.UUID(as_uuid=True), nullable=True))
        _add_column_if_missing(bind, "notification_templates", sa.Column("updated_by", postgresql.UUID(as_uuid=True), nullable=True))
        _add_column_if_missing(bind, "notification_templates", sa.Column("deleted_by", postgresql.UUID(as_uuid=True), nullable=True))


def downgrade() -> None:
    # Compatibility migration is intentionally non-destructive. Schema/data
    # restoration of legacy integer references is unsafe once new UUID rows
    # exist, so no reverse conversion is attempted.
    pass