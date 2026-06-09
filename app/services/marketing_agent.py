"""Marketing Expert AI Agent for Jane Aerospace lead outreach.

Generates short, human-tone, personalised cold emails using Claude Sonnet.
Implements 3 A/B body variants + 3 subject line variants.

Body variants (angle):
  A - Challenge: opens by naming the lead's specific pain point
  B - Industry insight: FOMO / "most companies in your space are still..."
  C - Social proof: "we work with companies like yours..."

Subject variants:
  S1 - "Quick question, [FirstName]"
  S2 - "[CompanyName] — supply chain"
  S3 - "15 min? [Industry/role context]"

Email rules (non-negotiable):
  - Max 4 sentences in body
  - Supply chain as a SERVICE (we own your supply chain, not sell software)
  - Human tone — sounds like Leo wrote it personally at 8am
  - No "I hope this email finds you well"
  - No "I wanted to reach out"
  - No "I came across your profile"
  - No bullet points, no headers
  - Body only — greeting ("Hi [Name],") and sign-off ("Leo") added separately
  - CTA is always soft: "Worth a quick call?" / "15 minutes this week?"
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
# Variant A — Challenge Angle
# ---------------------------------------------------------------------------

_SYSTEM_A = """You are Leo Charles, Founder of Jane Aerospace — India's supply chain as a service company.

Write 3-4 sentences for a cold email using the CHALLENGE ANGLE.
Open by naming the lead's specific operational challenge based on their role, company, location and summary.
Then one sentence: how Jane Aerospace as a dedicated supply chain partner (not a vendor, not software — a service) solves it.
End with a soft CTA like "Worth a quick call this week?" or "15 minutes?"

STRICT RULES:
- Max 4 sentences total. Short. Punchy.
- Do NOT use: "I hope", "I wanted to reach out", "I came across", "Please find", "Kindly", "As per"
- Do NOT use bullet points or numbered lists
- Sound like a real person wrote this at 8am — not an AI
- Supply chain as a service = Jane Aerospace becomes your supply chain team, end-to-end
- No greeting (we add "Hi [Name]," separately), no sign-off (we add "Leo" separately)
- Return ONLY the body sentences, nothing else"""

_SYSTEM_B = """You are Leo Charles, Founder of Jane Aerospace — India's supply chain as a service company.

Write 3-4 sentences for a cold email using the INDUSTRY INSIGHT ANGLE.
Start with an observation about what most companies in their industry or region are still doing wrong in supply chain.
Create a slight FOMO — they're operating like it's 5 years ago.
Then: Jane Aerospace as a supply chain as a service partner changes this.
Soft CTA at the end.

STRICT RULES:
- Max 4 sentences total
- Start with "Most [industry] companies..." or "A lot of [role]s in [location]..." or similar
- Do NOT use: "I hope", "I wanted to reach out", "I came across", bullets, long sentences
- Sound exactly like a human — informal but credible
- No greeting, no sign-off — just the body
- Return ONLY the body sentences"""

_SYSTEM_C = """You are Leo Charles, Founder of Jane Aerospace — India's supply chain as a service company.

Write 3-4 sentences for a cold email using the SOCIAL PROOF ANGLE.
Reference that we've recently worked with companies similar to theirs (same industry or scale).
Mention a specific outcome or capability — what changed for them.
Connect it to this lead's situation.
Soft CTA.

STRICT RULES:
- Max 4 sentences total
- Do NOT invent company names — say "a [type of company] in [similar location/industry]"
- Do NOT use: "I hope", "I wanted to reach out", "I came across", bullets
- Very human, warm, peer-to-peer tone
- No greeting, no sign-off
- Return ONLY the body sentences"""


def _build_user_msg(business_name: str, contact_name: str | None, designation: str | None,
                    location: str | None, summary: str | None) -> str:
    parts = [f"Company: {business_name}"]
    if contact_name:
        parts.append(f"Contact: {contact_name}")
    if designation:
        parts.append(f"Designation: {designation}")
    if location:
        parts.append(f"Location: {location}")
    if summary:
        parts.append(f"Context/Summary: {summary}")
    parts.append("\nWrite the email body now.")
    return "\n".join(parts)


# ---------------------------------------------------------------------------
# Subject line variants
# ---------------------------------------------------------------------------

def _subject_s1(contact_name: str | None) -> str:
    first = (contact_name or "").split()[0].capitalize() if contact_name else "there"
    return f"Quick question, {first}"


def _subject_s2(business_name: str) -> str:
    short = business_name.split()[0] if business_name else business_name
    return f"{short} — supply chain"


def _subject_s3(designation: str | None, location: str | None) -> str:
    if designation:
        role_word = designation.split()[0]
        return f"15 min? {role_word} × supply chain"
    if location:
        return f"Supply chain in {location} — 15 min?"
    return "15 minutes — Jane Aerospace"


# ---------------------------------------------------------------------------
# Fallback bodies (when Claude is unavailable)
# ---------------------------------------------------------------------------

_FALLBACKS_A = [
    "Managing supply chain end-to-end while running a growing business pulls your team in too many directions. Jane Aerospace works as your dedicated supply chain partner — we own the coordination, vendor relationships, and delivery so you can focus on growth. Worth a quick call this week?",
    "The hardest part of scaling isn't the product — it's keeping the supply chain from becoming the bottleneck. Jane Aerospace runs supply chain as a service: one partner, full accountability, zero chaos on your end. 15 minutes to see if it fits?",
]

_FALLBACKS_B = [
    "Most growing companies in India are still running supply chain the old way — fragmented vendors, manual follow-ups, and no single point of accountability when things break. Jane Aerospace works as a dedicated supply chain-as-a-service partner, so there's one team owning the whole thing. Worth 15 minutes?",
    "A lot of operations teams are spending 40% of their time chasing vendors and fixing supply chain gaps that shouldn't exist. Jane Aerospace takes that entire function off your plate as a service. Worth a quick call?",
]

_FALLBACKS_C = [
    "We recently helped a mid-size manufacturer in India consolidate their vendor base and cut procurement lead times significantly — running their supply chain as a service rather than an in-house headache. Given what you're building at {company}, thought this might be relevant. 15 minutes?",
    "We've been working with a few companies in your space as their dedicated supply chain partner — handling everything from vendor coordination to last-mile so their team stays focused on the business. Thought it might be worth a conversation.",
]


# ---------------------------------------------------------------------------
# Main entrypoint
# ---------------------------------------------------------------------------

def generate_outreach(
    business_name: str,
    contact_name: str | None,
    designation: str | None,
    location: str | None,
    summary: str | None,
    is_repeat_lead: bool = False,
) -> dict:
    """Generate personalised outreach email.

    Returns:
        {
          "body": str,           # 3-4 sentence body (no greeting/sign-off)
          "subject": str,        # email subject line
          "variant": str,        # "A", "B", or "C"
          "subject_variant": str # "S1", "S2", or "S3"
        }
    """
    # Select body variant by weighted random (self-adjusting based on reply rates)
    weights = get_variant_weights()
    variant = random.choices(["A", "B", "C"], weights=weights, k=1)[0]

    # Select subject variant uniformly (tracked separately later)
    subject_variant = random.choice(["S1", "S2", "S3"])

    # Generate subject
    if subject_variant == "S1":
        subject = _subject_s1(contact_name)
    elif subject_variant == "S2":
        subject = _subject_s2(business_name)
    else:
        subject = _subject_s3(designation, location)

    # Generate body via AI
    user_msg = _build_user_msg(business_name, contact_name, designation, location, summary)

    # Repeat lead (#25): prepend a warm reconnect opener before the variant body
    if is_repeat_lead:
        first_name = (contact_name or business_name).split()[0]
        reconnect_prefix = (
            f"Great to reconnect, {first_name} — it's been a while since we last spoke. "
            "A lot has moved forward on our end and I wanted to share a quick update."
        )
        if variant == "A":
            body = _ask(_SYSTEM_A, user_msg) or random.choice(_FALLBACKS_A)
        elif variant == "B":
            body = _ask(_SYSTEM_B, user_msg) or random.choice(_FALLBACKS_B)
        else:
            fb = random.choice(_FALLBACKS_C).format(company=business_name)
            body = _ask(_SYSTEM_C, user_msg) or fb
        body = f"{reconnect_prefix}\n\n{body}"
    elif variant == "A":
        body = _ask(_SYSTEM_A, user_msg) or random.choice(_FALLBACKS_A)
    elif variant == "B":
        body = _ask(_SYSTEM_B, user_msg) or random.choice(_FALLBACKS_B)
    else:
        fb = random.choice(_FALLBACKS_C).format(company=business_name)
        body = _ask(_SYSTEM_C, user_msg) or fb

    logger.info("outreach_generated", company=business_name, variant=variant, subject_variant=subject_variant)

    return {
        "body": body,
        "subject": subject,
        "variant": variant,
        "subject_variant": subject_variant,
    }


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
