"""
Read the Google Sheet and show which leads are tracked in DB.
Untracked leads are imported automatically.

Usage (inside worker container):
  docker compose exec worker python check_csv.py
"""
import asyncio
import re
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession
from app.db.session import engine
from app.db.models import LeadV2, LeadStatus
from app.services.sheets import GoogleSheetsService
from app.core.config import settings


def _parse_row(row: dict) -> dict:
    """Extract fields from any sheet column naming convention."""
    email = (
        row.get("email") or row.get("Email")
        or row.get("Business Email") or row.get("business_email") or ""
    ).strip()
    contact_name = (
        row.get("contact_name") or row.get("Contact Name")
        or row.get("Contact person") or row.get("contact person")
        or row.get("Person Name") or row.get("Full Name") or row.get("name") or ""
    ).strip() or None
    business_name = (
        row.get("business_name") or row.get("Business Name")
        or row.get("Company") or row.get("Name of Company")
        or row.get("name of the company") or row.get("Name of the Company") or ""
    ).strip() or contact_name or ""
    designation = (row.get("designation") or row.get("Designation") or "").strip()
    raw_summary = (row.get("summary") or row.get("Summary") or "").strip()
    summary = raw_summary
    if designation and designation not in summary:
        summary = f"{designation}. {summary}".strip(" .")
    return {
        "email": email,
        "contact_name": contact_name,
        "business_name": business_name,
        "summary": summary or None,
    }


async def main():
    svc = GoogleSheetsService()
    if not svc.client:
        print("ERROR: GOOGLE_SHEETS_CREDENTIALS_JSON not configured.")
        return
    if not settings.GOOGLE_SHEETS_SPREADSHEET_ID:
        print("ERROR: GOOGLE_SHEETS_SPREADSHEET_ID not set.")
        return

    try:
        sheet = svc.client.open_by_key(settings.GOOGLE_SHEETS_SPREADSHEET_ID)
        worksheet = sheet.worksheet(settings.GOOGLE_SHEETS_WORKSHEET_NAME)
        rows = worksheet.get_all_records()
    except Exception as e:
        print(f"ERROR reading sheet '{settings.GOOGLE_SHEETS_WORKSHEET_NAME}': {e}")
        return

    if not rows:
        print(f"Sheet '{settings.GOOGLE_SHEETS_WORKSHEET_NAME}' is empty.")
        return

    print(f"\nSheet : {settings.GOOGLE_SHEETS_WORKSHEET_NAME}")
    print(f"Cols  : {list(rows[0].keys())}")
    print(f"Rows  : {len(rows)}\n")

    async with AsyncSession(engine) as session:
        existing = {
            r[0]: r[1]
            for r in (await session.execute(
                text("SELECT email, status FROM leads_v2")
            )).fetchall()
        }

        print(f"{'STATUS':<14} {'EMAIL':<35} {'CONTACT':<25} {'BUSINESS'}")
        print("-" * 110)

        added = 0
        for row in rows:
            parsed = _parse_row(row)
            email = parsed["email"]
            contact = parsed["contact_name"] or ""
            business = parsed["business_name"] or ""

            if not email or not re.match(r"^[^@]+@[^@]+\.[^@]+$", email):
                print(f"{'SKIP-BAD-EMAIL':<14} {email or '—':<35} {contact:<25} {business}")
                continue

            if email in existing:
                db_status = existing[email]
                print(f"{'IN DB ('+db_status+')':<14} {email:<35} {contact:<25} {business}")
            else:
                if not business:
                    print(f"{'SKIP-NO-BIZ':<14} {email:<35} {contact:<25} —")
                    continue
                session.add(LeadV2(
                    email=email,
                    contact_name=parsed["contact_name"],
                    business_name=business,
                    summary=parsed["summary"],
                    status=LeadStatus.NEW,
                ))
                print(f"{'IMPORTED':<14} {email:<35} {contact:<25} {business}")
                added += 1

        if added:
            await session.commit()

    print(f"\n  Imported {added} new lead(s) → will be processed by next pipeline cycle.\n")
    await engine.dispose()


asyncio.run(main())
