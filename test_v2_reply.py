import asyncio
from datetime import datetime, timezone, timedelta
from sqlalchemy.future import select
from app.db.session import SessionLocal
from app.db.models import LeadV2, ZohoSlot, SlotStatus, LeadStatus
from app.workers.v2_tasks import _process_reply_v2

async def main():
    print("--- Setting up mock data for testing V2 reply ---")
    
    # Calculate mock slot times for different days
    today = datetime.now(timezone.utc).date()
    
    # Day 1: tomorrow
    day1 = today + timedelta(days=1)
    slot_day1_10am = datetime.combine(day1, datetime.strptime("10:00 AM", "%I:%M %p").time(), timezone.utc)
    
    # Day 4: 4 days from now
    day4 = today + timedelta(days=4)
    slot_day4_11am = datetime.combine(day4, datetime.strptime("11:00 AM", "%I:%M %p").time(), timezone.utc)
    
    # Day 5: 5 days from now
    day5 = today + timedelta(days=5)
    slot_day5_2pm = datetime.combine(day5, datetime.strptime("02:00 PM", "%I:%M %p").time(), timezone.utc)
    
    async with SessionLocal() as db:
        # 1. Create or get LeadV2
        lead = (await db.execute(
            select(LeadV2).where(LeadV2.email == "bharathreddyget@gmail.com")
        )).scalar_one_or_none()
        
        if not lead:
            lead = LeadV2(
                business_name="jane",
                email="bharathreddyget@gmail.com",
                status=LeadStatus.SENT
            )
            db.add(lead)
            print("Created new LeadV2 with status SENT.")
        else:
            lead.status = LeadStatus.SENT
            lead.offered_slots_json = None
            print("Reset existing LeadV2 status to SENT.")

        # 2. Add available Zoho slots for the days
        times = [slot_day1_10am, slot_day4_11am, slot_day5_2pm]
        for t in times:
            slot_id = f"z_slot_{t.strftime('%Y-%m-%d_%H:%M:%S')}"
            existing_slot = (await db.execute(
                select(ZohoSlot).where(ZohoSlot.zoho_slot_id == slot_id)
            )).scalar_one_or_none()
            
            if not existing_slot:
                db.add(ZohoSlot(
                    zoho_slot_id=slot_id,
                    slot_time=t,
                    status=SlotStatus.AVAILABLE
                ))
                print(f"Added mock ZohoSlot: {t.strftime('%Y-%m-%d %I:%M %p')}")
            else:
                existing_slot.status = SlotStatus.AVAILABLE
                print(f"Reset ZohoSlot to AVAILABLE: {t.strftime('%Y-%m-%d %I:%M %p')}")
                
        await db.commit()

    # Case 1: Filter slots (e.g. "after [Day 2]")
    filter_day = today + timedelta(days=2)
    filter_day_name = filter_day.strftime("%A")
    
    print(f"\n--- Simulating Lead Reply: Requesting slots after {filter_day_name} ---")
    mock_reply_filter = {
        "from_addr": "bharathreddyget@gmail.com",
        "body": f"Can we meet? Send me the slots after this {filter_day_name}.",
        "subject": "Re: Let's schedule a call!"
    }
    
    print(f"Lead email: {mock_reply_filter['from_addr']}")
    print(f"Reply body: \"{mock_reply_filter['body']}\"")
    
    async with SessionLocal() as db:
        result = await _process_reply_v2(db, mock_reply_filter)
        print("Processing result:", result)
        await db.commit()

    print("\n--- Verifying Database State (Offered slots should only be after Day 2) ---")
    async with SessionLocal() as db:
        lead = (await db.execute(
            select(LeadV2).where(LeadV2.email == "bharathreddyget@gmail.com")
        )).scalar_one()
        print(f"Lead status: {lead.status}")
        print(f"Lead offered slots JSON: {lead.offered_slots_json}")

if __name__ == "__main__":
    asyncio.run(main())
