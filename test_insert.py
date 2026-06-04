"""Test: manually insert a lead and verify it persists."""
import asyncio
from sqlalchemy import text
from app.db.session import engine, SessionLocal
from app.db.models import LeadV2, LeadStatus


async def main():
    async with SessionLocal() as db:
        db.add(LeadV2(business_name="Test Co", email="test_debug@example.com", status=LeadStatus.NEW))
        await db.commit()
        print("Inserted test lead.")

    async with engine.connect() as conn:
        rows = (await conn.execute(text("SELECT id, email, status FROM leads_v2"))).fetchall()
        print(f"Leads in DB: {len(rows)}")
        for r in rows:
            print(f"  id={r[0]} email={r[1]} status={r[2]}")

    await engine.dispose()


asyncio.run(main())
