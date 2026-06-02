import os
from fastapi import APIRouter, Request, Form, BackgroundTasks, status
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from app.core.config import settings
from app.services import unipile, email_service

router = APIRouter(tags=["linkedin-outreach"])

current_dir = os.path.dirname(os.path.abspath(__file__))
templates_dir = os.path.join(os.path.dirname(os.path.dirname(current_dir)), "templates")
templates = Jinja2Templates(directory=templates_dir)

YES_KEYWORDS = [
    "yes", "sure", "interested", "sounds good", "okay",
    "let's talk", "would love to", "definitely", "connect",
    "absolutely", "of course", "happy to", "love to",
]

def contains_yes_keyword(text: str) -> bool:
    if not text:
        return False
    msg_lower = text.lower().strip()
    return any(kw in msg_lower for kw in YES_KEYWORDS)


def _parse_webhook_payload(payload: dict) -> tuple[str, str, str, str]:
    message_text = (
        payload.get("message")
        or payload.get("text")
        or payload.get("body", "")
    )
    sender_info = payload.get("sender") or {}
    attendee_id = (
        sender_info.get("attendee_id")
        or sender_info.get("id")
        or payload.get("sender_profile_id")
        or payload.get("attendee_id")
        or ""
    )
    attendee_name = (
        sender_info.get("attendee_name")
        or sender_info.get("name")
        or payload.get("sender_name")
        or "Valued Contact"
    )
    chat_id = payload.get("chat_id") or ""
    return str(message_text), str(attendee_id), str(attendee_name), str(chat_id)


async def process_webhook_background(payload: dict):
    event_type = payload.get("event", "message_received")
    if event_type not in ("message_received", ""):
        print(f"[WEBHOOK] Ignored non-message event: {event_type}")
        return

    message_text, attendee_id, attendee_name, chat_id = _parse_webhook_payload(payload)
    print(f"[WEBHOOK] Message from '{attendee_name}': \"{message_text}\"")

    if not contains_yes_keyword(message_text):
        print("[WEBHOOK] No affirmative keyword detected — message ignored.")
        return

    print("[WEBHOOK] Affirmative keyword detected! Starting outreach sequence.")

    if not attendee_id:
        print("[WEBHOOK] Error: could not determine attendee_id from payload. Aborting.")
        return

    email = await unipile.get_profile_email(attendee_id)

    if email:
        print(f"[WEBHOOK] Email found: {email}. Sending booking email via SendGrid...")
        email_service.send_booking_email(to_email=email, recipient_name=attendee_name)
    else:
        print("[WEBHOOK] No email found. Sending LinkedIn DM with form link...")
        fallback_url = f"{settings.APP_URL.rstrip('/')}/form"
        first_name = attendee_name.split()[0] if attendee_name and attendee_name != "Valued Contact" else "there"
        fallback_message = (
            f"Hi {first_name}! Thanks for your interest. "
            f"Please fill in this quick form so I can send you the meeting link: {fallback_url}"
        )
        await unipile.send_linkedin_message(
            chat_id=chat_id or None,
            attendee_id=attendee_id,
            text=fallback_message,
        )


@router.post("/webhook/linkedin", status_code=status.HTTP_200_OK)
async def webhook_linkedin(request: Request, background_tasks: BackgroundTasks):
    try:
        payload = await request.json()
    except Exception:
        return {"error": "Invalid JSON payload"}
    print(f"[WEBHOOK] Received payload keys: {list(payload.keys())}")
    background_tasks.add_task(process_webhook_background, payload)
    return {"status": "accepted"}


@router.post("/webhook/calendly", status_code=status.HTTP_200_OK)
async def webhook_calendly(request: Request, background_tasks: BackgroundTasks):
    """Receives Calendly invitee.created events and records the booking."""
    try:
        payload = await request.json()
    except Exception:
        return {"error": "Invalid JSON"}

    print(f"[CALENDLY] Webhook received: {payload.get('event', 'unknown')}")
    background_tasks.add_task(process_calendly_booking, payload)
    return {"status": "accepted"}


async def process_calendly_booking(payload: dict):
    try:
        event_name = payload.get("event", "")
        if event_name not in ("invitee.created", ""):
            print(f"[CALENDLY] Ignored event: {event_name}")
            return

        p = payload.get("payload", payload)
        invitee = p.get("invitee", {})
        event = p.get("event", {})

        lead_name = invitee.get("name") or p.get("name") or "Unknown"
        lead_email = invitee.get("email") or p.get("email") or ""
        slot_start = event.get("start_time") or p.get("start_time") or ""
        slot_end = event.get("end_time") or p.get("end_time") or ""
        booking_link = event.get("uri") or p.get("uri") or ""

        print(f"[CALENDLY] New booking: {lead_name} <{lead_email}> @ {slot_start}")

        # Record booking in DB
        await _save_calendly_booking(lead_name, lead_email, slot_start, slot_end, booking_link)

        # Notify organizer
        email_service.send_organizer_booking_notification(
            lead_name=lead_name,
            lead_email=lead_email,
            slot_start=slot_start,
        )

        # Confirm to lead
        if lead_email:
            email_service.send_booking_confirmation_to_lead(
                to_email=lead_email,
                recipient_name=lead_name,
                slot_start=slot_start,
            )

    except Exception as e:
        print(f"[CALENDLY] Error processing booking: {e}")


async def _save_calendly_booking(lead_name: str, lead_email: str, slot_start: str, slot_end: str, booking_link: str):
    try:
        from app.db.session import SessionLocal
        from app.db.models import Lead, Booking, Organizer
        from app.db.base import BookingState
        from sqlalchemy import select
        from datetime import datetime, timezone

        async with SessionLocal() as db:
            # Find or create lead
            lead = (await db.execute(select(Lead).where(Lead.email == lead_email))).scalar_one_or_none()
            if not lead:
                lead = Lead(email=lead_email, name=lead_name)
                db.add(lead)
                await db.flush()

            # Find first active organizer
            organizer = (await db.execute(
                select(Organizer).where(Organizer.is_active == True).limit(1)
            )).scalar_one_or_none()

            if not organizer:
                print("[CALENDLY] No active organizer found — booking not saved.")
                return

            def parse_dt(s):
                if not s:
                    return None
                try:
                    return datetime.fromisoformat(s.replace("Z", "+00:00"))
                except Exception:
                    return None

            # Update existing booking if present (from form submit), else create new
            existing = (await db.execute(
                select(Booking).where(Booking.lead_id == lead.id)
                .order_by(Booking.created_at.desc()).limit(1)
            )).scalar_one_or_none()

            if existing:
                existing.state = BookingState.BOOKING_CONFIRMED
                existing.slot_start = parse_dt(slot_start)
                existing.slot_end = parse_dt(slot_end)
                existing.booking_link = booking_link or existing.booking_link
                print(f"[CALENDLY] Booking updated to confirmed for {lead_email}")
            else:
                booking = Booking(
                    lead_id=lead.id,
                    organizer_id=organizer.id,
                    state=BookingState.BOOKING_CONFIRMED,
                    slot_start=parse_dt(slot_start),
                    slot_end=parse_dt(slot_end),
                    booking_link=booking_link,
                )
                db.add(booking)
                print(f"[CALENDLY] New booking saved to DB for {lead_email}")

            await db.commit()

    except Exception as e:
        print(f"[CALENDLY] DB save error: {e}")


@router.get("/form", response_class=HTMLResponse)
async def get_form(request: Request):
    return templates.TemplateResponse("form.html", {"request": request})


@router.post("/submit", response_class=HTMLResponse)
async def post_submit(
    request: Request,
    background_tasks: BackgroundTasks,
    name: str = Form(...),
    email: str = Form(...),
    timezone: str = Form(...),
):
    print(f"[FORM] New submission: name={name}, email={email}, timezone={timezone}")

    # Persist lead + booking to DB
    try:
        from app.db.session import SessionLocal
        from app.db.models import Lead, Booking, Organizer
        from app.db.base import BookingState
        from sqlalchemy import select as sa_select
        async with SessionLocal() as db:
            # Upsert lead
            lead = (await db.execute(
                sa_select(Lead).where(Lead.email == email)
            )).scalar_one_or_none()
            if not lead:
                lead = Lead(email=email, name=name, timezone=timezone)
                db.add(lead)
                await db.flush()
            else:
                lead.name = name
                lead.timezone = timezone

            # Find first active organizer
            organizer = (await db.execute(
                sa_select(Organizer).where(Organizer.is_active == True).limit(1)
            )).scalar_one_or_none()

            if organizer:
                # Only create a booking if one doesn't already exist for this lead
                existing = (await db.execute(
                    sa_select(Booking).where(Booking.lead_id == lead.id)
                    .order_by(Booking.created_at.desc()).limit(1)
                )).scalar_one_or_none()

                if not existing:
                    booking = Booking(
                        lead_id=lead.id,
                        organizer_id=organizer.id,
                        state=BookingState.EMAIL_SENT,
                        booking_link=settings.CALENDLY_LINK,
                    )
                    db.add(booking)
                    print(f"[FORM] Booking record created for {email}")
                else:
                    print(f"[FORM] Existing booking found for {email}, skipping creation")
            else:
                print("[FORM] No active organizer found — booking not created")

            await db.commit()
            print(f"[FORM] Lead saved to DB: {email}")
    except Exception as exc:
        print(f"[FORM] DB save error (non-fatal): {exc}")

    # Send booking email to lead
    background_tasks.add_task(email_service.send_booking_email, to_email=email, recipient_name=name)

    # Notify admin
    background_tasks.add_task(
        email_service.send_admin_notification,
        lead_name=name,
        lead_email=email,
        lead_timezone=timezone,
    )

    return templates.TemplateResponse(
        "thankyou.html",
        {
            "request": request,
            "email": email,
            "calendly_link": settings.CALENDLY_LINK,
        },
    )
