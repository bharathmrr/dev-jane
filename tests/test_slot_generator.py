"""Unit tests for the pure slot generator (no DB / no network)."""
from __future__ import annotations

from datetime import date, datetime, time, timezone

from app.services.slot_generator import WeeklyRule, generate_slots


def _rule(weekday: int) -> WeeklyRule:
    return WeeklyRule(weekday=weekday, start=time(9, 0), end=time(11, 0))


def test_generates_back_to_back_slots():
    # A Monday far in the future to keep the test deterministic.
    now = datetime(2030, 1, 6, 0, 0, tzinfo=timezone.utc)  # 2030-01-06 is a Sunday
    slots = generate_slots(
        tz_name="UTC",
        rules=[_rule(0)],  # Monday 09:00-11:00 => two 30-min... but dur=60 => two 60-min
        holidays=set(),
        duration_minutes=60,
        buffer_minutes=0,
        horizon_days=2,
        busy=[],
        now=now,
    )
    # Monday is 2030-01-07. Two 1h slots: 09:00 and 10:00.
    assert len(slots) == 2
    assert slots[0].start.hour == 9
    assert slots[1].start.hour == 10


def test_busy_interval_blocks_slot():
    now = datetime(2030, 1, 6, 0, 0, tzinfo=timezone.utc)
    busy = [
        (
            datetime(2030, 1, 7, 9, 0, tzinfo=timezone.utc),
            datetime(2030, 1, 7, 10, 0, tzinfo=timezone.utc),
        )
    ]
    slots = generate_slots(
        tz_name="UTC",
        rules=[_rule(0)],
        holidays=set(),
        duration_minutes=60,
        buffer_minutes=0,
        horizon_days=2,
        busy=busy,
        now=now,
    )
    assert all(s.start.hour != 9 for s in slots)
    assert any(s.start.hour == 10 for s in slots)


def test_holiday_excludes_day():
    now = datetime(2030, 1, 6, 0, 0, tzinfo=timezone.utc)
    slots = generate_slots(
        tz_name="UTC",
        rules=[_rule(0)],
        holidays={date(2030, 1, 7)},
        duration_minutes=60,
        buffer_minutes=0,
        horizon_days=2,
        busy=[],
        now=now,
    )
    assert slots == []


def test_timezone_offset_applied():
    # 09:00 in Asia/Kolkata (UTC+5:30) should be 03:30 UTC.
    now = datetime(2030, 1, 6, 0, 0, tzinfo=timezone.utc)
    slots = generate_slots(
        tz_name="Asia/Kolkata",
        rules=[WeeklyRule(weekday=0, start=time(9, 0), end=time(10, 0))],
        holidays=set(),
        duration_minutes=60,
        buffer_minutes=0,
        horizon_days=2,
        busy=[],
        now=now,
    )
    assert len(slots) == 1
    assert slots[0].start.hour == 3 and slots[0].start.minute == 30
