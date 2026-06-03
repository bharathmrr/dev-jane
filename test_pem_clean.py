import json
import traceback
from cryptography.hazmat.primitives.serialization import load_pem_private_key
from app.core.config import settings

creds = json.loads(settings.GOOGLE_SHEETS_CREDENTIALS_JSON)
key_str = creds['private_key']

try:
    load_pem_private_key(key_str.encode('utf-8'), password=None)
except Exception as e:
    traceback.print_exc()
