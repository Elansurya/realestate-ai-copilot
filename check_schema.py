"""
One-off diagnostic script to inspect the users table and user_role enum
directly via the application's own async engine — no psql required.
"""

import asyncio
from sqlalchemy import text
from app.db.session import engine


async def check_schema() -> None:
    async with engine.connect() as conn:
        result = await conn.execute(
            text(
                "SELECT column_name, data_type, udt_name, is_nullable, column_default "
                "FROM information_schema.columns "
                "WHERE table_schema = 'public' AND table_name = 'users' "
                "ORDER BY ordinal_position"
            )
        )
        print("\n--- users table columns ---")
        for row in result:
            print(dict(row._mapping))

        result = await conn.execute(
            text(
                "SELECT enumlabel FROM pg_enum "
                "JOIN pg_type ON pg_enum.enumtypid = pg_type.oid "
                "WHERE pg_type.typname = 'user_role' "
                "ORDER BY pg_enum.enumsortorder"
            )
        )
        print("\n--- user_role enum values ---")
        for row in result:
            print(row[0])


if __name__ == "__main__":
    asyncio.run(check_schema())