import json
import uuid
from datetime import datetime, timezone

from celery import shared_task
from sqlalchemy.future import select
from structlog import get_logger

from app.db.models import AvailableDateV2, LeadV2, LeadStatus, ZohoSlot, SlotStatus
from app.services.sheets import GoogleSheetsService
from app.services.zoho import ZohoBookingsService
from app.services.email_service import (
    make_book_url,
    send_v2_slots_email,
    send_booking_confirmation_to_lead,
    send_organizer_booking_notification,
)
from app.workers.runtime import run_async

logger = get_logger(__name__)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _match_slot_simple(body: str, slot_strings: list) -> str | None:
    """Pick the lead's chosen slot from reply body without LLM."""
    body_lower = body.lower()

    # Hard decline signals
    decline = ["not interested", "unsubscribe", "remove me", "stop emailing", "no thanks", "no thank you"]
    if any(w in body_lower for w in decline):
        return None

    # Ordinal references → map to index
    ordinals = [
        (["first", "1st", "option 1", "number 1", "slot 1", " 1 ", "(1)"], 0),
        (["second", "2nd", "option 2", "number 2", "slot 2", " 2 ", "(2)"], 1),
        (["third", "3rd", "option 3", "number 3", "slot 3", " 3 ", "(3)"], 2),
    ]
    for keywords, idx in ordinals:
        if idx < len(slot_strings) and any(kw in body_lower for kw in keywords):
            return slot_strings[idx]

    # Day-name + time match (e.g. "Thursday" + "10:00 am" from "Thursday, Jun 04 at 10:00 AM")
    for slot_str in slot_strings:
        parts = slot_str.lower().replace(",", "").split()
        day_name = parts[0] if parts else ""
        time_part = " ".join(parts[-2:]) if len(parts) >= 2 else ""
        if day_name and time_part and day_name in body_lower and time_part in body_lower:
            return slot_str

    # Generic confirmation → first available slot
    confirm = [
        "yes", "yeah", "yep", "sure", "ok", "okay", "confirm", "works for me",
        "that works", "perfect", "great", "sounds good", "looks good",
        "let's do it", "lets do it", "any of them", "any works", "available",
        "i'm in", "im in",
    ]
    if any(w in body_lower for w in confirm):
        return slot_strings[0] if slot_strings else None

    return None


# ---------------------------------------------------------------------------
# Sync Google Sheet → leads_v2
# ---------------------------------------------------------------------------

async def _sync_google_sheet(db):
    sheets_service = GoogleSheetsService()
    records = sheets_service.fetch_new_leads()
    if not records:
        return "No records found or sheets not configured."

    for row in records:
        email = row.get("email") or row.get("Business Email") or row.get("business_email")
        name = (row.get("business_name") or row.get("name")
                or row.get("Company") or row.get("Person Name"))
        if not email or not name:
            continue
        existing = (
            await db.execute(select(LeadV2).where(LeadV2.email == email))
        ).scalar_one_or_none()
        if not existing:
            db.add(LeadV2(business_name=name, email=email, status=LeadStatus.NEW))
            logger.info("added_lead_from_sheet", email=email)
    return "Sync complete"


@shared_task
def sync_google_sheet():
    logger.info("running_sync_google_sheet")
    return run_async(_sync_google_sheet)


# ---------------------------------------------------------------------------
# Sync Zoho slots → zoho_slots (kept for reference / Zoho booking availability)
# ---------------------------------------------------------------------------

async def _sync_zoho_slots(db):
    zoho_service = ZohoBookingsService()
    today = datetime.now().strftime("%d-%b-%Y")
    slots_data = zoho_service.fetch_available_slots(today)
    if not slots_data:
        return "No slots found or zoho not configured."

    for slot_item in slots_data:
        # Zoho returns either a dict with time fields or a plain string
        if isinstance(slot_item, dict):
            raw_id = str(slot_item.get("id") or slot_item.get("time", slot_item))
            zoho_id = f"z_slot_{raw_id}"
            try:
                date_val = slot_item.get("date", "")
                time_val = slot_item.get("time", "")
                slot_time = datetime.strptime(f"{date_val} {time_val}", "%Y-%m-%d %H:%M").replace(tzinfo=timezone.utc)
            except (ValueError, TypeError):
                slot_time = datetime.now(timezone.utc)
        else:
            slot_str = str(slot_item)
            zoho_id = f"z_slot_{slot_str}"
            try:
                slot_time = datetime.strptime(slot_str, "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
            except ValueError:
                slot_time = datetime.now(timezone.utc)

        existing = (
            await db.execute(select(ZohoSlot).where(ZohoSlot.zoho_slot_id == zoho_id))
        ).scalar_one_or_none()
        if not existing:
            db.add(ZohoSlot(zoho_slot_id=zoho_id, slot_time=slot_time, status=SlotStatus.AVAILABLE))
    return "Slots synced"


@shared_task
def sync_zoho_slots():
    logger.info("running_sync_zoho_slots")
    return run_async(_sync_zoho_slots)


# ---------------------------------------------------------------------------
# Process new leads → send slot-offer email
# ---------------------------------------------------------------------------

async def _process_new_leads(db):
    leads = (
        await db.execute(select(LeadV2).where(LeadV2.status == LeadStatus.NEW))
    ).scalars().all()
    if not leads:
        return "No new leads."

    now = datetime.now(timezone.utc)
    available_slots = (
        await db.execute(
            select(AvailableDateV2)
            .where(AvailableDateV2.is_available == True)
            .where(AvailableDateV2.slot_datetime > now)
            .order_by(AvailableDateV2.slot_datetime)
        )
    ).scalars().all()

    top3 = available_slots[:3]
    if not top3:
        logger.warning("no_available_slots_configured")
        return "No available slots to offer. Add slots via the admin dashboard."

    slot_strings = [s.slot_datetime.strftime("%A, %b %d at %I:%M %p") for s in top3]
    offered_json = json.dumps(slot_strings)

    for lead in leads:
        book_urls = [make_book_url(str(lead.id), i) for i in range(len(slot_strings))]
        success = send_v2_slots_email(lead.email, lead.business_name, slot_strings, book_urls=book_urls)
        if success:
            lead.status = LeadStatus.SENT
            lead.sent_at = datetime.now(timezone.utc)
            lead.offered_slots_json = offered_json
            logger.info("lead_email_sent", email=lead.email)

    return f"Processed {len(leads)} leads with {len(slot_strings)} slots."


@shared_task
def process_new_leads():
    logger.info("running_process_new_leads")
    return run_async(_process_new_leads)


# ---------------------------------------------------------------------------
# Process reply → match slot → Zoho booking → confirm emails
# ---------------------------------------------------------------------------

async def _process_reply_v2(db, reply: dict) -> str:
    from_addr = reply.get("from_addr")
    body = reply.get("body")
    if not from_addr or not body:
        return "Missing email or body"

    lead = (
        await db.execute(
            select(LeadV2).where(
                LeadV2.email == from_addr,
                LeadV2.status == LeadStatus.SENT,
            )
        )
    ).scalar_one_or_none()

    if not lead:
        logger.info("no_matching_sent_lead", email=from_addr)
        return "No matching lead"

    # Use slots that were actually offered to this lead
    if lead.offered_slots_json:
        slot_strings = json.loads(lead.offered_slots_json)
    else:
        now = datetime.now(timezone.utc)
        fallback = (
            await db.execute(
                select(AvailableDateV2)
                .where(AvailableDateV2.is_available == True)
                .where(AvailableDateV2.slot_datetime > now)
                .order_by(AvailableDateV2.slot_datetime)
            )
        ).scalars().all()
        slot_strings = [s.slot_datetime.strftime("%A, %b %d at %I:%M %p") for s in fallback[:3]]

    selected_str = _match_slot_simple(body, slot_strings)

    if not selected_str:
        lead.status = LeadStatus.REPLIED
        lead.replied_at = datetime.now(timezone.utc)
        logger.info("reply_no_slot_matched", email=from_addr)
        return "Replied but no slot matched"

    logger.info("slot_matched", email=from_addr, slot=selected_str)

    # Find the matching AvailableDateV2 record to get exact datetime for Zoho
    all_slots = (
        await db.execute(select(AvailableDateV2))
    ).scalars().all()

    matched_record = None
    for s in all_slots:
        if s.slot_datetime.strftime("%A, %b %d at %I:%M %p") == selected_str:
            matched_record = s
            break

    # Determine date/time strings for Zoho
    if matched_record:
        date_str = matched_record.slot_datetime.strftime("%d-%b-%Y")
        time_str = matched_record.slot_datetime.strftime("%H:%M")
    else:
        # Fallback: parse slot string directly
        try:
            import dateutil.parser
            parsed = dateutil.parser.parse(selected_str)
            date_str = parsed.strftime("%d-%b-%Y")
            time_str = parsed.strftime("%H:%M")
        except Exception:
            date_str = "N/A"
            time_str = "N/A"

    # Create Zoho booking
    zoho = ZohoBookingsService()
    booking_id, meeting_link = zoho.create_booking(
        name=lead.business_name,
        email=lead.email,
        date_str=date_str,
        time_str=time_str,
    )

    # Mark this slot unavailable so it's not offered again
    if matched_record:
        matched_record.is_available = False

    # Update lead
    lead.status = LeadStatus.BOOKED
    lead.replied_at = datetime.now(timezone.utc)
    lead.booked_at = datetime.now(timezone.utc)
    lead.selected_slot = selected_str
    lead.booking_id = booking_id or f"mock_{uuid.uuid4()}"
    lead.zoho_meeting_link = meeting_link

    # Send emails
    send_booking_confirmation_to_lead(
        lead.email, lead.business_name, selected_str, meeting_link=meeting_link
    )
    send_organizer_booking_notification(
        lead.business_name, lead.email, selected_str, meeting_link=meeting_link
    )

    logger.info("booking_complete", email=from_addr, booking_id=lead.booking_id)
    return f"Booked (ID: {lead.booking_id})"


@shared_task
def process_reply_v2(reply: dict):
    logger.info("running_process_reply_v2", email=reply.get("from_addr"))
    return run_async(_process_reply_v2, reply)


@shared_task
def check_inbox_replies_v2():
    logger.info("running_check_inbox_replies_v2")
    from app.workers.email_tasks import poll_imap
    return poll_imap()
