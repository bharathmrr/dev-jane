import asyncio
from sqlalchemy.future import select
from app.db.session import SessionLocal
from app.db.models import LeadV2
from app.workers.v2_tasks import sync_google_sheet

def run_sync_task():
    print("--- Running Google Sheets Sync Celery Task ---")
    result = sync_google_sheet()
    print("Task result:", result)

async def check_db():
    print("\nChecking database for imported leads...")
    async with SessionLocal() as db:
        leads = (await db.execute(select(LeadV2))).scalars().all()
        print(f"Total leads in database: {len(leads)}")
        for lead in leads:
            print(f"- ID: {lead.id}, Name: {lead.business_name}, Email: {lead.email}, Status: {lead.status}")

def main():
    # Run Celery task outside of any asyncio event loop (exactly like a Celery worker thread)
    run_sync_task()
    
    # Run db check in a new event loop
    asyncio.run(check_db())

if __name__ == "__main__":
    main()
