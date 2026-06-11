"""Jane Aerospace outreach email generator — fixed professional templates.

Fixed templates replace AI generation for consistent, professional messaging.
A/B/C variants track which body angle performs best (reply rate).
"""
from __future__ import annotations

import random
from structlog import get_logger

from app.core.config import settings
from app.services.slot_lock import get_variant_weights

logger = get_logger(__name__)

_SONNET = "claude-sonnet-4-6"
_HAIKU = "claude-haiku-4-5"

_client = None


def _get_client():
    global _client
    if _client is None:
        import anthropic
        _client = anthropic.Anthropic(api_key=settings.ANTHROPIC_API_KEY)
    return _client


def _ask(system: str, user: str, model: str = _SONNET, max_tokens: int = 200) -> str:
    """Used only for reply-handling (questions, pricing, etc.) — not outreach."""
    if not settings.ANTHROPIC_API_KEY:
        return ""
    try:
        msg = _get_client().messages.create(
            model=model,
            max_tokens=max_tokens,
            system=system,
            messages=[{"role": "user", "content": user}],
        )
        return msg.content[0].text.strip()
    except Exception as exc:
        logger.warning("marketing_agent_api_error", error=str(exc))
        return ""


# ---------------------------------------------------------------------------
# Subject lines — professional, sector-relevant
# ---------------------------------------------------------------------------

def _subject_s1(_contact_name: str | None) -> str:
    return "Aerospace & Defence Supply Chain Solutions — Jane Aerospace"


def _subject_s2(business_name: str) -> str:
    return f"Partnership Opportunity — Jane Aerospace & {business_name}"


def _subject_s3(_designation: str | None, _location: str | None) -> str:
    return "Strategic Collaboration — Jane Aerospace"


# ---------------------------------------------------------------------------
# Fixed professional body templates (Variant A / B / C)
# ---------------------------------------------------------------------------

def _body_a(business_name: str, contact_name: str | None,
            _designation: str | None, is_repeat_lead: bool) -> str:
    name_ref = (contact_name or business_name).split()[0].capitalize()
    if is_repeat_lead:
        return (
            f"It was great speaking with you previously, {name_ref}, and I wanted to reconnect "
            f"with a quick update on what we have been building at Jane Aerospace.\n\n"
            f"Since we last spoke, we have deepened our work in defence-grade vendor qualification, "
            f"AI-enabled procurement, and supply chain resilience — areas I believe remain highly "
            f"relevant to {business_name}.\n\n"
            f"I would welcome a brief 20-minute call to catch up and explore where we might collaborate."
        )
    return (
        f"My name is Leo Peter Charles — I am the Founder of Jane Aerospace, India's first integrated "
        f"aerospace supply chain company, which I established in 2019 after working extensively in the "
        f"unmanned aviation and defence ecosystem.\n\n"
        f"We specialise in end-to-end supply chain solutions for the aerospace and defence sector — "
        f"vendor qualification, procurement optimisation, and AI-enabled sourcing. We have been "
        f"recognised among India's Top 10 Aerospace & Defence Startups and I was named in the "
        f"'40 Under 40 India 2025' leaders list.\n\n"
        f"I came across {business_name} and believe there is a strong strategic fit worth a short "
        f"conversation. I would appreciate 20 minutes of your time — no obligations, just a focused call."
    )


def _body_b(business_name: str, _contact_name: str | None,
            _designation: str | None, location: str | None) -> str:
    sector_note = f" in the {location} region" if location else ""
    return (
        f"I am Leo Peter Charles, Founder of Jane Aerospace — I started the company in 2019 after "
        f"years in the drone and defence supply chain space{sector_note}, and we have since grown "
        f"into India's leading aerospace supply chain services firm.\n\n"
        f"We work alongside procurement and operations teams at aerospace and defence companies to "
        f"reduce sourcing lead times, qualify vendors to international standards, and build resilient "
        f"supply networks — helping organisations like {business_name} operate with greater efficiency.\n\n"
        f"Would a brief 20-minute call this week or next give us the chance to see if there is a fit?"
    )


def _body_c(business_name: str, _contact_name: str | None, summary: str | None) -> str:
    context = ""
    if summary:
        snippet = summary.strip().rstrip(".").split(".")[0]
        context = f" — particularly in areas like {snippet.lower()}" if snippet else ""
    return (
        f"I am Leo Peter Charles, Founder of Jane Aerospace — recognised in the '40 Under 40 India "
        f"2025' list for our work in aerospace supply chain innovation.\n\n"
        f"We have helped companies{context} reduce procurement cycle times, improve vendor "
        f"qualification, and build more resilient sourcing networks within the aerospace and defence "
        f"sector. Our approach combines deep domain expertise with AI-enabled tooling.\n\n"
        f"Given what your team is doing at {business_name}, I believe there is a meaningful fit worth "
        f"exploring. I would be glad to share relevant case studies on a concise 20-minute call — "
        f"no commitment required."
    )


# ---------------------------------------------------------------------------
# Main entrypoint
# ---------------------------------------------------------------------------

def _generate_ai_outreach(
    business_name: str,
    contact_name: str | None,
    designation: str | None,
    location: str | None,
    summary: str | None,
    is_repeat_lead: bool = False,
) -> tuple[str, str] | None:
    """Use Claude to write a fully personalised cold outreach email.

    Returns (subject, body) or None if AI unavailable.
    """
    if not settings.ANTHROPIC_API_KEY:
        return None

    role_line = f"Their designation: {designation}" if designation else "Their role is not specified."
    location_line = f"They are based in {location}." if location else ""
    summary_line = f"Company background: {summary}" if summary else ""
    repeat_line = (
        "IMPORTANT: This person has engaged with Jane Aerospace before. "
        "Open with a warm reconnect — do NOT introduce Jane from scratch."
        if is_repeat_lead else ""
    )

    system = (
        "You are Leo Peter Charles — Founder & Managing Director of Jane Aerospace, "
        "India's first integrated aerospace supply chain company (est. 2019, Bengaluru). "
        "You are an aeronautical engineer with 10+ years in the drone, unmanned aviation, "
        "and defence supply chain ecosystem. You were named in '40 Under 40 India 2025'. "
        "Jane Aerospace has been recognised as one of India's Top 10 Aerospace & Defence Startups.\n\n"
        "Write a cold outreach email to a potential business lead. Rules:\n"
        "- Address the person BY NAME if provided. Use first name only.\n"
        "- ALWAYS reference their SPECIFIC ROLE/DESIGNATION in the opening line — "
        "  show you know who they are and what they do.\n"
        "- Reference their SPECIFIC COMPANY by name — make it clear this is not a bulk email.\n"
        "- Use the company summary/background to draw ONE specific connection to Jane Aerospace's work.\n"
        "- Introduce yourself as Leo Peter Charles, Founder of Jane Aerospace, "
        "  briefly (1 sentence) — aeronautical engineer, 10+ years in the sector.\n"
        "- Explain in 1-2 sentences what Jane Aerospace does that is RELEVANT to their business.\n"
        "- End with a clear, low-pressure CTA: request a 20-minute call.\n"
        "- Total length: 4-5 short paragraphs. NO bullet points. NO jargon. "
        "  Sound like a real person writing a real email, not a sales template.\n"
        "- DO NOT include a greeting line (no 'Hi X,') — that is added separately.\n"
        "- DO NOT include a sign-off — that is added separately.\n"
        "- Return ONLY the email body paragraphs. Nothing else.\n\n"
        "Also generate a subject line: professional, specific to their company or role, "
        "under 60 characters. Return it on the VERY FIRST LINE prefixed with 'SUBJECT: ', "
        "then a blank line, then the body."
    )

    user_parts = [
        f"Lead name: {contact_name or 'Unknown'}",
        f"Company: {business_name}",
        role_line,
        location_line,
        summary_line,
        repeat_line,
    ]
    user = "\n".join(p for p in user_parts if p)

    try:
        raw = _ask(system, user, model=_SONNET, max_tokens=500)
        if not raw:
            return None
        lines = raw.strip().split("\n")
        subject = ""
        body_lines = []
        for i, line in enumerate(lines):
            if line.startswith("SUBJECT:"):
                subject = line.replace("SUBJECT:", "").strip()
            elif subject and line.strip():
                body_lines = lines[i:]
                break
        body = "\n\n".join(
            p.strip() for p in "\n".join(body_lines).split("\n\n") if p.strip()
        )
        if not subject:
            subject = f"Partnership Opportunity Jane Aerospace & {business_name}"
        return subject, body
    except Exception as exc:
        logger.warning("ai_outreach_failed", company=business_name, error=str(exc))
        return None


def generate_outreach(
    business_name: str,
    contact_name: str | None,
    designation: str | None,
    location: str | None,
    summary: str | None,
    is_repeat_lead: bool = False,
) -> dict:
    """Generate a personalised outreach email.

    Tries AI-generated personalisation first (using Claude).
    Falls back to fixed templates if AI is unavailable.

    Returns:
        {
          "body": str,
          "subject": str,
          "variant": str,        # "AI", "A", "B", or "C"
          "subject_variant": str # "AI" or "S1"/"S2"/"S3"
        }
    """
    # Try AI personalisation first
    ai_result = _generate_ai_outreach(
        business_name=business_name,
        contact_name=contact_name,
        designation=designation,
        location=location,
        summary=summary,
        is_repeat_lead=is_repeat_lead,
    )
    if ai_result:
        subject, body = ai_result
        logger.info("ai_outreach_generated", company=business_name, designation=designation)
        return {"body": body, "subject": subject, "variant": "AI", "subject_variant": "AI"}

    # Fallback to fixed templates
    weights = get_variant_weights()
    variant = random.choices(["A", "B", "C"], weights=weights, k=1)[0]
    subject_variant = random.choice(["S1", "S2", "S3"])

    if subject_variant == "S1":
        subject = _subject_s1(contact_name)
    elif subject_variant == "S2":
        subject = _subject_s2(business_name)
    else:
        subject = _subject_s3(designation, location)

    if variant == "A":
        body = _body_a(business_name, contact_name, designation, is_repeat_lead)
    elif variant == "B":
        body = _body_b(business_name, contact_name, designation, location)
    else:
        body = _body_c(business_name, contact_name, summary)

    logger.info("template_outreach_generated", company=business_name, variant=variant)
    return {"body": body, "subject": subject, "variant": variant, "subject_variant": subject_variant}


# ---------------------------------------------------------------------------
# Reply handlers — Marketing Expert Agent generates contextual replies
# ---------------------------------------------------------------------------

def generate_question_reply(
    contact_name: str | None,
    business_name: str,
    question: str,
) -> str:
    """Generate a human reply to a lead's question about Jane Aerospace."""
    system = (
        "You are Leo Charles, Founder of Jane Aerospace — a supply chain as a service company.\n"
        "A potential lead has asked a question. Answer it conversationally, honestly, and briefly.\n"
        "Keep it to 2-3 sentences. Sound like a real person replying to an email, not a sales pitch.\n"
        "End with a gentle nudge toward a quick call.\n"
        "Do NOT use bullet points. No jargon. No 'Great question!' opener.\n"
        "Return ONLY the reply body (no greeting, no sign-off)."
    )
    user = f"Lead: {contact_name or 'Unknown'} at {business_name}\nTheir question: {question}"
    reply = _ask(system, user, max_tokens=150)
    return reply or (
        "Happy to answer that — it's a good question and comes up a lot. "
        "Would it be easier to walk through it on a quick call? "
        "15 minutes and I can give you a much clearer picture than email allows."
    )


def generate_pricing_reply(contact_name: str | None, business_name: str) -> str:
    """Generate a reply to a pricing inquiry."""
    system = (
        "You are Leo Charles, Founder of Jane Aerospace.\n"
        "A lead has asked about pricing/cost. Respond honestly:\n"
        "- We structure pricing based on scope, volume, and the services needed\n"
        "- Not a fixed-price product — it's a service, so it depends on what they need\n"
        "- The right next step is a call to understand their situation first\n"
        "Keep it 2-3 sentences. Human. No jargon. No bullet points.\n"
        "Return ONLY the body."
    )
    user = f"Lead: {contact_name or 'someone'} at {business_name} asking about pricing."
    reply = _ask(system, user, max_tokens=120)
    return reply or (
        "Pricing depends on the scope — we tailor the engagement to what each company actually needs, "
        "so there's no single number I can give without understanding your setup first. "
        "A quick call would let me give you a much more honest answer. Worth 15 minutes?"
    )


def generate_not_decision_maker_reply(
    contact_name: str | None,
    business_name: str,
    referred_to: str | None,
) -> str:
    """Generate a reply when the contact says they're not the decision maker."""
    ref = referred_to or "the right person"
    system = (
        "You are Leo Charles, Founder of Jane Aerospace.\n"
        "The contact has said they're not the decision maker and referred you to someone else.\n"
        "Respond graciously, ask if they'd like a one-paragraph overview to share internally.\n"
        "2-3 sentences max. Very warm. No pressure.\n"
        "Return ONLY the body."
    )
    user = f"Contact: {contact_name or 'them'} at {business_name}. They referred to: {ref}."
    reply = _ask(system, user, max_tokens=120)
    return reply or (
        f"Completely understood — happy to connect with {ref} directly. "
        "If it helps, I can send over a short one-pager you could share internally so they have some context before we speak. "
        "Just let me know what works best."
    )


def generate_case_study_reply(contact_name: str | None, business_name: str) -> str:
    """Reply to a case study / references request (#47)."""
    system = (
        "You are Leo Charles, Founder of Jane Aerospace.\n"
        "A lead has asked for case studies or references. Reply warmly:\n"
        "- Mention we've worked with companies in manufacturing, aerospace, and FMCG sectors\n"
        "- We can share a brief one-pager with outcomes and references on a call (not over email for confidentiality)\n"
        "- Soft CTA toward a quick call\n"
        "2-3 sentences. No bullet points. Human tone.\n"
        "Return ONLY the body."
    )
    user = f"Contact: {contact_name or 'someone'} at {business_name} asking for case studies."
    reply = _ask(system, user, max_tokens=130)
    return reply or (
        "Happy to share — we've worked with companies in manufacturing, aerospace supply chains, and FMCG distribution, "
        "and the outcomes have been pretty significant in terms of lead time reduction and cost visibility. "
        "I prefer to walk through specifics on a call rather than email (some of it is confidential to our clients) — worth 15 minutes?"
    )


def generate_existing_vendor_reply(contact_name: str | None, business_name: str) -> str:
    """Reply when lead says they already have a vendor (#48)."""
    system = (
        "You are Leo Charles, Founder of Jane Aerospace.\n"
        "A lead says they already have a vendor for supply chain. Reply with:\n"
        "- Acknowledge it — don't be defensive\n"
        "- Jane Aerospace often works ALONGSIDE existing vendors, not replacing them\n"
        "- We fill specific gaps — vendor intelligence, procurement tech, or a particular category\n"
        "- Soft CTA: 'even if it's just a comparison, worth 15 min?'\n"
        "2-3 sentences. No pushiness. Peer tone.\n"
        "Return ONLY the body."
    )
    user = f"Contact: {contact_name or 'someone'} at {business_name}. They have an existing vendor."
    reply = _ask(system, user, max_tokens=130)
    return reply or (
        "Totally fair — most companies we work with already have vendors in place, and we're not looking to displace anyone. "
        "We tend to come in alongside existing setups to handle specific gaps — vendor intelligence, a particular category, or procurement technology. "
        "Even just a 15-minute comparison might be worth it."
    )


def generate_deadline_reply(
    contact_name: str | None,
    business_name: str,
    deadline_text: str | None,
) -> str:
    """Reply when lead mentions a specific deadline or urgency (#50)."""
    deadline_ref = f"your {deadline_text}" if deadline_text else "your upcoming timeline"
    system = (
        "You are Leo Charles, Founder of Jane Aerospace.\n"
        "A lead has mentioned a specific deadline or upcoming expansion/launch.\n"
        "Acknowledge the timing urgency — this is actually a great reason to talk NOW rather than later.\n"
        "Be energetic but not pushy. Reference their specific deadline/context.\n"
        "End with a very direct CTA: 'Can we get 20 minutes this week before that gets closer?'\n"
        "2-3 sentences max.\n"
        "Return ONLY the body."
    )
    user = f"Contact: {contact_name or 'someone'} at {business_name}. Deadline/urgency context: {deadline_ref}."
    reply = _ask(system, user, max_tokens=130)
    return reply or (
        f"Given {deadline_ref}, the timing actually makes a lot of sense to talk now rather than after things get hectic. "
        "Supply chain decisions made early in an expansion tend to save a lot of pain later — and that's exactly what Jane Aerospace helps with. "
        "Can we find 20 minutes this week before that gets closer?"
    )


def generate_assistant_reply(
    contact_name: str | None,
    business_name: str,
    on_behalf_of: str | None,
) -> str:
    """Reply when someone is writing on behalf of the actual decision maker (#10)."""
    principal = on_behalf_of or "the team"
    system = (
        "You are Leo Charles, Founder of Jane Aerospace.\n"
        "An assistant is replying on behalf of a decision maker.\n"
        "Acknowledge the assistant warmly, thank them for reaching out.\n"
        "Ask them to confirm a convenient time for their principal, or offer to send a brief overview "
        "they can share before the call.\n"
        "2-3 sentences. Respectful and efficient.\n"
        "Return ONLY the body."
    )
    user = f"Assistant at {business_name} writing on behalf of {principal}."
    reply = _ask(system, user, max_tokens=120)
    return reply or (
        f"Thank you for reaching out on {principal}'s behalf — I appreciate you coordinating. "
        "Would it be easiest to share a brief one-pager that you could pass along, "
        "or would you prefer to pick a time directly for a short call?"
    )


def generate_nda_reply(contact_name: str | None, business_name: str) -> str:
    """Reply when lead asks for NDA/legal docs before meeting (#46)."""
    system = (
        "You are Leo Charles, Founder of Jane Aerospace.\n"
        "A lead has asked for an NDA or legal agreement before the initial meeting.\n"
        "Respond professionally: completely understandable, we can arrange this.\n"
        "Note that the initial call is exploratory and non-committal (no confidential info shared).\n"
        "But if they'd feel more comfortable with an NDA first, we can arrange that — flag it to the team.\n"
        "2-3 sentences. Professional tone.\n"
        "Return ONLY the body."
    )
    user = f"Contact: {contact_name or 'someone'} at {business_name} requesting NDA before meeting."
    reply = _ask(system, user, max_tokens=120)
    return reply or (
        "Completely understood — happy to arrange that. "
        "The initial call is exploratory and nothing confidential would be shared, but if you'd feel more comfortable "
        "with a mutual NDA in place first, I'll have our team get that across to you promptly."
    )


def generate_multilingual_reply(
    contact_name: str | None,
    business_name: str,
    original_question: str,
    language: str,
) -> str:
    """Generate a reply in the same language as the lead's message (#6).

    reply_type: "general" | "slots_offer" | "question_answer"
    """
    system = (
        f"You are Leo Charles, Founder of Jane Aerospace — India's supply chain as a service company.\n"
        f"The lead wrote to you in {language}. Reply ENTIRELY in {language}.\n"
        "Keep it warm, brief (2-3 sentences), and professional.\n"
        "If their message was a question about supply chain or your services, answer it.\n"
        "If it was a vague positive reply, offer to share available meeting slots.\n"
        "End with a soft CTA.\n"
        "Return ONLY the reply body — no greeting, no sign-off."
    )
    user = (
        f"Lead: {contact_name or 'someone'} at {business_name}\n"
        f"Their message: {original_question[:400]}"
    )
    reply = _ask(system, user, max_tokens=200)
    return reply or (
        "Thank you for your message. I would love to discuss how Jane Aerospace can help. "
        "Could we arrange a brief call this week?"
    )


def generate_high_intent_nudge(contact_name: str | None, business_name: str) -> str:
    """Nudge for leads who open email 5+ times but never click (#3)."""
    system = (
        "You are Leo Charles, Founder of Jane Aerospace.\n"
        "This lead has opened our email multiple times but hasn't clicked to book a slot.\n"
        "Send a very short, human, non-pushy follow-up:\n"
        "- Acknowledge they may have had questions\n"
        "- Offer to answer anything before they book\n"
        "- Keep it 2 sentences. Warm. Zero pressure.\n"
        "Return ONLY the body."
    )
    first = (contact_name or "").split()[0].capitalize() if contact_name else "there"
    user = f"Lead: {first} at {business_name}"
    reply = _ask(system, user, max_tokens=80)
    return reply or (
        "Saw you had a look at our email — happy to answer any questions before we find a time to connect. "
        "No pressure at all — just reply here if anything comes to mind."
    )


def generate_pending_booking_nudge(contact_name: str | None, business_name: str, slot: str) -> str:
    """Nudge when lead clicked a slot but didn't confirm within 20 min (#17)."""
    system = (
        "You are Leo Charles, Founder of Jane Aerospace.\n"
        "A lead started to book a slot but didn't complete the confirmation.\n"
        "Send a gentle, very short 1-2 sentence nudge.\n"
        "Reference their specific slot. Offer an alternative if the slot no longer works.\n"
        "Return ONLY the body."
    )
    first = (contact_name or "").split()[0].capitalize() if contact_name else "there"
    user = f"Lead: {first} at {business_name}. Slot they started: {slot}"
    reply = _ask(system, user, max_tokens=80)
    return reply or (
        f"Noticed you started booking the {slot} slot — still want to lock that in? "
        "If that time no longer works, just reply and I'll find you something better."
    )


def generate_no_show_reply(contact_name: str | None, business_name: str, slot: str) -> str:
    """Empathetic reply after a no-show (#27)."""
    system = (
        "You are Leo Charles, Founder of Jane Aerospace.\n"
        "A lead missed their scheduled meeting. Send an empathetic, zero-guilt follow-up.\n"
        "'Things come up' — offer to reschedule. Keep it 2 sentences. Warm, not passive-aggressive.\n"
        "Return ONLY the body."
    )
    first = (contact_name or "").split()[0].capitalize() if contact_name else "there"
    user = f"Lead: {first} at {business_name}. Missed slot: {slot}"
    reply = _ask(system, user, max_tokens=80)
    return reply or (
        f"Things come up — no worries at all about missing the {slot} call. "
        "Happy to find another time whenever works for you."
    )


def generate_zoho_cancelled_reply(contact_name: str | None, business_name: str, slot: str) -> str:
    """Reply when lead cancels directly via Zoho (webhook) (#26)."""
    system = (
        "You are Leo Charles, Founder of Jane Aerospace.\n"
        "A lead cancelled their booking directly via the calendar system.\n"
        "Send a warm, zero-pressure reply. Offer to find a better time. 2 sentences.\n"
        "Return ONLY the body."
    )
    first = (contact_name or "").split()[0].capitalize() if contact_name else "there"
    user = f"Lead: {first} at {business_name}. Cancelled slot: {slot}"
    reply = _ask(system, user, max_tokens=80)
    return reply or (
        f"Sorry to see the {slot} booking get cancelled — completely understand. "
        "Whenever you'd like to find a better time, just let me know and I'll sort it out straight away."
    )


def generate_scheduled_followup_confirm(
    contact_name: str | None,
    business_name: str,
    followup_date: str,
) -> str:
    """Confirmation reply when lead says 'contact me next month / in July' (#34)."""
    system = (
        "You are Leo Charles, Founder of Jane Aerospace.\n"
        "A lead has asked to be contacted at a specific future date. Confirm you'll do that.\n"
        "Be warm and specific about the date. 2 sentences max.\n"
        "Return ONLY the body."
    )
    first = (contact_name or "").split()[0].capitalize() if contact_name else "there"
    user = f"Lead: {first} at {business_name}. They want to be contacted on: {followup_date}"
    reply = _ask(system, user, max_tokens=80)
    return reply or (
        f"Noted — I'll reach out on {followup_date} and we can go from there. "
        "Looking forward to it."
    )


def generate_same_company_slot_reply(
    contact_name: str | None,
    business_name: str,
    colleague_name: str | None,
    colleague_slot: str,
) -> str:
    """Reply when another person from the same company already has a slot (#24)."""
    colleague_ref = colleague_name or "a colleague"
    system = (
        "You are Leo Charles, Founder of Jane Aerospace.\n"
        "A lead tried to book a slot, but someone from the same company already has a slot with us.\n"
        "Let them know warmly. Offer a different available slot or ask if they'd like to join the same call.\n"
        "2 sentences. No awkwardness.\n"
        "Return ONLY the body."
    )
    first = (contact_name or "").split()[0].capitalize() if contact_name else "there"
    user = f"Lead: {first} at {business_name}. Colleague ({colleague_ref}) already booked: {colleague_slot}"
    reply = _ask(system, user, max_tokens=100)
    return reply or (
        f"Interestingly, {colleague_ref} from {business_name} already has a call with us on {colleague_slot} — "
        "happy to either add you to that same call or find you a separate slot if you'd prefer."
    )
