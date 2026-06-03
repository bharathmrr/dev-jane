import asyncio
import datetime as dt
import hashlib
import hmac as _hmac
import json
import uuid
from typing import Any, Dict

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import HTMLResponse
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.db.session import get_db
from app.db.models import AvailableDateV2, LeadV2, LeadStatus, ZohoSlot
from app.services.email_service import (
    send_booking_confirmation_to_lead,
    send_organizer_booking_notification,
)
from app.services.zoho import ZohoBookingsService
from app.workers.v2_tasks import sync_google_sheet, process_new_leads

router = APIRouter(prefix="/v2", tags=["v2"])


# ---------------------------------------------------------------------------
# Manual triggers
# ---------------------------------------------------------------------------

@router.post("/sync-sheet")
def trigger_sync_sheet():
    sync_google_sheet.delay()
    return {"message": "Google Sheet sync triggered in background"}


@router.post("/send-email")
def trigger_send_email():
    process_new_leads.delay()
    return {"message": "Lead processing triggered in background"}


# ---------------------------------------------------------------------------
# Leads
# ---------------------------------------------------------------------------

@router.get("/leads")
async def get_leads(db: AsyncSession = Depends(get_db)):
    result = await db.execute(
        select(LeadV2).order_by(LeadV2.created_at.desc())
    )
    leads = result.scalars().all()
    return [
        {
            "id": str(lead.id),
            "business_name": lead.business_name,
            "email": lead.email,
            "status": lead.status.value,
            "sent_at": lead.sent_at,
            "replied_at": lead.replied_at,
            "booked_at": lead.booked_at,
            "selected_slot": lead.selected_slot,
            "booking_id": lead.booking_id,
            "zoho_meeting_link": lead.zoho_meeting_link,
        }
        for lead in leads
    ]


# ---------------------------------------------------------------------------
# Zoho slots (read-only view of synced Zoho data)
# ---------------------------------------------------------------------------

@router.get("/slots")
async def get_slots(db: AsyncSession = Depends(get_db)):
    result = await db.execute(select(ZohoSlot).order_by(ZohoSlot.slot_time))
    slots = result.scalars().all()
    return [
        {
            "id": str(slot.id),
            "zoho_slot_id": slot.zoho_slot_id,
            "slot_time": slot.slot_time,
            "status": slot.status.value,
            "booked_email": slot.booked_email,
        }
        for slot in slots
    ]


# ---------------------------------------------------------------------------
# Dashboard stats
# ---------------------------------------------------------------------------

@router.get("/dashboard/stats")
async def get_dashboard_stats(db: AsyncSession = Depends(get_db)):
    result = await db.execute(select(LeadV2))
    leads = result.scalars().all()
    total = len(leads)
    sent    = sum(1 for l in leads if l.status != LeadStatus.NEW)
    replied = sum(1 for l in leads if l.status in (LeadStatus.REPLIED, LeadStatus.BOOKED))
    booked  = sum(1 for l in leads if l.status == LeadStatus.BOOKED)
    return {
        "total_leads":     total,
        "sent_leads":      sent,
        "replied_leads":   replied,
        "booked_leads":    booked,
        "conversion_rate": (booked / total) if total > 0 else 0.0,
    }


# ---------------------------------------------------------------------------
# Available date-time slots (admin-managed)
# ---------------------------------------------------------------------------

def _slot_to_dict(slot: AvailableDateV2) -> dict:
    return {
        "id":            str(slot.id),
        "slot_datetime": slot.slot_datetime.isoformat(),
        "is_available":  slot.is_available,
        "display":       slot.slot_datetime.strftime("%A, %b %d at %I:%M %p"),
    }


@router.get("/available-dates")
async def get_available_dates(db: AsyncSession = Depends(get_db)):
    result = await db.execute(
        select(AvailableDateV2).order_by(AvailableDateV2.slot_datetime)
    )
    return [_slot_to_dict(s) for s in result.scalars().all()]


class AddSlotRequest(BaseModel):
    slot_datetime: str  # ISO-8601, e.g. "2026-06-05T10:00:00"


@router.post("/available-dates")
async def add_available_date(req: AddSlotRequest, db: AsyncSession = Depends(get_db)):
    try:
        parsed = dt.datetime.fromisoformat(req.slot_datetime)
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid datetime format. Use ISO-8601.")
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=dt.timezone.utc)

    existing = (await db.execute(
        select(AvailableDateV2).where(AvailableDateV2.slot_datetime == parsed)
    )).scalar_one_or_none()
    if existing:
        raise HTTPException(status_code=409, detail="Slot already exists.")

    slot = AvailableDateV2(slot_datetime=parsed, is_available=True)
    db.add(slot)
    await db.flush()
    await db.refresh(slot)
    return _slot_to_dict(slot)


@router.patch("/available-dates/{slot_id}/toggle")
async def toggle_available_date(slot_id: str, db: AsyncSession = Depends(get_db)):
    try:
        sid = uuid.UUID(slot_id)
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid slot ID.")
    slot = await db.get(AvailableDateV2, sid)
    if not slot:
        raise HTTPException(status_code=404, detail="Slot not found.")
    slot.is_available = not slot.is_available
    await db.flush()
    return _slot_to_dict(slot)


@router.delete("/available-dates/{slot_id}")
async def delete_available_date(slot_id: str, db: AsyncSession = Depends(get_db)):
    try:
        sid = uuid.UUID(slot_id)
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid slot ID.")
    slot = await db.get(AvailableDateV2, sid)
    if not slot:
        raise HTTPException(status_code=404, detail="Slot not found.")
    await db.delete(slot)
    return {"status": "deleted"}


# ---------------------------------------------------------------------------
# One-click booking via email card link
# ---------------------------------------------------------------------------

def _verify_book_sig(lead_id: str, slot_idx: int, sig: str) -> bool:
    msg = f"{lead_id}:{slot_idx}".encode()
    expected = _hmac.new(
        settings.EMAIL_HMAC_SECRET.encode(), msg, hashlib.sha256
    ).hexdigest()[:16]
    return _hmac.compare_digest(sig, expected)


def _confirmed_page(name: str, slot: str, meeting_link: str | None) -> str:
    join = (
        f'<a href="{meeting_link}" style="display:inline-block;margin-top:20px;'
        f'padding:14px 28px;background:#1d4ed8;color:#fff;text-decoration:none;'
        f'border-radius:8px;font-weight:700;font-size:15px;">Join Meeting</a>'
        if meeting_link else
        "<p style='color:#6b7280;font-size:14px;margin-top:16px;'>"
        "A calendar invite will be sent to you shortly.</p>"
    )
    return f"""<!DOCTYPE html><html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
</head>
<body style="font-family:-apple-system,sans-serif;background:#f0f9ff;
             display:flex;justify-content:center;align-items:center;min-height:100vh;margin:0;">
  <div style="background:#fff;border-radius:14px;padding:48px 40px;max-width:480px;
              text-align:center;box-shadow:0 4px 24px rgba(0,0,0,.08);">
    <div style="font-size:48px;margin-bottom:16px;">&#x2705;</div>
    <h1 style="color:#1e3a8a;font-size:24px;margin:0 0 8px;">Meeting Confirmed!</h1>
    <p style="color:#374151;font-size:16px;margin:0 0 4px;">Hi <strong>{name}</strong>,</p>
    <p style="color:#374151;font-size:15px;">
      Your meeting is booked for<br>
      <strong style="color:#1e3a8a;font-size:16px;">{slot}</strong>
    </p>
    {join}
    <p style="color:#9ca3af;font-size:13px;margin-top:28px;">
      Check your email for the calendar invite.
    </p>
  </div>
</body></html>"""


def _slot_taken_page(slot: str) -> str:
    return f"""<!DOCTYPE html><html><head><meta charset="utf-8"></head>
<body style="font-family:-apple-system,sans-serif;background:#fff7ed;
             display:flex;justify-content:center;align-items:center;min-height:100vh;margin:0;">
  <div style="background:#fff;border-radius:14px;padding:48px 40px;max-width:480px;
              text-align:center;box-shadow:0 4px 24px rgba(0,0,0,.08);">
    <div style="font-size:48px;margin-bottom:16px;">&#x23F0;</div>
    <h1 style="color:#c2410c;font-size:22px;">Slot No Longer Available</h1>
    <p style="color:#374151;">
      Sorry, <strong>{slot}</strong> was just booked by someone else.
    </p>
    <p style="color:#6b7280;font-size:14px;">
      Please reply to the original email to request a different time.
    </p>
  </div>
</body></html>"""


def _already_booked_page(slot: str, meeting_link: str | None) -> str:
    join = (
        f'<a href="{meeting_link}" style="display:inline-block;margin-top:16px;'
        f'padding:12px 24px;background:#1d4ed8;color:#fff;text-decoration:none;'
        f'border-radius:8px;font-weight:600;">Join Meeting</a>'
        if meeting_link else ""
    )
    return f"""<!DOCTYPE html><html><head><meta charset="utf-8"></head>
<body style="font-family:-apple-system,sans-serif;background:#f0fdf4;
             display:flex;justify-content:center;align-items:center;min-height:100vh;margin:0;">
  <div style="background:#fff;border-radius:14px;padding:48px 40px;max-width:480px;
              text-align:center;box-shadow:0 4px 24px rgba(0,0,0,.08);">
    <div style="font-size:48px;margin-bottom:16px;">&#x1F4C5;</div>
    <h1 style="color:#166534;font-size:22px;">Already Booked</h1>
    <p style="color:#374151;">Your meeting for <strong>{slot}</strong> is confirmed.</p>
    {join}
  </div>
</body></html>"""


@router.get("/book/{lead_id}/{slot_idx}/{sig}", response_class=HTMLResponse)
async def one_click_book(
    lead_id: str, slot_idx: int, sig: str, db: AsyncSession = Depends(get_db)
):
    if not _verify_book_sig(lead_id, slot_idx, sig):
        raise HTTPException(status_code=400, detail="Invalid or expired booking link.")

    try:
        lid = uuid.UUID(lead_id)
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid link.")

    lead = await db.get(LeadV2, lid)
    if not lead:
        raise HTTPException(status_code=404, detail="Lead not found.")

    # Idempotent — already booked
    if lead.status == LeadStatus.BOOKED:
        return HTMLResponse(_already_booked_page(lead.selected_slot or "", lead.zoho_meeting_link))

    if not lead.offered_slots_json:
        raise HTTPException(status_code=400, detail="No slots on record for this lead.")

    slot_strings = json.loads(lead.offered_slots_json)
    if slot_idx >= len(slot_strings):
        raise HTTPException(status_code=400, detail="Slot no longer available.")

    selected_str = slot_strings[slot_idx]

    # Find the matching AvailableDateV2 record
    all_slots = (await db.execute(select(AvailableDateV2))).scalars().all()
    matched = next(
        (s for s in all_slots
         if s.slot_datetime.strftime("%A, %b %d at %I:%M %p") == selected_str),
        None,
    )

    # Check DB availability BEFORE calling Zoho
    # Covers: slot deleted, or already booked by someone else
    if not matched or not matched.is_available:
        return HTMLResponse(_slot_taken_page(selected_str))

    date_str = matched.slot_datetime.strftime("%d-%b-%Y")
    time_str = matched.slot_datetime.strftime("%H:%M")

    # Create Zoho booking (blocking I/O — run in thread)
    zoho = ZohoBookingsService()
    booking_id, meeting_link = await asyncio.to_thread(
        zoho.create_booking,
        name=lead.business_name,
        email=lead.email,
        date_str=date_str,
        time_str=time_str,
    )

    # Mark slot unavailable
    if matched:
        matched.is_available = False

    # Update lead
    now = dt.datetime.now(dt.timezone.utc)
    lead.status = LeadStatus.BOOKED
    lead.replied_at = now
    lead.booked_at = now
    lead.selected_slot = selected_str
    lead.booking_id = booking_id or f"click_{uuid.uuid4()}"
    lead.zoho_meeting_link = meeting_link
    await db.flush()

    # Send confirmation emails (blocking I/O — run in thread)
    await asyncio.to_thread(
        send_booking_confirmation_to_lead,
        lead.email, lead.business_name, selected_str, meeting_link,
    )
    await asyncio.to_thread(
        send_organizer_booking_notification,
        lead.business_name, lead.email, selected_str, meeting_link,
    )

    return HTMLResponse(_confirmed_page(lead.business_name, selected_str, meeting_link))


# ---------------------------------------------------------------------------
# Webhooks (stubs)
# ---------------------------------------------------------------------------

@router.post("/webhook/email-reply")
def webhook_email_reply(_payload: Dict[str, Any]):
    return {"status": "received"}


@router.post("/webhook/zoho-booking")
def webhook_zoho_booking(_payload: Dict[str, Any]):
    return {"status": "received"}
