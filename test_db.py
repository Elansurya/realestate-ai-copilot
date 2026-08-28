import asyncio

from sqlalchemy import text

from app.db.session import engine


async def test_database_connection():
    try:
        async with engine.connect() as conn:
            result = await conn.execute(text("SELECT version();"))
            version = result.scalar_one()

        print("DATABASE CONNECTION: PASS")
        print(f"PostgreSQL: {version}")

    except Exception as e:
        print("DATABASE CONNECTION: FAIL")
        print(f"Error type: {type(e).__name__}")
        print(f"Error: {e}")

    finally:
        await engine.dispose()


if __name__ == "__main__":
    asyncio.run(test_database_connection())
