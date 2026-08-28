# backend/alembic/versions/payment_module_migration.py

"""payment module migration

Revision ID: payment_module_migration
Revises: 20260731_0001
Create Date: 2026-08-01 00:00:00.000000
"""

from typing import Sequence, Union

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "payment_module_migration"
down_revision: Union[str, None] = "20260731_0001"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


payment_status_enum = postgresql.ENUM(
    "PENDING",
    "SUCCESS",
    "FAILED",
    "REFUNDED",
    "PARTIAL",
    name="payment_status_enum",
    create_type=False,
)

payment_mode_enum = postgresql.ENUM(
    "CASH",
    "UPI",
    "BANK_TRANSFER",
    "CHEQUE",
    "CARD",
    "OTHER",
    name="payment_mode_enum",
    create_type=False,
)

payment_type_enum = postgresql.ENUM(
    "TOKEN",
    "ADVANCE",
    "INSTALLMENT",
    "FULL_PAYMENT",
    "REFUND",
    name="payment_type_enum",
    create_type=False,
)


def upgrade() -> None:
    bind = op.get_bind()

    payment_status_enum.create(bind, checkfirst=True)
    payment_mode_enum.create(bind, checkfirst=True)
    payment_type_enum.create(bind, checkfirst=True)

    op.create_table(
        "payments",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
            nullable=False,
        ),
        sa.Column("payment_number", sa.String(length=30), nullable=False),
        sa.Column(
            "booking_id",
            postgresql.UUID(as_uuid=True),
            nullable=False,
        ),
        sa.Column(
            "customer_id",
            postgresql.UUID(as_uuid=True),
            nullable=False,
        ),
        sa.Column("property_id", sa.Integer(), nullable=False),
        sa.Column("received_by", sa.Integer(), nullable=True),
        sa.Column(
            "payment_date",
            sa.Date(),
            nullable=False,
            server_default=sa.text("CURRENT_DATE"),
        ),
        sa.Column("payment_amount", sa.Numeric(14, 2), nullable=False),
        sa.Column(
            "payment_mode",
            payment_mode_enum,
            nullable=False,
        ),
        sa.Column("transaction_reference", sa.String(length=100), nullable=True),
        sa.Column(
            "payment_status",
            payment_status_enum,
            nullable=False,
            server_default="PENDING",
        ),
        sa.Column(
            "payment_type",
            payment_type_enum,
            nullable=False,
        ),
        sa.Column("bank_name", sa.String(length=150), nullable=True),
        sa.Column("cheque_number", sa.String(length=50), nullable=True),
        sa.Column("remarks", sa.String(length=500), nullable=True),
        sa.Column("receipt_number", sa.String(length=30), nullable=True),
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
            server_default=sa.func.now(),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.ForeignKeyConstraint(
            ["booking_id"], ["bookings.id"], name="fk_payments_booking_id", ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(
            ["customer_id"], ["customers.id"], name="fk_payments_customer_id", ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(
            ["property_id"], ["properties.id"], name="fk_payments_property_id", ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(
            ["received_by"], ["users.id"], name="fk_payments_received_by", ondelete="SET NULL"
        ),
        sa.CheckConstraint("payment_amount > 0", name="ck_payments_amount_positive"),
        sa.CheckConstraint(
            "payment_mode != 'CHEQUE' OR cheque_number IS NOT NULL",
            name="ck_payments_cheque_number_required",
        ),
        sa.UniqueConstraint("payment_number", name="uq_payments_payment_number"),
        sa.UniqueConstraint("receipt_number", name="uq_payments_receipt_number"),
    )

    op.create_index(
        "ix_payments_payment_number", "payments", ["payment_number"], unique=True
    )
    op.create_index("ix_payments_booking_id", "payments", ["booking_id"])
    op.create_index("ix_payments_customer_id", "payments", ["customer_id"])
    op.create_index("ix_payments_property_id", "payments", ["property_id"])
    op.create_index("ix_payments_received_by", "payments", ["received_by"])

    op.create_index(
        "ix_payments_booking_status", "payments", ["booking_id", "payment_status"]
    )
    op.create_index(
        "ix_payments_customer_date", "payments", ["customer_id", "payment_date"]
    )
    op.create_index(
        "ix_payments_date_active", "payments", ["payment_date", "is_active"]
    )

    op.create_index(
        "ix_payments_active_only",
        "payments",
        ["id"],
        postgresql_where=sa.text("is_active = true"),
    )
    op.create_index(
        "ix_payments_success_txn_ref",
        "payments",
        ["transaction_reference"],
        unique=True,
        postgresql_where=sa.text("payment_status = 'SUCCESS' AND transaction_reference IS NOT NULL"),
    )


def downgrade() -> None:
    bind = op.get_bind()

    op.drop_index("ix_payments_success_txn_ref", table_name="payments")
    op.drop_index("ix_payments_active_only", table_name="payments")
    op.drop_index("ix_payments_date_active", table_name="payments")
    op.drop_index("ix_payments_customer_date", table_name="payments")
    op.drop_index("ix_payments_booking_status", table_name="payments")
    op.drop_index("ix_payments_received_by", table_name="payments")
    op.drop_index("ix_payments_property_id", table_name="payments")
    op.drop_index("ix_payments_customer_id", table_name="payments")
    op.drop_index("ix_payments_booking_id", table_name="payments")
    op.drop_index("ix_payments_payment_number", table_name="payments")

    op.drop_table("payments")

    payment_type_enum.drop(bind, checkfirst=True)
    payment_mode_enum.drop(bind, checkfirst=True)
    payment_status_enum.drop(bind, checkfirst=True)