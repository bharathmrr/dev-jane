"""Email intent extraction.

Turns a free-text email reply into a structured decision the booking engine can
act on. The LLM does the messy natural-language understanding ("second option",
"can we do Wed 5pm?", "BOOK 3"); deterministic code does everything that touches
money/calendars.

Output schema (validated with pydantic):
    intent: one of EmailIntent
    selected_slot_index: 1-based index into the offered slots, if a confirmation
    proposed_datetime_text: raw phrase describing a new time, if suggesting
    confidence: 0..1
    reasoning: short rationale (for audit logs, never shown to the lead)
"""
from __future__ import annotations

from pydantic import BaseModel, Field, field_validator

from app.core.logging import get_logger
from app.core.config import settings
from app.db.base import EmailIntent
from app.services.llm_client import get_llm_client

log = get_logger(__name__)

SYSTEM_PROMPT = """You classify a single email reply from a person who was \
offered a list of meeting slots. Decide which slot they want.

Return ONLY a JSON object (no prose, no markdown fences) with keys:
  selected_slot: a string representing the time they selected (e.g., "15:00", "10:00 AM"), or null if they didn't select any.

Rules:
- "3 PM works", "15:00", "I'll take the 3pm one" => { "selected_slot": "15:00" }
- "None of these work" => { "selected_slot": null }
"""

USER_TEMPLATE = """Offered slots:
{slots_block}

The person's email reply:
\"\"\"
{reply_text}
\"\"\"
"""


class IntentResult(BaseModel):
    selected_slot: str | None = None



def _format_slots(offered: list[str]) -> str:
    if not offered:
        return "(no slots were offered in this thread)"
    lines = []
    for s in offered:
        lines.append(f"- {s}")
    return "\n".join(lines)


async def classify_reply(reply_text: str, offered_slots: list[str]) -> IntentResult:
    """Classify an inbound reply against the slots most recently offered."""
    client = get_llm_client()
    user = USER_TEMPLATE.format(
        slots_block=_format_slots(offered_slots),
        reply_text=reply_text[:4000],
    )
    raw = await client.complete_json(
        system=SYSTEM_PROMPT,
        user=user,
        model=settings.LLM_INTENT_MODEL,
        max_tokens=settings.LLM_MAX_TOKENS,
    )
    try:
        result = IntentResult.model_validate(raw)
    except Exception:  # defensive: never let a bad LLM payload crash processing
        log.warning("intent_validation_failed", raw=raw)
        result = IntentResult()

    log.info(
        "intent_classified",
        selected_slot=result.selected_slot,
    )
    return result

