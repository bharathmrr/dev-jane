from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, EmailStr
from sqlalchemy import select
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


class EmailPreviewRequest(BaseModel):
    lead_name: str
    lead_email: EmailStr
    organizer_id: uuid.UUID


class EmailPreviewResponse(BaseModel):
    subject: str
    body: str
    from_email: str
    to_email: str
    organizer_name: str
    slots_count: int


@router.post("/leads/preview", response_model=EmailPreviewResponse)
async def preview_offer_email(
    payload: EmailPreviewRequest,
    db: AsyncSession = Depends(get_db),
    _: User = Depends(require_roles(UserRole.ADMIN, UserRole.ORGANIZER, UserRole.AGENT)),
) -> EmailPreviewResponse:
    """Return a preview of the scheduling email without sending it."""
    from app.core.config import settings
    from app.services.notifications import templates

    organizer = await db.get(Organizer, payload.organizer_id)
    if not organizer or not organizer.is_active:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Organizer not found")

    slots = await offerable_slots(db, organizer, limit=5)
    if not slots:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            "Organizer has no available slots. Set up availability first."
        )

    payloads = [slot_to_payload(s, organizer.timezone) for s in slots]
    body = templates.render_offer(payload.lead_name, organizer.display_name, payloads)

    return EmailPreviewResponse(
        subject=f"Available times with {organizer.display_name}",
        body=body,
        from_email=settings.SMTP_FROM_EMAIL,
        to_email=payload.lead_email,
        organizer_name=organizer.display_name,
        slots_count=len(slots),
    )


@router.post(
    "/leads", response_model=BookingOut, status_code=status.HTTP_201_CREATED
)
async def create_lead(
    payload: LeadCreate,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(require_roles(UserRole.ADMIN, UserRole.ORGANIZER, UserRole.AGENT)),
) -> Booking:
    organizer = await db.get(Organizer, payload.organizer_id)
    if not organizer or not organizer.is_active:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Organizer not found")
    if user.role == UserRole.ORGANIZER:
        my_org = (
            await db.execute(select(Organizer).where(Organizer.user_id == user.id))
        ).scalar_one_or_none()
        if not my_org or my_org.id != payload.organizer_id:
            raise HTTPException(status.HTTP_403_FORBIDDEN, "You can only send emails as yourself")

    booking = await booking_svc.create_lead_and_booking(
        db, name=payload.name, email=payload.email, organizer=organizer
    )

    # Send email synchronously (no Celery needed).
    # Falls back gracefully if SMTP fails — booking is still created.
    try:
        from app.workers.email_tasks import _send_offer
        await _send_offer(db, str(booking.id))
    except Exception as exc:
        import logging
        logging.getLogger("app").error(f"Email send failed: {exc}")

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
    user: User = Depends(require_roles(UserRole.ADMIN, UserRole.ORGANIZER, UserRole.AGENT)),
) -> Booking:
    booking = await db.get(Booking, booking_id)
    if not booking:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Booking not found")
    if user.role == UserRole.ORGANIZER:
        my_org = (
            await db.execute(select(Organizer).where(Organizer.user_id == user.id))
        ).scalar_one_or_none()
        if not my_org or booking.organizer_id != my_org.id:
            raise HTTPException(status.HTTP_403_FORBIDDEN, "You can only cancel your own bookings")
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
