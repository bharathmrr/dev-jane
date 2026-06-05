"""3-layer availability engine for the Jane Aerospace V2 pipeline.

Layer 1 – Zoho Bookings API  (source of truth)
  fetch_available_slots() asks Zoho which times are open on a given date.
  Zoho itself already excludes times that have a confirmed booking in its
  own calendar, so this is the authoritative "is this slot physically free?"
  check.

Layer 2 – In-process TTL cache
  Zoho is queried per-day (one HTTP call per weekday).  Results are cached
  in memory for ZOHO_CACHE_TTL seconds (default 300 s / 5 min).  The cache
  is INVALIDATED after every Stage-2 slot dispatch so the very next lead
  always gets a fresh pool.

Layer 3 – Database guard  (our own bookings + held slots)
  Even if Zoho says a slot is free, we skip it if:
    a) It is BOOKED in our DB (LeadV2.status == BOOKED or ZohoSlot.status == BOOKED)
    b) It was already OFFERED to another SENT/REPLIED lead in the last 48 h
       (prevents double-offering the same time to two different people)

Flow per lead:
  get_slots_for_week / get_available_slots
    → Layer 2 (cache hit?) → Layer 1 (Zoho) → Layer 2 (store)
    → Layer 3a (remove booked)
    → Layer 3b (remove held)
    → distribute across dates (round-robin)
    → return first N
"""
from __future__ import annotations

import json
import time as _time
from datetime import datetime, timedelta, timezone, date
from zoneinfo import ZoneInfo
from typing import TypedDict

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from structlog import get_logger

from app.core.config import settings
from app.services.zoho import ZohoBookingsService

logger = get_logger(__name__)

IST = ZoneInfo("Asia/Kolkata")
SLOT_START_HOUR = 9
SLOT_END_HOUR = 18

# ---------------------------------------------------------------------------
# Layer 2 — In-process TTL caches
# ---------------------------------------------------------------------------

_week_slots_cache: dict = {
    "this": {"slots": None, "at": 0.0},
    "next": {"slots": None, "at": 0.0},
}
_general_cache: dict = {"slots": None, "at": 0.0}

# Per-day Zoho raw response cache (avoids re-fetching same date within TTL)
_day_cache: dict[str, tuple[list, float]] = {}


def invalidate_week_cache(week: str | None = None) -> None:
    """Bust the cache after a Stage-2 slot dispatch.

    This guarantees the next lead gets a fresh Zoho fetch so the just-offered
    slots are not shown again.  Pass week=None to bust both weeks.
    """
    targets = [week] if week else ["this", "next"]
    for w in targets:
        if w in _week_slots_cache:
            _week_slots_cache[w] = {"slots": None, "at": 0.0}
    _general_cache["slots"] = None
    _general_cache["at"] = 0.0
    logger.info("week_cache_invalidated", targets=targets)


# ---------------------------------------------------------------------------
# Shared type
# ---------------------------------------------------------------------------

class SlotInfo(TypedDict):
    display: str   # "Monday, Jun 07 at 09:00 AM"
    date_str: str  # "07-Jun-2026"
    time_str: str  # "09:00"
    iso: str       # "2026-06-07T09:00:00+05:30"


# ---------------------------------------------------------------------------
# Layer 1 helpers — Zoho fetch (with per-day cache)
# ---------------------------------------------------------------------------

def _parse_zoho_slot_hour(slot_item: dict | str) -> int | None:
    """Extract the 24h hour (int) from a Zoho slot item.

    Zoho returns times like '09:30 AM', '05:00 PM'.
    Must try 12h format FIRST (before stripping AM/PM) to get correct hour.
    """
    try:
        raw = (
            slot_item.get("time") or slot_item.get("start_time") or slot_item.get("from_time") or ""
            if isinstance(slot_item, dict) else slot_item
        )
        raw = raw.strip()
        if not raw:
            return None
        # Try 12-hour format first (e.g. "05:00 PM" → 17, "09:30 AM" → 9)
        for fmt in ("%I:%M %p", "%I:%M:%S %p"):
            try:
                return datetime.strptime(raw, fmt).hour
            except ValueError:
                pass
        # Fall back to 24-hour format (e.g. "14:00", "09:30:00")
        for fmt in ("%H:%M", "%H:%M:%S"):
            try:
                return datetime.strptime(raw.split()[0], fmt).hour
            except ValueError:
                pass
    except Exception:
        pass
    return None


def _zoho_available_hours_for_day(day: date) -> set[int] | None:
    """Return the set of available hours on `day` from Zoho (Layer 1 + Layer 2 per-day cache).

    Returns:
        set[int]  — hours reported by Zoho as free
        None      — Zoho is configured but returned empty (treat as fully blocked)
    If Zoho is not configured (no service_id), returns all hours 9-19 as open.
    """
    ttl = settings.ZOHO_CACHE_TTL
    date_str = day.strftime("%d-%b-%Y")

    # Layer 2 per-day cache hit
    if date_str in _day_cache:
        cached_items, cached_at = _day_cache[date_str]
        if _time.time() < cached_at:
            items = cached_items
        else:
            del _day_cache[date_str]
            items = None
    else:
        items = None

    if items is None:
        # Layer 1 — real Zoho call
        zoho = ZohoBookingsService()
        if not zoho.service_id:
            # Zoho not configured → assume all slots open (dev/test mode)
            return set(range(SLOT_START_HOUR, SLOT_END_HOUR + 1))
        try:
            items = zoho.fetch_available_slots(date_str)
            logger.info("zoho_fetch_ok", date=date_str, count=len(items))
        except Exception as exc:
            logger.warning("zoho_fetch_failed", date=date_str, error=str(exc))
            return None
        # Store in per-day cache
        _day_cache[date_str] = (items, _time.time() + ttl)

    if not items:
        # Zoho is configured and says nothing is free
        return None

    hours = {h for item in items if (h := _parse_zoho_slot_hour(item)) is not None}
    return hours if hours else None


def _build_slot_info(day: date, hour: int) -> SlotInfo:
    slot_dt = datetime(day.year, day.month, day.day, hour, 0, tzinfo=IST)
    return SlotInfo(
        display=slot_dt.strftime("%A, %b %d at %I:%M %p"),
        date_str=slot_dt.strftime("%d-%b-%Y"),
        time_str=slot_dt.strftime("%H:%M"),
        iso=slot_dt.isoformat(),
    )


# ---------------------------------------------------------------------------
# Layer 3 helpers — DB guard
# ---------------------------------------------------------------------------

async def _get_booked_slots(db: AsyncSession) -> tuple[set[str], set[str]]:
    """Return (iso_set, display_set) of confirmed bookings from our DB.

    Covers both ZohoSlot table (BOOKED status) and LeadV2 table (selected_slot
    for BOOKED leads).  These are HARD exclusions — never offer them again.
    """
    from app.db.models import ZohoSlot, LeadV2, SlotStatus, LeadStatus

    zoho_result = await db.execute(
        select(ZohoSlot.slot_time).where(ZohoSlot.status == SlotStatus.BOOKED)
    )
    booked_iso: set[str] = set()
    for (slot_time,) in zoho_result:
        if slot_time:
            if slot_time.tzinfo is None:
                slot_time = slot_time.replace(tzinfo=timezone.utc)
            booked_iso.add(slot_time.astimezone(IST).isoformat())

    lead_result = await db.execute(
        select(LeadV2.selected_slot).where(
            LeadV2.status == LeadStatus.BOOKED,
            LeadV2.selected_slot.is_not(None),
        )
    )
    booked_display: set[str] = {r[0] for r in lead_result if r[0]}
    return booked_iso, booked_display


async def _get_held_slots(db: AsyncSession, exclude_lead_id=None) -> set[str]:
    """ISO set of slots offered to leads in the last 2 h (soft hold window).

    Slots are held for 2h after being offered so two leads never see the
    same slot simultaneously. After 2h, unreserved slots return to the pool.
    `replied_at` is always set when slots are offered (both week-click and reply).
    """
    from sqlalchemy import or_
    from app.db.models import LeadV2, LeadStatus
    cutoff = datetime.now(timezone.utc) - timedelta(hours=2)
    q = (
        select(LeadV2.offered_slots_json).where(
            LeadV2.status.in_([LeadStatus.SENT, LeadStatus.REPLIED]),
            LeadV2.offered_slots_json.is_not(None),
            # replied_at is set whenever slots are offered (week-click or reply)
            LeadV2.replied_at >= cutoff,
        )
    )
    if exclude_lead_id is not None:
        q = q.where(LeadV2.id != exclude_lead_id)
    result = await db.execute(q)
    held: set[str] = set()
    for (json_str,) in result:
        if json_str:
            try:
                for s in json.loads(json_str):
                    if isinstance(s, dict) and s.get("iso"):
                        held.add(s["iso"])
            except Exception:
                pass
    return held


# ---------------------------------------------------------------------------
# Slot distribution — spread across different dates (round-robin)
# ---------------------------------------------------------------------------

def _distribute_slots_by_date(slots: list[SlotInfo]) -> list[SlotInfo]:
    """Reorder so the returned slots span as many different dates as possible.

    Example: if we have 4 slots on Mon and 2 on Tue, result is:
      Mon-1, Tue-1, Mon-2, Tue-2, Mon-3, Mon-4
    instead of 4 consecutive Monday slots.
    """
    if not slots:
        return []
    grouped: dict[str, list[SlotInfo]] = {}
    for s in slots:
        grouped.setdefault(s["date_str"], []).append(s)

    ordered: list[SlotInfo] = []
    max_len = max(len(lst) for lst in grouped.values())
    for i in range(max_len):
        for date_str in grouped:
            if i < len(grouped[date_str]):
                ordered.append(grouped[date_str][i])
    return ordered


# ---------------------------------------------------------------------------
# Layer 3 — apply DB guard to a raw slot list
# ---------------------------------------------------------------------------

async def _apply_db_guard(
    db: AsyncSession, slots: list[SlotInfo]
) -> list[SlotInfo]:
    """Remove booked (hard) and held (soft) slots from `slots`.

    This is the Layer 3 pass — always called after a Zoho/cache fetch.
    """
    # Hard exclusions (confirmed bookings)
    booked_iso, booked_display = await _get_booked_slots(db)
    if booked_iso or booked_display:
        before = len(slots)
        slots = [
            s for s in slots
            if s["iso"] not in booked_iso and s["display"] not in booked_display
        ]
        if len(slots) < before:
            logger.info("booked_slots_excluded", removed=before - len(slots))

    # Soft exclusions (already offered to another lead within 48 h)
    held = await _get_held_slots(db)
    if held:
        before = len(slots)
        slots = [s for s in slots if s["iso"] not in held]
        if len(slots) < before:
            logger.info("held_slots_excluded", removed=before - len(slots))

    return slots


# ---------------------------------------------------------------------------
# Public API — Week-based fetch (Stage 1 → Stage 2 via week buttons)
# ---------------------------------------------------------------------------

def _next_n_working_days(from_date, n: int) -> list:
    """Return n working days (Mon-Fri) starting from from_date (inclusive if weekday)."""
    days = []
    d = from_date
    while len(days) < n:
        if d.weekday() < 5:
            days.append(d)
        d += timedelta(days=1)
    return days


def _pick_spread_hours(available_hours: set[int], now_ist_ts, day, slots_per_day: int = 2) -> list[int]:
    """Pick hours spread across the day (morning + afternoon), not just earliest."""
    valid = sorted(
        h for h in available_hours
        if SLOT_START_HOUR <= h <= SLOT_END_HOUR
        and datetime(day.year, day.month, day.day, h, 0, tzinfo=IST) > now_ist_ts
    )
    if not valid:
        return []

    if len(valid) <= slots_per_day:
        return valid

    # Split into morning (before 13) and afternoon (13+)
    morning = [h for h in valid if h < 13]
    afternoon = [h for h in valid if h >= 13]

    picked = []
    if morning and afternoon:
        # One from morning, one from afternoon — spread across the day
        picked.append(morning[len(morning) // 2])    # mid-morning
        picked.append(afternoon[len(afternoon) // 2])  # mid-afternoon
    elif morning:
        # Only morning available — pick two spread out
        picked.append(morning[0])
        picked.append(morning[-1])
    else:
        # Only afternoon — pick two spread out
        picked.append(afternoon[0])
        picked.append(afternoon[-1])

    return sorted(picked[:slots_per_day])


def _distribute_morning_afternoon(slots: list[SlotInfo]) -> list[SlotInfo]:
    """Reorder slots so morning and afternoon alternate within each day.

    Each lead sees a mix of morning + afternoon slots, not a block of all-morning.
    Order: Day1-morning, Day1-afternoon, Day2-morning, Day2-afternoon, ...
    """
    if not slots:
        return []
    # Group by date
    by_date: dict[str, dict] = {}
    for s in slots:
        d = s["date_str"]
        if d not in by_date:
            by_date[d] = {"morning": [], "afternoon": []}
        hour = int(s["time_str"].split(":")[0])
        if hour < 13:
            by_date[d]["morning"].append(s)
        else:
            by_date[d]["afternoon"].append(s)

    result: list[SlotInfo] = []
    dates = list(by_date.keys())
    max_per_half = max(
        max(len(v["morning"]) for v in by_date.values()),
        max(len(v["afternoon"]) for v in by_date.values()),
    )
    # Per-date interleave: morning[i] then afternoon[i] for each date before moving to i+1
    for i in range(max_per_half):
        for d in dates:
            m = by_date[d]["morning"]
            a = by_date[d]["afternoon"]
            if i < len(m):
                result.append(m[i])
            if i < len(a):
                result.append(a[i])
    return result


def _fetch_week_slots_sync(week: str, max_slots: int = 30) -> list[SlotInfo]:
    """Fetch ALL valid slots for 3 working days (full pool for DB guard to filter).

    'this' = today + next working days (3 days total, starting TODAY)
    'next' = 3 working days after 'this week' (always distinct)

    Pool is large (max_slots=30) so held-slot mechanism gives different
    leads genuinely different slots. Morning+afternoon are both included.
    """
    now_ist = datetime.now(IST)
    today = now_ist.date()
    now_ist_ts = datetime.now(IST)

    # Start from TODAY so "this week" includes today's afternoon slots
    all_work_days = _next_n_working_days(today, 6)

    work_days = all_work_days[:3] if week == "this" else all_work_days[3:6]

    slots: list[SlotInfo] = []

    for day in work_days:
        available_hours = _zoho_available_hours_for_day(day)
        if available_hours is None:
            continue

        for hour in sorted(available_hours):
            if len(slots) >= max_slots:
                break
            if hour < SLOT_START_HOUR or hour > SLOT_END_HOUR:
                continue
            slot_dt = datetime(day.year, day.month, day.day, hour, 0, tzinfo=IST)
            if slot_dt <= now_ist_ts:
                continue
            slots.append(_build_slot_info(day, hour))

    return slots


async def get_slots_for_week(db: AsyncSession, week: str, n: int = 6) -> list[SlotInfo]:
    """3-layer fetch: Cache → Zoho → DB guard → distribute → return N slots."""
    import asyncio

    ttl = settings.ZOHO_CACHE_TTL
    entry = _week_slots_cache.get(week, {"slots": None, "at": 0.0})

    # Layer 2 — week-level cache hit
    if entry["slots"] is not None and _time.time() < entry["at"]:
        all_slots = entry["slots"]
        logger.info("week_cache_hit", week=week, cached=len(all_slots))
    else:
        # Layer 1 — Zoho fetch (blocking, run in thread pool)
        all_slots = await asyncio.to_thread(_fetch_week_slots_sync, week, 30)
        _week_slots_cache[week] = {"slots": all_slots, "at": _time.time() + ttl}
        logger.info("week_cache_refreshed", week=week, slots=len(all_slots))

    # Layer 3 — DB guard (booked + held)
    all_slots = await _apply_db_guard(db, list(all_slots))

    # Interleave morning + afternoon across days so every lead sees time variety
    all_slots = _distribute_morning_afternoon(all_slots)

    logger.info("slots_returned", week=week, n=len(all_slots[:n]))
    return all_slots[:n]


# ---------------------------------------------------------------------------
# Public API — Date-filtered fetch (reply says "after Jan 8" etc.)
# ---------------------------------------------------------------------------

async def get_available_slots(
    db: AsyncSession,
    n: int = 6,
    lookahead_days: int = 21,
    start_date: date | None = None,
) -> list[SlotInfo]:
    """3-layer fetch starting from start_date (or tomorrow if None)."""
    import asyncio

    ttl = settings.ZOHO_CACHE_TTL

    # Layer 2 — general cache only when no specific start_date
    if start_date is None and _general_cache["slots"] is not None and _time.time() < _general_cache["at"]:
        all_slots = _general_cache["slots"]
        logger.info("general_cache_hit", cached=len(all_slots))
    else:
        def _fetch() -> list[SlotInfo]:
            now_ist = datetime.now(IST)
            tomorrow = now_ist.date() + timedelta(days=1)
            query_start = start_date if (start_date and start_date > tomorrow) else tomorrow

            slots: list[SlotInfo] = []
            day = query_start
            days_checked = 0
            now_ist_ts = datetime.now(IST)

            while len(slots) < 20 and days_checked < lookahead_days:
                days_checked += 1
                if day.weekday() >= 5:
                    day += timedelta(days=1)
                    continue

                # Layer 1 + per-day Layer 2
                available_hours = _zoho_available_hours_for_day(day)
                if available_hours is None:
                    day += timedelta(days=1)
                    continue

                for hour in sorted(available_hours):
                    if len(slots) >= 20:
                        break
                    if hour < SLOT_START_HOUR or hour > SLOT_END_HOUR:
                        continue
                    slot_dt = datetime(day.year, day.month, day.day, hour, 0, tzinfo=IST)
                    if slot_dt <= now_ist_ts:
                        continue
                    slots.append(_build_slot_info(day, hour))

                day += timedelta(days=1)
            return slots

        # Layer 1 — Zoho (in thread so we don't block event loop)
        all_slots = await asyncio.to_thread(_fetch)

        if start_date is None:
            _general_cache["slots"] = all_slots
            _general_cache["at"] = _time.time() + ttl
            logger.info("general_cache_set", slots=len(all_slots))
        else:
            logger.info("dynamic_fetch_for_start_date", start_date=start_date, slots=len(all_slots))

    # Layer 3 — DB guard (booked + held)
    all_slots = await _apply_db_guard(db, list(all_slots))

    # Spread across different days
    all_slots = _distribute_slots_by_date(all_slots)

    logger.info("slots_returned", start_date=start_date, n=len(all_slots[:n]))
    return all_slots[:n]


# ---------------------------------------------------------------------------
# Day labels for Stage 1 email (pill badges under week buttons)
# ---------------------------------------------------------------------------

def get_week_day_labels(week: str) -> list[str]:
    """Return unique day labels from the cached slots ('Thu Jun 5').

    Returns [] if the cache is cold — caller should pre-warm with get_slots_for_week.
    """
    entry = _week_slots_cache.get(week, {"slots": None, "at": 0.0})
    if not entry["slots"]:
        return []
    seen: set[str] = set()
    labels: list[str] = []
    for s in entry["slots"]:
        d = s["date_str"]
        if d not in seen:
            seen.add(d)
            dt = datetime.strptime(d, "%d-%b-%Y")
            labels.append(f"{dt.strftime('%a')} {dt.strftime('%b')} {dt.day}")
    return labels
