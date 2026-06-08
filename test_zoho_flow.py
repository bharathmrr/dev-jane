"""Test script — send a lead to Zoho Flow webhook → creates lead in Zoho CRM."""
import requests

# Paste your full webhook URL here
WEBHOOK_URL = "https://flow.zoho.in/60059702520/flow/webhook/incoming?zapikey=1001.7ff0fec76fe52f368e2e177953bb0089.787f63fe407acd85184f37cab1e63301&isdebug=false"

# Sample lead data (JSON format)
payload = {
    "contact_name": "Test Lead",
    "business_name": "Test Company Pvt Ltd",
    "email": "testlead@example.com",
    "summary": "Procurement manager in aerospace supply chain",
    "designation": "Procurement Manager",
}

response = requests.post(WEBHOOK_URL, json=payload)

print(f"Status: {response.status_code}")
print(f"Response: {response.text}")

if response.status_code == 200:
    print("\nSUCCESS -- Check Zoho CRM Leads for the new record!")
else:
    print("\nFAILED -- Check the webhook URL is correct")
