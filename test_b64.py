import json
import base64
from app.core.config import settings

creds = json.loads(settings.GOOGLE_SHEETS_CREDENTIALS_JSON)
key_str = creds['private_key']

# Strip header/footer
clean_key = key_str.replace("-----BEGIN PRIVATE KEY-----", "").replace("-----END PRIVATE KEY-----", "").strip()
# Remove all newlines and backslash-n
clean_key = clean_key.replace("\n", "").replace("\\n", "").replace("\r", "")

print("Cleaned base64 length:", len(clean_key))
try:
    decoded = base64.b64decode(clean_key)
    print("SUCCESS decoding base64! Decoded bytes length:", len(decoded))
except Exception as e:
    print("ERROR decoding base64:", str(e))
