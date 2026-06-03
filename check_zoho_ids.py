import sys
import os
import requests

sys.path.append(os.path.dirname(os.path.abspath(__file__)))

from app.services.zoho import ZohoBookingsService
from app.core.config import settings

def main():
    service = ZohoBookingsService()
    headers = service._get_headers()
    print("Headers:", headers)
    
    print("\n--- Services ---")
    url_services = f"https://www.zohoapis.{settings.ZOHO_DC}/bookings/v1/json/services"
    try:
        resp = requests.get(url_services, headers=headers)
        print("Status:", resp.status_code)
        print("Services response:", resp.text)
    except Exception as e:
        print("Failed to get services:", e)

    print("\n--- Staff ---")
    url_staff = f"https://www.zohoapis.{settings.ZOHO_DC}/bookings/v1/json/staffs"
    try:
        resp = requests.get(url_staff, headers=headers)
        print("Status:", resp.status_code)
        print("Staff response:", resp.text)
    except Exception as e:
        print("Failed to get staff:", e)

if __name__ == "__main__":
    main()
