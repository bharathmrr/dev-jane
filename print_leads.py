import asyncio
from sqlalchemy.future import select
from app.db.session import SessionLocal
from app.db.models import LeadV2

async def main():
    print("--- Database Leads ---")
    async with SessionLocal() as db:
        leads = (await db.execute(select(LeadV2))).scalars().all()
        print(f"Total leads: {len(leads)}")
        for lead in leads:
            print(f"- ID: {lead.id}, Name: {lead.business_name}, Email: {lead.email}, Status: {lead.status}")

if __name__ == "__main__":
    asyncio.run(main())
