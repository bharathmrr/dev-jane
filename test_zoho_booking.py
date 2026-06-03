import sys
import os
from datetime import datetime

# Ensure the root directory is in python path
sys.path.append(os.path.dirname(os.path.abspath(__file__)))

from app.services.zoho import ZohoBookingsService
from app.core.config import settings
from app.core.logging import configure_logging

def test_zoho():
    configure_logging()
    print("--- Zoho Booking Service Test ---")
    print(f"Zoho DC: {settings.ZOHO_DC}")
    print(f"Zoho Client ID: {settings.ZOHO_CLIENT_ID}")
    print(f"Zoho Service ID: {settings.ZOHO_SERVICE_ID}")
    print(f"Zoho Staff ID: {settings.ZOHO_STAFF_ID}")
    
    service = ZohoBookingsService()
    
    print("\n1. Testing OAuth token retrieval...")
    token = service._get_access_token()
    if token:
        print(f"[OK] Success! Token retrieved (starts with: {token[:15]}...)")
    else:
        print("[FAIL] Failed to retrieve access token. Check credentials in .env")
        return

    print("\n2. Testing slot retrieval...")
    today = datetime.now().strftime("%d-%b-%Y")
    print(f"Fetching slots for today ({today})...")
    slots = service.fetch_available_slots(today)
    print(f"[OK] Success! Fetched {len(slots)} slots.")
    if slots:
        print("First 3 slots:")
        for s in slots[:3]:
            print(f" - {s}")
    else:
        print("No slots returned for today (or none configured).")

    print("\n3. Testing booking creation (dry-run/mock confirmation)...")
    print("To test booking creation, we will try to create a test booking.")
    confirm = input("Do you want to create a test booking? (y/N): ").strip().lower()
    if confirm == 'y':
        date_str = input(f"Enter date (DD-MMM-YYYY) [default: {today}]: ").strip() or today
        time_str = input("Enter time (HH:MM) [e.g. 10:00]: ").strip()
        if not time_str:
            print("Time is required to test booking. Skipping booking test.")
            return
            
        print(f"Creating booking for test user on {date_str} at {time_str}...")
        booking_id, meeting_link = service.create_booking(
            name="Test User",
            email="test_user@example.com",
            date_str=date_str,
            time_str=time_str
        )
        if booking_id:
            print(f"[OK] Success! Booking created.")
            print(f"   Booking ID: {booking_id}")
            print(f"   Meeting Link: {meeting_link}")
        else:
            print("[FAIL] Booking creation failed. See backend logs for details.")
    else:
        print("Skipped booking creation test.")

if __name__ == "__main__":
    test_zoho()
