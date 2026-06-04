import sys
import os
from datetime import datetime, timedelta

sys.path.append(os.path.dirname(os.path.abspath(__file__)))

from app.services.zoho import ZohoBookingsService

def main():
    service = ZohoBookingsService()
    today = datetime.now().strftime("%d-%b-%Y")
    tomorrow = (datetime.now() + timedelta(days=1)).strftime("%d-%b-%Y")
    
    print(f"--- Available slots for today ({today}) ---")
    print(service.fetch_available_slots(today))
    
    print(f"\n--- Available slots for tomorrow ({tomorrow}) ---")
    print(service.fetch_available_slots(tomorrow))

if __name__ == "__main__":
    main()
