import json
import asyncio
from app.core.config import settings
from app.services.sheets import GoogleSheetsService

async def main():
    print("--- Google Sheets Sync Test ---")
    print(f"Spreadsheet ID: {settings.GOOGLE_SHEETS_SPREADSHEET_ID or 'Not Set'}")
    print(f"Worksheet Name: {settings.GOOGLE_SHEETS_WORKSHEET_NAME or 'Not Set'}")
    print(f"Credentials JSON configured: {'Yes' if settings.GOOGLE_SHEETS_CREDENTIALS_JSON else 'No'}")
    
    if not settings.GOOGLE_SHEETS_CREDENTIALS_JSON:
        print("\n[ERROR] GOOGLE_SHEETS_CREDENTIALS_JSON is not set in your .env file.")
        print("Please add your Google Service Account JSON string to the .env file.")
        return
        
    if not settings.GOOGLE_SHEETS_SPREADSHEET_ID:
        print("\n[ERROR] GOOGLE_SHEETS_SPREADSHEET_ID is not set in your .env file.")
        print("Please add the ID of your Google Sheet to the .env file.")
        return

    print("\nAttempting to connect to Google Sheets...")
    service = GoogleSheetsService()
    if not service.client:
        print("[ERROR] Failed to authorize Google Sheets client. Check your credentials JSON formatting.")
        return

    print("Successfully authorized. Fetching records...")
    
    try:
        sheet = service.client.open_by_key(settings.GOOGLE_SHEETS_SPREADSHEET_ID)
        worksheets = sheet.worksheets()
        available_tabs = [ws.title for ws in worksheets]
        print(f"\nSpreadsheet connected successfully! Available tabs in your sheet: {available_tabs}")
    except Exception as e:
        print(f"[ERROR] Could not connect to spreadsheet. Have you shared the sheet with the service account email? Error: {str(e)}")
        return

    records = service.fetch_new_leads()
    print(f"\nFetched {len(records)} records from sheet '{settings.GOOGLE_SHEETS_WORKSHEET_NAME}':")
    for i, record in enumerate(records, 1):
        print(f"Record {i}: {record}")

if __name__ == "__main__":
    asyncio.run(main())
