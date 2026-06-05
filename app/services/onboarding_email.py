"""Email helpers for the customer onboarding pipeline.

All emails sent via Gmail SMTP (same pattern as email_service.py).
"""
from __future__ import annotations

import hashlib
import hmac as _hmac
import smtplib
import ssl
from email.mime.application import MIMEApplication
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

from app.core.config import settings


# ---------------------------------------------------------------------------
# KYC form URL (HMAC-signed)
# ---------------------------------------------------------------------------

def make_kyc_url(onboarding_id: str, token: str) -> str:
    return f"{settings.APP_URL.rstrip('/')}/api/v1/onboarding/kyc/form/{onboarding_id}/{token}"


def make_kyc_token(onboarding_id: str) -> str:
    msg = f"kyc:{onboarding_id}".encode()
    return _hmac.new(settings.ONBOARDING_HMAC_SECRET.encode(), msg, hashlib.sha256).hexdigest()[:24]


def verify_kyc_token(onboarding_id: str, token: str) -> bool:
    expected = make_kyc_token(onboarding_id)
    return _hmac.compare_digest(expected, token)


# ---------------------------------------------------------------------------
# Email send helpers
# ---------------------------------------------------------------------------

def _send_html_email(
    to_email: str,
    subject: str,
    html_content: str,
    attachment_bytes: bytes | None = None,
    attachment_name: str | None = None,
) -> bool:
    try:
        msg = MIMEMultipart("mixed" if attachment_bytes else "alternative")
        msg["Subject"] = subject
        msg["From"] = f"{settings.ORGANIZER_NAME} <{settings.SMTP_FROM_EMAIL}>"
        msg["To"] = to_email

        html_part = MIMEMultipart("alternative")
        html_part.attach(MIMEText(html_content, "html"))
        msg.attach(html_part)

        if attachment_bytes and attachment_name:
            part = MIMEApplication(attachment_bytes, Name=attachment_name)
            part["Content-Disposition"] = f'attachment; filename="{attachment_name}"'
            msg.attach(part)

        ctx = ssl.create_default_context()
        with smtplib.SMTP(settings.SMTP_HOST, settings.SMTP_PORT, timeout=20) as server:
            server.ehlo()
            if settings.SMTP_USE_TLS:
                server.starttls(context=ctx)
                server.ehlo()
            if settings.SMTP_USERNAME:
                server.login(settings.SMTP_USERNAME, settings.SMTP_PASSWORD)
            server.send_message(msg)

        print(f"[ONBOARDING EMAIL] Sent '{subject}' -> {to_email}")
        return True
    except Exception as exc:
        print(f"[ONBOARDING EMAIL] Failed '{subject}' -> {to_email}: {exc}")
        return False


def _sig() -> str:
    n = settings.ORGANIZER_NAME
    e = settings.ORGANIZER_EMAIL
    return (
        f'<p style="margin:32px 0 4px 0;font-family:Arial,sans-serif;font-size:15px;color:#111;">Kind regards,</p>'
        f'<p style="margin:0 0 2px 0;font-family:Arial,sans-serif;font-size:15px;"><strong>{n}</strong></p>'
        f'<p style="margin:0 0 2px 0;font-family:Arial,sans-serif;font-size:13px;color:#555;">Founder &amp; Managing Director, Jane Aerospace</p>'
        f'<p style="margin:0;"><a href="mailto:{e}" style="color:#1155cc;font-size:13px;">{e}</a></p>'
    )


def _wrap(body: str) -> str:
    return (
        '<div style="font-family:Arial,sans-serif;font-size:15px;color:#222;max-width:640px;margin:0 auto;">'
        + body + _sig() + "</div>"
    )


# ---------------------------------------------------------------------------
# KYC emails
# ---------------------------------------------------------------------------

def send_kyc_form_email(
    to_email: str,
    lead_name: str,
    company_name: str,
    kyc_url: str,
) -> bool:
    subject = f"KYC Form — Jane Aerospace Partnership with {company_name}"
    html = _wrap(f"""
        <p>Dear {lead_name},</p>
        <p>Thank you for your interest in partnering with <strong>Jane Aerospace</strong>.</p>
        <p>As the next step in our onboarding process, we kindly request you to complete your
        <strong>KYC (Know Your Customer)</strong> verification by filling out the form below.
        This is a mandatory requirement before we can proceed further.</p>
        <p style="margin:24px 0;">
            <a href="{kyc_url}"
               style="background:#1a56db;color:#fff;padding:12px 28px;border-radius:6px;
                      text-decoration:none;font-size:16px;font-weight:bold;display:inline-block;">
                Complete KYC Form
            </a>
        </p>
        <p style="color:#555;font-size:13px;">If the button above doesn't work, copy and paste this link into your browser:<br>
        <a href="{kyc_url}" style="color:#1155cc;">{kyc_url}</a></p>
        <p>Please have the following documents ready before you begin:</p>
        <ul>
            <li>Company Incorporation Certificate</li>
            <li>GST Certificate (for Indian companies)</li>
        </ul>
        <p>The form typically takes less than 5 minutes to complete.</p>
    """)
    return _send_html_email(to_email, subject, html)


def send_kyc_reminder_email(
    to_email: str,
    lead_name: str,
    company_name: str,
    kyc_url: str,
    followup_num: int,
    email_body_html: str,
) -> bool:
    subject = f"Reminder #{followup_num}: KYC Form Pending — {company_name}"
    html = _wrap(email_body_html + f"""
        <p style="margin:24px 0;">
            <a href="{kyc_url}"
               style="background:#1a56db;color:#fff;padding:12px 28px;border-radius:6px;
                      text-decoration:none;font-size:16px;font-weight:bold;display:inline-block;">
                Complete KYC Form
            </a>
        </p>
    """)
    return _send_html_email(to_email, subject, html)


def send_kyc_rejected_email(
    to_email: str,
    lead_name: str,
    company_name: str,
    email_body_html: str,
    kyc_url: str,
) -> bool:
    subject = f"KYC Review Update — Action Required | {company_name}"
    html = _wrap(email_body_html + f"""
        <p style="margin:24px 0;">
            <a href="{kyc_url}"
               style="background:#e53e3e;color:#fff;padding:12px 28px;border-radius:6px;
                      text-decoration:none;font-size:16px;font-weight:bold;display:inline-block;">
                Resubmit KYC Form
            </a>
        </p>
    """)
    return _send_html_email(to_email, subject, html)


def send_kyc_approved_email(
    to_email: str,
    lead_name: str,
    company_name: str,
) -> bool:
    subject = f"KYC Approved — Proceeding to NDA | {company_name}"
    html = _wrap(f"""
        <p>Dear {lead_name},</p>
        <p>We are pleased to inform you that your KYC submission has been
        <strong>reviewed and approved</strong> by our team.</p>
        <p>We will now prepare the Non-Disclosure Agreement (NDA) and send it to you shortly.
        Please keep an eye on your inbox.</p>
        <p>Thank you for your cooperation in completing this step promptly.</p>
    """)
    return _send_html_email(to_email, subject, html)


# ---------------------------------------------------------------------------
# NDA emails
# ---------------------------------------------------------------------------

def send_nda_to_lead(
    to_email: str,
    lead_name: str,
    company_name: str,
    nda_content_html: str,
    nda_pdf_bytes: bytes | None = None,
) -> bool:
    subject = f"Non-Disclosure Agreement (NDA) — Jane Aerospace × {company_name}"
    intro = _wrap(f"""
        <p>Dear {lead_name},</p>
        <p>Please find the <strong>Non-Disclosure Agreement (NDA)</strong> between
        Jane Aerospace and {company_name} below (and attached).</p>
        <p>Kindly review the document, sign it, and <strong>reply to this email</strong>
        with the signed copy attached. If you have any questions, please reply to this email.</p>
        <hr style="margin:24px 0;border:none;border-top:1px solid #eee;">
        <div style="background:#f9f9f9;padding:20px;border-radius:8px;font-size:14px;
                    white-space:pre-wrap;font-family:Georgia,serif;line-height:1.7;">
            {nda_content_html}
        </div>
        <hr style="margin:24px 0;border:none;border-top:1px solid #eee;">
        <p style="color:#555;font-size:13px;">Please print, sign, scan, and reply with the signed PDF.</p>
    """)
    return _send_html_email(
        to_email, subject, intro,
        attachment_bytes=nda_pdf_bytes,
        attachment_name=f"NDA_{company_name.replace(' ', '_')}.pdf" if nda_pdf_bytes else None,
    )


def send_nda_reminder(
    to_email: str,
    lead_name: str,
    company_name: str,
    email_body_html: str,
    followup_num: int,
) -> bool:
    subject = f"Reminder #{followup_num}: NDA Awaiting Your Signature — {company_name}"
    html = _wrap(email_body_html)
    return _send_html_email(to_email, subject, html)


def send_nda_sign_rejection(
    to_email: str,
    lead_name: str,
    company_name: str,
    email_body_html: str,
) -> bool:
    subject = f"NDA Signature Review — Action Required | {company_name}"
    html = _wrap(email_body_html)
    return _send_html_email(to_email, subject, html)


def send_nda_approved_email(
    to_email: str,
    lead_name: str,
    company_name: str,
) -> bool:
    subject = f"NDA Approved — Proceeding to Customer Agreement | {company_name}"
    html = _wrap(f"""
        <p>Dear {lead_name},</p>
        <p>We are pleased to confirm that the signed NDA has been
        <strong>reviewed and approved</strong> by our team.</p>
        <p>We will now prepare the Customer Agreement and send it to you shortly.</p>
        <p>Thank you for your continued cooperation.</p>
    """)
    return _send_html_email(to_email, subject, html)


# ---------------------------------------------------------------------------
# Customer Agreement emails
# ---------------------------------------------------------------------------

def send_agreement_to_lead(
    to_email: str,
    lead_name: str,
    company_name: str,
    agreement_content_html: str,
    agreement_pdf_bytes: bytes | None = None,
) -> bool:
    subject = f"Customer Agreement — Jane Aerospace × {company_name}"
    html = _wrap(f"""
        <p>Dear {lead_name},</p>
        <p>Please find the <strong>Customer Agreement</strong> between
        Jane Aerospace and {company_name} below.</p>
        <p>Kindly review, sign, and <strong>reply to this email</strong>
        with the signed copy attached.</p>
        <hr style="margin:24px 0;border:none;border-top:1px solid #eee;">
        <div style="background:#f9f9f9;padding:20px;border-radius:8px;font-size:14px;
                    white-space:pre-wrap;font-family:Georgia,serif;line-height:1.7;">
            {agreement_content_html}
        </div>
        <hr style="margin:24px 0;border:none;border-top:1px solid #eee;">
        <p style="color:#555;font-size:13px;">Please print, sign, scan, and reply with the signed PDF.</p>
    """)
    return _send_html_email(
        to_email, subject, html,
        attachment_bytes=agreement_pdf_bytes,
        attachment_name=f"Agreement_{company_name.replace(' ', '_')}.pdf" if agreement_pdf_bytes else None,
    )


def send_agreement_reminder(
    to_email: str,
    lead_name: str,
    company_name: str,
    email_body_html: str,
    followup_num: int,
) -> bool:
    subject = f"Reminder #{followup_num}: Customer Agreement Awaiting Signature — {company_name}"
    html = _wrap(email_body_html)
    return _send_html_email(to_email, subject, html)


def send_agreement_sign_rejection(
    to_email: str,
    lead_name: str,
    company_name: str,
    email_body_html: str,
) -> bool:
    subject = f"Customer Agreement Signature Review — Action Required | {company_name}"
    html = _wrap(email_body_html)
    return _send_html_email(to_email, subject, html)


def send_agreement_approved_email(
    to_email: str,
    lead_name: str,
    company_name: str,
) -> bool:
    subject = f"Customer Agreement Approved — Welcome to Jane Aerospace! | {company_name}"
    html = _wrap(f"""
        <p>Dear {lead_name},</p>
        <p>We are delighted to confirm that your Customer Agreement has been
        <strong>approved</strong> by our team.</p>
        <p>Welcome aboard! Our team will reach out to schedule your onboarding and training session shortly.</p>
        <p>We look forward to a successful partnership with {company_name}.</p>
    """)
    return _send_html_email(to_email, subject, html)


# ---------------------------------------------------------------------------
# Team notification emails
# ---------------------------------------------------------------------------

def notify_team_kyc_submitted(
    lead_name: str,
    company_name: str,
    onboarding_id: str,
    attempt_number: int,
) -> bool:
    review_url = f"{settings.APP_URL.rstrip('/')}/dashboard#onboarding-{onboarding_id}"
    subject = f"KYC Submitted for Review — {company_name} (Attempt #{attempt_number})"
    html = f"""
        <p><strong>{lead_name}</strong> from <strong>{company_name}</strong> has submitted their KYC form
        (Attempt #{attempt_number}).</p>
        <p><a href="{review_url}">Click here to review on the dashboard</a></p>
    """
    return _send_html_email(settings.ORGANIZER_EMAIL, subject, html)


def notify_team_signed_doc_received(
    lead_name: str,
    company_name: str,
    onboarding_id: str,
    doc_type: str,
) -> bool:
    review_url = f"{settings.APP_URL.rstrip('/')}/dashboard#onboarding-{onboarding_id}"
    subject = f"Signed {doc_type} Received — {company_name}"
    html = f"""
        <p><strong>{lead_name}</strong> from <strong>{company_name}</strong> has sent back the signed {doc_type}.</p>
        <p><a href="{review_url}">Click here to review on the dashboard</a></p>
    """
    return _send_html_email(settings.ORGANIZER_EMAIL, subject, html)
