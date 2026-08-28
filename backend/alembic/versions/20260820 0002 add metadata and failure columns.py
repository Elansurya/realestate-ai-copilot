"""add metadata_payload and failure_reason columns to notifications

Revision ID: 20260820_0002
Revises: 20260820_0001
Create Date: 2026-08-20
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "20260820_0002"
down_revision = "20260820_0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Both columns are nullable in the ORM model (Notification.metadata_payload,
    # Notification.failure_reason), so no server_default/backfill is required —
    # unlike the earlier `category` column, which was NOT NULL.
    op.add_column(
        "notifications",
        sa.Column("metadata_payload", postgresql.JSONB(), nullable=True),
    )
    op.add_column(
        "notifications",
        sa.Column("failure_reason", sa.Text(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("notifications", "failure_reason")
    op.drop_column("notifications", "metadata_payload")