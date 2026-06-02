"""
Direct email send test — bypasses Celery, runs the full flow synchronously.
Usage:
  python scripts/test_send_email.py <lead_name> <lead_email> <organizer_id>
"""
import asyncio
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from sqlalchemy import select
from app.db.session import SessionLocal
from app.db.models import Booking, EmailThread, Lead, Organizer
from app.workers.email_tasks import _send_offer
from app.services import booking as booking_svc


async def main(lead_name: str, lead_email: str, organizer_id: str):
    async with SessionLocal() as db:
        # Get organizer
        org = await db.get(Organizer, organizer_id)
        if not org:
            print(f"ERROR: Organizer {organizer_id} not found")
            return

        if not org.is_active:
            print(f"ERROR: Organizer {org.display_name} is inactive")
            return

        print(f"Organizer: {org.display_name} (tz={org.timezone})")
        avail = org.availability_rules
        print(f"Availability rules: {len(avail)} days")

        if len(avail) == 0:
            print("ERROR: Organizer has no availability rules — no slots to offer!")
            return

        # Create lead + booking
        booking = await booking_svc.create_lead_and_booking(
            db, name=lead_name, email=lead_email, organizer=org
        )
        print(f"Created booking: {booking.id}  state={booking.state}")

        # Send the offer email directly (synchronous, no Celery)
        await _send_offer(db, str(booking.id))
        print(f"Email sent to {lead_email}")
        print(f"Booking state after: {booking.state}")
        print("SUCCESS")


if __name__ == "__main__":
    name = sys.argv[1] if len(sys.argv) > 1 else "Test Lead"
    email = sys.argv[2] if len(sys.argv) > 2 else "temp70845@gmail.com"
    org_id = sys.argv[3] if len(sys.argv) > 3 else None

    if not org_id:
        # Find first active organizer
        async def get_org():
            async with SessionLocal() as db:
                result = await db.execute(
                    select(Organizer).where(Organizer.is_active == True)
                )
                o = result.scalars().first()
                if o:
                    print(f"Using organizer: {o.display_name} ({o.id})")
                    return str(o.id)
                return None
        org_id = asyncio.run(get_org())
        if not org_id:
            print("ERROR: No active organizers found. Create one first.")
            sys.exit(1)

    asyncio.run(main(name, email, org_id))
