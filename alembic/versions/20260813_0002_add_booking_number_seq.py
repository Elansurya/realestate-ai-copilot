"""add bookings booking_number sequence

Revision ID: 20260813_0002
Revises: 20260804_0002
Create Date: 2026-08-13 00:00:00.000000
"""

from __future__ import annotations

from alembic import op
from sqlalchemy import Sequence

revision = "20260813_0002"
down_revision = "20260804_0002"
branch_labels = None
depends_on = None

_SEQUENCE = Sequence("bookings_booking_number_seq", start=1)


def upgrade() -> None:
    # checkfirst=True mirrors the idempotent-creation pattern already
    # used for the booking_status/booking_payment_status/
    # booking_payment_mode ENUM types in 20260731_0001 — safe to run
    # even if the sequence already exists from a prior partial run.
    _SEQUENCE.create(op.get_bind(), checkfirst=True)


def downgrade() -> None:
    _SEQUENCE.drop(op.get_bind(), checkfirst=True)