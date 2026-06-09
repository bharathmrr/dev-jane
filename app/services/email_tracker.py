"""Email open tracking + SMTP daily send throttle.

Scenario #3  — track opens via 1×1 pixel; if 5+ opens with no click → nudge
Scenario #40 — SMTP daily send limit; cap at 400/day, queue remainder next day

Pixel URL: {APP_URL}/api/v1/v2/track/open/{lead_id}/{sig}
When loaded by the email client, the endpoint records the open in Redis
and updates the DB via a lightweight background increment.

Redis keys:
  open:{lead_id}         — open count for this lead (incremented on pixel load)
  smtp:sent:{YYYY-MM-DD} — total emails sent today (IST date)
"""
from __future__ import annotations

import hashlib
import hmac as _hmac
from datetime import datetime
from zoneinfo import ZoneInfo

from structlog import get_logger

from app.core.config import settings

logger = get_logger(__name__)

_IST = ZoneInfo("Asia/Kolkata")
_DAILY_SMTP_LIMIT = 400   # hard cap — stays comfortably under most provider limits


# ---------------------------------------------------------------------------
# Tracking pixel URL builder
# ---------------------------------------------------------------------------

def make_open_pixel_url(lead_id: str) -> str:
    """Generate a signed 1×1 GIF URL for embedding in outbound emails."""
    msg = f"open:{lead_id}".encode()
    sig = _hmac.new(settings.EMAIL_HMAC_SECRET.encode(), msg, hashlib.sha256).hexdigest()[:16]
    return f"{settings.APP_URL.rstrip('/')}/api/v1/v2/track/open/{lead_id}/{sig}"


def verify_open_sig(lead_id: str, sig: str) -> bool:
    msg = f"open:{lead_id}".encode()
    expected = _hmac.new(settings.EMAIL_HMAC_SECRET.encode(), msg, hashlib.sha256).hexdigest()[:16]
    return _hmac.compare_digest(expected, sig)


def pixel_html(lead_id: str) -> str:
    """Return a 1×1 invisible tracking pixel HTML tag."""
    url = make_open_pixel_url(lead_id)
    return f'<img src="{url}" width="1" height="1" style="display:none;" alt="" />'


# ---------------------------------------------------------------------------
# SMTP daily send throttle  (#40)
# ---------------------------------------------------------------------------

def _today_key() -> str:
    today = datetime.now(_IST).strftime("%Y-%m-%d")
    return f"smtp:sent:{today}"


def smtp_daily_count() -> int:
    """Return how many emails have been sent today (IST)."""
    try:
        from app.services.slot_lock import _r
        r = _r()
        val = r.get(_today_key())
        return int(val) if val else 0
    except Exception:
        return 0


def smtp_can_send() -> bool:
    """Return True if we are under the daily send limit."""
    return smtp_daily_count() < _DAILY_SMTP_LIMIT


def smtp_record_send(count: int = 1) -> None:
    """Increment today's SMTP send counter. Sets TTL to 25h to survive day boundary."""
    try:
        from app.services.slot_lock import _r
        r = _r()
        key = _today_key()
        r.incrby(key, count)
        r.expire(key, 90_000)   # 25 hours — covers day boundary
    except Exception:
        pass


def smtp_remaining_today() -> int:
    """How many emails can still be sent today."""
    return max(0, _DAILY_SMTP_LIMIT - smtp_daily_count())


# ---------------------------------------------------------------------------
# Open count helpers (Redis)  (#3)
# ---------------------------------------------------------------------------

def increment_open_count(lead_id: str) -> int:
    """Increment the open counter for a lead in Redis. Returns new total."""
    try:
        from app.services.slot_lock import _r
        r = _r()
        key = f"open:{lead_id}"
        new_count = r.incr(key)
        r.expire(key, 30 * 86400)   # 30 days TTL
        return int(new_count)
    except Exception:
        return 0


def get_open_count(lead_id: str) -> int:
    try:
        from app.services.slot_lock import _r
        r = _r()
        val = r.get(f"open:{lead_id}")
        return int(val) if val else 0
    except Exception:
        return 0
