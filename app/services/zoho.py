import time
import requests
from typing import List, Dict, Any, Optional
from structlog import get_logger
from app.core.config import settings

logger = get_logger(__name__)

# Module-level cache so all instances share one token (valid ~1 hour)
_token_cache: dict = {"token": None, "expires_at": 0.0}


class ZohoBookingsService:
    def __init__(self):
        self.base_url = f"https://www.zohoapis.{settings.ZOHO_DC}/bookings/v1/json"
        self.client_id = settings.ZOHO_CLIENT_ID
        self.client_secret = settings.ZOHO_CLIENT_SECRET
        self.refresh_token = settings.ZOHO_REFRESH_TOKEN
        self.service_id = settings.ZOHO_SERVICE_ID
        self.staff_id = settings.ZOHO_STAFF_ID
        self.access_token = None

    def _get_access_token(self) -> Optional[str]:
        if not self.refresh_token:
            logger.warning("Zoho refresh token is not set.")
            return None

        # Return cached token if still valid
        if _token_cache["token"] and time.time() < _token_cache["expires_at"]:
            return _token_cache["token"]

        url = f"https://accounts.zoho.{settings.ZOHO_DC}/oauth/v2/token"
        params = {
            "refresh_token": self.refresh_token,
            "client_id": self.client_id,
            "client_secret": self.client_secret,
            "grant_type": "refresh_token"
        }
        try:
            response = requests.post(url, data=params)
            response.raise_for_status()
            data = response.json()
            token = data.get("access_token")
            if token:
                _token_cache["token"] = token
                _token_cache["expires_at"] = time.time() + 3500  # refresh before 1hr expiry
            return token
        except Exception as e:
            logger.error(f"Failed to fetch Zoho access token: {str(e)}")
            return None

    def _get_headers(self) -> Dict[str, str]:
        token = self._get_access_token()
        if token:
            return {"Authorization": f"Zoho-oauthtoken {token}"}
        return {}

    def fetch_available_slots(self, date_str: str) -> List[Dict[str, Any]]:
        """Fetch slots for a specific date (Format: DD-MMM-YYYY)"""
        headers = self._get_headers()
        if not headers or not self.service_id or not self.staff_id:
            return []

        url = f"{self.base_url}/availableslots"
        params = {
            "service_id": self.service_id,
            "staff_id": self.staff_id,
            "selected_date": date_str
        }
        
        try:
            response = requests.get(url, headers=headers, params=params)
            response.raise_for_status()
            data = response.json()
            return data.get("response", {}).get("returnvalue", {}).get("data", [])
        except Exception as e:
            logger.error(f"Failed to fetch Zoho slots: {str(e)}")
            return []

    def create_booking(
        self, name: str, email: str, date_str: str, time_str: str
    ) -> tuple[Optional[str], Optional[str]]:
        """Create a booking. Returns (booking_id, meeting_link)."""
        headers = self._get_headers()
        if not headers:
            return None, None

        # Zoho Bookings V1 API expects 'from_time' in "dd-MMM-yyyy HH:mm:ss" format
        if len(time_str) == 5:
            from_time = f"{date_str} {time_str}:00"
        else:
            from_time = f"{date_str} {time_str}"

        url = f"{self.base_url}/appointment"
        payload = {
            "service_id": self.service_id,
            "staff_id": self.staff_id,
            "customer_details": {"name": name, "email": email},
            "from_time": from_time,
        }

        try:
            response = requests.post(url, headers=headers, json=payload)
            response.raise_for_status()
            data = response.json()
            rv = data.get("response", {}).get("returnvalue", {})
            if data.get("response", {}).get("status") == "success":
                booking_id = rv.get("booking_id") or rv.get("id")
                meeting_link = (
                    rv.get("meeting_url")
                    or rv.get("meeting_link")
                    or rv.get("join_url")
                    or rv.get("online_meeting", {}).get("join_url")
                )
                return booking_id, meeting_link
            logger.error(f"Zoho booking failed: {data}")
            return None, None
        except Exception as e:
            logger.error(f"Failed to create Zoho booking: {str(e)}")
            return None, None

    def cancel_booking(self, booking_id: str) -> bool:
        """Cancel an existing booking. Returns True on success."""
        headers = self._get_headers()
        if not headers or not booking_id:
            return False
        url = f"{self.base_url}/appointment"
        params = {"booking_id": booking_id}
        try:
            response = requests.delete(url, headers=headers, params=params)
            data = response.json()
            success = data.get("response", {}).get("status") == "success"
            if success:
                logger.info("zoho_booking_cancelled", booking_id=booking_id)
            else:
                logger.warning("zoho_cancel_failed", booking_id=booking_id, response=data)
            return success
        except Exception as e:
            logger.error(f"Failed to cancel Zoho booking {booking_id}: {str(e)}")
            return False
