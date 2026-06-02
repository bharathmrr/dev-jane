"""Availability service.

Bridges the database to the pure slot generator: loads an organizer's rules,
holidays and busy intervals (confirmed bookings + active reservations) and
returns the next N offerable slots.
"""
from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.base import BookingState, ReservationStatus
from app.db.models import (
    AvailabilityRule,
    Booking,
    Holiday,
    Organizer,
    Reservation,
)
from app.services.slot_generator import Slot, WeeklyRule, generate_slots

_BLOCKING_STATES = (
    BookingState.SLOT_RESERVED,
    BookingState.BOOKING_CONFIRMED,
    BookingState.REMINDER_SENT,
    BookingState.MEETING_COMPLETED,
)


async def busy_intervals(
    db: AsyncSession, organizer_id, now: datetime
) -> list[tuple[datetime, datetime]]:
    intervals: list[tuple[datetime, datetime]] = []

    booked = await db.execute(
        select(Booking.slot_start, Booking.slot_end).where(
            Booking.organizer_id == organizer_id,
            Booking.state.in_(_BLOCKING_STATES),
            Booking.slot_start.is_not(None),
        )
    )
    intervals += [(s, e) for s, e in booked.all() if s and e]

    held = await db.execute(
        select(Reservation.slot_start, Reservation.slot_end).where(
            Reservation.organizer_id == organizer_id,
            Reservation.status == ReservationStatus.HELD,
            Reservation.expires_at > now,
        )
    )
    intervals += [(s, e) for s, e in held.all()]
    return intervals


async def offerable_slots(
    db: AsyncSession,
    organizer: Organizer,
    *,
    limit: int = 5,
    now: datetime | None = None,
) -> list[Slot]:
    now = now or datetime.now(timezone.utc)

    rules_rows = (
        await db.execute(
            select(AvailabilityRule).where(
                AvailabilityRule.organizer_id == organizer.id
            )
        )
    ).scalars().all()
    rules = [
        WeeklyRule(weekday=r.weekday, start=r.start_time, end=r.end_time)
        for r in rules_rows
    ]

    holiday_rows = (
        await db.execute(
            select(Holiday.day).where(Holiday.organizer_id == organizer.id)
        )
    ).all()
    holidays = {d for (d,) in holiday_rows}

    busy = await busy_intervals(db, organizer.id, now)

    slots = generate_slots(
        tz_name=organizer.timezone,
        rules=rules,
        holidays=holidays,
        duration_minutes=organizer.default_meeting_minutes,
        buffer_minutes=organizer.buffer_minutes,
        horizon_days=organizer.booking_horizon_days,
        busy=busy,
        now=now,
    )
    return slots[:limit]
