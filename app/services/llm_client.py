"""Provider-agnostic LLM client.

A thin interface so the rest of the app never imports a vendor SDK directly.
The Anthropic implementation uses the Messages API. A `stub` provider returns
deterministic output for tests/CI (no network, no key required).

Model strings are configured in settings (LLM_INTENT_MODEL / LLM_NEGOTIATION_MODEL).
"""
from __future__ import annotations

import json
from typing import Protocol

from app.core.config import settings
from app.core.logging import get_logger

log = get_logger(__name__)


class LLMClient(Protocol):
    async def complete_json(
        self, *, system: str, user: str, model: str, max_tokens: int
    ) -> dict: ...

    async def complete_with_tools(
        self, *, system: str, messages: list, tools: list, model: str, max_tokens: int
    ) -> dict:
        """One turn of a tool-use conversation.

        Returns ``{"text", "tool_calls": [{"id","name","input"}], "content", "stop_reason"}``
        where ``content`` is the assistant message's blocks (to append back into
        ``messages`` before sending tool results).
        """
        ...


class AnthropicClient:
    """Uses the official anthropic async SDK. Requests JSON-only output."""

    def __init__(self, api_key: str):
        from anthropic import AsyncAnthropic

        self._client = AsyncAnthropic(api_key=api_key, timeout=settings.LLM_TIMEOUT_SECONDS)

    async def complete_json(
        self, *, system: str, user: str, model: str, max_tokens: int
    ) -> dict:
        resp = await self._client.messages.create(
            model=model,
            max_tokens=max_tokens,
            system=system,
            messages=[{"role": "user", "content": user}],
        )
        text = "".join(
            block.text for block in resp.content if block.type == "text"
        ).strip()
        return _safe_json(text)

    async def complete_with_tools(
        self, *, system: str, messages: list, tools: list, model: str, max_tokens: int
    ) -> dict:
        resp = await self._client.messages.create(
            model=model,
            max_tokens=max_tokens,
            system=system,
            messages=messages,
            tools=tools,
        )
        text = "".join(b.text for b in resp.content if b.type == "text").strip()
        tool_calls = [
            {"id": b.id, "name": b.name, "input": b.input}
            for b in resp.content
            if b.type == "tool_use"
        ]
        content: list[dict] = []
        for b in resp.content:
            if b.type == "text":
                content.append({"type": "text", "text": b.text})
            elif b.type == "tool_use":
                content.append(
                    {"type": "tool_use", "id": b.id, "name": b.name, "input": b.input}
                )
        return {
            "text": text,
            "tool_calls": tool_calls,
            "content": content,
            "stop_reason": resp.stop_reason,
        }


class StubClient:
    """Heuristic fallback for tests / when no API key is configured."""

    async def complete_json(
        self, *, system: str, user: str, model: str, max_tokens: int
    ) -> dict:
        body = user.lower()
        if any(k in body for k in ("cancel", "not interested")):
            intent = "cancel"
        elif any(k in body for k in ("reschedule", "move it", "change the time")):
            intent = "reschedule"
        elif any(k in body for k in ("yes", "book", "confirm", "option", "works")):
            intent = "confirm_slot"
        elif any(k in body for k in ("can we do", "prefer", "how about", "instead")):
            intent = "suggest_new_time"
        elif "no" in body:
            intent = "reject_slots"
        else:
            intent = "general_query"

        # Heuristic to find which offered slot is mentioned in the reply for V2
        selected_slot = None
        if intent == "confirm_slot":
            import re
            offered_slots = re.findall(r"-\s*(.+)", user)
            for slot in offered_slots:
                # Check if the slot text (e.g. "10:00 am" or "10:00") is in the reply text
                clean_slot = slot.strip().lower()
                # Split at newline to check only the reply part
                reply_part = body.split("reply:")[-1] if "reply:" in body else body
                if clean_slot in reply_part or clean_slot.split()[0] in reply_part:
                    selected_slot = slot.strip()
                    break
            # Fallback to the first slot if confirm_slot but not matched by text
            if not selected_slot and offered_slots:
                selected_slot = offered_slots[0].strip()

        return {
            "intent": intent,
            "selected_slot": selected_slot,
            "selected_slot_index": 1 if intent == "confirm_slot" else None,
            "proposed_datetime_text": None,
            "confidence": 0.4,
            "reasoning": "stub heuristic",
        }

    async def complete_with_tools(
        self, *, system: str, messages: list, tools: list, model: str, max_tokens: int
    ) -> dict:
        """Deterministic keyword routing so the co-pilot works with no API key and
        the tool-use loop is unit-testable. Calls one tool, then on the follow-up
        (a tool_result is present) returns a short final answer."""
        def _has_tool_result(msgs: list) -> bool:
            for m in msgs:
                c = m.get("content") if isinstance(m, dict) else None
                if isinstance(c, list) and any(
                    isinstance(b, dict) and b.get("type") == "tool_result" for b in c
                ):
                    return True
            return False

        def _first_user_text(msgs: list) -> str:
            for m in msgs:
                if isinstance(m, dict) and m.get("role") == "user":
                    c = m.get("content")
                    if isinstance(c, str):
                        return c
                    if isinstance(c, list):
                        for b in c:
                            if isinstance(b, dict) and b.get("type") == "text":
                                return b.get("text", "")
            return ""

        def _text(s: str) -> dict:
            return {"text": s, "tool_calls": [],
                    "content": [{"type": "text", "text": s}], "stop_reason": "end_turn"}

        def _call(name: str, inp: dict) -> dict:
            tid = "stub_" + name
            return {"text": "", "tool_calls": [{"id": tid, "name": name, "input": inp}],
                    "content": [{"type": "tool_use", "id": tid, "name": name, "input": inp}],
                    "stop_reason": "tool_use"}

        if _has_tool_result(messages):
            return _text("Here's what I found — let me know if you want details.")

        body = _first_user_text(messages).lower()
        if any(k in body for k in ("approval", "pending", "waiting", "to action")):
            return _call("list_pending", {"kind": "approvals"})
        if "comment" in body:
            return _call("navigate", {"type": "open_comments"})
        if any(k in body for k in ("notification", "alert")):
            return _call("navigate", {"type": "open_notifications"})
        if any(k in body for k in ("summary", "overview", "how many", "booked",
                                   "pipeline", "this week", "total")):
            return _call("pipeline_summary", {})
        if any(k in body for k in ("open ", "show ", "find ", "pull up", "go to", "take me")):
            for view in ("overview", "kanban", "leads", "sheet", "bookings", "docs"):
                if view in body:
                    return _call("navigate", {"type": "navigate", "view": view})
            return _call("search_leads", {"query": body})
        return _text("Hi! I can find leads, summarize your pipeline, and open views for you. "
                     "Try saying 'show pending approvals' or 'open this week's bookings'.")


def _safe_json(text: str) -> dict:
    cleaned = text.strip().removeprefix("```json").removeprefix("```").removesuffix("```")
    try:
        return json.loads(cleaned)
    except json.JSONDecodeError:
        log.warning("llm_json_parse_failed", raw=text[:500])
        return {"intent": "unknown", "confidence": 0.0, "reasoning": "parse_error"}


_client: LLMClient | None = None


def get_llm_client() -> LLMClient:
    global _client
    if _client is not None:
        return _client
    if settings.LLM_PROVIDER == "anthropic" and settings.ANTHROPIC_API_KEY:
        _client = AnthropicClient(settings.ANTHROPIC_API_KEY)
    else:
        if settings.LLM_PROVIDER == "anthropic":
            log.warning("llm_no_api_key_falling_back_to_stub")
        _client = StubClient()
    return _client
