"""Explicit booking state machine.

Transitions are validated centrally so no service can move a booking into an
illegal state. Every transition is audit-logged by the booking service.
"""
from __future__ import annotations

from app.db.base import BookingState as S

# Allowed transitions: from-state -> set of valid next states.
TRANSITIONS: dict[S, set[S]] = {
    S.LEAD_CREATED: {S.EMAIL_SENT, S.CANCELLED},
    S.EMAIL_SENT: {S.WAITING_FOR_REPLY, S.CANCELLED},
    S.WAITING_FOR_REPLY: {
        S.SLOT_SELECTED,
        S.WAITING_FOR_REPLY,  # re-send / re-offer
        S.CANCELLED,
    },
    S.SLOT_SELECTED: {S.SLOT_RESERVED, S.WAITING_FOR_REPLY, S.CANCELLED},
    S.SLOT_RESERVED: {
        S.BOOKING_CONFIRMED,
        S.WAITING_FOR_REPLY,  # reservation expired -> re-offer
        S.CANCELLED,
    },
    S.BOOKING_CONFIRMED: {
        S.REMINDER_SENT,
        S.RESCHEDULED,
        S.CANCELLED,
        S.MEETING_COMPLETED,
    },
    S.REMINDER_SENT: {S.MEETING_COMPLETED, S.RESCHEDULED, S.CANCELLED},
    S.RESCHEDULED: {S.WAITING_FOR_REPLY, S.SLOT_SELECTED, S.CANCELLED},
    S.MEETING_COMPLETED: set(),
    S.CANCELLED: set(),
}

TERMINAL: set[S] = {S.MEETING_COMPLETED, S.CANCELLED}


class IllegalTransition(Exception):
    def __init__(self, src: S, dst: S):
        super().__init__(f"Illegal booking transition {src.value} -> {dst.value}")
        self.src, self.dst = src, dst


def can_transition(src: S, dst: S) -> bool:
    return dst in TRANSITIONS.get(src, set())


def assert_transition(src: S, dst: S) -> None:
    if not can_transition(src, dst):
        raise IllegalTransition(src, dst)
