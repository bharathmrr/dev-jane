import asyncio
import datetime as dt
import hashlib
import hmac as _hmac
import json
import uuid
from typing import Any, Dict
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, HTTPException, BackgroundTasks
from fastapi.responses import HTMLResponse
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.db.session import get_db
from app.db.models import LeadV2, LeadStatus, ZohoSlot, SlotStatus
from app.services.availability_v2 import get_slots_for_week, invalidate_week_cache
from app.services.email_service import (
    make_book_url,
    send_booking_confirmation_to_lead,
    send_organizer_booking_notification,
    send_v2_slots_email,
)
from app.services.zoho import ZohoBookingsService
from app.workers.celery_app import celery_app

_IST = ZoneInfo("Asia/Kolkata")

router = APIRouter(prefix="/v2", tags=["v2"])


# ---------------------------------------------------------------------------
# Manual triggers
# ---------------------------------------------------------------------------

@router.post("/sync-sheet")
def trigger_sync_sheet():
    celery_app.send_task("app.workers.v2_tasks.sync_google_sheet")
    return {"message": "Google Sheet sync triggered in background"}


@router.post("/send-email")
def trigger_send_email():
    celery_app.send_task("app.workers.v2_tasks.process_new_leads")
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
            "contact_name": lead.contact_name or "",
            "business_name": lead.business_name or "",
            "email": lead.email,
            "summary": lead.summary or "",
            "status": lead.status.value,
            "sent_at": lead.sent_at.isoformat() if lead.sent_at else None,
            "replied_at": lead.replied_at.isoformat() if lead.replied_at else None,
            "booked_at": lead.booked_at.isoformat() if lead.booked_at else None,
            "selected_slot": lead.selected_slot,
            "booking_id": lead.booking_id,
            "zoho_meeting_link": lead.zoho_meeting_link,
            "created_at": lead.created_at.isoformat() if lead.created_at else None,
        }
        for lead in leads
    ]


# ---------------------------------------------------------------------------
# CRM import — Zoho Flow pushes leads here when created/updated in Zoho CRM
# ---------------------------------------------------------------------------

class CRMLeadPayload(BaseModel):
    email: str
    contact_name: str = ""
    business_name: str = ""
    summary: str = ""


@router.post("/leads/import-crm")
async def import_crm_lead(payload: CRMLeadPayload, db: AsyncSession = Depends(get_db)):
    """Zoho Flow webhook — create or update a lead from Zoho CRM."""
    existing = (await db.execute(
        select(LeadV2).where(LeadV2.email == payload.email)
    )).scalar_one_or_none()

    if existing:
        if payload.contact_name:
            existing.contact_name = payload.contact_name
        if payload.business_name:
            existing.business_name = payload.business_name
        if payload.summary:
            existing.summary = payload.summary
        await db.commit()
        return {"message": "Lead updated", "lead_id": str(existing.id), "action": "updated"}

    new_lead = LeadV2(
        email=payload.email,
        contact_name=payload.contact_name,
        business_name=payload.business_name,
        summary=payload.summary,
        status=LeadStatus.NEW,
    )
    db.add(new_lead)
    await db.commit()
    await db.refresh(new_lead)
    return {"message": "Lead created", "lead_id": str(new_lead.id), "action": "created"}


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
            "slot_time": slot.slot_time.isoformat() if slot.slot_time else None,
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
        "conversion_rate": round((booked / total) * 100, 1) if total > 0 else 0.0,
    }


# ---------------------------------------------------------------------------
# One-click booking via email card link
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# Week selection via email card click (Stage 1 → Stage 2)
# ---------------------------------------------------------------------------

def _verify_week_sig(lead_id: str, week: str, sig: str) -> bool:
    msg = f"{lead_id}:week:{week}".encode()
    expected = _hmac.new(
        settings.EMAIL_HMAC_SECRET.encode(), msg, hashlib.sha256
    ).hexdigest()[:16]
    return _hmac.compare_digest(sig, expected)


def _slots_sent_page(name: str, week_label: str) -> str:
    return f"""<!DOCTYPE html><html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"></head>
<body style="font-family:-apple-system,sans-serif;background:#eff6ff;
             display:flex;justify-content:center;align-items:center;min-height:100vh;margin:0;">
  <div style="background:#fff;border-radius:14px;padding:48px 40px;max-width:480px;
              text-align:center;box-shadow:0 4px 24px rgba(0,0,0,.08);">
    <div style="font-size:48px;margin-bottom:16px;">&#x1F4EC;</div>
    <h1 style="color:#1e3a8a;font-size:22px;margin:0 0 12px;">Slots Sent!</h1>
    <p style="color:#374151;font-size:15px;margin:0 0 8px;">Hi <strong>{name}</strong>,</p>
    <p style="color:#374151;font-size:15px;">
      We've sent the available slots for <strong>{week_label}</strong> to your inbox.<br>
      Please check your email and click any slot to confirm instantly.
    </p>
    <p style="color:#9ca3af;font-size:13px;margin-top:24px;">You can close this tab.</p>
  </div>
</body></html>"""


def _arranging_slots_page(name: str) -> str:
    return f"""<!DOCTYPE html><html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"></head>
<body style="font-family:-apple-system,sans-serif;background:#f0f9ff;
             display:flex;justify-content:center;align-items:center;min-height:100vh;margin:0;">
  <div style="background:#fff;border-radius:14px;padding:48px 40px;max-width:480px;
              text-align:center;box-shadow:0 4px 24px rgba(0,0,0,.08);">
    <div style="font-size:48px;margin-bottom:16px;">&#x1F4EC;</div>
    <h1 style="color:#1e3a8a;font-size:22px;margin:0 0 12px;">We're On It, {name}!</h1>
    <p style="color:#374151;font-size:15px;margin:0 0 8px;">
      We are personally arranging the best available times for you.
    </p>
    <p style="color:#374151;font-size:15px;">
      You will receive an email shortly with your options — please check your inbox.
    </p>
    <p style="color:#9ca3af;font-size:13px;margin-top:24px;">You can close this tab.</p>
  </div>
</body></html>"""


@router.get("/week/{lead_id}/{week}/{sig}", response_class=HTMLResponse)
async def select_week(
    lead_id: str, week: str, sig: str,
    background_tasks: BackgroundTasks,
    db: AsyncSession = Depends(get_db)
):
    if week not in ("this", "next"):
        raise HTTPException(status_code=400, detail="Invalid week selection.")
    if not _verify_week_sig(lead_id, week, sig):
        raise HTTPException(status_code=400, detail="Invalid or expired link.")

    try:
        lid = uuid.UUID(lead_id)
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid link.")

    lead = await db.get(LeadV2, lid)
    if not lead:
        raise HTTPException(status_code=404, detail="Lead not found.")

    if lead.status == LeadStatus.BOOKED:
        return HTMLResponse(_already_booked_page(lead.selected_slot or "", lead.zoho_meeting_link))

    from app.services.availability_v2 import get_available_slots

    # Try requested week → other week → broad 14-day search
    slot_infos = await get_slots_for_week(db, week, 6)
    if not slot_infos:
        other = "next" if week == "this" else "this"
        slot_infos = await get_slots_for_week(db, other, 6)
        if slot_infos:
            week = other
    if not slot_infos:
        slot_infos = await get_available_slots(db, n=6, lookahead_days=14)

    greeting_name = (lead.contact_name or lead.business_name).split()[0].capitalize()

    if not slot_infos:
        # Truly nothing available — send graceful "when are you available?" email
        from app.workers.v2_tasks import _send_graceful_no_slots
        background_tasks.add_task(_send_graceful_no_slots, lead)
        return HTMLResponse(_arranging_slots_page(greeting_name))

    slot_strings = [s["display"] for s in slot_infos]
    book_urls = [make_book_url(str(lead.id), i) for i in range(len(slot_infos))]

    # Send Stage 2 email with clickable slot cards in background
    background_tasks.add_task(
        send_v2_slots_email,
        lead.email, lead.business_name, slot_strings,
        book_urls=book_urls,
        contact_name=lead.contact_name,
    )

    # Store offered slots and mark offered_at so _get_held_slots blocks these
    # slots for other leads within the 2h hold window
    lead.offered_slots_json = json.dumps(slot_infos)
    lead.replied_at = dt.datetime.now(dt.timezone.utc)
    await db.flush()

    # Bust the week cache so the NEXT lead gets a fresh Zoho fetch
    invalidate_week_cache(week)

    week_label = "This Week" if week == "this" else "Next Week"
    return HTMLResponse(_slots_sent_page(greeting_name, week_label))


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
    lead_id: str, slot_idx: int, sig: str,
    background_tasks: BackgroundTasks,
    db: AsyncSession = Depends(get_db)
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

    if lead.status == LeadStatus.BOOKED:
        return HTMLResponse(_already_booked_page(lead.selected_slot or "", lead.zoho_meeting_link))

    if not lead.offered_slots_json:
        raise HTTPException(status_code=400, detail="No slots on record for this lead.")

    raw_slots = json.loads(lead.offered_slots_json)
    if slot_idx >= len(raw_slots):
        raise HTTPException(status_code=400, detail="Slot index out of range.")

    slot_item = raw_slots[slot_idx]

    if isinstance(slot_item, dict):
        selected_str = slot_item["display"]
        date_str = slot_item.get("date_str") or ""
        time_str = slot_item.get("time_str") or ""
    else:
        selected_str = str(slot_item)
        date_str = ""
        time_str = ""

    # Parse date/time from display string if missing (legacy format)
    if not date_str or not time_str:
        try:
            import dateutil.parser
            parsed = dateutil.parser.parse(selected_str)
            date_str = parsed.strftime("%d-%b-%Y")
            time_str = parsed.strftime("%H:%M")
        except Exception:
            raise HTTPException(status_code=400, detail="Cannot parse slot date/time.")

    # Check if slot is already booked locally
    from app.services.availability_v2 import _get_booked_slots, IST as _IST
    booked_iso, booked_display = await _get_booked_slots(db)
    try:
        hour = int(time_str.split(":")[0])
        slot_date = dt.datetime.strptime(date_str, "%d-%b-%Y").date()
        slot_dt_ist = dt.datetime(slot_date.year, slot_date.month, slot_date.day, hour, 0, tzinfo=_IST)
        if slot_dt_ist.isoformat() in booked_iso or slot_dt_ist.strftime("%A, %b %d at %I:%M %p") in booked_display or selected_str in booked_display:
            return HTMLResponse(_slot_taken_page(selected_str))
    except Exception:
        pass

    # Call Zoho to create the booking
    zoho = ZohoBookingsService()
    booking_id, meeting_link = await asyncio.to_thread(
        zoho.create_booking,
        name=lead.contact_name or lead.business_name,
        email=lead.email,
        date_str=date_str,
        time_str=time_str,
    )

    if booking_id is None:
        return HTMLResponse(_slot_taken_page(selected_str))

    now = dt.datetime.now(dt.timezone.utc)
    lead.status = LeadStatus.BOOKED
    lead.replied_at = now
    lead.booked_at = now
    lead.selected_slot = selected_str
    lead.booking_id = booking_id
    lead.zoho_meeting_link = meeting_link

    # Sync ZohoSlot status in DB
    try:
        hour = int(time_str.split(":")[0])
        slot_date = dt.datetime.strptime(date_str, "%d-%b-%Y").date()
        slot_dt_ist = dt.datetime(slot_date.year, slot_date.month, slot_date.day, hour, 0, tzinfo=_IST)
        slot_dt_utc = slot_dt_ist.astimezone(dt.timezone.utc)
        slot_row = (await db.execute(
            select(ZohoSlot).where(ZohoSlot.slot_time == slot_dt_utc)
        )).scalar_one_or_none()
        if slot_row:
            slot_row.status = SlotStatus.BOOKED
            slot_row.booked_email = lead.email
    except Exception:
        pass

    await db.flush()

    # Offload slow email dispatches to FastAPI background tasks so the web response returns immediately
    background_tasks.add_task(
        send_booking_confirmation_to_lead,
        lead.email, lead.business_name, selected_str, meeting_link,
        lead.contact_name,
    )
    background_tasks.add_task(
        send_organizer_booking_notification,
        lead.business_name, lead.email, selected_str, meeting_link,
    )

    greeting_name = (lead.contact_name or lead.business_name).split()[0]
    return HTMLResponse(_confirmed_page(greeting_name, selected_str, meeting_link))


# ---------------------------------------------------------------------------
# Webhooks (stubs)
# ---------------------------------------------------------------------------

@router.post("/webhook/email-reply")
def webhook_email_reply(_payload: Dict[str, Any]):
    return {"status": "received"}


@router.post("/webhook/zoho-booking")
def webhook_zoho_booking(_payload: Dict[str, Any]):
    return {"status": "received"}
