import requests
import json

access_token = "1000.4cf281c09ee78a09d334766f44c79555.146510e9666c4a55090c3c4e33873e87"
headers = {"Authorization": f"Zoho-oauthtoken {access_token}"}

print("--- Services ---")
resp = requests.get("https://www.zohoapis.in/bookings/v1/json/services", headers=headers)
print(resp.text)

print("\n--- Staff ---")
resp = requests.get("https://www.zohoapis.in/bookings/v1/json/staffs", headers=headers)
print(resp.text)
