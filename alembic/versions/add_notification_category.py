"""add category column to notifications

Revision ID: 20260820_0001
Revises: 20260813_0002
Create Date: 2026-08-20
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "20260820_0001"
down_revision = "20260813_0002"
branch_labels = None
depends_on = None


NOTIFICATION_CATEGORY_VALUES = (
    "lead",
    "deal",
    "task",
    "property",
    "payment",
    "appointment",
    "system",
    "marketing",
)


def upgrade() -> None:
    bind = op.get_bind()

    notification_category_enum = postgresql.ENUM(
        *NOTIFICATION_CATEGORY_VALUES,
        name="notification_category_enum",
        create_type=False,
    )

    # Explicitly create the type once (idempotent via checkfirst).
    notification_category_enum.create(bind, checkfirst=True)

    # create_type=False prevents add_column from re-issuing CREATE TYPE.
    op.add_column(
        "notifications",
        sa.Column(
            "category",
            notification_category_enum,
            nullable=False,
            server_default="system",
        ),
    )

    # Drop the temporary default so future inserts must supply category
    # explicitly, matching the ORM model (no Python-side default there).
    op.alter_column(
        "notifications",
        "category",
        server_default=None,
    )


def downgrade() -> None:
    bind = op.get_bind()

    op.drop_column("notifications", "category")

    notification_category_enum = postgresql.ENUM(
        *NOTIFICATION_CATEGORY_VALUES,
        name="notification_category_enum",
        create_type=False,
    )
    notification_category_enum.drop(bind, checkfirst=True)