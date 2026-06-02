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
offered a list of numbered meeting slots. Decide what they want.

Return ONLY a JSON object (no prose, no markdown fences) with keys:
  intent: one of [confirm_slot, reject_slots, suggest_new_time, reschedule, \
cancel, general_query]
  selected_slot_index: integer (1-based) of the slot they chose, or null
  proposed_datetime_text: the exact phrase describing a new time they want, or null
  confidence: number 0..1
  reasoning: one short sentence

Rules:
- "YES 2", "BOOK 3", "option 2", "the second one", "11am works" => confirm_slot \
with the matching index when determinable.
- "none of these", "no thanks" with no alternative => reject_slots.
- "can we do Wednesday 5pm?", "I prefer mornings" => suggest_new_time and put \
the phrase in proposed_datetime_text.
- "need to move our meeting" for an already-booked meeting => reschedule.
- "cancel", "not interested anymore" => cancel.
- Questions about the meeting (agenda, who's attending) => general_query.
- If you cannot tell, use general_query with low confidence."""

USER_TEMPLATE = """Offered slots (1-based):
{slots_block}

The person's email reply:
\"\"\"
{reply_text}
\"\"\"
"""


class IntentResult(BaseModel):
    intent: EmailIntent = EmailIntent.UNKNOWN
    selected_slot_index: int | None = None
    proposed_datetime_text: str | None = None
    confidence: float = Field(0.0, ge=0.0, le=1.0)
    reasoning: str = ""

    @field_validator("intent", mode="before")
    @classmethod
    def _coerce_intent(cls, v):
        try:
            return EmailIntent(v)
        except (ValueError, TypeError):
            return EmailIntent.UNKNOWN


def _format_slots(offered: list[dict]) -> str:
    if not offered:
        return "(no slots were offered in this thread)"
    lines = []
    for i, s in enumerate(offered, start=1):
        lines.append(f"{i}. {s.get('label', s.get('start'))}")
    return "\n".join(lines)


async def classify_reply(reply_text: str, offered_slots: list[dict]) -> IntentResult:
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

    # Guard: index must be within the offered range to be trusted.
    if result.selected_slot_index is not None and not (
        1 <= result.selected_slot_index <= len(offered_slots)
    ):
        result.selected_slot_index = None
        if result.intent == EmailIntent.CONFIRM_SLOT:
            result.intent = EmailIntent.GENERAL_QUERY
    log.info(
        "intent_classified",
        intent=result.intent.value,
        idx=result.selected_slot_index,
        confidence=result.confidence,
    )
    return result
