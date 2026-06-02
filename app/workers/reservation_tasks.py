"""Maintenance task: expire stale slot holds and re-offer to the lead."""
from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.logging import get_logger
from app.db.base import BookingState, ReservationStatus
from app.db.models import Booking, Reservation
from app.services import booking as booking_svc
from app.workers.celery_app import celery_app
from app.workers.runtime import run_async

log = get_logger(__name__)


async def _sweep(db: AsyncSession) -> int:
    now = datetime.now(timezone.utc)
    rows = (
        await db.execute(
            select(Reservation).where(
                Reservation.status == ReservationStatus.HELD,
                Reservation.expires_at <= now,
            )
        )
    ).scalars().all()

    count = 0
    for res in rows:
        res.status = ReservationStatus.EXPIRED
        booking = await db.get(Booking, res.booking_id)
        if booking and booking.state == BookingState.SLOT_RESERVED:
            # Slot frees up; move the lead back to negotiation and re-offer.
            await booking_svc.transition(db, booking, BookingState.WAITING_FOR_REPLY)
            from app.workers.email_tasks import send_offer_email

            send_offer_email.delay(str(booking.id))
        count += 1
    if count:
        log.info("reservations_expired", count=count)
    return count


@celery_app.task
def sweep_expired_reservations() -> int:
    return run_async(_sweep)
