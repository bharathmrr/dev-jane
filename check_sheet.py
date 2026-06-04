"""Print raw records from Google Sheet to debug column name mismatch."""
from app.services.sheets import GoogleSheetsService

svc = GoogleSheetsService()
records = svc.fetch_new_leads()

if not records:
    print("Sheet returned 0 records — check credentials or worksheet name.")
else:
    print(f"Found {len(records)} rows. Column headers from first row:")
    print(list(records[0].keys()))
    print()
    for r in records:
        print(r)
