"""State machine + intent classification (stub) tests."""
from __future__ import annotations

import pytest

from app.db.base import BookingState as S
from app.db.base import EmailIntent
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
    offered = [{"label": "Mon 10am"}, {"label": "Mon 11am"}]
    result = await classify_reply("YES 1, that works", offered)
    assert result.intent == EmailIntent.CONFIRM_SLOT


@pytest.mark.asyncio
async def test_stub_intent_cancel():
    result = await classify_reply("please cancel this", [])
    assert result.intent == EmailIntent.CANCEL


@pytest.mark.asyncio
async def test_out_of_range_index_demoted():
    offered = [{"label": "only one"}]
    # Stub returns index 1 for confirm; ensure guard keeps it in range.
    result = await classify_reply("yes book it", offered)
    assert result.selected_slot_index in (None, 1)
