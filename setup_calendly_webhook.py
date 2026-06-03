#!/usr/bin/env python3
"""Set up Calendly webhook via API"""

import requests
import sys
import base64
import json

# Calendly token
TOKEN = "eyJraWQiOiIxY2UxZTEzNjE3ZGNmNzY2YjNjZWJjY2Y4ZGM1YmFmYThhNjVlNjg0MDIzZjdjMzJiZTgzNDliMjM4MDEzNWI0IiwidHlwIjoiUEFUIiwiYWxnIjoiRVMyNTYifQ.eyJpc3MiOiJodHRwczovL2F1dGguY2FsZW5kbHkuY29tIiwiaWF0IjoxNzgwMzkyOTY5LCJqdGkiOiJmZDJmMzhiMS1hNDBkLTQzNzEtYTU0YS1lYzU2ODM5MzZlNTMiLCJ1c2VyX3V1aWQiOiIyZTM0OWMyYS04MGM5LTRkYzMtOWFkMy1lZjY2M2YxODhjNmMiLCJzY29wZSI6IndlYmhvb2tzOnJlYWQgd2ViaG9va3M6d3JpdGUgYWN0aXZpdHlfbG9nOnJlYWQgZGF0YV9jb21wbGlhbmNlOndyaXRlIG91dGdvaW5nX2NvbW11bmljYXRpb25zOnJlYWQgY29udGFjdHM6cmVhZCBjb250YWN0czp3cml0ZSBncm91cHM6cmVhZCBvcmdhbml6YXRpb25zOnJlYWQgb3JnYW5pemF0aW9uczp3cml0ZSB1c2VyczpyZWFkIGF2YWlsYWJpbGl0eTpyZWFkIGF2YWlsYWJpbGl0eTp3cml0ZSBldmVudF90eXBlczpyZWFkIGV2ZW50X3R5cGVzOndyaXRlIGxvY2F0aW9uczpyZWFkIHJvdXRpbmdfZm9ybXM6cmVhZCBzaGFyZXM6d3JpdGUgc2NoZWR1bGVkX2V2ZW50czpyZWFkIHNjaGVkdWxlZF9ldmVudHM6d3JpdGUgc2NoZWR1bGluZ19saW5rczp3cml0ZSJ9.dzH2-Mno4aEp3Rg6H1HtQ0eOkwTK9FZg7zHPHV1OxKM1_gqPZO60v5WYU6JsSspGIAJJjTeqa7TNtP-FkZp0KA"

# Your webhook URL (the ngrok URL from .env)
WEBHOOK_URL = "https://barrier-thirteen-untidy.ngrok-free.dev/webhook/calendly"

headers = {
    "Authorization": f"Bearer {TOKEN}",
    "Content-Type": "application/json"
}

# Step 1: Get current user (to verify token works)
print("[*] Verifying token...")
resp = requests.get("https://api.calendly.com/users/me", headers=headers)
if resp.status_code != 200:
    print(f"[ERROR] Token verification failed: {resp.status_code}")
    print(resp.text)
    sys.exit(1)

user_data = resp.json()
user_uri = user_data['resource']['uri']
org_uri = user_data['resource'].get('current_organization')
print(f"[OK] Token valid. User: {user_data['resource']['name']}")
if org_uri:
    print(f"   Organization: {org_uri}")

# Validate org_uri was obtained
if not org_uri:
    print(f"[ERROR] Could not get organization from user data")
    sys.exit(1)

# Step 2: Try listing event types to verify we have access
print("\n[*] Verifying API access...")
resp = requests.get("https://api.calendly.com/event_types", headers=headers)
if resp.status_code != 200:
    print(f"[WARN] Could not fetch event types: {resp.status_code}")
else:
    print(f"[OK] API access verified")

# Step 3: Create webhook with organization scope
print(f"\n[*] Creating webhook for invitee.created -> {WEBHOOK_URL}")
resp = requests.post(
    "https://api.calendly.com/webhook_subscriptions",
    headers=headers,
    json={
        "url": WEBHOOK_URL,
        "events": ["invitee.created"],
        "organization": org_uri,
        "scope": "organization"
    }
)

if resp.status_code in [200, 201]:
    webhook = resp.json()['resource']
    print(f"[OK] Webhook created!")
    print(f"   ID: {webhook['uri']}")
    print(f"   Events: {webhook['events']}")
    print(f"   URL: {webhook['callback_url']}")
    print(f"   State: {webhook.get('state', 'active')}")
else:
    print(f"[ERROR] Failed: {resp.status_code}")
    print(resp.text)
    sys.exit(1)

print("\n[OK] Calendly webhook setup complete!")
print("When leads book, the dashboard will update automatically.")
