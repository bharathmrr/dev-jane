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
        
    raw_slot = slots[0]
    print(f"\nFound slot: {raw_slot}")
    
    # Parse to "dd-MMM-yyyy HH:mm:ss"
    try:
        parsed_time = datetime.strptime(raw_slot, "%I:%M %p").time()
        time_24 = parsed_time.strftime("%H:%M:%S")
    except ValueError:
        time_24 = "09:00:00"
        
    from_time = f"{tomorrow} {time_24}"
    print(f"Testing booking with from_time: '{from_time}'")
    url = f"{service.base_url}/appointment"
    
    test_email = f"test_{int(datetime.now().timestamp())}@example.com"
    
    payload = {
        "service_id": service.service_id,
        "staff_id": service.staff_id,
        "customer_details": {"name": "Test User", "email": test_email},
        "from_time": from_time,
    }
    
    print("Payload:", payload)
    
    response = requests.post(url, headers=headers, json=payload)
    print("Response text:", response.text)

if __name__ == "__main__":
    main()
