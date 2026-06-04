import asyncio
from sqlalchemy import text
from app.db.session import engine

async def main():
    print("[MIGRATION] Altering leads_v2 table to add summary column if not exists...")
    async with engine.begin() as conn:
        await conn.execute(text("ALTER TABLE leads_v2 ADD COLUMN IF NOT EXISTS summary TEXT;"))
    print("[MIGRATION] Done.")
    await engine.dispose()

if __name__ == "__main__":
    asyncio.run(main())
