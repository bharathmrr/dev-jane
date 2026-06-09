"""Claude-powered email body generation + lead scoring for Jane Aerospace outreach.

Pipeline:
  1. score_lead()           — Scores a lead 1-10 using contact name + summary notes.
                              Only leads with score >= 6 get Stage 1 emails.
  2. generate_outreach_body() — Writes 2 personalised paragraphs (para 1 = personal reference
                                from summary, para 2 = Jane Aerospace pitch + CTA).
  3. analyze_reply_intent() — Classifies lead reply: book | list_slots | decline | unclear.
  4. extract_slot_from_reply() — Legacy: extracts specific date+time from reply text.
  5. detect_week_preference()  — Legacy: heuristic this/next week detection.

Models:
  - claude-haiku-4-5  → scoring + intent (fast, low cost)
  - claude-sonnet-4-6 → outreach body generation (higher quality)

Rules enforced in all AI outputs:
  - Sender is Leo Charles, Jane Aerospace
  - Focus on supply chain, vendor integration, Atmanirbhar Bharat, drone manufacturing
  - Never use "looking for", "looking into", or "30-minute"
  - No LinkedIn or social media mentions
  - 2 short paragraphs, no greeting, no sign-off
"""
from __future__ import annotations

import json as _json
import random
import time as _time
from structlog import get_logger

from app.core.config import settings

logger = get_logger(__name__)

_HAIKU  = "claude-haiku-4-5"
_SONNET = "claude-sonnet-4-6"

_claude_client = None


def _get_client():
    global _claude_client
    if _claude_client is None:
        import anthropic
        _claude_client = anthropic.Anthropic(api_key=settings.ANTHROPIC_API_KEY)
    return _claude_client


def _ask(system: str, user: str, model: str, max_tokens: int = 256) -> str:
    """Call Claude with exponential backoff on rate-limit errors (#43).

    Retries: 5 s → 15 s → 45 s. After 3 failures returns '' so callers use fallback templates.
    """
    if not settings.ANTHROPIC_API_KEY:
        return ""
    delays = [5, 15, 45]
    for attempt, delay in enumerate(delays + [None], start=1):
        try:
            client = _get_client()
            msg = client.messages.create(
                model=model,
                max_tokens=max_tokens,
                system=system,
                messages=[{"role": "user", "content": user}],
            )
            return msg.content[0].text.strip()
        except Exception as exc:
            err = str(exc).lower()
            is_rate_limit = any(k in err for k in ("rate_limit", "ratelimit", "429", "overloaded", "too many"))
            if is_rate_limit and delay is not None:
                logger.warning("claude_rate_limit_retry", model=model, attempt=attempt, retry_in=delay)
                _time.sleep(delay)
            else:
                logger.warning("claude_api_error", model=model, attempt=attempt, error=str(exc))
                return ""
    return ""


# ---------------------------------------------------------------------------
# Lead Scoring (Contact Name + Summary/Notes → 1-10 score)
# ---------------------------------------------------------------------------

def score_lead(contact_name: str | None, business_name: str, summary: str | None) -> int:
    """Score a lead 1-10. Leads scoring < 6 are skipped. Falls back to 7 if AI unavailable."""
    if not settings.ANTHROPIC_API_KEY:
        logger.info("claude_key_missing_scoring_default_7", lead=business_name)
        return 7

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
        f"Contact: {contact_name or business_name}\n"
        f"Company: {business_name}\n"
        f"Notes/Summary: {summary or 'No additional notes available.'}"
    )

    text = _ask(system_prompt, user_msg, model=_HAIKU, max_tokens=5)
    if not text:
        return 7
    try:
        score = int("".join(c for c in text if c.isdigit())[:2])
        score = max(1, min(10, score))
        logger.info("lead_scored", lead=business_name, score=score)
        return score
    except Exception:
        logger.warning("claude_lead_scoring_parse_failed", raw=text)
        return 7


# ---------------------------------------------------------------------------
# Static fallback body (used when Claude is unavailable)
# ---------------------------------------------------------------------------

def _static_body(_contact_name: str | None, business_name: str, summary: str | None = None) -> str:
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
    return f"{para1}\n\n{random.choice(bodies_para2)}"


# ---------------------------------------------------------------------------
# Outreach body generation (2 personalised paragraphs via Claude Sonnet)
# ---------------------------------------------------------------------------

def generate_outreach_body(
    business_name: str,
    recipient_name: str,
    summary: str | None = None,
    contact_name: str | None = None,
) -> str:
    """Generate a 2-paragraph personalised outreach body using Claude Sonnet."""
    if not settings.ANTHROPIC_API_KEY:
        logger.info("claude_key_missing_using_static_body")
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

    body = _ask(system_prompt, user_msg, model=_SONNET, max_tokens=220)
    if body:
        logger.info("outreach_body_generated", lead=business_name, chars=len(body))
        return body

    return _static_body(contact_name, business_name, summary)


# ---------------------------------------------------------------------------
# Reply intent analysis (classify lead reply + extract date constraints)
# ---------------------------------------------------------------------------

def analyze_reply_intent(reply_body: str, today_str: str) -> dict:
    """Analyze a lead's reply using Claude Haiku. Returns classified intent + extracted fields.

    Intent values (17 total):
      book                — specific date + time mentioned, ready to book
      list_slots          — wants to see available slots (with optional filters)
      decline             — not interested / unsubscribe
      reschedule          — "wrong slot", "by mistake", "want to change"
      out_of_office       — OOO auto-reply (return_date extracted if present)
      question            — asking something about Jane Aerospace / supply chain
      callback_request    — "call me", phone number provided
      pricing             — asking about cost / pricing
      not_decision_maker  — "please contact my boss / colleague / manager"
      angry_negative      — hostile / angry tone → flag for human, NO AI reply
      assistant_reply     — "on behalf of X" → adjust tone, ask assistant to confirm
      job_change          — "I no longer work here, contact X" → new lead
      nda_request         — asking for NDA/legal docs before meeting → flag human
      case_study_request  — asking for case studies / references → template email
      existing_vendor     — "we already have a vendor" → complement reply
      deadline_mention    — mentions specific deadline / urgency → HIGH PRIORITY
      unclear             — positive but vague ("yes", "sure", "ok")

    Returns dict with all possible fields (None if not applicable).
    """
    default_res = {
        "intent": "unclear",
        "date": None,
        "time": None,
        "after_date": None,
        "week": None,
        "specific_date": None,
        "phone_number": None,
        "return_date": None,
        "question_text": None,
        "referred_to": None,
        "language": "english",          # detected reply language
        "deadline_text": None,          # extracted deadline string (#50)
        "on_behalf_of": None,           # name of actual decision maker (#10)
        "new_contact_name": None,       # from job_change (#16)
        "new_contact_email": None,      # from job_change (#16)
        "followup_date": None,          # "contact me next month" (#34)
    }
    body_lower = reply_body.lower()

    # ── Fast-path heuristics (pre-AI, catches obvious cases cheaply) ───────────

    # Angry / hostile — must be first to prevent AI from replying
    angry_kws = [
        "stop spamming", "spam", "harassment", "harassing", "leave me alone",
        "threatening", "report you", "block you", "scam", "fraud", "fake",
        "how dare", "disgusting", "pathetic", "waste of time", "stupid email",
        "delete my email", "never contact",
    ]
    if any(w in body_lower for w in angry_kws):
        default_res["intent"] = "angry_negative"
        return default_res

    # Hard decline
    decline_kws = ["not interested", "unsubscribe", "remove me", "no thanks",
                   "no thank you", "stop emailing", "do not contact", "don't contact"]
    if any(w in body_lower for w in decline_kws):
        default_res["intent"] = "decline"
        return default_res

    # OOO
    ooo_kws = ["out of office", "out of the office", "on leave", "on vacation",
               "will be back", "returning on", "back on", "away until", "i am currently away"]
    if any(w in body_lower for w in ooo_kws):
        default_res["intent"] = "out_of_office"

    # Reschedule
    reschedule_kws = ["wrong slot", "by mistake", "booked by mistake", "wrong time",
                      "accidentally", "can we change", "cancel and", "want to reschedule",
                      "need to reschedule", "clicked wrong"]
    if any(w in body_lower for w in reschedule_kws):
        default_res["intent"] = "reschedule"

    # Pricing
    pricing_kws = ["how much", "what is the cost", "pricing", "price",
                   "cost per", "fees", "charges", "budget"]
    if any(w in body_lower for w in pricing_kws):
        default_res["intent"] = "pricing"

    # Callback
    callback_kws = ["call me", "give me a call", "my number", "contact number",
                    "phone", "whatsapp", "mobile"]
    if any(w in body_lower for w in callback_kws):
        default_res["intent"] = "callback_request"

    # Job change
    job_kws = ["no longer work", "i have left", "left the company", "no longer employed",
               "has left the company", "my replacement", "new point of contact",
               "please update your records", "i resigned", "i departed"]
    if any(w in body_lower for w in job_kws):
        default_res["intent"] = "job_change"

    # On behalf of (assistant replies)
    behalf_kws = ["on behalf of", "dr.", "mr.", "ms.", "mrs.", "asked me to",
                  "instructed me to", "writing on behalf", "reaching out on behalf"]
    if any(w in body_lower for w in behalf_kws):
        default_res["intent"] = "assistant_reply"

    # NDA / legal docs
    nda_kws = ["nda", "non-disclosure", "confidentiality agreement", "legal",
               "sign something first", "paperwork first", "documentation first"]
    if any(w in body_lower for w in nda_kws):
        default_res["intent"] = "nda_request"

    # Case studies / references
    case_kws = ["case study", "case studies", "references", "past work", "portfolio",
                "examples", "who have you worked with", "clients", "customers"]
    if any(w in body_lower for w in case_kws):
        default_res["intent"] = "case_study_request"

    # Existing vendor
    vendor_kws = ["already have a vendor", "existing vendor", "currently use",
                  "already working with", "have a supplier", "using someone else",
                  "have a partner", "already contracted"]
    if any(w in body_lower for w in vendor_kws):
        default_res["intent"] = "existing_vendor"

    # Deadline / urgency (#50)
    deadline_kws = ["deadline", "expansion", "q1", "q2", "q3", "q4", "by march",
                    "by june", "by december", "this quarter", "next quarter",
                    "end of year", "urgent", "asap", "immediately", "launching"]
    if any(w in body_lower for w in deadline_kws) and default_res["intent"] == "unclear":
        default_res["intent"] = "deadline_mention"

    # Scheduled follow-up (#34) — "contact me next month", "reach out in July"
    followup_kws = ["contact me next", "reach out next", "email me in", "follow up in",
                    "try me in", "get back to me in", "after next month"]
    if any(w in body_lower for w in followup_kws) and default_res["intent"] == "unclear":
        default_res["intent"] = "list_slots"   # will be overridden by AI with followup_date

    # Week heuristics (fallback for list_slots)
    this_kws = ["this week", "sooner", "asap", "as soon", "current week", "today", "tomorrow"]
    next_kws = ["next week", "later", "after this", "following week", "week after"]
    if default_res["intent"] == "unclear":
        if any(k in body_lower for k in this_kws):
            default_res["intent"] = "list_slots"
            default_res["week"] = "this"
        elif any(k in body_lower for k in next_kws):
            default_res["intent"] = "list_slots"
            default_res["week"] = "next"

    if not settings.ANTHROPIC_API_KEY:
        return default_res

    system_prompt = (
        f"Today is {today_str}.\n"
        "Classify a lead's reply to a Jane Aerospace meeting invite. Return ONLY valid JSON with these exact keys:\n"
        "{\n"
        "  \"intent\": string,\n"
        "  \"date\": \"YYYY-MM-DD\" or null,\n"
        "  \"time\": \"HH:MM\" or null,\n"
        "  \"after_date\": \"YYYY-MM-DD\" or null,\n"
        "  \"week\": \"this\" | \"next\" or null,\n"
        "  \"specific_date\": \"YYYY-MM-DD\" or null,\n"
        "  \"phone_number\": string or null,\n"
        "  \"return_date\": \"YYYY-MM-DD\" or null,\n"
        "  \"question_text\": string or null,\n"
        "  \"referred_to\": string or null,\n"
        "  \"language\": string (e.g. \"english\", \"hindi\", \"tamil\", \"telugu\", \"kannada\"),\n"
        "  \"deadline_text\": string or null (e.g. 'Q3 expansion', 'by December'),\n"
        "  \"on_behalf_of\": string or null (actual decision maker's name),\n"
        "  \"new_contact_name\": string or null,\n"
        "  \"new_contact_email\": string or null,\n"
        "  \"followup_date\": \"YYYY-MM-DD\" or null\n"
        "}\n\n"
        "Intent values:\n"
        "- \"book\": specific date AND time given. Set date + time (24h). morning=09:00, afternoon=14:00, evening=17:00.\n"
        "- \"list_slots\": wants available slots. Set specific_date (single day), after_date (range), or week (this/next).\n"
        "- \"decline\": not interested / unsubscribe / stop emailing / no thank you.\n"
        "- \"reschedule\": 'wrong slot', 'by mistake', 'booked accidentally', 'cancel and rebook'.\n"
        "- \"out_of_office\": OOO auto-reply. Extract return_date if mentioned.\n"
        "- \"question\": asking about Jane Aerospace, supply chain, services. Set question_text.\n"
        "- \"callback_request\": asking for a phone call, giving phone number. Extract phone_number.\n"
        "- \"pricing\": asking about cost, pricing, fees.\n"
        "- \"not_decision_maker\": 'contact my boss', 'speak to our manager'. Extract referred_to.\n"
        "- \"angry_negative\": hostile, angry, threatening, calling it spam/scam.\n"
        "- \"assistant_reply\": 'on behalf of X', 'Dr. X asked me'. Extract on_behalf_of.\n"
        "- \"job_change\": 'I no longer work here', 'left the company'. Extract new_contact_name and new_contact_email if present.\n"
        "- \"nda_request\": asking for NDA/confidentiality agreement before meeting.\n"
        "- \"case_study_request\": asking for case studies, references, examples.\n"
        "- \"existing_vendor\": 'we already have a vendor/supplier/partner'.\n"
        "- \"deadline_mention\": mentions specific deadline or urgency (Q3, expansion, launching). Extract deadline_text.\n"
        "- \"unclear\": positive but vague ('yes', 'ok', 'sure', 'sounds good', 'interested').\n"
        "Rules: resolve relative days to next occurrence. Do NOT set both specific_date and after_date.\n"
        "Detect language accurately — many leads write in Hindi, Tamil, or other Indian languages."
    )

    text = _ask(system_prompt, f"Reply:\n{reply_body[:800]}", model=_HAIKU, max_tokens=350)
    if not text:
        return default_res

    try:
        clean = text.strip()
        for fence in ("```json", "```"):
            if clean.startswith(fence):
                clean = clean[len(fence):]
        if clean.endswith("```"):
            clean = clean[:-3]

        data = _json.loads(clean.strip())
        if isinstance(data, dict) and "intent" in data:
            all_keys = [
                "date", "time", "after_date", "week", "specific_date",
                "phone_number", "return_date", "question_text", "referred_to",
                "language", "deadline_text", "on_behalf_of",
                "new_contact_name", "new_contact_email", "followup_date",
            ]
            for key in all_keys:
                if key not in data or data[key] == "null":
                    data[key] = default_res.get(key)
            if not data.get("language"):
                data["language"] = "english"
            logger.info("reply_intent_parsed", intent=data["intent"],
                        lang=data.get("language"),
                        week=data.get("week"), specific_date=data.get("specific_date"))
            return data
    except Exception as exc:
        logger.warning("claude_reply_intent_parse_failed", error=str(exc), raw=text)

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
