"""Indian national and major global holidays.

Scenario #35 — skip sending on holidays, reschedule to next business day.

Holiday types:
  - Fixed: same date every year (e.g. Republic Day Jan 26)
  - Variable: date changes yearly (Diwali, Eid, Holi, etc.)
    Listed explicitly for 2025 and 2026.
"""
from __future__ import annotations

from datetime import date, timedelta

# ---------------------------------------------------------------------------
# Fixed holidays (month, day) — apply every year
# ---------------------------------------------------------------------------
_FIXED_HOLIDAYS: set[tuple[int, int]] = {
    (1, 26),   # Republic Day
    (8, 15),   # Independence Day
    (10, 2),   # Gandhi Jayanti
    (12, 25),  # Christmas Day
    (1, 1),    # New Year's Day
    (11, 1),   # Kannada Rajyotsava (Karnataka — adjust if needed)
}

# ---------------------------------------------------------------------------
# Variable holidays — explicit dates for 2025 and 2026
# ---------------------------------------------------------------------------
_VARIABLE_HOLIDAYS: set[date] = {
    # 2025
    date(2025, 1, 14),   # Makar Sankranti / Pongal
    date(2025, 2, 26),   # Maha Shivaratri
    date(2025, 3, 14),   # Holi
    date(2025, 3, 31),   # Eid al-Fitr (Ramadan end — approx)
    date(2025, 4, 14),   # Dr. Ambedkar Jayanti / Tamil New Year
    date(2025, 4, 18),   # Good Friday
    date(2025, 5, 12),   # Buddha Purnima
    date(2025, 6, 7),    # Eid al-Adha (approx)
    date(2025, 8, 16),   # Janmashtami
    date(2025, 9, 5),    # Onam (Thiruvonam)
    date(2025, 10, 2),   # Gandhi Jayanti (also fixed above)
    date(2025, 10, 20),  # Diwali (Lakshmi Puja)
    date(2025, 10, 21),  # Diwali Day 2
    date(2025, 11, 5),   # Guru Nanak Jayanti
    date(2025, 12, 25),  # Christmas
    # 2026
    date(2026, 1, 14),   # Makar Sankranti
    date(2026, 2, 15),   # Maha Shivaratri
    date(2026, 3, 3),    # Holi
    date(2026, 3, 20),   # Eid al-Fitr (approx)
    date(2026, 4, 3),    # Good Friday
    date(2026, 4, 14),   # Dr. Ambedkar Jayanti
    date(2026, 5, 31),   # Buddha Purnima
    date(2026, 6, 27),   # Eid al-Adha (approx)
    date(2026, 8, 5),    # Janmashtami
    date(2026, 11, 8),   # Diwali (Lakshmi Puja — approx)
    date(2026, 11, 9),   # Diwali Day 2
    date(2026, 11, 24),  # Guru Nanak Jayanti
}


def is_holiday(d: date) -> bool:
    """Return True if the given date is an Indian or global holiday."""
    if (d.month, d.day) in _FIXED_HOLIDAYS:
        return True
    if d in _VARIABLE_HOLIDAYS:
        return True
    # Sundays are always off
    if d.weekday() == 6:
        return True
    return False


def next_business_day(d: date) -> date:
    """Return the next non-holiday weekday on or after the given date."""
    candidate = d
    for _ in range(14):  # safety limit — never infinite loop
        if candidate.weekday() < 6 and not is_holiday(candidate):
            return candidate
        candidate += timedelta(days=1)
    return candidate


def should_send_today(check_date: date | None = None) -> bool:
    """Return False if today is a holiday — emails should be deferred."""
    from datetime import date as _date
    from zoneinfo import ZoneInfo
    from datetime import datetime
    today = check_date or datetime.now(ZoneInfo("Asia/Kolkata")).date()
    return not is_holiday(today)
