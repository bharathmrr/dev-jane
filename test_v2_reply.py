import asyncio
from datetime import datetime, timezone, timedelta
from sqlalchemy.future import select
from app.db.session import SessionLocal
from app.db.models import LeadV2, ZohoSlot, SlotStatus, LeadStatus
from app.workers.v2_tasks import _process_reply_v2

async def main():
    print("--- Setting up mock data for testing V2 reply ---")
    
    # Calculate mock slot times for tomorrow
    tomorrow = datetime.now(timezone.utc).date() + timedelta(days=1)
    
    slot_10am = datetime.combine(tomorrow, datetime.strptime("10:00 AM", "%I:%M %p").time(), timezone.utc)
    slot_1pm = datetime.combine(tomorrow, datetime.strptime("1:00 PM", "%I:%M %p").time(), timezone.utc)
    slot_3pm = datetime.combine(tomorrow, datetime.strptime("3:00 PM", "%I:%M %p").time(), timezone.utc)
    
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
            print("Reset existing LeadV2 status to SENT.")

        # 2. Add 3 available Zoho slots for tomorrow
        times = [slot_10am, slot_1pm, slot_3pm]
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

    print("\n--- Simulating Lead Reply ---")
    mock_reply = {
        "from_addr": "bharathreddyget@gmail.com",
        "body": "Hi, I'd like to book the Thursday, Jun 04 at 10:00 AM slot. Thanks!",
        "subject": "Re: Let's schedule a call!"
    }
    
    print(f"Lead email: {mock_reply['from_addr']}")
    print(f"Reply body: \"{mock_reply['body']}\"")
    
    async with SessionLocal() as db:
        result = await _process_reply_v2(db, mock_reply)
        print("\nProcessing result:", result)
        await db.commit()

    print("\n--- Verifying Database State ---")
    async with SessionLocal() as db:
        lead = (await db.execute(
            select(LeadV2).where(LeadV2.email == "bharathreddyget@gmail.com")
        )).scalar_one()
        print(f"Lead status: {lead.status}")
        print(f"Lead selected slot: {lead.selected_slot}")
        print(f"Lead booking ID: {lead.booking_id}")
        
        slots = (await db.execute(
            select(ZohoSlot).order_by(ZohoSlot.slot_time)
        )).scalars().all()
        for s in slots:
            print(f"- Slot: {s.slot_time.strftime('%Y-%m-%d %I:%M %p')} | Status: {s.status} | Booked by: {s.booked_email}")

if __name__ == "__main__":
    asyncio.run(main())
