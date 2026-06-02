"""Booking orchestration service.

Owns booking creation, the outbound offer email, and audited state transitions.
Side-effecting email sends are dispatched to Celery so the API stays fast and
sends are retryable.
"""
from __future__ import annotations

import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.logging import get_logger
from app.core.security import sign_thread_token
from app.db.base import BookingState
from app.db.models import (
    AuditLog,
    Booking,
    EmailThread,
    Lead,
    Organizer,
)
from app.services.availability import offerable_slots
from app.services.timeutil import slot_to_payload
from app.state_machine import assert_transition

log = get_logger(__name__)


async def audit(
    db: AsyncSession, actor: str, action: str, entity_type: str, entity_id, data: dict
) -> None:
    db.add(
        AuditLog(
            actor=actor,
            action=action,
            entity_type=entity_type,
            entity_id=str(entity_id),
            data=data,
        )
    )


async def transition(
    db: AsyncSession, booking: Booking, dst: BookingState, actor: str = "system"
) -> None:
    src = booking.state
    assert_transition(src, dst)
    booking.state = dst
    await audit(
        db, actor, "booking.transition", "booking", booking.id,
        {"from": src.value, "to": dst.value},
    )


async def create_lead_and_booking(
    db: AsyncSession, *, name: str, email: str, organizer: Organizer
) -> Booking:
    lead = Lead(id=uuid.uuid4(), name=name, email=email)
    db.add(lead)
    await db.flush()

    booking = Booking(
        id=uuid.uuid4(),
        lead_id=lead.id,
        organizer_id=organizer.id,
        state=BookingState.LEAD_CREATED,
    )
    db.add(booking)
    await db.flush()

    thread = EmailThread(
        id=uuid.uuid4(),
        booking_id=booking.id,
        reply_token=sign_thread_token(str(booking.id)),
        subject=f"Scheduling a meeting with {organizer.display_name}",
    )
    db.add(thread)
    await audit(db, "system", "booking.created", "booking", booking.id, {"email": email})
    await db.flush()
    return booking


async def prepare_offer(
    db: AsyncSession, booking: Booking, *, limit: int = 5
) -> list[dict]:
    """Compute slots, snapshot them on the booking, and return payloads.

    The snapshot (`offered_slots`) is what inbound replies are matched against,
    so "YES 2" maps deterministically to the slot the lead actually saw.
    """
    organizer = await db.get(Organizer, booking.organizer_id)
    lead = await db.get(Lead, booking.lead_id)
    tz = lead.timezone or organizer.timezone
    slots = await offerable_slots(db, organizer, limit=limit)
    payloads = [slot_to_payload(s, tz) for s in slots]
    booking.offered_slots = payloads
    await db.flush()
    return payloads
