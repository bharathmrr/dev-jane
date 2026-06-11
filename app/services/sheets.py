import json
from typing import List, Dict, Any
import gspread
from google.oauth2.service_account import Credentials
from app.core.config import settings
from structlog import get_logger

logger = get_logger(__name__)

class GoogleSheetsService:
    def __init__(self):
        self.scopes = [
            'https://www.googleapis.com/auth/spreadsheets.readonly'
        ]
        self.client = self._get_client()

    def _get_client(self):
        if not settings.GOOGLE_SHEETS_CREDENTIALS_JSON:
            logger.warning("GOOGLE_SHEETS_CREDENTIALS_JSON is not set. Google Sheets sync will be skipped.")
            return None
        
        try:
            creds_dict = json.loads(settings.GOOGLE_SHEETS_CREDENTIALS_JSON)
            credentials = Credentials.from_service_account_info(creds_dict, scopes=self.scopes)
            return gspread.authorize(credentials)
        except Exception as e:
            logger.error(f"Failed to authenticate Google Sheets: {str(e)}")
            return None

    def fetch_new_leads(self) -> List[Dict[str, Any]]:
        if not self.client or not settings.GOOGLE_SHEETS_SPREADSHEET_ID:
            return []

        try:
            sheet = self.client.open_by_key(settings.GOOGLE_SHEETS_SPREADSHEET_ID)
            worksheet = sheet.worksheet(settings.GOOGLE_SHEETS_WORKSHEET_NAME)
            rows = worksheet.get_all_values()
            if not rows:
                return []
            # Build header map — skip blank or duplicate columns
            raw_headers = [h.strip().lower().replace(" ", "_") for h in rows[0]]
            seen: set = set()
            headers: list[str] = []
            for h in raw_headers:
                if h and h not in seen:
                    headers.append(h)
                    seen.add(h)
                else:
                    headers.append("")  # placeholder for skipped column
            records = []
            for row in rows[1:]:
                if not any(row):
                    continue
                rec: dict = {}
                for i, val in enumerate(row):
                    if i < len(headers) and headers[i]:
                        rec[headers[i]] = val.strip()
                if rec:
                    records.append(rec)
            return records
        except Exception as e:
            logger.error(f"Failed to fetch leads from Google Sheets: {str(e)}")
            return []
