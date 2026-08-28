"""
One-off script to create all database tables from SQLAlchemy models.

This is a temporary bootstrap utility for local development only.
In production, schema changes should be managed exclusively through
Alembic migrations (to be set up next), not this script — this exists
solely to unblock local testing of the Phase 03 auth flow before
Alembic is wired in.

Run with:
    python create_tables.py
"""

import asyncio

from app.db.base import Base
from app.db.session import engine

# Import all models here so their tables are registered on Base.metadata
# before create_all() runs. Without this import, SQLAlchemy has no
# knowledge of the User model's table definition.
from app.models.user import User  # noqa: F401


async def create_all_tables() -> None:
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    print("All tables created successfully.")


if __name__ == "__main__":
    asyncio.run(create_all_tables())