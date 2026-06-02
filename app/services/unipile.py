import httpx
from typing import Optional
from app.core.config import settings

def get_base_url() -> str:
    """Builds the base URL for the Unipile API, ensuring proper schema prefix."""
    dsn = settings.UNIPILE_DSN.strip()
    if not dsn.startswith("http"):
        return f"https://{dsn}"
    return dsn

def get_headers() -> dict:
    """Builds standardized headers for authentication against the Unipile API."""
    return {
        "X-API-KEY": settings.UNIPILE_API_KEY,
        "Accept": "application/json",
        "Content-Type": "application/json"
    }

async def get_profile_email(identifier: str) -> Optional[str]:
    """
    Fetches the LinkedIn profile using Unipile API and attempts to extract the email address.
    """
    url = f"{get_base_url()}/api/v1/users/{identifier}"
    params = {
        "account_id": settings.UNIPILE_ACCOUNT_ID,
        "linkedin_sections": "*"  # Fetch all sections to maximize chance of email retrieval
    }
    
    print(f"[UNIPILE] Fetching profile for {identifier}...")
    async with httpx.AsyncClient(timeout=15.0) as client:
        try:
            response = await client.get(url, headers=get_headers(), params=params)
            if response.status_code == 200:
                data = response.json()
                email = data.get("email")
                if email:
                    print(f"[UNIPILE] Successfully retrieved email for {identifier}: {email}")
                    return str(email).strip()
                print(f"[UNIPILE] No email found in profile data for {identifier}.")
            else:
                print(f"[UNIPILE] Failed to retrieve profile. Status: {response.status_code}, Response: {response.text}")
        except Exception as e:
            print(f"[UNIPILE] Exception while fetching profile for {identifier}: {e}")
            
    return None

async def send_linkedin_message(chat_id: Optional[str], attendee_id: str, text: str) -> bool:
    """
    Sends a LinkedIn message back to the lead.
    If chat_id is available, uses the existing chat thread.
    Otherwise, starts a new chat thread.
    """
    headers = get_headers()
    
    async with httpx.AsyncClient(timeout=15.0) as client:
        try:
            # 1. Try sending to existing chat if chat_id is available
            if chat_id:
                url = f"{get_base_url()}/api/v1/chats/{chat_id}/messages"
                payload = {
                    "text": text,
                    "account_id": settings.UNIPILE_ACCOUNT_ID
                }
                print(f"[UNIPILE] Sending message to existing chat {chat_id}...")
                response = await client.post(url, headers=headers, json=payload)
                if response.status_code in (200, 201):
                    print(f"[UNIPILE] Message sent successfully to chat {chat_id}.")
                    return True
                print(f"[UNIPILE] Failed to send to chat {chat_id}. Status: {response.status_code}. Retrying by creating a new chat...")
            
            # 2. Fallback to starting a new chat if existing chat failed or chat_id is missing
            url = f"{get_base_url()}/api/v1/chats"
            payload = {
                "account_id": settings.UNIPILE_ACCOUNT_ID,
                "attendees_ids": [attendee_id],
                "text": text
            }
            print(f"[UNIPILE] Initiating new chat with attendee {attendee_id}...")
            response = await client.post(url, headers=headers, json=payload)
            if response.status_code in (200, 201):
                print(f"[UNIPILE] Message sent successfully via new chat with attendee {attendee_id}.")
                return True
                
            print(f"[UNIPILE] Failed to send message via new chat. Status: {response.status_code}, Response: {response.text}")
        except Exception as e:
            print(f"[UNIPILE] Exception while sending LinkedIn message to {attendee_id}: {e}")
            
    return False
