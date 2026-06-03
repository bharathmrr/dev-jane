import json
from app.core.config import settings

creds = json.loads(settings.GOOGLE_SHEETS_CREDENTIALS_JSON)
key_str = creds['private_key']
lines = key_str.replace("-----BEGIN PRIVATE KEY-----", "").replace("-----END PRIVATE KEY-----", "").strip().split("\n")

for i, line in enumerate(lines, 1):
    print(f"Line {i}: length {len(line)}, content: {repr(line)}")
