from app.db.session import engine
from sqlalchemy import text

try:
    with engine.connect() as conn:
        result = conn.execute(text("SELECT version();"))
        print(result.fetchone())

    print("✅ Database Connected Successfully!")

except Exception as e:
    print("❌ Database Connection Failed")
    print(e)