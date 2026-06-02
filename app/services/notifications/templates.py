"""Plain-text email body rendering.

Bodies are intentionally simple and instruction-forward because the *reply* is
the booking UI. Every offer email restates the reply grammar ("YES 1", "BOOK 2",
or a free-text suggestion).
"""
from __future__ import annotations


def render_offer(lead_name: str, organizer_name: str, slots: list[dict]) -> str:
    lines = [
        f"Hi {lead_name},",
        "",
        f"Thanks for your interest. Here are available times with {organizer_name}:",
        "",
    ]
    for i, s in enumerate(slots, start=1):
        lines.append(f"  {i}. {s['label']}")
    lines += [
        "",
        "To book, just reply with the option number, for example:",
        "    YES 1",
        "    BOOK 2",
        "",
        "Prefer a different time? Reply with what works, e.g. "
        '"Can we do Wednesday 5pm?"',
        "",
        "We'll confirm by email — no links or sign-ups needed.",
    ]
    return "\n".join(lines)


def render_reserved(lead_name: str, slot_label: str, minutes: int) -> str:
    return "\n".join(
        [
            f"Hi {lead_name},",
            "",
            f"We're holding {slot_label} for you for the next {minutes} minutes "
            "while we finalize it.",
            "",
            "You'll get a confirmation shortly.",
        ]
    )


def render_confirmation(
    lead_name: str, organizer_name: str, slot_label: str, location: str | None
) -> str:
    body = [
        f"Hi {lead_name},",
        "",
        f"Your meeting with {organizer_name} is confirmed:",
        "",
        f"    {slot_label}",
    ]
    if location:
        body += ["", f"    Where: {location}"]
    body += [
        "",
        'To reschedule, reply "reschedule". To cancel, reply "cancel".',
    ]
    return "\n".join(body)


def render_alternatives(lead_name: str, slots: list[dict]) -> str:
    lines = [
        f"Hi {lead_name},",
        "",
        "That time just got taken — sorry about that! Here are the closest "
        "options still open:",
        "",
    ]
    for i, s in enumerate(slots, start=1):
        lines.append(f"  {i}. {s['label']}")
    lines += ["", "Reply with an option number to grab one."]
    return "\n".join(lines)


def render_reminder(lead_name: str, organizer_name: str, slot_label: str) -> str:
    return "\n".join(
        [
            f"Hi {lead_name},",
            "",
            f"Reminder: your meeting with {organizer_name} is at {slot_label}.",
            "",
            "See you then!",
        ]
    )
