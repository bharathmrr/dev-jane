"""Remove all cell background colors and conditional formatting from every sheet
in the Jane Aerospace Google Spreadsheet.

Run once to clean up leftover formatting from old code:
    python scripts/clear_sheet_colors.py
"""
import json
import os
import sys

import gspread
from google.oauth2.service_account import Credentials

SPREADSHEET_ID = "1MOz_PjP8vijZswdjuM24IJwAzR-B1AGEPmAyWN37TGU"
CREDS_JSON = os.environ.get("GOOGLE_SHEETS_CREDENTIALS_JSON", "")

if not CREDS_JSON:
    env_path = os.path.join(os.path.dirname(__file__), "..", ".env")
    for line in open(env_path):
        line = line.strip()
        if line.startswith("GOOGLE_SHEETS_CREDENTIALS_JSON="):
            CREDS_JSON = line.split("=", 1)[1].strip().strip("'\"")
            break

if not CREDS_JSON:
    print("[ERROR] GOOGLE_SHEETS_CREDENTIALS_JSON not found")
    sys.exit(1)

creds = Credentials.from_service_account_info(
    json.loads(CREDS_JSON),
    scopes=[
        "https://spreadsheets.google.com/feeds",
        "https://www.googleapis.com/auth/drive",
        "https://www.googleapis.com/auth/spreadsheets",
    ],
)
gc = gspread.authorize(creds)
sh = gc.open_by_key(SPREADSHEET_ID)

WHITE = {"red": 1.0, "green": 1.0, "blue": 1.0}

for ws in sh.worksheets():
    print(f"  Clearing: {ws.title!r} (id={ws.id}) ...", end=" ", flush=True)
    sheet_id = ws.id

    requests = [
        # 1. Remove ALL conditional format rules on this sheet
        {
            "deleteConditionalFormatRule": {
                "sheetId": sheet_id,
                "index": 0,
            }
        },
        # 2. Reset ALL cell backgrounds to white across entire used range
        {
            "repeatCell": {
                "range": {
                    "sheetId": sheet_id,
                    "startRowIndex": 0,
                    "startColumnIndex": 0,
                },
                "cell": {
                    "userEnteredFormat": {
                        "backgroundColor": WHITE,
                    }
                },
                "fields": "userEnteredFormat.backgroundColor",
            }
        },
    ]

    # Delete conditional format rules one by one (they shift down after each delete)
    # First find out how many exist
    meta = sh.fetch_sheet_metadata()
    sheet_meta = next(
        (s for s in meta["sheets"] if s["properties"]["sheetId"] == sheet_id), None
    )
    cf_rules = (sheet_meta or {}).get("conditionalFormats", []) if sheet_meta else []
    n_rules = len(cf_rules)

    delete_reqs = [
        {"deleteConditionalFormatRule": {"sheetId": sheet_id, "index": 0}}
        for _ in range(n_rules)
    ]
    clear_bg_req = [{
        "repeatCell": {
            "range": {"sheetId": sheet_id},
            "cell": {"userEnteredFormat": {"backgroundColor": WHITE}},
            "fields": "userEnteredFormat.backgroundColor",
        }
    }]

    batch = delete_reqs + clear_bg_req
    if batch:
        sh.batch_update({"requests": batch})

    print(f"done ({n_rules} conditional rules removed)")

print("\nAll sheets cleared.")
