"""Voice co-pilot tool-use loop — exercised against the deterministic StubClient.

These cover the navigation path and the no-tool greeting path, which need no DB,
so the loop logic (tool dispatch, directive capture, termination) is unit-tested
in isolation. Data tools (search_leads etc.) are integration-tested elsewhere.
"""
from __future__ import annotations

import pytest

from app.services.assistant import AssistantService
from app.services.llm_client import StubClient


class _Role:
    value = "admin"


class _FakeUser:
    def __init__(self):
        self.id = "u-1"
        self.full_name = "Test Operator"
        self.role = _Role()


def _svc():
    # db=None is safe: the paths under test never touch the database.
    return AssistantService(db=None, user=_FakeUser(), client=StubClient())


@pytest.mark.asyncio
async def test_navigate_directive_captured():
    out = await _svc().run("open the comments please")
    assert out["directive"] == {"type": "open_comments"}
    assert isinstance(out["reply"], str) and out["reply"]
    assert "navigate" in out["used_tools"]


@pytest.mark.asyncio
async def test_navigate_to_view():
    out = await _svc().run("take me to bookings")
    assert out["directive"] == {"type": "navigate", "view": "bookings"}


@pytest.mark.asyncio
async def test_greeting_has_no_directive():
    out = await _svc().run("hi there")
    assert out["directive"] is None
    assert out["reply"]
    assert out["used_tools"] == []


@pytest.mark.asyncio
async def test_loop_terminates_with_reply():
    # 'pending approvals' routes to a data tool; with db=None that tool raises and
    # is swallowed, but the loop must still terminate and return a string reply.
    out = await _svc().run("show me pending approvals")
    assert isinstance(out["reply"], str) and out["reply"]
