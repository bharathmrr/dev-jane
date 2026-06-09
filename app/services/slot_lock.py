"""Redis-based slot reservation lock.

Prevents concurrent bookings of the same slot when multiple leads click at once.

Lock lifecycle:
  1. Lead clicks a slot → acquire_slot_lock() — atomic SETNX, only one wins
  2. If acquired → proceed to Zoho booking
  3. Zoho confirms → lock stays (Zoho is now source of truth, lock expires naturally)
  4. Zoho rejects → release_slot_lock() immediately so slot becomes available again
  5. No-one books within TTL → lock expires automatically

Lock key: slot:lock:{date_str}:{time_str_compact}
TTL: 600 seconds (10 minutes) — enough to complete a booking flow
"""
from __future__ import annotations

import redis as _redis
from structlog import get_logger

from app.core.config import settings

logger = get_logger(__name__)
_LOCK_TTL = 600  # 10 minutes


def _r() -> _redis.Redis:
    return _redis.from_url(str(settings.REDIS_URL), decode_responses=True, socket_timeout=3)


def _key(date_str: str, time_str: str) -> str:
    return f"slot:lock:{date_str}:{time_str.replace(':', '')}"


def acquire_slot_lock(date_str: str, time_str: str, lead_email: str) -> bool:
    """Atomically acquire a reservation lock for a slot.

    Returns True if this caller now holds the lock (slot is theirs to book).
    Returns False if another lead already holds it.
    """
    try:
        result = _r().set(_key(date_str, time_str), lead_email, nx=True, ex=_LOCK_TTL)
        acquired = bool(result)
        logger.info("slot_lock_acquire", date=date_str, time=time_str, email=lead_email, acquired=acquired)
        return acquired
    except Exception as exc:
        # If Redis is down, fail open — let Zoho be the final guard
        logger.warning("slot_lock_redis_error", error=str(exc))
        return True


def release_slot_lock(date_str: str, time_str: str) -> None:
    """Release a slot lock (Zoho rejected the booking or booking cancelled)."""
    try:
        _r().delete(_key(date_str, time_str))
        logger.info("slot_lock_released", date=date_str, time=time_str)
    except Exception as exc:
        logger.warning("slot_lock_release_error", error=str(exc))


def is_slot_locked(date_str: str, time_str: str) -> bool:
    """Check if a slot is currently reserved by another lead."""
    try:
        return bool(_r().exists(_key(date_str, time_str)))
    except Exception:
        return False


def get_all_locked_slots() -> list[str]:
    """Return all currently locked slot keys — used to grey out slots in emails."""
    try:
        raw = _r().keys("slot:lock:*")
        return [str(k) for k in (raw if isinstance(raw, list) else [])]
    except Exception:
        return []


# ---------------------------------------------------------------------------
# A/B test performance tracking (same Redis instance)
# ---------------------------------------------------------------------------

def record_variant_sent(variant: str) -> None:
    """Increment sent counter for an A/B variant."""
    try:
        _r().incr(f"ab:body:{variant}:sent")
    except Exception:
        pass


def record_variant_reply(variant: str) -> None:
    """Increment reply counter when a lead with this variant replies."""
    try:
        _r().incr(f"ab:body:{variant}:replied")
    except Exception:
        pass


def get_variant_weights(segment: str | None = None) -> list[int]:
    """Return weighted probabilities [A, B, C] based on observed reply rates.

    Starts at [33, 33, 34] and self-adjusts as replies come in.
    Minimum weight per variant: 5 (never fully kill a variant).
    Needs at least 5 sends per variant before adjusting.

    If segment is provided (e.g. 'ceo', 'coo', 'manufacturing'), returns
    segment-specific weights if enough data exists (#A/B Step 5).
    """
    try:
        r = _r()
        prefix = f"ab:body:{{v}}:seg:{segment}" if segment else "ab:body:{v}"

        weights = []
        totals_ok = True
        for v in ["A", "B", "C"]:
            key_sent = prefix.format(v=v) + ":sent"
            key_rep  = prefix.format(v=v) + ":replied"
            sent    = int(str(r.get(key_sent) or 0))
            replied = int(str(r.get(key_rep) or 0))
            if sent < 5:
                totals_ok = False
                break
            rate = replied / max(sent, 1)
            weights.append(max(int(rate * 100), 5))

        if not totals_ok or not weights:
            # Fall back to global weights if segment has insufficient data
            if segment:
                return get_variant_weights(segment=None)
            return [33, 33, 34]

        return weights
    except Exception:
        return [33, 33, 34]


# ---------------------------------------------------------------------------
# Send-time variant tracking (#A/B — morning/afternoon/evening)
# ---------------------------------------------------------------------------

def record_send_time_sent(time_variant: str) -> None:
    """Increment sent counter for a send-time variant."""
    try:
        _r().incr(f"ab:time:{time_variant}:sent")
    except Exception:
        pass


def record_send_time_reply(time_variant: str) -> None:
    """Increment reply counter for a send-time variant."""
    try:
        _r().incr(f"ab:time:{time_variant}:replied")
    except Exception:
        pass


def get_send_time_stats() -> dict:
    """Return reply rates for each send-time variant."""
    try:
        r = _r()
        result = {}
        for t in ["morning", "afternoon", "evening"]:
            sent    = int(str(r.get(f"ab:time:{t}:sent") or 0))
            replied = int(str(r.get(f"ab:time:{t}:replied") or 0))
            result[t] = {
                "sent": sent,
                "replied": replied,
                "reply_rate_pct": round(replied / max(sent, 1) * 100, 1),
            }
        return result
    except Exception:
        return {}


def record_variant_sent_segment(variant: str, segment: str | None) -> None:
    """Increment sent counter for variant + optional segment."""
    try:
        r = _r()
        r.incr(f"ab:body:{variant}:sent")
        if segment:
            r.incr(f"ab:body:{variant}:seg:{segment}:sent")
    except Exception:
        pass


def record_variant_reply_segment(variant: str, segment: str | None) -> None:
    """Increment reply counter for variant + optional segment."""
    try:
        r = _r()
        r.incr(f"ab:body:{variant}:replied")
        if segment:
            r.incr(f"ab:body:{variant}:seg:{segment}:replied")
    except Exception:
        pass


def classify_segment(designation: str | None, location: str | None) -> str | None:
    """Return a segment key for A/B sub-analysis.

    Combines designation tier and (optionally) regional location so we can
    discover whether Variant B works better in South India, etc.
    """
    if not designation:
        return None
    d = designation.lower()
    loc = (location or "").lower()

    if any(t in d for t in ["ceo", "founder", "managing director", "chairman", "owner"]):
        tier = "ceo"
    elif any(t in d for t in ["coo", "chief operating", "vp operations", "head of operations"]):
        tier = "coo"
    elif any(t in d for t in ["supply chain", "procurement", "logistics", "sourcing"]):
        tier = "supply_chain"
    elif any(t in d for t in ["director", "head", "manager"]):
        tier = "manager"
    else:
        tier = "other"

    # Append regional qualifier when location is known
    south_india = {"chennai", "bengaluru", "bangalore", "hyderabad", "coimbatore",
                   "kochi", "kerala", "tamil", "andhra", "telangana", "karnataka"}
    if any(city in loc for city in south_india):
        return f"{tier}_south"

    return tier
