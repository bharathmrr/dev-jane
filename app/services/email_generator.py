"""Groq-powered email body generation + lead scoring for Jane Aerospace outreach.

Pipeline:
  1. score_lead()           — Scores a lead 1-10 using contact name + summary notes.
                              Only leads with score >= 6 get Stage 1 emails.
  2. generate_outreach_body() — Writes 2 personalised paragraphs (para 1 = personal reference
                                from summary, para 2 = Jane Aerospace pitch + CTA).
  3. analyze_reply_intent() — Classifies lead reply: book | list_slots | decline | unclear.
  4. extract_slot_from_reply() — Legacy: extracts specific date+time from reply text.
  5. detect_week_preference()  — Legacy: heuristic this/next week detection.

Rules enforced in all AI outputs:
  - Sender is Leo Charles, Jane Aerospace
  - Focus on supply chain, vendor integration, Atmanirbhar Bharat, drone manufacturing
  - Never use "looking for", "looking into", or "30-minute"
  - No LinkedIn or social media mentions
  - 2 short paragraphs, no greeting, no sign-off
"""
from __future__ import annotations

import random
from structlog import get_logger

from app.core.config import settings

logger = get_logger(__name__)

_MODEL = "llama-3.3-70b-versatile"

_groq_client = None


def _get_client():
    global _groq_client
    if _groq_client is None:
        from groq import Groq
        _groq_client = Groq(api_key=settings.GROQ_API_KEY)
    return _groq_client


# ---------------------------------------------------------------------------
# Lead Scoring (Contact Name + Summary/Notes → 1-10 score)
# ---------------------------------------------------------------------------

def score_lead(contact_name: str | None, business_name: str, summary: str | None) -> int:
    """Score a lead 1-10 using contact name + summary notes.

    Returns an integer 1-10. Leads scoring < 6 are skipped (not worth emailing).
    Falls back to score 7 (send) if Groq is unavailable.
    """
    if not settings.GROQ_API_KEY:
        logger.info("groq_key_missing_scoring_default_7", lead=business_name)
        return 7

    name_info = contact_name or business_name
    summary_info = summary or "No additional notes available."

    system_prompt = (
        "You are a B2B sales qualification assistant for Jane Aerospace, an Indian company "
        "specialising in supply chain solutions, vendor integration, AI-enabled procurement, "
        "commercial drone products, and aerospace consulting (Atmanirbhar Bharat).\n\n"
        "Score the following lead from 1 to 10 based on how likely they are to be a "
        "relevant, high-value contact worth reaching out to.\n\n"
        "Scoring criteria:\n"
        "- 8-10: Strong fit — directly in aerospace, defence, manufacturing, supply chain, "
        "  procurement, logistics, drones/UAV, or government (MoD/DRDO/ISRO adjacent).\n"
        "- 6-7: Moderate fit — adjacent industries (engineering, automotive, tech, infrastructure) "
        "  or unclear but promising summary notes.\n"
        "- 4-5: Weak fit — retail, consumer, education, HR, finance with no obvious link.\n"
        "- 1-3: Not relevant — clearly unrelated industry or explicitly not a target.\n\n"
        "Reply with ONLY a single integer from 1 to 10. No explanation."
    )
    user_msg = (
        f"Contact: {name_info}\n"
        f"Company: {business_name}\n"
        f"Notes/Summary: {summary_info}"
    )

    try:
        client = _get_client()
        resp = client.chat.completions.create(
            model=_MODEL,
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_msg},
            ],
            max_tokens=5,
            temperature=0,
        )
        text = resp.choices[0].message.content.strip()
        score = int("".join(c for c in text if c.isdigit())[:2])
        score = max(1, min(10, score))
        logger.info("lead_scored", lead=business_name, contact=name_info, score=score)
        return score
    except Exception as exc:
        logger.warning("groq_lead_scoring_failed", error=str(exc), lead=business_name)
        return 7  # default: send


# ---------------------------------------------------------------------------
# Static fallback body (used when Groq is unavailable)
# ---------------------------------------------------------------------------

def _static_body(contact_name: str | None, business_name: str, summary: str | None = None) -> str:
    name_ref = contact_name or business_name

    # Para 1 — personal reference built from summary if available
    if summary:
        para1 = (
            f"I came across your profile while researching companies in the supply chain and "
            f"procurement space — {summary.strip().rstrip('.')}. That immediately caught my "
            f"attention given what we do at Jane Aerospace."
        )
    else:
        para1 = (
            f"I came across {business_name} while researching companies driving meaningful work "
            f"in procurement and supply chain innovation, and your name kept coming up in the "
            f"right conversations."
        )

    bodies_para2 = [
        (
            "At Jane Aerospace, we help companies like yours integrate vendor networks, "
            "streamline procurement workflows, and build resilient supply chains aligned with "
            "Atmanirbhar Bharat and unmanned aviation. I'd love a focused conversation — no "
            "pitch, just a genuine exchange on whether there's something worth exploring together."
        ),
        (
            "Jane Aerospace builds AI-enabled supply chain and procurement solutions for "
            "companies that need operational efficiency without the overhead. We also work in "
            "commercial drone products and aerospace consulting. Worth a conversation to see "
            "if there's a fit?"
        ),
        (
            "We're building India's one-stop procurement platform and supply chain infrastructure "
            "for the unmanned aviation sector. Given what you're doing at "
            f"{business_name}, I think there's a genuine intersection worth exploring. "
            "A brief call would tell us quickly."
        ),
    ]
    para2 = random.choice(bodies_para2)
    return f"{para1}\n\n{para2}"


# ---------------------------------------------------------------------------
# Outreach body generation (2 personalised paragraphs via Groq)
# ---------------------------------------------------------------------------

def generate_outreach_body(
    business_name: str,
    recipient_name: str,
    summary: str | None = None,
    contact_name: str | None = None,
) -> str:
    """Generate a 2-paragraph personalised outreach body using Groq.

    Para 1 — personal reference: weave in contact_name + summary notes naturally.
    Para 2 — Jane Aerospace pitch + soft CTA (no sign-off, no greeting).
    Falls back to static body if Groq fails.
    """
    if not settings.GROQ_API_KEY:
        logger.info("groq_key_missing_using_static_body")
        return _static_body(contact_name, business_name, summary)

    name_ref = contact_name or recipient_name

    system_prompt = (
        "You are a warm, professional B2B outreach writer for Leo Charles, founder of Jane Aerospace.\n\n"
        "Jane Aerospace:\n"
        "- Supply chain solutions: vendor integration, AI-enabled procurement, production optimisation\n"
        "- Commercial drone products and aerospace consulting (Atmanirbhar Bharat / unmanned aviation)\n\n"
        "Write EXACTLY 2 short paragraphs (no greeting, no sign-off, no subject line):\n"
        "  PARAGRAPH 1 (personal reference, ~40-55 words): Reference the contact's name and/or the "
        "provided notes/summary naturally — show you've done your homework. Make it feel personal "
        "and specific, not generic. Do NOT mention how you found them or say 'I came across'.\n"
        "  PARAGRAPH 2 (pitch + soft CTA, ~40-55 words): Explain what Jane Aerospace does and why "
        "it's relevant to them. End with a soft, non-pushy invitation to connect.\n\n"
        "STRICT RULES:\n"
        "- Do NOT say 'looking for', 'looking into', '30-minute', 'quick call', or '30 min'\n"
        "- Do NOT mention LinkedIn, social media, or how you found the lead\n"
        "- Do NOT include a greeting (Hi, Hello, Dear) or sign-off (Best, Thanks, Regards)\n"
        "- Total max 110 words across both paragraphs\n"
        "- Warm and direct tone — not robotic or salesy"
    )

    user_msg = (
        f"Contact Name: {name_ref}\n"
        f"Company: {business_name}\n"
    )
    if summary:
        user_msg += f"Notes/Context about this lead: {summary}\n"
    user_msg += "\nWrite the 2-paragraph email body now."

    try:
        client = _get_client()
        resp = client.chat.completions.create(
            model=_MODEL,
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_msg},
            ],
            max_tokens=220,
            temperature=0.80,
        )
        body = resp.choices[0].message.content.strip()
        if body:
            logger.info("outreach_body_generated", lead=business_name, contact=name_ref, chars=len(body))
            return body
    except Exception as exc:
        logger.warning("groq_email_generation_failed", error=str(exc))

    return _static_body(contact_name, business_name, summary)


# ---------------------------------------------------------------------------
# Reply intent analysis (classify lead reply + extract date constraints)
# ---------------------------------------------------------------------------

def analyze_reply_intent(reply_body: str, today_str: str) -> dict:
    """Analyze the lead's reply using Groq to detect booking intent and date constraints.

    Returns:
      {
        "intent": "book" | "list_slots" | "decline" | "unclear",
        "date": "YYYY-MM-DD" or None,
        "time": "HH:MM" or None,
        "after_date": "YYYY-MM-DD" or None,
        "week": "this" | "next" or None
      }
    """
    default_res = {
        "intent": "unclear",
        "date": None,
        "time": None,
        "after_date": None,
        "week": None,
    }
    body_lower = reply_body.lower()

    # Decline keywords — fast path
    decline_kws = ["not interested", "unsubscribe", "remove me", "no thanks", "no thank you", "stop emailing"]
    if any(w in body_lower for w in decline_kws):
        default_res["intent"] = "decline"
        return default_res

    # Week keywords — heuristic fallback
    this_kws = ["this week", "this one", "sooner", "asap", "as soon", "now", "current week", "today", "tomorrow"]
    next_kws = ["next week", "next one", "later", "after this", "following week", "week after"]
    if any(k in body_lower for k in this_kws):
        default_res["intent"] = "list_slots"
        default_res["week"] = "this"
    elif any(k in body_lower for k in next_kws):
        default_res["intent"] = "list_slots"
        default_res["week"] = "next"

    if not settings.GROQ_API_KEY:
        return default_res

    try:
        import json as _json
        client = _get_client()
        system_prompt = (
            f"Today is {today_str}.\n"
            "Analyze a lead's reply to a meeting invitation.\n"
            "Return ONLY valid JSON with these keys:\n"
            "{\n"
            "  \"intent\": \"book\" | \"list_slots\" | \"decline\" | \"unclear\",\n"
            "  \"date\": \"YYYY-MM-DD\" or null,\n"
            "  \"time\": \"HH:MM\" or null,\n"
            "  \"after_date\": \"YYYY-MM-DD\" or null,\n"
            "  \"week\": \"this\" | \"next\" or null\n"
            "}\n\n"
            "Guidelines:\n"
            "- \"decline\": not interested / unsubscribe / stop emailing.\n"
            "- \"book\": specific date AND time mentioned (e.g. 'Tuesday at 2pm'). "
            "  Set date+time. morning=09:00, afternoon=14:00, evening=17:00.\n"
            "- \"list_slots\": wants to see available slots, or specifies a range "
            "  (e.g. 'after Jan 8', 'next week', 'any time after Wednesday'). "
            "  Set after_date (inclusive start date) or week.\n"
            "- \"unclear\": positive but no time info ('sounds good', 'yes sure').\n"
            "- Resolve relative dates relative to today."
        )
        resp = client.chat.completions.create(
            model=_MODEL,
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": f"Reply: {reply_body[:600]}"},
            ],
            max_tokens=150,
            temperature=0,
        )
        text = resp.choices[0].message.content.strip()
        if text.startswith("```json"):
            text = text[7:]
        if text.endswith("```"):
            text = text[:-3]
        data = _json.loads(text.strip())
        if isinstance(data, dict) and "intent" in data:
            for key in ["date", "time", "after_date", "week"]:
                if key not in data or data[key] == "null":
                    data[key] = None
            logger.info("reply_intent_parsed", intent=data["intent"], after_date=data.get("after_date"), week=data.get("week"))
            return data
    except Exception as exc:
        logger.warning("groq_reply_intent_analysis_failed", error=str(exc))

    return default_res


# ---------------------------------------------------------------------------
# Legacy helpers (kept for compatibility)
# ---------------------------------------------------------------------------

def extract_slot_from_reply(reply_body: str, today_str: str) -> dict | None:
    """Legacy: extract specific date+time from reply. Returns {"date": ..., "time": ...} or None."""
    result = analyze_reply_intent(reply_body, today_str)
    if result["intent"] == "book" and result.get("date") and result.get("time"):
        return {"date": result["date"], "time": result["time"]}
    return None


def detect_week_preference(reply_body: str) -> str | None:
    """Legacy: detect 'this', 'next', or None from a reply."""
    result = analyze_reply_intent(reply_body, "")
    if result["intent"] == "decline":
        return None
    return result.get("week") or "next"
