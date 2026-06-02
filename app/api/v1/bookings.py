from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.deps import get_current_user, require_roles
from app.db.base import BookingState, UserRole
from app.db.models import Booking, Organizer, User
from app.db.session import get_db
from app.schemas import BookingOut, LeadCreate, SlotOut
from app.services import booking as booking_svc
from app.services.availability import offerable_slots
from app.services.timeutil import slot_to_payload

router = APIRouter(tags=["bookings"])


@router.post(
    "/leads", response_model=BookingOut, status_code=status.HTTP_201_CREATED
)
async def create_lead(
    payload: LeadCreate,
    db: AsyncSession = Depends(get_db),
    _: User = Depends(require_roles(UserRole.ADMIN, UserRole.AGENT)),
) -> Booking:
    organizer = await db.get(Organizer, payload.organizer_id)
    if not organizer or not organizer.is_active:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Organizer not found")
    booking = await booking_svc.create_lead_and_booking(
        db, name=payload.name, email=payload.email, organizer=organizer
    )
    # Kick off the first offer email asynchronously.
    from app.workers.email_tasks import send_offer_email

    send_offer_email.delay(str(booking.id))
    return booking


@router.get("/bookings/{booking_id}", response_model=BookingOut)
async def get_booking(
    booking_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    _: User = Depends(get_current_user),
) -> Booking:
    booking = await db.get(Booking, booking_id)
    if not booking:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Booking not found")
    return booking


@router.post("/bookings/{booking_id}/cancel", response_model=BookingOut)
async def cancel_booking(
    booking_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(require_roles(UserRole.ADMIN, UserRole.AGENT)),
) -> Booking:
    booking = await db.get(Booking, booking_id)
    if not booking:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Booking not found")
    from app.services.reservation import release_reservation

    await release_reservation(db, booking)
    await booking_svc.transition(db, booking, BookingState.CANCELLED, actor=user.email)
    return booking


@router.get("/organizers/{organizer_id}/slots", response_model=list[SlotOut])
async def list_slots(
    organizer_id: uuid.UUID,
    limit: int = 5,
    db: AsyncSession = Depends(get_db),
    _: User = Depends(get_current_user),
) -> list[dict]:
    organizer = await db.get(Organizer, organizer_id)
    if not organizer:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Organizer not found")
    slots = await offerable_slots(db, organizer, limit=limit)
    return [slot_to_payload(s, organizer.timezone) for s in slots]
