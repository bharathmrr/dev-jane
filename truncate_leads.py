"""Truncate leads_v2 table and reset the identity sequence."""
import asyncio
from sqlalchemy import text
from app.db.session import engine


async def main():
    async with engine.begin() as conn:
        count = (await conn.execute(text("SELECT COUNT(*) FROM leads_v2"))).scalar()
        print(f"Rows before: {count}")

        await conn.execute(text("TRUNCATE TABLE leads_v2 RESTART IDENTITY CASCADE"))
        print("Truncated leads_v2.")

        count = (await conn.execute(text("SELECT COUNT(*) FROM leads_v2"))).scalar()
        print(f"Rows after:  {count}")

    await engine.dispose()


asyncio.run(main())
