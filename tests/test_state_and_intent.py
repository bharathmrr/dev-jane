"""State machine + intent classification (stub) tests."""
from __future__ import annotations

import pytest

from app.db.base import BookingState as S
from app.services.email_intent import classify_reply
from app.state_machine import IllegalTransition, assert_transition, can_transition


def test_valid_transition():
    assert can_transition(S.LEAD_CREATED, S.EMAIL_SENT)
    assert can_transition(S.SLOT_RESERVED, S.BOOKING_CONFIRMED)


def test_invalid_transition_raises():
    with pytest.raises(IllegalTransition):
        assert_transition(S.LEAD_CREATED, S.BOOKING_CONFIRMED)


def test_terminal_states_have_no_exits():
    assert not can_transition(S.CANCELLED, S.EMAIL_SENT)
    assert not can_transition(S.MEETING_COMPLETED, S.RESCHEDULED)


@pytest.mark.asyncio
async def test_stub_intent_confirm():
    # Stub picks a slot when the reply confirms; selected_slot should be non-None.
    result = await classify_reply("YES 1, that works", ["Mon 10am", "Mon 11am"])
    assert result.selected_slot is not None


@pytest.mark.asyncio
async def test_stub_intent_cancel():
    # Cancel reply → stub returns no selected slot.
    result = await classify_reply("please cancel this", [])
    assert result.selected_slot is None


@pytest.mark.asyncio
async def test_stub_confirm_picks_first_slot():
    # When reply matches no specific slot text, stub falls back to first offered slot.
    result = await classify_reply("yes book it", ["only one"])
    assert result.selected_slot is not None
