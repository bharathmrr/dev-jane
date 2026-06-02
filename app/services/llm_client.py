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
        return {
            "intent": intent,
            "selected_slot_index": 1 if intent == "confirm_slot" else None,
            "proposed_datetime_text": None,
            "confidence": 0.4,
            "reasoning": "stub heuristic",
        }


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
