import sys
import os
import requests

sys.path.append(os.path.dirname(os.path.abspath(__file__)))

from app.services.zoho import ZohoBookingsService
from app.core.config import settings

def main():
    service = ZohoBookingsService()
    headers = service._get_headers()
    
    # We will try the V1 appointments list endpoint
    # E.g. https://www.zohoapis.in/bookings/v1/json/appointments
    # Fetch appointment list via POST
    url = f"https://www.zohoapis.{settings.ZOHO_DC}/bookings/v1/json/fetchappointment"
    
    payload = {
        "from_time": "01-Jun-2026 00:00:00",
        "to_time": "30-Jun-2026 23:59:59"
    }
    
    print(f"Fetching appointments from: {url}...")
    try:
        response = requests.post(url, headers=headers, data=payload)
        print("Status Code:", response.status_code)
        print("Response Text:", response.text)
    except Exception as e:
        print("Request failed:", e)

if __name__ == "__main__":
    main()
