import sys
import os
import requests
from datetime import datetime, timedelta

sys.path.append(os.path.dirname(os.path.abspath(__file__)))

from app.services.zoho import ZohoBookingsService
from app.core.config import settings

def main():
    service = ZohoBookingsService()
    headers = service._get_headers()
    
    # Calculate tomorrow's date
    tomorrow = (datetime.now() + timedelta(days=1)).strftime("%d-%b-%Y")
    print(f"Fetching available slots for tomorrow ({tomorrow})...")
    
    slots = service.fetch_available_slots(tomorrow)
    print("Available slots tomorrow:", slots)
    
    if not slots:
        print("No slots available tomorrow. Cannot proceed with booking test.")
        return
        
    # Let's try booking the first slot.
    # We will test both format styles: 24-hour ("16:30") and 12-hour AM/PM ("04:30 PM").
    raw_slot = slots[0]
    print(f"\nFound slot: {raw_slot}")
    
    # Parse to 24-hour and 12-hour formats
    try:
        parsed_time = datetime.strptime(raw_slot, "%I:%M %p").time()
        time_24 = parsed_time.strftime("%H:%M")
        time_12 = parsed_time.strftime("%I:%M %p")
    except ValueError:
        time_24 = raw_slot
        time_12 = raw_slot
        
    print(f"Testing booking with 24-hour format: {time_24}")
    url = f"{service.base_url}/appointment"
    
    # Use a unique email to avoid "already booked" conflicts for the same customer
    test_email = f"test_{int(datetime.now().timestamp())}@example.com"
    
    payload_24 = {
        "service_id": service.service_id,
        "staff_id": service.staff_id,
        "customer_details": {"name": "Test User", "email": test_email},
        "date": tomorrow,
        "time": time_24,
    }
    
    response = requests.post(url, headers=headers, json=payload_24)
    print("24-hour format response:", response.text)
    
    # If 24-hour failed, let's try 12-hour format
    if "failure" in response.text:
        print(f"\n24-hour format failed. Testing booking with 12-hour format: {time_12}")
        payload_12 = {
            "service_id": service.service_id,
            "staff_id": service.staff_id,
            "customer_details": {"name": "Test User", "email": test_email},
            "date": tomorrow,
            "time": time_12,
        }
        response = requests.post(url, headers=headers, json=payload_12)
        print("12-hour format response:", response.text)

if __name__ == "__main__":
    main()
