"""Helpers for rendering UTC slots into human-friendly local strings."""
from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

from app.services.slot_generator import Slot


def slot_label(slot: Slot, tz_name: str) -> str:
    tz = ZoneInfo(tz_name)
    local = slot.start.astimezone(tz)
    # e.g. "Mon 03 Jun, 10:00 AM (IST)"
    return local.strftime("%a %d %b, %I:%M %p (%Z)")


def slot_to_payload(slot: Slot, tz_name: str) -> dict:
    return {
        "start": slot.start.isoformat(),
        "end": slot.end.isoformat(),
        "label": slot_label(slot, tz_name),
    }


def parse_iso(value: str) -> datetime:
    return datetime.fromisoformat(value)


def slot_label_from_iso(start_iso: str, tz_name: str) -> str:
    tz = ZoneInfo(tz_name)
    local = datetime.fromisoformat(start_iso).astimezone(tz)
    return local.strftime("%a %d %b, %I:%M %p (%Z)")
