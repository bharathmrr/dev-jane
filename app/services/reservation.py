"""Reservation service — the BookMyShow-style slot hold.

Sequence when a lead confirms a slot:
  1. Acquire a Redis lock keyed on (organizer, slot_start) — *fail fast*, so the
     first concurrent confirmer wins and the rest are told to pick again.
  2. Inside the lock, re-check the DB for an active booking/hold on that slot
     (defense in depth; the DB partial-unique index is the final arbiter).
  3. Insert a Reservation row (status=HELD, expires_at=now+TTL) and move the
     booking to SLOT_RESERVED.
  4. Release the Redis lock. The hold persists in Postgres + a Redis TTL key
     until confirmed or expired (a Celery beat job sweeps expired holds).

This keeps the critical section tiny (just the DB write) while the 10-minute
*hold* outlives the lock.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.logging import get_logger
from app.core.redis_client import LockNotAcquired, distributed_lock, get_redis
from app.db.base import BookingState, ReservationStatus
from app.db.models import Booking, Reservation
from app.state_machine import assert_transition

log = get_logger(__name__)


class SlotUnavailable(Exception):
    """The requested slot is already held or booked by someone else."""


def _slot_key(organizer_id, slot_start: datetime) -> str:
    return f"slot:{organizer_id}:{int(slot_start.timestamp())}"


async def reserve_slot(
    db: AsyncSession,
    booking: Booking,
    slot_start: datetime,
    slot_end: datetime,
    idempotency_key: str | None = None,
) -> Reservation:
    organizer_id = booking.organizer_id
    key = _slot_key(organizer_id, slot_start)
    idem = idempotency_key or f"{booking.id}:{int(slot_start.timestamp())}"

    # Idempotency: a retried reply for the same slot returns the existing hold.
    existing = (
        await db.execute(
            select(Reservation).where(Reservation.idempotency_key == idem)
        )
    ).scalar_one_or_none()
    if existing and existing.status == ReservationStatus.HELD:
        return existing

    try:
        async with distributed_lock(key, ttl_seconds=settings.LOCK_TIMEOUT_SECONDS):
            # Re-check for a live hold/booking now that we hold the lock.
            clash = (
                await db.execute(
                    select(Reservation).where(
                        Reservation.organizer_id == organizer_id,
                        Reservation.slot_start == slot_start,
                        Reservation.status == ReservationStatus.HELD,
                        Reservation.expires_at > datetime.now(timezone.utc),
                    )
                )
            ).scalar_one_or_none()
            if clash and clash.booking_id != booking.id:
                raise SlotUnavailable(key)

            expires = datetime.now(timezone.utc) + timedelta(
                seconds=settings.RESERVATION_TTL_SECONDS
            )
            reservation = Reservation(
                id=uuid.uuid4(),
                booking_id=booking.id,
                organizer_id=organizer_id,
                slot_start=slot_start,
                slot_end=slot_end,
                status=ReservationStatus.HELD,
                expires_at=expires,
                idempotency_key=idem,
            )
            db.add(reservation)

            assert_transition(booking.state, BookingState.SLOT_RESERVED)
            booking.state = BookingState.SLOT_RESERVED
            booking.slot_start = slot_start
            booking.slot_end = slot_end

            try:
                await db.flush()
            except IntegrityError as exc:
                # Partial-unique index tripped => someone confirmed first.
                await db.rollback()
                log.info("slot_race_lost", key=key, error=str(exc))
                raise SlotUnavailable(key) from exc

            # Mirror a TTL marker in Redis for fast availability filtering.
            await get_redis().set(
                f"hold:{key}", str(booking.id),
                ex=settings.RESERVATION_TTL_SECONDS,
            )
            log.info("slot_reserved", booking_id=str(booking.id), key=key)
            return reservation

    except LockNotAcquired as exc:
        raise SlotUnavailable(key) from exc


async def confirm_reservation(db: AsyncSession, booking: Booking) -> None:
    res = (
        await db.execute(
            select(Reservation).where(
                Reservation.booking_id == booking.id,
                Reservation.status == ReservationStatus.HELD,
            )
        )
    ).scalar_one_or_none()
    if not res:
        raise SlotUnavailable("no_active_hold")
    if res.expires_at <= datetime.now(timezone.utc):
        res.status = ReservationStatus.EXPIRED
        raise SlotUnavailable("hold_expired")

    res.status = ReservationStatus.CONFIRMED
    assert_transition(booking.state, BookingState.BOOKING_CONFIRMED)
    booking.state = BookingState.BOOKING_CONFIRMED
    await db.flush()
    log.info("booking_confirmed", booking_id=str(booking.id))


async def release_reservation(db: AsyncSession, booking: Booking) -> None:
    res = (
        await db.execute(
            select(Reservation).where(
                Reservation.booking_id == booking.id,
                Reservation.status == ReservationStatus.HELD,
            )
        )
    ).scalar_one_or_none()
    if res:
        res.status = ReservationStatus.RELEASED
        await get_redis().delete(f"hold:{_slot_key(booking.organizer_id, res.slot_start)}")
        await db.flush()
