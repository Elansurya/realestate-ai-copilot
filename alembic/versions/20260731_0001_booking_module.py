"""booking module

Revision ID: 20260731_0001
Revises: 0e7dcb13e642
Create Date: 2026-07-31 00:00:00.000000
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

# NOTE: Replace the placeholder below with the actual revision id of the
# current migration head (e.g. the Lead module migration) before running.
# Determine it via `alembic heads` or by inspecting
# backend/alembic/versions/ — do not guess this value.
revision = "20260731_0001"
down_revision = "0e7dcb13e642"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("CREATE EXTENSION IF NOT EXISTS pgcrypto")

    # NOTE: create_type=False on all three ENUM objects below is
    # intentional. The types are created explicitly (with
    # checkfirst=True) immediately after these definitions. Without
    # create_type=False, SQLAlchemy/Alembic will *also* try to create
    # the type as a side effect of op.create_table(), which raises
    # psycopg.errors.DuplicateObject ("type ... already exists") once
    # the explicit creation above has already run — or on any database
    # where the type was already created by a prior partial run.
    booking_status_enum = postgresql.ENUM(
        "PENDING", "CONFIRMED", "CANCELLED", "COMPLETED", "REFUNDED",
        name="booking_status",
        create_type=False,
    )
    booking_payment_status_enum = postgresql.ENUM(
        "PENDING", "PARTIALLY_PAID", "PAID", "OVERDUE", "REFUNDED",
        name="booking_payment_status",
        create_type=False,
    )
    booking_payment_mode_enum = postgresql.ENUM(
        "CASH", "CHEQUE", "BANK_TRANSFER", "UPI", "CARD", "OTHER",
        name="booking_payment_mode",
        create_type=False,
    )

    bind = op.get_bind()
    booking_status_enum.create(bind, checkfirst=True)
    booking_payment_status_enum.create(bind, checkfirst=True)
    booking_payment_mode_enum.create(bind, checkfirst=True)

    op.create_table(
        "bookings",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            server_default=sa.text("gen_random_uuid()"),
            primary_key=True,
        ),
        sa.Column(
            "booking_number",
            sa.String(length=30),
            nullable=False,
            doc="Human-readable booking reference, e.g. BOOK-2026-000001.",
        ),
        sa.Column("customer_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("property_id", sa.Integer(), nullable=False),
        sa.Column("lead_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("agent_id", sa.Integer(), nullable=True),
        sa.Column("created_by", sa.Integer(), nullable=True),
        sa.Column(
            "booking_date",
            sa.Date(),
            nullable=False,
            server_default=sa.text("CURRENT_DATE"),
        ),
        sa.Column("booking_amount", sa.Numeric(14, 2), nullable=True),
        sa.Column("token_amount", sa.Numeric(14, 2), nullable=True),
        sa.Column("payment_mode", booking_payment_mode_enum, nullable=True),
        sa.Column("payment_reference", sa.String(length=100), nullable=True),
        sa.Column(
            "payment_date",
            sa.DateTime(timezone=True),
            nullable=True,
            doc="Timestamp the token/booking payment was actually received.",
        ),
        sa.Column(
            "status",
            booking_status_enum,
            nullable=False,
            server_default="PENDING",
        ),
        sa.Column(
            "payment_status",
            booking_payment_status_enum,
            nullable=False,
            server_default="PENDING",
        ),
        sa.Column("site_visit_date", sa.Date(), nullable=True),
        sa.Column("next_follow_up", sa.Date(), nullable=True),
        sa.Column("remarks", sa.Text(), nullable=True),
        sa.Column("cancellation_reason", sa.Text(), nullable=True),
        sa.Column(
            "is_active",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("true"),
        ),
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
            ["customer_id"], ["customers.id"], ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(
            ["property_id"], ["properties.id"], ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(["lead_id"], ["leads.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["agent_id"], ["users.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["created_by"], ["users.id"], ondelete="SET NULL"),
        sa.CheckConstraint(
            "booking_amount IS NULL OR booking_amount >= 0",
            name="ck_bookings_booking_amount_non_negative",
        ),
        sa.CheckConstraint(
            "token_amount IS NULL OR token_amount >= 0",
            name="ck_bookings_token_amount_non_negative",
        ),
        sa.CheckConstraint(
            "token_amount IS NULL OR booking_amount IS NULL OR "
            "token_amount <= booking_amount",
            name="ck_bookings_token_amount_lte_booking_amount",
        ),
    )

    # ----------------------------------------------------------------
    # Single-column FK / lookup indexes
    # ----------------------------------------------------------------
    op.create_index(
        "ix_bookings_booking_number",
        "bookings",
        ["booking_number"],
        unique=True,
    )
    op.create_index("ix_bookings_customer_id", "bookings", ["customer_id"])
    op.create_index("ix_bookings_property_id", "bookings", ["property_id"])
    op.create_index("ix_bookings_lead_id", "bookings", ["lead_id"])
    op.create_index("ix_bookings_agent_id", "bookings", ["agent_id"])
    op.create_index("ix_bookings_created_by", "bookings", ["created_by"])
    op.create_index("ix_bookings_booking_date", "bookings", ["booking_date"])

    # ----------------------------------------------------------------
    # Partial indexes — scoped to active rows only, matching the
    # repository's default `is_active=True` query pattern
    # ----------------------------------------------------------------
    op.create_index(
        "ix_bookings_status",
        "bookings",
        ["status"],
        postgresql_where=sa.text("is_active = true"),
    )
    op.create_index(
        "ix_bookings_payment_status",
        "bookings",
        ["payment_status"],
        postgresql_where=sa.text("is_active = true"),
    )
    op.create_index(
        "ix_bookings_next_follow_up",
        "bookings",
        ["next_follow_up"],
        postgresql_where=sa.text(
            "is_active = true AND next_follow_up IS NOT NULL"
        ),
    )
    op.create_index(
        "ix_bookings_status_payment_status",
        "bookings",
        ["status", "payment_status"],
        postgresql_where=sa.text("is_active = true"),
    )
    op.create_index(
        "ix_bookings_agent_id_status",
        "bookings",
        ["agent_id", "status"],
        postgresql_where=sa.text("is_active = true"),
    )
    op.create_index(
        "ix_bookings_customer_id_property_id",
        "bookings",
        ["customer_id", "property_id"],
    )

    # ----------------------------------------------------------------
    # DB-enforced business rule: at most one ACTIVE booking per
    # customer/property pair. Closes the TOCTOU race that the
    # application-level pre-check alone cannot prevent under
    # concurrency.
    # ----------------------------------------------------------------
    op.create_index(
        "uq_bookings_active_customer_property",
        "bookings",
        ["customer_id", "property_id"],
        unique=True,
        postgresql_where=sa.text("is_active = true"),
    )

    # ----------------------------------------------------------------
    # updated_at auto-maintenance trigger — guarantees correctness for
    # any write path (ORM, raw SQL, bulk `update()` statements), not
    # just ORM `onupdate=func.now()` (which only fires on session
    # flush).
    # ----------------------------------------------------------------
    op.execute(
        """
        CREATE OR REPLACE FUNCTION set_bookings_updated_at()
        RETURNS TRIGGER AS $$
        BEGIN
            NEW.updated_at = now();
            RETURN NEW;
        END;
        $$ LANGUAGE plpgsql;
        """
    )
    op.execute(
        """
        CREATE TRIGGER trg_bookings_set_updated_at
        BEFORE UPDATE ON bookings
        FOR EACH ROW
        EXECUTE FUNCTION set_bookings_updated_at();
        """
    )


def downgrade() -> None:
    # Trigger/function must be dropped before the table.
    op.execute("DROP TRIGGER IF EXISTS trg_bookings_set_updated_at ON bookings")
    op.execute("DROP FUNCTION IF EXISTS set_bookings_updated_at()")

    op.drop_index("uq_bookings_active_customer_property", table_name="bookings")
    op.drop_index("ix_bookings_customer_id_property_id", table_name="bookings")
    op.drop_index("ix_bookings_agent_id_status", table_name="bookings")
    op.drop_index("ix_bookings_status_payment_status", table_name="bookings")
    op.drop_index("ix_bookings_next_follow_up", table_name="bookings")
    op.drop_index("ix_bookings_payment_status", table_name="bookings")
    op.drop_index("ix_bookings_status", table_name="bookings")
    op.drop_index("ix_bookings_booking_date", table_name="bookings")
    op.drop_index("ix_bookings_created_by", table_name="bookings")
    op.drop_index("ix_bookings_agent_id", table_name="bookings")
    op.drop_index("ix_bookings_lead_id", table_name="bookings")
    op.drop_index("ix_bookings_property_id", table_name="bookings")
    op.drop_index("ix_bookings_customer_id", table_name="bookings")
    op.drop_index("ix_bookings_booking_number", table_name="bookings")

    op.drop_table("bookings")

    bind = op.get_bind()
    postgresql.ENUM(name="booking_payment_mode").drop(bind, checkfirst=True)
    postgresql.ENUM(name="booking_payment_status").drop(bind, checkfirst=True)
    postgresql.ENUM(name="booking_status").drop(bind, checkfirst=True)