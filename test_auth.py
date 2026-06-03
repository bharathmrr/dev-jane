import json
from google.oauth2.service_account import Credentials
from app.core.config import settings

creds_dict = json.loads(settings.GOOGLE_SHEETS_CREDENTIALS_JSON)
print("Project ID:", creds_dict.get("project_id"))
print("Client Email:", creds_dict.get("client_email"))
print("Private Key start:", repr(creds_dict.get("private_key")[:50]))

try:
    scopes = ['https://www.googleapis.com/auth/spreadsheets.readonly']
    credentials = Credentials.from_service_account_info(creds_dict, scopes=scopes)
    print("SUCCESS: Credentials loaded!")
except Exception as e:
    print("ERROR loading credentials:")
    import traceback
    traceback.print_exc()
