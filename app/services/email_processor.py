"""Email processor — the decision core that turns an inbound reply into action.

Given a persisted inbound EmailMessage on a thread, it:
  1. Classifies intent (LLM) against the slots most recently offered.
  2. Routes by intent:
       confirm_slot     -> reserve the chosen slot (first-wins); on race, offer
                           nearest alternatives.
       reject_slots     -> offer a fresh batch of slots.
       suggest_new_time -> (LLM/date-parse the phrase →) try to honor it, else
                           offer nearest alternatives.
       reschedule       -> move a confirmed booking back to negotiation.
       cancel           -> release any hold and cancel.
       general_query    -> flag for human handoff (no automated action).
  3. Records the outbound response to send (returned as an "action" the caller's
     Celery task executes via SMTP) and audits everything.

This function is transport-agnostic: it returns an OutboundAction describing the
email to send rather than sending it, which keeps it unit-testable.
"""
from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.logging import get_logger
from app.db.base import BookingState, EmailIntent
from app.db.models import Booking, EmailMessage, EmailThread, Lead, Organizer
from app.services import booking as booking_svc
from app.services.conflict_resolver import alternatives_near
from app.services.email_intent import classify_reply
from app.services.notifications import templates
from app.services.reservation import SlotUnavailable, release_reservation, reserve_slot
from app.services.timeutil import parse_iso, slot_to_payload

log = get_logger(__name__)


@dataclass
class OutboundAction:
    to_addr: str
    subject: str
    body: str
    reply_token: str
    in_reply_to: str | None


async def _lead_and_org(db: AsyncSession, booking: Booking) -> tuple[Lead, Organizer, str]:
    lead = await db.get(Lead, booking.lead_id)
    org = await db.get(Organizer, booking.organizer_id)
    tz = lead.timezone or org.timezone
    return lead, org, tz


async def process_inbound(
    db: AsyncSession, message: EmailMessage
) -> OutboundAction | None:
    thread = await db.get(EmailThread, message.thread_id)
    booking = await db.get(Booking, thread.booking_id)
    lead, org, tz = await _lead_and_org(db, booking)

    result = await classify_reply(message.body_text or "", booking.offered_slots)
    message.intent = result.intent
    message.intent_payload = result.model_dump(mode="json")
    message.processed = True
    await booking_svc.audit(
        db, lead.email, "email.classified", "email_message", message.id,
        message.intent_payload,
    )

    handler = {
        EmailIntent.CONFIRM_SLOT: _handle_confirm,
        EmailIntent.REJECT_SLOTS: _handle_reoffer,
        EmailIntent.SUGGEST_NEW_TIME: _handle_suggest,
        EmailIntent.RESCHEDULE: _handle_reschedule,
        EmailIntent.CANCEL: _handle_cancel,
    }.get(result.intent, _handle_general)

    return await handler(db, booking, lead, org, tz, result, thread)


async def _handle_confirm(db, booking, lead, org, tz, result, thread):
    idx = result.selected_slot_index
    if idx is None or not booking.offered_slots:
        return await _handle_general(db, booking, lead, org, tz, result, thread)

    chosen = booking.offered_slots[idx - 1]
    start, end = parse_iso(chosen["start"]), parse_iso(chosen["end"])

    if booking.state == BookingState.WAITING_FOR_REPLY:
        await booking_svc.transition(db, booking, BookingState.SLOT_SELECTED)

    try:
        await reserve_slot(db, booking, start, end)
    except SlotUnavailable:
        return await _offer_alternatives(db, booking, lead, org, tz, thread, start)

    # Hold acquired → confirm immediately (reservation-first then confirm).
    from app.services.reservation import confirm_reservation

    await confirm_reservation(db, booking)
    # Use organizer's pre-set link or auto-generate a Jitsi room
    meeting_link = org.meeting_link or f"https://meet.jit.si/meeting-{uuid.uuid4().hex[:10]}"
    booking.booking_link = meeting_link  # persist so dashboard can display it
    body = templates.render_confirmation(
        lead.name, org.display_name, chosen["label"], meeting_link
    )
    return OutboundAction(
        lead.email, f"Confirmed: {chosen['label']}", body,
        thread.reply_token, thread.root_message_id,
    )


async def _offer_alternatives(db, booking, lead, org, tz, thread, requested_start):
    alts = await alternatives_near(db, org, requested_start, count=3)
    payloads = [slot_to_payload(s, tz) for s in alts]
    booking.offered_slots = payloads
    if booking.state != BookingState.WAITING_FOR_REPLY:
        await booking_svc.transition(db, booking, BookingState.WAITING_FOR_REPLY)
    body = templates.render_alternatives(lead.name, payloads)
    return OutboundAction(
        lead.email, "A few other times", body,
        thread.reply_token, thread.root_message_id,
    )


async def _handle_reoffer(db, booking, lead, org, tz, result, thread):
    payloads = await booking_svc.prepare_offer(db, booking, limit=5)
    body = templates.render_offer(lead.name, org.display_name, payloads)
    return OutboundAction(
        lead.email, "More available times", body,
        thread.reply_token, thread.root_message_id,
    )


async def _handle_suggest(db, booking, lead, org, tz, result, thread):
    # A production system would date-parse result.proposed_datetime_text (e.g.
    # via dateparser in the lead's tz) and try to reserve the exact slot. Here we
    # offer the nearest available options to the soonest window as a safe default.
    return await _offer_alternatives(db, booking, lead, org, tz, thread, None)


async def _handle_reschedule(db, booking, lead, org, tz, result, thread):
    if booking.state in (BookingState.BOOKING_CONFIRMED, BookingState.REMINDER_SENT):
        await release_reservation(db, booking)
        await booking_svc.transition(db, booking, BookingState.RESCHEDULED)
        await booking_svc.transition(db, booking, BookingState.WAITING_FOR_REPLY)
    payloads = await booking_svc.prepare_offer(db, booking, limit=5)
    body = templates.render_offer(lead.name, org.display_name, payloads)
    return OutboundAction(
        lead.email, "Let's find a new time", body,
        thread.reply_token, thread.root_message_id,
    )


async def _handle_cancel(db, booking, lead, org, tz, result, thread):
    await release_reservation(db, booking)
    await booking_svc.transition(db, booking, BookingState.CANCELLED)
    booking.cancel_reason = "lead_cancelled_via_email"
    body = (
        f"Hi {lead.name},\n\nNo problem — I've cancelled the request. "
        "Reply any time if you'd like to reschedule."
    )
    return OutboundAction(
        lead.email, "Cancelled", body, thread.reply_token, thread.root_message_id,
    )


async def _handle_general(db, booking, lead, org, tz, result, thread):
    # No safe automated action — flag for a human. We deliberately do NOT send an
    # auto-reply that could loop; instead we audit and let an agent pick it up.
    await booking_svc.audit(
        db, "system", "email.handoff", "booking", booking.id,
        {"reason": "general_query_or_low_confidence", "confidence": result.confidence},
    )
    log.info("email_handoff", booking_id=str(booking.id))
    return None
