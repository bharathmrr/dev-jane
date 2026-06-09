"""Long email thread summarizer.

Scenario #45 — After every 5 messages in a thread, summarize context
so AI intent classification doesn't lose track of earlier messages.

Claude Haiku reads the last N messages + existing summary → produces
a compressed summary for the next round of replies.
"""
from __future__ import annotations

from structlog import get_logger

from app.core.config import settings

logger = get_logger(__name__)

_HAIKU = "claude-haiku-4-5"
_SUMMARIZE_EVERY = 5     # summarize after this many messages
_KEEP_RECENT = 3         # always include this many recent messages verbatim


def should_summarize(message_count: int) -> bool:
    """Return True when it's time to summarize the thread."""
    return message_count > 0 and message_count % _SUMMARIZE_EVERY == 0


def summarize_thread(
    messages: list[dict],
    existing_summary: str | None = None,
) -> str:
    """Summarize a list of email messages into a compact context string.

    Args:
        messages: list of {"role": "lead"|"us", "body": str}
        existing_summary: previous summary if this is a rolling update

    Returns:
        New summary string (max ~200 words).
    """
    if not settings.ANTHROPIC_API_KEY:
        return existing_summary or ""

    try:
        import anthropic
        client = anthropic.Anthropic(api_key=settings.ANTHROPIC_API_KEY)

        thread_text = ""
        if existing_summary:
            thread_text += f"[Earlier context]: {existing_summary}\n\n"

        for i, msg in enumerate(messages):
            role = "Lead" if msg.get("role") == "lead" else "Jane Aerospace"
            thread_text += f"{role}: {msg.get('body', '')[:400]}\n\n"

        system = (
            "You are summarizing an email thread between Jane Aerospace (supply chain as a service company) "
            "and a potential lead. Produce a concise summary in under 150 words that captures:\n"
            "- What the lead's main need or concern is\n"
            "- What Jane Aerospace has offered or replied\n"
            "- The current state of the conversation (interested/not interested/pending slot/booked/etc.)\n"
            "- Any key details mentioned (dates, phone numbers, referrals, questions asked)\n"
            "Return ONLY the summary, no preamble."
        )

        msg = client.messages.create(
            model=_HAIKU,
            max_tokens=200,
            system=system,
            messages=[{"role": "user", "content": f"Thread:\n\n{thread_text}"}],
        )
        summary = msg.content[0].text.strip()
        logger.info("thread_summarized", length=len(summary))
        return summary

    except Exception as exc:
        logger.warning("thread_summarize_error", error=str(exc))
        return existing_summary or ""


def build_context_for_ai(
    current_reply: str,
    messages: list[dict],
    thread_summary: str | None,
) -> str:
    """Build a context-enriched version of the current reply for AI intent analysis.

    Includes thread summary + last few messages so the AI has full context.
    """
    parts = []

    if thread_summary:
        parts.append(f"[Thread context]: {thread_summary}")

    recent = messages[-_KEEP_RECENT:] if len(messages) > _KEEP_RECENT else messages
    for msg in recent:
        role = "Lead" if msg.get("role") == "lead" else "Jane Aerospace"
        parts.append(f"[Previous — {role}]: {msg.get('body', '')[:300]}")

    parts.append(f"[Current reply]: {current_reply}")
    return "\n\n".join(parts)
