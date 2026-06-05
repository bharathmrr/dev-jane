"""AI helpers for the customer onboarding pipeline.

Uses Claude (Anthropic) for:
  - Detecting company type (Indian / Overseas) from lead summary
  - Generating KYC rejection emails
  - Filling NDA / Agreement templates with KYC data
  - Revising documents based on team feedback
  - Generating sign-rejection emails for leads
"""
from __future__ import annotations

import json

from app.core.config import settings

_client = None


def _get_client():
    global _client
    if _client is None:
        import anthropic
        _client = anthropic.Anthropic(api_key=settings.ANTHROPIC_API_KEY)
    return _client


def _ask(model: str, system: str, user: str, max_tokens: int = 2048) -> str:
    if not settings.ANTHROPIC_API_KEY:
        return ""
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
        print(f"[AI] Error: {exc}")
        return ""


# ---------------------------------------------------------------------------
# Company type detection
# ---------------------------------------------------------------------------

def detect_company_type(business_name: str, summary: str) -> str:
    """Returns 'indian' or 'overseas'. Defaults to 'overseas' if unsure."""
    prompt = (
        f"Business name: {business_name}\n"
        f"Summary: {summary}\n\n"
        "Is this company based in India, or is it an overseas (international/non-Indian) company?\n"
        "Reply with exactly one word: indian OR overseas"
    )
    result = _ask(
        settings.LLM_INTENT_MODEL,
        "You classify companies as Indian or overseas based on their name and description. "
        "Reply with exactly one word: indian or overseas.",
        prompt,
        max_tokens=10,
    ).lower().strip()

    return "indian" if "indian" in result else "overseas"


# ---------------------------------------------------------------------------
# KYC emails
# ---------------------------------------------------------------------------

def generate_kyc_rejection_email(
    rejection_notes: str,
    lead_name: str,
    company_name: str,
    attempt_number: int,
) -> str:
    """Draft a professional email asking the lead to resubmit their KYC."""
    prompt = (
        f"Lead name: {lead_name}\n"
        f"Company: {company_name}\n"
        f"KYC submission attempt: #{attempt_number}\n"
        f"Reviewer's rejection reason: {rejection_notes}\n\n"
        "Write a polite, professional email body (HTML) addressed to the lead explaining that their KYC "
        "submission needs to be corrected and resubmitted. Reference the specific issue raised. "
        "Include a warm but firm call to action. Do NOT include a subject line. "
        "Return only the HTML body content (no <html>/<body> wrapper tags)."
    )
    result = _ask(
        settings.LLM_NEGOTIATION_MODEL,
        "You are drafting professional business emails on behalf of Jane Aerospace. "
        "Write in a respectful, clear, and concise tone.",
        prompt,
    )
    if not result:
        return (
            f"<p>Dear {lead_name},</p>"
            f"<p>Thank you for submitting your KYC documents. After review, our team has identified "
            f"some items that require correction:</p>"
            f"<blockquote>{rejection_notes}</blockquote>"
            f"<p>Please use the link provided in our earlier email to resubmit your KYC form with "
            f"the corrected information. We appreciate your cooperation.</p>"
        )
    return result


def generate_kyc_reminder_email(
    lead_name: str,
    company_name: str,
    followup_num: int,
    days_since_sent: int,
) -> str:
    """Draft a daily KYC reminder email."""
    prompt = (
        f"Lead name: {lead_name}\n"
        f"Company: {company_name}\n"
        f"Follow-up number: #{followup_num}\n"
        f"Days since KYC form was sent: {days_since_sent}\n\n"
        "Write a short, friendly reminder email body (HTML) asking the lead to complete their KYC form. "
        "Be brief (2-3 short paragraphs). Do NOT include a subject line or <html>/<body> wrapper."
    )
    result = _ask(
        settings.LLM_INTENT_MODEL,
        "You write concise reminder emails for Jane Aerospace. Friendly, professional tone.",
        prompt,
    )
    if not result:
        return (
            f"<p>Dear {lead_name},</p>"
            f"<p>This is a friendly reminder to complete your KYC form. "
            f"Please use the link in our earlier email to submit your documents.</p>"
            f"<p>If you have any questions, feel free to reply to this email.</p>"
        )
    return result


# ---------------------------------------------------------------------------
# NDA / Agreement template filling
# ---------------------------------------------------------------------------

def fill_document_template(
    template_content: str,
    kyc_data: dict,
    doc_type: str = "NDA",
) -> str:
    """Fill a document template with KYC data. Returns the filled document text."""
    kyc_json = json.dumps(kyc_data, indent=2)
    prompt = (
        f"Document type: {doc_type}\n"
        f"KYC Data:\n{kyc_json}\n\n"
        f"Template:\n{template_content}\n\n"
        "Fill all placeholder fields in the template using the KYC data provided. "
        "Placeholders may appear as {{field_name}}, [FIELD_NAME], _________, or similar blank markers. "
        "Replace every blank or placeholder with the correct value from KYC data. "
        "If a field isn't in KYC data, leave it as [TO BE FILLED]. "
        "Return the complete filled document text only — no commentary, no preamble."
    )
    result = _ask(
        settings.LLM_NEGOTIATION_MODEL,
        f"You are a legal document assistant. Fill in the {doc_type} template accurately using the provided data.",
        prompt,
        max_tokens=4096,
    )
    return result or template_content


def revise_document(
    current_content: str,
    revision_notes: str,
    kyc_data: dict,
    doc_type: str = "NDA",
) -> str:
    """Revise a filled document based on team feedback."""
    kyc_json = json.dumps(kyc_data, indent=2)
    prompt = (
        f"Document type: {doc_type}\n"
        f"KYC Data:\n{kyc_json}\n\n"
        f"Current document:\n{current_content}\n\n"
        f"Required changes from team:\n{revision_notes}\n\n"
        "Apply all requested changes to the document. Return the complete revised document text only."
    )
    result = _ask(
        settings.LLM_NEGOTIATION_MODEL,
        f"You are a legal document assistant. Revise the {doc_type} document as instructed.",
        prompt,
        max_tokens=4096,
    )
    return result or current_content


# ---------------------------------------------------------------------------
# NDA / Agreement sign-rejection and reminder emails
# ---------------------------------------------------------------------------

def generate_sign_rejection_email(
    rejection_notes: str,
    lead_name: str,
    company_name: str,
    doc_type: str = "NDA",
) -> str:
    """Email to lead explaining why their signed document was rejected."""
    prompt = (
        f"Lead name: {lead_name}\n"
        f"Company: {company_name}\n"
        f"Document type: {doc_type}\n"
        f"Issues found by team: {rejection_notes}\n\n"
        f"Write a professional HTML email body asking the lead to review and re-sign the {doc_type}. "
        "Explain the issues clearly and politely. Include a call to action to reply with the corrected document. "
        "Do NOT include subject line or wrapper tags."
    )
    result = _ask(
        settings.LLM_NEGOTIATION_MODEL,
        "You draft professional business emails for Jane Aerospace. Clear, courteous tone.",
        prompt,
    )
    if not result:
        return (
            f"<p>Dear {lead_name},</p>"
            f"<p>Thank you for sending the signed {doc_type}. After review, our team found the following:</p>"
            f"<blockquote>{rejection_notes}</blockquote>"
            f"<p>Please address the above and send the corrected signed {doc_type} back to us. "
            "We appreciate your prompt attention to this matter.</p>"
        )
    return result


def generate_doc_reminder_email(
    lead_name: str,
    company_name: str,
    followup_num: int,
    doc_type: str = "NDA",
    days_since_sent: int = 1,
) -> str:
    """Daily reminder email for unsigned NDA or Agreement."""
    prompt = (
        f"Lead name: {lead_name}\n"
        f"Company: {company_name}\n"
        f"Document: {doc_type}\n"
        f"Follow-up: #{followup_num} (sent {days_since_sent} day(s) ago)\n\n"
        f"Write a short, polite reminder HTML email body asking the lead to sign and return the {doc_type}. "
        "Keep it to 2-3 paragraphs. No subject line, no wrapper tags."
    )
    result = _ask(
        settings.LLM_INTENT_MODEL,
        "You write concise reminder emails for Jane Aerospace. Polite, professional.",
        prompt,
    )
    if not result:
        return (
            f"<p>Dear {lead_name},</p>"
            f"<p>This is a friendly reminder to sign and return the {doc_type} we sent earlier. "
            "Please reply to this email with the signed document attached.</p>"
            "<p>If you have any questions, feel free to reach out.</p>"
        )
    return result
