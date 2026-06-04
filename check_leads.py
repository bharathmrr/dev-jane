"""Show all leads and their current status."""
import asyncio
from sqlalchemy import text
from app.db.session import engine


async def main():
    async with engine.connect() as conn:
        rows = (await conn.execute(
            text("SELECT id, email, business_name, status, sent_at FROM leads_v2 ORDER BY id")
        )).fetchall()

    if not rows:
        print("DB is empty — no leads at all.")
    else:
        print(f"{'ID':<5} {'STATUS':<10} {'EMAIL':<40} {'NAME':<30} SENT_AT")
        print("-" * 110)
        for r in rows:
            print(f"{r[0]:<5} {r[3]:<10} {r[1]:<40} {r[2]:<30} {r[4]}")

    await engine.dispose()


asyncio.run(main())
