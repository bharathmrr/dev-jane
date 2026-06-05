import asyncio
import csv
import json
import os
import re
from datetime import datetime, timezone, timedelta

from celery import shared_task
from sqlalchemy.future import select
from structlog import get_logger

from app.db.models import LeadV2, LeadStatus, ZohoSlot, SlotStatus
from app.services.availability_v2 import get_slots_for_week
from app.services.email_generator import (
    generate_outreach_body,
    score_lead,
    detect_week_preference,
    extract_slot_from_reply,
    analyze_reply_intent,
)
from app.services.sheets import GoogleSheetsService
from app.services.zoho import ZohoBookingsService
from app.services.email_service import (
    make_book_url,
    make_week_url,
    send_week_selection_email,
    send_v2_slots_email,
    send_v2_reminder_email,
    send_booking_confirmation_to_lead,
    send_organizer_booking_notification,
)
from app.workers.runtime import run_async

logger = get_logger(__name__)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _extract_min_hour(body: str) -> int | None:
    """Parse 'after 12', 'afternoon', '2pm onwards' etc. from a reply body.

    Returns the minimum hour (24h int) the lead wants, or None if no preference.
    """
    b = body.lower()
    # "after 12", "after 2pm", "from 3", "post 14", "starting from 2"
    m = re.search(
        r'\b(?:after|post|from|starting\s+from?)\s+(\d{1,2})(?:\s*(?:pm|am))?\b', b
    )
    if m:
        h = int(m.group(1))
        ctx = b[m.start(): m.start() + 18]
        if "pm" in ctx and h != 12:
            h += 12
        elif h <= 6:  # "after 2" without am/pm → assume afternoon
            h += 12
        return min(max(h, 9), 19)

    if any(w in b for w in ["afternoon", "post lunch", "post-lunch", "after lunch"]):
        return 13
    if any(w in b for w in ["evening", "evenings"]):
        return 17
    # Any bare "pm" reference (not "9am" context) → noon minimum
    if re.search(r'\bpm\b', b) and not re.search(r'\d\s*am\b', b):
        return 12
    return None


def _match_slot_simple(body: str, slot_strings: list[str]) -> str | None:
    """Pick the lead's chosen slot ONLY when they explicitly name it.

    Never auto-books based on vague words like 'ok', 'sure', 'great'.
    Those go to the AI intent layer which may show slots again or confirm.
    Only books when:
      1. Lead mentions a slot by ordinal (first, 1st, option 2...)
      2. Lead mentions the exact day + time of one of the offered slots
    """
    body_lower = body.lower()

    # Ordinal selection — "the first one", "option 2", "slot 3"
    ordinals = [
        (["first one", "1st one", "option 1", "number 1", "slot 1", "first slot", "1st slot"], 0),
        (["second one", "2nd one", "option 2", "number 2", "slot 2", "second slot", "2nd slot"], 1),
        (["third one", "3rd one", "option 3", "number 3", "slot 3", "third slot", "3rd slot"], 2),
        (["fourth one", "4th one", "option 4", "number 4", "slot 4", "fourth slot", "4th slot"], 3),
        (["fifth one", "5th one", "option 5", "number 5", "slot 5", "fifth slot", "5th slot"], 4),
        (["sixth one", "6th one", "option 6", "number 6", "slot 6", "sixth slot", "6th slot"], 5),
    ]
    for keywords, idx in ordinals:
        if idx < len(slot_strings) and any(kw in body_lower for kw in keywords):
            return slot_strings[idx]

    # Exact slot match — day name AND time both present in reply
    for slot_str in slot_strings:
        parts = slot_str.lower().replace(",", "").split()
        day_name = parts[0] if parts else ""
        # time_part e.g. "09:00 am"
        time_part = " ".join(parts[-2:]) if len(parts) >= 2 else ""
        if day_name and time_part and day_name in body_lower and time_part in body_lower:
            return slot_str

    return None


def _load_slot_infos(lead) -> tuple[list[dict], list[str]]:
    """Parse offered_slots_json — handles both old (list of str) and new (list of dict) formats."""
    if not lead.offered_slots_json:
        return [], []
    raw = json.loads(lead.offered_slots_json)
    if not raw:
        return [], []
    if isinstance(raw[0], dict):
        return raw, [s["display"] for s in raw]
    wrapped = [{"display": s, "date_str": None, "time_str": None, "iso": None} for s in raw]
    return wrapped, raw


# ---------------------------------------------------------------------------
# Sync Google Sheet → leads_v2
# ---------------------------------------------------------------------------

async def _sync_google_sheet(db):
    sheets_service = GoogleSheetsService()
    records = sheets_service.fetch_new_leads()
    if not records:
        logger.warning("sheet_sync_no_records")
        return "No records found or sheets not configured."

    logger.info("sheet_sync_fetched", total_rows=len(records), sample_keys=list(records[0].keys()) if records else [])

    added = skipped_existing = skipped_missing = 0
    for row in records:
        email = (
            row.get("email") or row.get("Email")
            or row.get("Business Email") or row.get("business_email") or ""
        ).strip()
        contact_name = (
            row.get("contact person") or row.get("Contact person")
            or row.get("contact_person") or row.get("Contact Name")
            or row.get("Person Name") or row.get("contact_name")
            or row.get("Full Name") or row.get("name") or ""
        ).strip() or None
        business_name = (
            row.get("name of the company") or row.get("Name of the Company")
            or row.get("Name of Company") or row.get("name_of_company")
            or row.get("Company") or row.get("business_name")
            or row.get("Business Name") or ""
        ).strip() or contact_name
        designation = (row.get("Designation") or row.get("designation") or "").strip()
        raw_summary = (row.get("Summary") or row.get("summary") or "").strip()
        summary = raw_summary
        if designation and designation not in summary:
            summary = f"{designation}. {summary}".strip(" .")
        if not summary:
            summary = None
        if not email or not re.match(r"^[^@]+@[^@]+\.[^@]+$", email):
            logger.warning("sheet_row_invalid_email", email=email)
            skipped_missing += 1
            continue
        if not business_name:
            logger.warning("sheet_row_missing_fields", row_keys=list(row.keys()), email=email, business=business_name)
            skipped_missing += 1
            continue
        existing = (
            await db.execute(select(LeadV2).where(LeadV2.email == email))
        ).scalar_one_or_none()
        if not existing:
            db.add(LeadV2(
                business_name=business_name,
                contact_name=contact_name,
                email=email,
                summary=summary,
                status=LeadStatus.NEW,
            ))
            logger.info("added_lead_from_sheet", email=email, contact=contact_name, summary=summary)
            added += 1
        else:
            skipped_existing += 1

    logger.info("sheet_sync_done", added=added, already_in_db=skipped_existing, missing_fields=skipped_missing)
    return f"Sync complete: +{added} new, {skipped_existing} existing, {skipped_missing} missing fields"


@shared_task
def sync_google_sheet():
    logger.info("running_sync_google_sheet")
    return run_async(_sync_google_sheet)


# ---------------------------------------------------------------------------
# Sync leads.csv → leads_v2
# ---------------------------------------------------------------------------

CSV_PATH = os.environ.get("LEADS_CSV_PATH", "/app/leads.csv")


async def _sync_csv_leads(db):
    if not os.path.exists(CSV_PATH):
        logger.warning("leads_csv_not_found", path=CSV_PATH)
        return "leads.csv not found"

    added = skipped_existing = skipped_missing = 0
    with open(CSV_PATH, newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        rows = list(reader)

    logger.info("csv_sync_fetched", total_rows=len(rows), path=CSV_PATH)

    for row in rows:
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
        ).strip() or contact_name
        designation = (row.get("designation") or row.get("Designation") or "").strip()
        raw_summary = (row.get("summary") or row.get("Summary") or "").strip()
        summary = raw_summary
        if designation and designation not in summary:
            summary = f"{designation}. {summary}".strip(" .")
        if not summary:
            summary = None

        # Basic email validation: must have @ and a dot after the @
        if not email or not re.match(r"^[^@]+@[^@]+\.[^@]+$", email):
            logger.warning("csv_row_invalid_email", email=email)
            skipped_missing += 1
            continue

        if not business_name:
            logger.warning("csv_row_missing_fields", email=email, business=business_name)
            skipped_missing += 1
            continue

        existing = (
            await db.execute(select(LeadV2).where(LeadV2.email == email))
        ).scalar_one_or_none()
        if not existing:
            db.add(LeadV2(
                business_name=business_name,
                contact_name=contact_name,
                email=email,
                summary=summary,
                status=LeadStatus.NEW,
            ))
            logger.info("added_lead_from_csv", email=email, contact=contact_name)
            added += 1
        else:
            skipped_existing += 1

    logger.info("csv_sync_done", added=added, already_in_db=skipped_existing, missing_fields=skipped_missing)
    return f"CSV sync: +{added} new, {skipped_existing} existing, {skipped_missing} missing fields"


@shared_task
def sync_csv_leads():
    logger.info("running_sync_csv_leads")
    return run_async(_sync_csv_leads)


# ---------------------------------------------------------------------------
# Sync Zoho slots → zoho_slots (reference table, not used for availability)
# ---------------------------------------------------------------------------

async def _sync_zoho_slots(db):
    from datetime import datetime as _dt
    zoho_service = ZohoBookingsService()
    today = _dt.now().strftime("%d-%b-%Y")
    slots_data = zoho_service.fetch_available_slots(today)
    if not slots_data:
        return "No slots found or zoho not configured."

    for slot_item in slots_data:
        if isinstance(slot_item, dict):
            raw_id = str(slot_item.get("id") or slot_item.get("time", slot_item))
            zoho_id = f"z_slot_{raw_id}"
            try:
                date_val = slot_item.get("date", "")
                time_val = slot_item.get("time", "")
                slot_time = _dt.strptime(
                    f"{date_val} {time_val}", "%Y-%m-%d %H:%M"
                ).replace(tzinfo=timezone.utc)
            except (ValueError, TypeError):
                slot_time = _dt.now(timezone.utc)
        else:
            slot_str = str(slot_item)
            zoho_id = f"z_slot_{slot_str}"
            try:
                slot_time = _dt.strptime(slot_str, "%Y-%m-%d %H:%M:%S").replace(
                    tzinfo=timezone.utc
                )
            except ValueError:
                slot_time = _dt.now(timezone.utc)

        existing = (
            await db.execute(select(ZohoSlot).where(ZohoSlot.zoho_slot_id == zoho_id))
        ).scalar_one_or_none()
        if not existing:
            db.add(ZohoSlot(
                zoho_slot_id=zoho_id, slot_time=slot_time, status=SlotStatus.AVAILABLE
            ))
    return "Slots synced"


@shared_task
def sync_zoho_slots():
    logger.info("running_sync_zoho_slots")
    return run_async(_sync_zoho_slots)


# ---------------------------------------------------------------------------
# Process new leads → send Stage 1 week-selection email
# ---------------------------------------------------------------------------

async def _process_new_leads(db):
    leads = (
        await db.execute(select(LeadV2).where(LeadV2.status == LeadStatus.NEW))
    ).scalars().all()
    if not leads:
        return "No new leads."

    sent = 0
    skipped = 0
    for lead in leads:
        # ── Lead scoring: skip low-quality leads (score < 6 out of 10) ──────────
        lead_score = score_lead(
            contact_name=lead.contact_name,
            business_name=lead.business_name,
            summary=lead.summary,
        )
        if lead_score < 6:
            logger.info(
                "lead_skipped_low_score",
                email=lead.email,
                score=lead_score,
                contact=lead.contact_name,
            )
            skipped += 1
            continue

        logger.info("lead_qualified", email=lead.email, score=lead_score)

        # Mark SENT immediately and flush to prevent double-processing by concurrent task
        lead.status = LeadStatus.SENT
        lead.sent_at = datetime.now(timezone.utc)
        lead.follow_up_count = 0
        lead.reminder_sent_at = None
        await db.flush()

        display_name = lead.contact_name or lead.business_name
        body_text = generate_outreach_body(
            business_name=lead.business_name,
            recipient_name=display_name,
            summary=lead.summary,
            contact_name=lead.contact_name,
        )
        this_url = make_week_url(str(lead.id), "this")
        next_url = make_week_url(str(lead.id), "next")
        success = send_week_selection_email(
            lead.email, lead.business_name,
            this_week_url=this_url,
            next_week_url=next_url,
            body_text=body_text,
            contact_name=lead.contact_name,
        )
        if success:
            logger.info("week_selection_email_sent", email=lead.email, score=lead_score)
            sent += 1
        else:
            # Revert status so we retry on the next cycle
            lead.status = LeadStatus.NEW
            lead.sent_at = None
            logger.warning("week_selection_email_failed_reverted", email=lead.email)

    return f"Stage 1 emails sent to {sent}/{len(leads)} leads. Skipped (low score): {skipped}."


@shared_task
def process_new_leads():
    logger.info("running_process_new_leads")
    return run_async(_process_new_leads)


# ---------------------------------------------------------------------------
# Follow-up reminders — Stage 1 leads that haven't clicked yet
# First reminder: 35 min after initial email
# Second reminder: 25 min after first (≈60 min total), max 2 follow-ups
# ---------------------------------------------------------------------------

async def _send_v2_reminders(db) -> str:
    now = datetime.now(timezone.utc)
    first_cutoff = now - timedelta(minutes=35)
    second_cutoff = now - timedelta(minutes=25)

    result = await db.execute(
        select(LeadV2).where(
            LeadV2.status == LeadStatus.SENT,
            LeadV2.offered_slots_json.is_(None),  # Stage 1 only (no week chosen yet)
            LeadV2.follow_up_count < 2,
        )
    )
    leads = result.scalars().all()
    if not leads:
        return "No leads needing reminders."

    sent_count = 0
    for lead in leads:
        if not lead.sent_at:
            continue

        if lead.follow_up_count == 0 and lead.sent_at <= first_cutoff:
            this_url = make_week_url(str(lead.id), "this")
            next_url = make_week_url(str(lead.id), "next")
            ok = send_v2_reminder_email(
                lead.email, lead.business_name, this_url, next_url,
                follow_up_count=1,
                contact_name=lead.contact_name,
            )
            if ok:
                lead.follow_up_count = 1
                lead.reminder_sent_at = now
                sent_count += 1
                logger.info("reminder_1_sent", email=lead.email)

        elif (
            lead.follow_up_count == 1
            and lead.reminder_sent_at is not None
            and lead.reminder_sent_at <= second_cutoff
        ):
            this_url = make_week_url(str(lead.id), "this")
            next_url = make_week_url(str(lead.id), "next")
            ok = send_v2_reminder_email(
                lead.email, lead.business_name, this_url, next_url,
                follow_up_count=2,
                contact_name=lead.contact_name,
            )
            if ok:
                lead.follow_up_count = 2
                sent_count += 1
                logger.info("reminder_2_sent", email=lead.email)

    return f"Reminders sent: {sent_count}"


@shared_task
def send_v2_reminders():
    logger.info("running_send_v2_reminders")
    return run_async(_send_v2_reminders)


# ---------------------------------------------------------------------------
# Booking — create Zoho booking, update lead, sync ZohoSlot, send emails
# ---------------------------------------------------------------------------

async def _do_booking(db, lead, date_str: str, time_str: str, display_str: str) -> str:
    from zoneinfo import ZoneInfo
    from app.services.availability_v2 import _get_booked_slots, _get_held_slots
    IST = ZoneInfo("Asia/Kolkata")

    async def _send_alternatives(reason: str) -> str:
        logger.warning(reason, email=lead.email, slot=display_str)
        from app.services.availability_v2 import get_available_slots
        # Try this week → next week → broader 14-day search
        alt_slots = await get_slots_for_week(db, "this", 6)
        if not alt_slots:
            alt_slots = await get_slots_for_week(db, "next", 6)
        if not alt_slots:
            alt_slots = await get_available_slots(db, n=6, lookahead_days=14)
        if alt_slots:
            slot_strings = [s["display"] for s in alt_slots]
            book_urls = [make_book_url(str(lead.id), i) for i in range(len(alt_slots))]
            lead.offered_slots_json = json.dumps(alt_slots)
            lead.replied_at = datetime.now(timezone.utc)
            send_v2_slots_email(
                lead.email, lead.business_name, slot_strings,
                book_urls=book_urls,
                body_text=None,
                contact_name=lead.contact_name,
            )
        else:
            _send_graceful_no_slots(lead)
            lead.replied_at = datetime.now(timezone.utc)
        return f"{reason} — sent {len(alt_slots) if alt_slots else 0} alternatives"

    # Build slot_dt_ist once — used by both guard checks below
    slot_dt_ist = None
    try:
        hour = int(time_str.split(":")[0])
        slot_date = datetime.strptime(date_str, "%d-%b-%Y").date()
        slot_dt_ist = datetime(slot_date.year, slot_date.month, slot_date.day, hour, 0, tzinfo=IST)
    except Exception as exc:
        logger.warning("booking_parse_slot_dt_error", error=str(exc))

    # ── Check 1: Hard DB exclusion (already confirmed-booked in our DB) ────────
    try:
        booked_iso, booked_display = await _get_booked_slots(db)
        if slot_dt_ist and (slot_dt_ist.isoformat() in booked_iso or display_str in booked_display):
            return await _send_alternatives("booking_rejected_hard_booked_in_db")
    except Exception as exc:
        logger.warning("booking_precheck_error", error=str(exc))

    # ── Check 2: Soft DB exclusion (held by another lead in last 2 h) ──────────
    try:
        held = await _get_held_slots(db, exclude_lead_id=lead.id)
        if slot_dt_ist and slot_dt_ist.isoformat() in held:
            return await _send_alternatives("booking_rejected_held_by_another_lead")
    except Exception as exc:
        logger.warning("booking_held_check_error", error=str(exc))

    # ── Check 3: Zoho real-time booking (source of truth) ──────────────────────
    zoho = ZohoBookingsService()
    booking_id, meeting_link = await asyncio.to_thread(
        zoho.create_booking,
        name=lead.contact_name or lead.business_name,
        email=lead.email,
        date_str=date_str,
        time_str=time_str,
    )

    if booking_id is None:
        return await _send_alternatives("zoho_booking_rejected_slot_taken")

    lead.status = LeadStatus.BOOKED
    lead.replied_at = datetime.now(timezone.utc)
    lead.booked_at = datetime.now(timezone.utc)
    lead.selected_slot = display_str
    lead.booking_id = booking_id
    lead.zoho_meeting_link = meeting_link

    # Sync ZohoSlot row in DB
    try:
        hour = int(time_str.split(":")[0])
        slot_date = datetime.strptime(date_str, "%d-%b-%Y").date()
        slot_dt_ist = datetime(slot_date.year, slot_date.month, slot_date.day, hour, 0, tzinfo=IST)
        slot_dt_utc = slot_dt_ist.astimezone(timezone.utc)
        slot_row = (await db.execute(
            select(ZohoSlot).where(ZohoSlot.slot_time == slot_dt_utc)
        )).scalar_one_or_none()
        if slot_row:
            slot_row.status = SlotStatus.BOOKED
            slot_row.booked_email = lead.email
    except Exception as exc:
        logger.warning("zoho_slot_db_sync_failed", error=str(exc))

    send_booking_confirmation_to_lead(
        lead.email, lead.business_name, display_str,
        meeting_link=meeting_link, contact_name=lead.contact_name,
    )
    send_organizer_booking_notification(
        lead.business_name, lead.email, display_str, meeting_link=meeting_link
    )
    logger.info("booking_complete", email=lead.email, booking_id=booking_id)
    return f"Booked (ID: {booking_id})"


async def _try_specific_date_reply(db, lead, body: str) -> str | None:
    """Extract specific date/time from reply text. Returns result string or None."""
    from datetime import date as _date
    from zoneinfo import ZoneInfo

    IST = ZoneInfo("Asia/Kolkata")
    SLOT_START, SLOT_END = 9, 19

    today_str = datetime.now(timezone.utc).strftime("%A, %d %B %Y")
    extracted = extract_slot_from_reply(body, today_str)
    if not extracted:
        return None

    try:
        parsed_date = _date.fromisoformat(extracted["date"])
        time_str = extracted.get("time", "")
        if not time_str:
            return None
        requested_hour = int(time_str.split(":")[0])
        date_str = parsed_date.strftime("%d-%b-%Y")
    except (KeyError, ValueError, TypeError):
        return None

    zoho = ZohoBookingsService()
    available_items = await asyncio.to_thread(zoho.fetch_available_slots, date_str)
    if not available_items:
        return None

    from app.services.availability_v2 import _get_booked_slots, _parse_zoho_slot_hour
    booked_iso, booked_display = await _get_booked_slots(db)

    available_hours: set[int] = set()
    for item in available_items:
        try:
            h = _parse_zoho_slot_hour(item)  # handles "05:00 PM" → 17 correctly
            if h is None:
                continue
            slot_dt = datetime(parsed_date.year, parsed_date.month, parsed_date.day, h, 0, tzinfo=IST)
            if slot_dt.isoformat() in booked_iso or slot_dt.strftime("%A, %b %d at %I:%M %p") in booked_display:
                continue
            available_hours.add(h)
        except Exception:
            pass

    if requested_hour in available_hours:
        display = datetime(
            parsed_date.year, parsed_date.month, parsed_date.day,
            requested_hour, 0, tzinfo=IST,
        ).strftime("%A, %b %d at %I:%M %p")
        logger.info("specific_date_exact_match", email=lead.email, slot=display)
        return await _do_booking(db, lead, date_str, time_str, display)

    # Requested time taken — send available alternatives for that day
    alt_slots = []
    for hour in sorted(available_hours):
        if SLOT_START <= hour <= SLOT_END:
            slot_dt = datetime(parsed_date.year, parsed_date.month, parsed_date.day, hour, 0, tzinfo=IST)
            alt_slots.append({
                "display": slot_dt.strftime("%A, %b %d at %I:%M %p"),
                "date_str": date_str,
                "time_str": f"{hour:02d}:00",
                "iso": slot_dt.isoformat(),
            })

    if not alt_slots:
        return None

    slot_strings = [s["display"] for s in alt_slots]
    book_urls = [make_book_url(str(lead.id), i) for i in range(len(alt_slots))]
    lead.offered_slots_json = json.dumps(alt_slots)
    lead.replied_at = datetime.now(timezone.utc)

    send_v2_slots_email(
        lead.email, lead.business_name, slot_strings,
        book_urls=book_urls,
        body_text=None,
        contact_name=lead.contact_name,
    )
    logger.info("specific_date_alternatives_sent", email=lead.email, date=date_str, count=len(alt_slots))
    return f"Sent {len(alt_slots)} slots for {date_str}"


# ---------------------------------------------------------------------------
# Process reply → stage routing → booking
# ---------------------------------------------------------------------------

def _send_graceful_no_slots(lead) -> None:
    """Send a polite 'when are you available?' email when no slots can be offered right now."""
    from app.services.email_service import _send_html_email, _signature

    greeting_name = (lead.contact_name or lead.business_name or "").split()[0].capitalize()
    subject = f"Re: Partnership Opportunity — Jane Aerospace"

    body_lines = [
        f"Hi {greeting_name},",
        "",
        "Thank you so much for your response — I truly appreciate you taking the time to get back to me.",
        "I wanted to reach out personally to let you know that our current time slots are being finalised and I want to make sure I find a time that works perfectly for you rather than offering something that may not be convenient.",
        "Could you please let me know your general availability? Even a rough idea — such as which days of the week work best for you, or whether mornings or afternoons are more suitable — would be very helpful.",
        "I will then personally check our schedule and come back to you with a confirmed time straight away. I want to make sure this conversation is as convenient as possible for you.",
        "Please feel free to reply directly to this email with your preferred times and I will take care of the rest.",
        "Looking forward to hearing from you and to a great conversation.",
    ]

    paras_html = "".join(
        f'<p style="margin:0 0 18px 0;font-family:Arial,sans-serif;font-size:15px;color:#111111;line-height:1.75;">{p}</p>'
        for p in body_lines if p != ""
    )
    html = f"""<!DOCTYPE html>
<html>
<head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"></head>
<body style="margin:0;padding:0;background:#ffffff;">
  <div style="max-width:600px;margin:40px auto;padding:0 24px 60px;">
    {paras_html}
    {_signature()}
  </div>
</body>
</html>"""

    _send_html_email(lead.email, subject, html)
    logger.info("graceful_no_slots_email_sent", email=lead.email)


async def _process_reply_v2(db, reply: dict) -> str:
    from zoneinfo import ZoneInfo
    IST = ZoneInfo("Asia/Kolkata")

    from_addr = reply.get("from_addr")
    body = reply.get("body")
    if not from_addr or not body:
        return "Missing email or body"

    # Look for SENT or REPLIED lead — prefer the one with offered_slots_json (Stage 2)
    result = (
        await db.execute(
            select(LeadV2).where(
                LeadV2.email == from_addr,
                LeadV2.status.in_([LeadStatus.SENT, LeadStatus.REPLIED]),
            ).order_by(LeadV2.created_at.desc())
        )
    ).scalars().all()
    if result:
        # Prefer whichever has offered_slots_json; otherwise take most recent
        lead = next((r for r in result if r.offered_slots_json), result[0])
    else:
        lead = None

    # If BOOKED and the meeting has already passed, reset to SENT for rescheduling
    if not lead:
        booked_lead = (
            await db.execute(
                select(LeadV2).where(
                    LeadV2.email == from_addr,
                    LeadV2.status == LeadStatus.BOOKED,
                ).order_by(LeadV2.created_at.desc())
            )
        ).scalars().first()
        if booked_lead:
            slot_passed = False
            if booked_lead.selected_slot:
                try:
                    import dateutil.parser
                    slot_dt = dateutil.parser.parse(booked_lead.selected_slot)
                    if slot_dt.tzinfo is None:
                        slot_dt = slot_dt.replace(tzinfo=IST)
                    slot_passed = slot_dt < datetime.now(IST)
                except Exception:
                    slot_passed = (
                        booked_lead.booked_at is not None
                        and (datetime.now(timezone.utc) - booked_lead.booked_at).total_seconds() > 86400
                    )
            if slot_passed:
                booked_lead.status = LeadStatus.SENT
                booked_lead.offered_slots_json = None
                booked_lead.follow_up_count = 0
                booked_lead.reminder_sent_at = None
                lead = booked_lead
                logger.info("booked_lead_reset_for_reschedule", email=from_addr)

    if not lead:
        logger.info("no_matching_sent_lead", email=from_addr)
        return "No matching lead"

    # Priority 1: If Stage 2 (has offered slots), try matching them simple-first
    selected_display = None
    if lead.offered_slots_json:
        # If slots were already sent very recently (< 5 min), don't send again
        if lead.replied_at:
            seconds_since = (datetime.now(timezone.utc) - lead.replied_at.replace(tzinfo=timezone.utc) if lead.replied_at.tzinfo is None else datetime.now(timezone.utc) - lead.replied_at).total_seconds()
            if seconds_since < 300:
                logger.info("slots_sent_recently_skipping_reply", email=from_addr, seconds_ago=int(seconds_since))
                return "Slots sent recently — skipping to avoid duplicate"

        slot_infos, slot_strings = _load_slot_infos(lead)
        selected_display = _match_slot_simple(body, slot_strings)
        if selected_display:
            logger.info("slot_matched_simple", email=from_addr, slot=selected_display)
            matched_info = next((s for s in slot_infos if s["display"] == selected_display), None)
            if matched_info and matched_info.get("date_str") and matched_info.get("time_str"):
                date_str = matched_info["date_str"]
                time_str = matched_info["time_str"]
            else:
                try:
                    import dateutil.parser
                    parsed = dateutil.parser.parse(selected_display)
                    date_str = parsed.strftime("%d-%b-%Y")
                    time_str = parsed.strftime("%H:%M")
                except Exception:
                    date_str = "N/A"
                    time_str = "N/A"
            return await _do_booking(db, lead, date_str, time_str, selected_display)

    # Priority 2: Use unified AI analysis for intent classification and extraction
    today_str = datetime.now(timezone.utc).strftime("%A, %d %B %Y")
    analysis = analyze_reply_intent(body, today_str)
    logger.info("reply_analysis_result", email=from_addr, analysis=analysis, reply_body=body)

    if analysis["intent"] == "decline":
        # Don't give up — send a warm "when works for you?" reply
        from app.services.email_service import _send_html_email, _signature
        greeting_name = (lead.contact_name or lead.business_name or "").split()[0].capitalize()
        subject = "Re: Partnership Opportunity — Jane Aerospace"
        body_lines = [
            f"Hi {greeting_name},",
            "",
            "Thank you for letting me know — I completely understand, schedules can get very busy and I appreciate you taking a moment to respond.",
            "I would not want to miss the opportunity to connect with you entirely. Would it be possible for you to let me know when a better time might be? Even a rough window — whether it is a particular week, day of the week, or time of day — would be very helpful for me to plan around.",
            "There is absolutely no pressure at all. If now is simply not the right time, I am more than happy to follow up at a later date that suits you better.",
            "Please feel free to reply to this email whenever it is convenient and I will make sure to find a slot that works perfectly for you.",
            "Thank you again for your time and I hope to speak with you soon.",
        ]
        paras_html = "".join(
            f'<p style="margin:0 0 18px 0;font-family:Arial,sans-serif;font-size:15px;color:#111111;line-height:1.75;">{p}</p>'
            for p in body_lines if p != ""
        )
        html = f"""<!DOCTYPE html>
<html>
<head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"></head>
<body style="margin:0;padding:0;background:#ffffff;">
  <div style="max-width:600px;margin:40px auto;padding:0 24px 60px;">
    {paras_html}
    {_signature()}
  </div>
</body>
</html>"""
        _send_html_email(lead.email, subject, html)
        lead.status = LeadStatus.REPLIED
        lead.replied_at = datetime.now(timezone.utc)
        logger.info("reply_handled_decline_sent_when_available_email", email=from_addr)
        return "Lead declined — sent 'when are you available?' reply"

    if analysis["intent"] == "book" and analysis["date"] and analysis["time"]:
        date_str = datetime.strptime(analysis["date"], "%Y-%m-%d").strftime("%d-%b-%Y")
        time_str = analysis["time"]

        # Parse date and time into IST display string
        try:
            parsed_dt = datetime.strptime(f"{analysis['date']} {analysis['time']}", "%Y-%m-%d %H:%M").replace(tzinfo=IST)
            display_str = parsed_dt.strftime("%A, %b %d at %I:%M %p")
        except Exception:
            display_str = f"{date_str} at {time_str}"
            try:
                parsed_dt = datetime.strptime(f"{analysis['date']} 09:00", "%Y-%m-%d %H:%M").replace(tzinfo=IST)
            except Exception:
                parsed_dt = datetime.now(IST)

        # Check if Zoho has availability for this slot
        zoho = ZohoBookingsService()
        available_items = await asyncio.to_thread(zoho.fetch_available_slots, date_str)

        # Filter out booked slots
        from app.services.availability_v2 import _get_booked_slots, _parse_zoho_slot_hour
        booked_iso, booked_display = await _get_booked_slots(db)

        available_hours = set()
        if available_items:
            for item in available_items:
                try:
                    h = _parse_zoho_slot_hour(item)  # handles "05:00 PM" → 17 correctly
                    if h is None:
                        continue
                    slot_dt = datetime(parsed_dt.year, parsed_dt.month, parsed_dt.day, h, 0, tzinfo=IST)
                    if slot_dt.isoformat() in booked_iso or slot_dt.strftime("%A, %b %d at %I:%M %p") in booked_display:
                        continue
                    available_hours.add(h)
                except Exception:
                    pass
        elif not zoho.service_id:
            # If Zoho is mock environment, assume business hours are available
            available_hours = set(range(9, 20))

        requested_hour = parsed_dt.hour
        if requested_hour in available_hours:
            logger.info("specific_date_exact_match_via_analysis", email=lead.email, slot=display_str)
            return await _do_booking(db, lead, date_str, time_str, display_str)

        # Requested time is taken — send available alternatives for that day
        alt_slots = []
        for hour in sorted(available_hours):
            if 9 <= hour <= 19:
                slot_dt = datetime(parsed_dt.year, parsed_dt.month, parsed_dt.day, hour, 0, tzinfo=IST)
                alt_slots.append({
                    "display": slot_dt.strftime("%A, %b %d at %I:%M %p"),
                    "date_str": date_str,
                    "time_str": f"{hour:02d}:00",
                    "iso": slot_dt.isoformat(),
                })

        # Apply time-of-day filter (e.g. "June 11 after 13" → only show 13:00+)
        min_hour = _extract_min_hour(body)
        if min_hour is not None and alt_slots:
            alt_slots = [s for s in alt_slots if int(s["time_str"].split(":")[0]) >= min_hour]
            logger.info("date_alternatives_filtered_by_min_hour", email=lead.email, min_hour=min_hour, remaining=len(alt_slots))

        if alt_slots:
            slot_strings = [s["display"] for s in alt_slots]
            book_urls = [make_book_url(str(lead.id), i) for i in range(len(alt_slots))]
            lead.offered_slots_json = json.dumps(alt_slots)
            lead.replied_at = datetime.now(timezone.utc)
            send_v2_slots_email(
                lead.email, lead.business_name, slot_strings,
                book_urls=book_urls,
                body_text=None,
                contact_name=lead.contact_name,
            )
            logger.info("specific_date_alternatives_sent", email=lead.email, date=date_str, count=len(alt_slots))
            return f"Sent {len(alt_slots)} alternatives for {date_str}"

        # If no alternatives for that day, fall back to listing slots starting from that date
        analysis["intent"] = "list_slots"
        analysis["after_date"] = analysis["date"]

    if analysis["intent"] == "list_slots":
        week = analysis.get("week")
        after_date_str = analysis.get("after_date")
        specific_date_str = analysis.get("specific_date")

        after_date = None
        if after_date_str:
            try:
                after_date = datetime.strptime(after_date_str, "%Y-%m-%d").date()
            except ValueError:
                pass

        from app.services.availability_v2 import (
            get_slots_for_week, get_available_slots,
            _parse_zoho_slot_hour, _apply_db_guard, _build_slot_info, IST as _IST_AV,
        )

        slot_infos = []

        # ── Specific day request ("I'm free Monday", "Monday works") ─────────────
        if specific_date_str:
            try:
                from datetime import date as _date_type
                specific_date = datetime.strptime(specific_date_str, "%Y-%m-%d").date()
                date_fmt = specific_date.strftime("%d-%b-%Y")
                logger.info("fetching_slots_for_specific_day", email=lead.email, date=date_fmt)

                zoho = ZohoBookingsService()
                zoho_items = await asyncio.to_thread(zoho.fetch_available_slots, date_fmt)
                if zoho_items:
                    now_ist_ts = datetime.now(_IST_AV)
                    raw_day_slots = []
                    for item in zoho_items:
                        h = _parse_zoho_slot_hour(item)
                        if h is None or h < 9 or h > 18:
                            continue
                        slot_dt = datetime(specific_date.year, specific_date.month, specific_date.day, h, 0, tzinfo=_IST_AV)
                        if slot_dt <= now_ist_ts:
                            continue
                        raw_day_slots.append(_build_slot_info(specific_date, h))

                    # Apply DB guard (removes booked + held)
                    if raw_day_slots:
                        slot_infos = await _apply_db_guard(db, raw_day_slots)

                if not slot_infos:
                    # No Zoho slots that specific day — try the next few days starting from it
                    logger.info("no_slots_for_specific_day_falling_back", email=lead.email, date=date_fmt)
                    slot_infos = await get_available_slots(db, n=6, start_date=specific_date)

                if not slot_infos:
                    # Still nothing — fall back to this week / next week pool
                    logger.info("specific_day_fallback_to_week", email=lead.email)
                    slot_infos = await get_slots_for_week(db, "this", 6)
                if not slot_infos:
                    slot_infos = await get_slots_for_week(db, "next", 6)

            except Exception as exc:
                logger.warning("specific_day_fetch_error", email=lead.email, error=str(exc))
                slot_infos = []

        elif after_date:
            logger.info("fetching_slots_after_date", email=lead.email, after_date=after_date)
            slot_infos = await get_available_slots(db, n=6, start_date=after_date)
        else:
            if not week:
                week = "next"
            logger.info("fetching_slots_for_week", email=lead.email, week=week)
            slot_infos = await get_slots_for_week(db, week, n=6)
            if not slot_infos:
                other = "next" if week == "this" else "this"
                slot_infos = await get_slots_for_week(db, other, n=6)

        if not slot_infos:
            logger.warning("no_slots_available_for_filter", email=lead.email)
            _send_graceful_no_slots(lead)
            lead.replied_at = datetime.now(timezone.utc)
            return "No slots — sent graceful week-selection email"

        # Apply time-of-day preference (e.g. "after 12", "afternoon", "after 13")
        min_hour = _extract_min_hour(body)
        if min_hour is not None:
            filtered = [s for s in slot_infos if int(s["time_str"].split(":")[0]) >= min_hour]
            logger.info("slots_filtered_by_min_hour", email=lead.email, min_hour=min_hour, remaining=len(filtered))
            if filtered:
                slot_infos = filtered
            else:
                # No slots after the time filter — try a broader date range without time limit
                logger.warning("no_slots_after_min_hour_filter_broadening", email=lead.email, min_hour=min_hour)
                from app.services.availability_v2 import get_available_slots
                broad = await get_available_slots(db, n=6, lookahead_days=14)
                broad_filtered = [s for s in broad if int(s["time_str"].split(":")[0]) >= min_hour]
                if broad_filtered:
                    slot_infos = broad_filtered
                    logger.info("broadened_search_found_slots", email=lead.email, count=len(slot_infos))
                else:
                    _send_graceful_no_slots(lead)
                    lead.replied_at = datetime.now(timezone.utc)
                    return "No slots after time filter — sent graceful email"

        slot_strings = [s["display"] for s in slot_infos]
        book_urls = [make_book_url(str(lead.id), i) for i in range(len(slot_infos))]

        body_text_for_email = (
            f"Here are the available times after {min_hour:02d}:00 — click any to confirm instantly:"
            if min_hour else None
        )
        send_v2_slots_email(
            lead.email, lead.business_name, slot_strings,
            book_urls=book_urls,
            body_text=body_text_for_email,
            contact_name=lead.contact_name,
        )

        lead.offered_slots_json = json.dumps(slot_infos)
        lead.replied_at = datetime.now(timezone.utc)
        # Bust cache so next lead gets a fresh Zoho fetch
        from app.services.availability_v2 import invalidate_week_cache
        invalidate_week_cache()
        logger.info("slots_sent_for_filter", email=lead.email, count=len(slot_infos), min_hour=min_hour)
        return f"Sent {len(slot_infos)} slots"

    # Default/Unclear positive intent fallback
    logger.info("reply_unclear_sending_general_slots", email=from_addr)
    from app.services.availability_v2 import get_slots_for_week, invalidate_week_cache
    week = "this"
    slot_infos = await get_slots_for_week(db, week, n=6)
    if not slot_infos:
        week = "next"
        slot_infos = await get_slots_for_week(db, week, n=6)

    if not slot_infos:
        logger.warning("no_general_slots_available", email=lead.email)
        _send_graceful_no_slots(lead)
        lead.replied_at = datetime.now(timezone.utc)
        return "No slots — sent graceful week-selection email"

    # Apply time-of-day preference (e.g. "after 12", "afternoon", "PM", "after 13")
    min_hour = _extract_min_hour(body)
    if min_hour is not None:
        filtered = [s for s in slot_infos if int(s["time_str"].split(":")[0]) >= min_hour]
        logger.info("slots_filtered_by_min_hour", email=lead.email, min_hour=min_hour, remaining=len(filtered))
        if filtered:
            slot_infos = filtered
        else:
            _send_graceful_no_slots(lead)
            lead.replied_at = datetime.now(timezone.utc)
            return "No slots after time filter — sent graceful email"

    slot_strings = [s["display"] for s in slot_infos]
    book_urls = [make_book_url(str(lead.id), i) for i in range(len(slot_infos))]

    body_text_for_email = (
        f"Here are the available times after {min_hour:02d}:00 — click any to confirm instantly:"
        if min_hour else
        "Thank you for getting back — here are the next available times. Click any to confirm your spot:"
    )
    send_v2_slots_email(
        lead.email, lead.business_name, slot_strings,
        book_urls=book_urls,
        body_text=body_text_for_email,
        contact_name=lead.contact_name,
    )

    lead.offered_slots_json = json.dumps(slot_infos)
    lead.replied_at = datetime.now(timezone.utc)
    # Bust cache so next lead gets a fresh Zoho fetch
    invalidate_week_cache()
    logger.info("general_slots_sent", email=lead.email, count=len(slot_infos), min_hour=min_hour)
    return f"Sent {len(slot_infos)} general slots"


@shared_task
def process_reply_v2(reply: dict):
    logger.info("running_process_reply_v2", email=reply.get("from_addr"))
    return run_async(_process_reply_v2, reply)


@shared_task
def check_inbox_replies_v2():
    logger.info("running_check_inbox_replies_v2")
    from app.workers.email_tasks import poll_imap
    return poll_imap()
