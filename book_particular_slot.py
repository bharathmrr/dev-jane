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
    
    date_str = "03-Jun-2026"
    time_str = "18:00:00"
    from_time = f"{date_str} {time_str}"
    
    print(f"Creating booking for date: {date_str}, time: {time_str} ({from_time})...")
    
    # Generate unique test email to avoid duplicate booking blocks
    test_email = f"test_{int(datetime.now().timestamp())}@example.com"
    
    payload = {
        "service_id": service.service_id,
        "staff_id": service.staff_id,
        "customer_details": {
            "name": "Test User 6PM",
            "email": test_email
        },
        "from_time": from_time,
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
