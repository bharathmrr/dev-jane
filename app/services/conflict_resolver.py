"""Conflict resolution & negotiation.

When a requested slot is unavailable (race lost, or a free-text suggestion that
doesn't fit availability), find the closest available alternatives to offer
back. "Closest" = smallest absolute distance from the requested start time,
preferring same-day then nearest day.
"""
from __future__ import annotations

from datetime import datetime

from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Organizer
from app.services.availability import offerable_slots
from app.services.slot_generator import Slot


async def alternatives_near(
    db: AsyncSession,
    organizer: Organizer,
    requested_start: datetime | None,
    *,
    count: int = 3,
    pool: int = 30,
) -> list[Slot]:
    """Return up to `count` open slots nearest to `requested_start`.

    If `requested_start` is None (e.g. a vague "got anything else?"), just return
    the soonest open slots.
    """
    candidates = await offerable_slots(db, organizer, limit=pool)
    if not candidates:
        return []
    if requested_start is None:
        return candidates[:count]

    def distance(s: Slot) -> tuple[int, float]:
        same_day = 0 if s.start.date() == requested_start.date() else 1
        return (same_day, abs((s.start - requested_start).total_seconds()))

    return sorted(candidates, key=distance)[:count]
