import sys
import os
import requests
from datetime import datetime

sys.path.append(os.path.dirname(os.path.abspath(__file__)))

from app.services.zoho import ZohoBookingsService
from app.core.config import settings

def main():
    service = ZohoBookingsService()
    headers = service._get_headers()
    print("Headers:", headers)
    
    url = f"{service.base_url}/appointment"
    
    # Let's check what slots are available first
    today = datetime.now().strftime("%d-%b-%Y")
    slots = service.fetch_available_slots(today)
    print("Available slots today:", slots)
    
    if not slots:
        print("No slots available today. Testing with a dummy future time.")
        time_str = "10:00"
    else:
        # Convert "04:30 PM" to "16:30"
        slot_item = slots[0]
        if isinstance(slot_item, dict):
            raw_time = slot_item.get("time", "10:00")
        else:
            raw_time = str(slot_item)
            
        try:
            parsed_time = datetime.strptime(raw_time, "%I:%M %p").time()
            time_str = parsed_time.strftime("%H:%M")
        except ValueError:
            time_str = raw_time
            
    print(f"Attempting to book at: {today} {time_str} (parsed from {slots[0] if slots else 'N/A'})")
    
    payload = {
        "service_id": service.service_id,
        "staff_id": service.staff_id,
        "customer_details": {"name": "Test User", "email": "test_user@example.com"},
        "date": today,
        "time": time_str,
    }
    
    print("Payload:", payload)
    
    try:
        response = requests.post(url, headers=headers, json=payload)
        print("HTTP Status Code:", response.status_code)
        print("Response Text:", response.text)
    except Exception as e:
        print("Request failed:", e)

if __name__ == "__main__":
    main()
