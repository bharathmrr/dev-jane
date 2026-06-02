"""Slot generation.

Pure-ish computation: given an organizer's weekly availability rules, holidays,
meeting duration/buffer, and a set of already-busy intervals, produce a list of
concrete bookable slots in UTC over the booking horizon.

Timezone handling: availability rules are defined in the organizer's local
timezone (so "09:00–17:00 Mon" survives DST shifts correctly). We localize each
candidate day in that zone using zoneinfo, then convert to UTC for storage and
conflict checks. DST gaps/folds are handled by zoneinfo's fold semantics.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo


@dataclass(frozen=True)
class Slot:
    start: datetime  # tz-aware UTC
    end: datetime  # tz-aware UTC

    def overlaps(self, other_start: datetime, other_end: datetime) -> bool:
        return self.start < other_end and other_start < self.end


@dataclass(frozen=True)
class WeeklyRule:
    weekday: int  # 0=Mon..6=Sun
    start: time
    end: time


def _daterange(start: date, days: int):
    for i in range(days):
        yield start + timedelta(days=i)


def generate_slots(
    *,
    tz_name: str,
    rules: list[WeeklyRule],
    holidays: set[date],
    duration_minutes: int,
    buffer_minutes: int,
    horizon_days: int,
    busy: list[tuple[datetime, datetime]],
    now: datetime | None = None,
    granularity_minutes: int | None = None,
) -> list[Slot]:
    """Return open slots in UTC, soonest first.

    Args mirror an organizer's configuration. `busy` is the list of existing
    (start,end) UTC intervals that block slots (confirmed bookings + active
    reservations). `granularity_minutes` controls slot step (defaults to
    duration, i.e. back-to-back slots).
    """
    tz = ZoneInfo(tz_name)
    now = now or datetime.now(timezone.utc)
    step = timedelta(minutes=granularity_minutes or duration_minutes)
    dur = timedelta(minutes=duration_minutes)
    buf = timedelta(minutes=buffer_minutes)

    rules_by_day: dict[int, list[WeeklyRule]] = {}
    for r in rules:
        rules_by_day.setdefault(r.weekday, []).append(r)

    today_local = now.astimezone(tz).date()
    slots: list[Slot] = []

    for day in _daterange(today_local, horizon_days):
        if day in holidays:
            continue
        for rule in rules_by_day.get(day.weekday(), []):
            # Localize window edges in the organizer's tz, then go to UTC.
            window_start = datetime.combine(day, rule.start, tzinfo=tz)
            window_end = datetime.combine(day, rule.end, tzinfo=tz)
            cursor = window_start
            while cursor + dur <= window_end:
                s_utc = cursor.astimezone(timezone.utc)
                e_utc = (cursor + dur).astimezone(timezone.utc)
                cursor += step
                if s_utc <= now:  # never offer past/imminent slots
                    continue
                slot = Slot(s_utc, e_utc)
                # Apply buffer when comparing against busy intervals.
                if any(
                    slot.overlaps(b0 - buf, b1 + buf) for b0, b1 in busy
                ):
                    continue
                slots.append(slot)

    slots.sort(key=lambda s: s.start)
    return slots
