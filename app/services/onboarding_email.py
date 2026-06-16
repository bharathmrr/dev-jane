"""Email helpers for the customer onboarding pipeline.

All emails sent via Gmail SMTP (same pattern as email_service.py).
"""
from __future__ import annotations

import hashlib
import hmac as _hmac
import smtplib
import ssl
import time
from email.mime.application import MIMEApplication
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

from app.core.config import settings


# ---------------------------------------------------------------------------
# KYC form URL (HMAC-signed)
# ---------------------------------------------------------------------------

def make_action_url(record_id: str, action: str, expires_days: int = 7) -> str:
    """Generate an HMAC-signed one-click action URL for team emails."""
    expires = int(time.time()) + expires_days * 86400
    msg = f"{record_id}:{action}:{expires}".encode()
    token = _hmac.new(settings.ONBOARDING_HMAC_SECRET.encode(), msg, hashlib.sha256).hexdigest()
    base = settings.APP_URL.rstrip("/")
    return f"{base}/api/v1/onboarding/action?id={record_id}&action={action}&token={token}&expires={expires}"


def _action_btn(url: str, label: str, color: str = "#1a56db") -> str:
    return (
        f'<a href="{url}" style="display:inline-block;background:{color};color:#fff;'
        f'padding:10px 22px;border-radius:6px;text-decoration:none;font-size:14px;'
        f'font-weight:bold;margin:4px 6px 4px 0;">{label}</a>'
    )


def make_kyc_url(onboarding_id: str, token: str) -> str:
    return f"{settings.APP_URL.rstrip('/')}/api/v1/onboarding/kyc/form/{onboarding_id}/{token}"


def make_kyc_token(onboarding_id: str) -> str:
    msg = f"kyc:{onboarding_id}".encode()
    return _hmac.new(settings.ONBOARDING_HMAC_SECRET.encode(), msg, hashlib.sha256).hexdigest()[:24]


def verify_kyc_token(onboarding_id: str, token: str) -> bool:
    expected = make_kyc_token(onboarding_id)
    return _hmac.compare_digest(expected, token)


def make_kyc_view_token(onboarding_id: str) -> str:
    msg = f"kyc_view:{onboarding_id}".encode()
    return _hmac.new(settings.ONBOARDING_HMAC_SECRET.encode(), msg, hashlib.sha256).hexdigest()[:32]


def verify_kyc_view_token(onboarding_id: str, token: str) -> bool:
    expected = make_kyc_view_token(onboarding_id)
    return _hmac.compare_digest(expected, token)


def make_kyc_view_url(onboarding_id: str) -> str:
    token = make_kyc_view_token(onboarding_id)
    return f"{settings.APP_URL.rstrip('/')}/api/v1/onboarding/kyc/view/{onboarding_id}/{token}"


# ---------------------------------------------------------------------------
# Document (NDA / Agreement) URLs — HMAC-signed, purpose-scoped
# ---------------------------------------------------------------------------

def make_doc_token(onboarding_id: str, doc_type: str, purpose: str) -> str:
    """purpose: 'edit' (team page) or 'sign' (lead page)."""
    msg = f"doc:{purpose}:{doc_type}:{onboarding_id}".encode()
    return _hmac.new(settings.ONBOARDING_HMAC_SECRET.encode(), msg, hashlib.sha256).hexdigest()[:24]


def verify_doc_token(onboarding_id: str, doc_type: str, purpose: str, token: str) -> bool:
    expected = make_doc_token(onboarding_id, doc_type, purpose)
    return _hmac.compare_digest(expected, token)


def make_doc_edit_url(onboarding_id: str, doc_type: str) -> str:
    token = make_doc_token(onboarding_id, doc_type, "edit")
    return f"{settings.APP_URL.rstrip('/')}/api/v1/documents/edit/{onboarding_id}/{doc_type}/{token}"


def make_doc_sign_url(onboarding_id: str, doc_type: str) -> str:
    token = make_doc_token(onboarding_id, doc_type, "sign")
    return f"{settings.APP_URL.rstrip('/')}/api/v1/documents/sign/{onboarding_id}/{doc_type}/{token}"


# ---------------------------------------------------------------------------
# Email send helpers
# ---------------------------------------------------------------------------

def _send_to_reviewers(subject: str, html_content: str) -> bool:
    """Send to ORGANIZER_EMAIL and ONBOARDING_REVIEWER_EMAIL (if set and different)."""
    ok = _send_html_email(settings.ORGANIZER_EMAIL, subject, html_content)
    reviewer = (settings.ONBOARDING_REVIEWER_EMAIL or "").strip()
    if reviewer and reviewer != settings.ORGANIZER_EMAIL:
        _send_html_email(reviewer, subject, html_content)
    return ok


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
    inner = body + _sig()
    return (
        '<!DOCTYPE html><html lang="en"><head>'
        '<meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width,initial-scale=1">'
        '<meta http-equiv="X-UA-Compatible" content="IE=edge">'
        '</head>'
        '<body style="margin:0;padding:0;background:#ffffff;">'
        '<div style="font-family:Arial,sans-serif;font-size:15px;color:#222222;'
        'max-width:640px;margin:40px auto;padding:0 24px 48px;">'
        + inner +
        '</div></body></html>'
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
# Document signing emails (PDF template flow)
# ---------------------------------------------------------------------------

_DOC_LABELS = {"nda": "Non-Disclosure Agreement (NDA)", "agreement": "Supply / Customer Agreement"}


def send_document_sign_email(
    to_email: str,
    lead_name: str,
    company_name: str,
    doc_type: str,
    sign_url: str,
) -> bool:
    label = _DOC_LABELS.get(doc_type, doc_type.upper())
    subject = f"{label} for Signature — Jane Aerospace × {company_name}"
    html = _wrap(f"""
        <p>Dear {lead_name},</p>
        <p>Please review and sign the <strong>{label}</strong> between Jane Aerospace and
        <strong>{company_name}</strong>. The document is pre-filled with your verified details.</p>
        <p style="margin:24px 0;">
            <a href="{sign_url}"
               style="background:#1a56db;color:#fff;padding:12px 28px;border-radius:6px;
                      text-decoration:none;font-size:16px;font-weight:bold;display:inline-block;">
                Review &amp; Sign Document
            </a>
        </p>
        <p style="color:#555;font-size:13px;">If the button doesn't work, open this link:<br>
        <a href="{sign_url}" style="color:#1155cc;">{sign_url}</a></p>
    """)
    return _send_html_email(to_email, subject, html)


def send_document_review_email(
    to_email: str,
    lead_name: str,
    company_name: str,
    doc_type: str,
    review_url: str,
) -> bool:
    """T&C review request — the lead accepts or requests changes (no signature yet)."""
    label = _DOC_LABELS.get(doc_type, doc_type.upper())
    subject = f"{label} for Your Review — Jane Aerospace × {company_name}"
    html = _wrap(f"""
        <p>Dear {lead_name},</p>
        <p>Please review the Terms &amp; Conditions of the <strong>{label}</strong> between
        Jane Aerospace and <strong>{company_name}</strong>. The document is pre-filled with
        your verified details.</p>
        <p>On the review page you can <strong>accept the terms</strong> or
        <strong>request changes</strong> with your comments — no signature is needed at this stage.
        Once you accept, we countersign and send you the final document for e-signature.</p>
        <p style="margin:24px 0;">
            <a href="{review_url}"
               style="background:#1a56db;color:#fff;padding:12px 28px;border-radius:6px;
                      text-decoration:none;font-size:16px;font-weight:bold;display:inline-block;">
                Review Terms &amp; Conditions
            </a>
        </p>
        <p style="color:#555;font-size:13px;">If the button doesn't work, open this link:<br>
        <a href="{review_url}" style="color:#1155cc;">{review_url}</a></p>
    """)
    return _send_html_email(to_email, subject, html)


def notify_team_document_comments(
    company_name: str,
    lead_email: str,
    doc_type: str,
    commenter: str,
    comments_text: str,
    edit_url: str,
) -> bool:
    """Lead requested changes — comments go to the team with the editor link."""
    label = _DOC_LABELS.get(doc_type, doc_type.upper())
    subject = f"💬 Changes Requested on {label} — {company_name}"
    safe = (comments_text or "").replace("<", "&lt;").replace("\n", "<br>")
    html = f"""
    <div style="font-family:Arial,sans-serif;font-size:14px;color:#222;max-width:560px;">
      <h2 style="color:#b45309;margin:0 0 12px;">Changes Requested — {label}</h2>
      <p><strong>{commenter}</strong> of <strong>{company_name}</strong> ({lead_email})
      reviewed the {label} and requested changes:</p>
      <div style="background:#fff7ed;border-left:4px solid #f59e0b;border-radius:6px;
                  padding:12px 16px;margin:14px 0;font-size:14px;color:#451a03;">{safe}</div>
      <p>Open the document, make the edits in the Live Editor, and resend the updated
      version for review:</p>
      <p style="margin:20px 0;">
        <a href="{edit_url}" style="background:#1a56db;color:#fff;padding:11px 24px;border-radius:6px;
           text-decoration:none;font-weight:bold;display:inline-block;">Open Document Editor</a>
      </p>
    </div>"""
    return _send_to_reviewers(subject, html)


def notify_team_terms_accepted(
    company_name: str,
    lead_email: str,
    doc_type: str,
    accepted_by: str,
    edit_url: str,
) -> bool:
    """Lead accepted the T&C — internal countersignature is now required."""
    label = _DOC_LABELS.get(doc_type, doc_type.upper())
    subject = f"✓ {label} Terms Accepted — {company_name} (internal signature required)"
    html = f"""
    <div style="font-family:Arial,sans-serif;font-size:14px;color:#222;max-width:560px;">
      <h2 style="color:#16a34a;margin:0 0 12px;">{label} Terms Accepted</h2>
      <p><strong>{accepted_by}</strong> of <strong>{company_name}</strong> ({lead_email})
      has accepted the Terms &amp; Conditions of the {label}.</p>
      <p><strong>Next step:</strong> an authorised Jane Aerospace representative signs the
      document; it is then automatically emailed to the lead for their counter-signature.</p>
      <p style="margin:20px 0;">
        <a href="{edit_url}" style="background:#16a34a;color:#fff;padding:11px 24px;border-radius:6px;
           text-decoration:none;font-weight:bold;display:inline-block;">Open to Sign &amp; Send</a>
      </p>
    </div>"""
    return _send_to_reviewers(subject, html)


def notify_team_document_signed(
    company_name: str,
    lead_email: str,
    doc_type: str,
    signed_name: str,
    signed_pdf: bytes | None = None,
) -> bool:
    label = _DOC_LABELS.get(doc_type, doc_type.upper())
    next_step = ("Supply Agreement has been generated and is ready for your review."
                 if doc_type == "nda" else "Onboarding is now complete.")
    subject = f"✓ {label} Signed — {company_name}"
    html = f"""
    <div style="font-family:Arial,sans-serif;font-size:14px;color:#222;max-width:560px;">
      <h2 style="color:#16a34a;margin:0 0 12px;">{label} Signed</h2>
      <p><strong>{company_name}</strong> ({lead_email}) has signed the {label}.</p>
      <p>Signed by: <strong>{signed_name}</strong></p>
      <p>{next_step}</p>
      <p style="color:#888;font-size:12px;">The signed PDF is attached for your records.</p>
    </div>"""
    ok = _send_html_email(settings.ORGANIZER_EMAIL, subject, html,
                          attachment_bytes=signed_pdf,
                          attachment_name=f"{doc_type}_signed_{company_name.replace(' ', '_')}.pdf")
    reviewer = (settings.ONBOARDING_REVIEWER_EMAIL or "").strip()
    if reviewer and reviewer != settings.ORGANIZER_EMAIL:
        _send_html_email(reviewer, subject, html,
                         attachment_bytes=signed_pdf,
                         attachment_name=f"{doc_type}_signed_{company_name.replace(' ', '_')}.pdf")
    return ok


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


def send_nda_contract_link(
    to_email: str,
    lead_name: str,
    company_name: str,
    contract_link: str,
) -> bool:
    """Send the Zoho Contracts NDA link to the lead for e-signature."""
    subject = f"Non-Disclosure Agreement — Jane Aerospace × {company_name}"
    html = _wrap(f"""
        <p>Dear {lead_name},</p>
        <p>Thank you for completing your KYC verification. We are pleased to move forward
        with the partnership process between <strong>Jane Aerospace</strong> and
        <strong>{company_name}</strong>.</p>
        <p>As the next step, please review and e-sign the <strong>Non-Disclosure Agreement (NDA)</strong>
        using the secure link below. The document has been pre-filled with your company details.</p>
        <p style="margin:28px 0;">
            <a href="{contract_link}"
               style="background:#1a56db;color:#fff;padding:13px 30px;border-radius:7px;
                      text-decoration:none;font-size:16px;font-weight:bold;display:inline-block;">
                Review &amp; Sign NDA
            </a>
        </p>
        <p style="color:#555;font-size:13px;">
            If the button above doesn't work, copy and paste this link into your browser:<br>
            <a href="{contract_link}" style="color:#1155cc;">{contract_link}</a>
        </p>
        <p>Please complete the signing process at your earliest convenience.
        If you have any questions regarding the terms, do not hesitate to reach out.</p>
    """)
    return _send_html_email(to_email, subject, html)


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


def send_agreement_contract_link(
    to_email: str,
    lead_name: str,
    company_name: str,
    contract_link: str,
) -> bool:
    """Send the Zoho Contracts Customer Agreement link to the lead for e-signature."""
    subject = f"Customer Agreement — Jane Aerospace × {company_name}"
    html = _wrap(f"""
        <p>Dear {lead_name},</p>
        <p>We are pleased to confirm that your NDA has been reviewed and approved.
        As the final step in the onboarding process, please review and e-sign the
        <strong>Customer Agreement</strong> between <strong>Jane Aerospace</strong>
        and <strong>{company_name}</strong>.</p>
        <p>The agreement has been pre-filled with your verified company information.
        Please use the secure link below to review all terms and complete the signature.</p>
        <p style="margin:28px 0;">
            <a href="{contract_link}"
               style="background:#1a56db;color:#fff;padding:13px 30px;border-radius:7px;
                      text-decoration:none;font-size:16px;font-weight:bold;display:inline-block;">
                Review &amp; Sign Customer Agreement
            </a>
        </p>
        <p style="color:#555;font-size:13px;">
            If the button above doesn't work, copy and paste this link into your browser:<br>
            <a href="{contract_link}" style="color:#1155cc;">{contract_link}</a>
        </p>
        <p>Once the agreement is signed, our team will reach out to schedule your onboarding session.
        We look forward to a long and successful partnership.</p>
    """)
    return _send_html_email(to_email, subject, html)


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
    kyc_score: int = 0,
    kyc_summary: str = "",
    issues: str = "",
) -> bool:
    approve_url = make_action_url(onboarding_id, "approve_kyc")
    reject_url = make_action_url(onboarding_id, "reject_kyc")
    subject = f"KYC Manual Review Required — {company_name} (Attempt #{attempt_number})"

    score_color = "#16a34a" if kyc_score >= 80 else "#d97706" if kyc_score >= 50 else "#dc2626"
    score_html = (
        f'<p style="margin:12px 0;"><strong>KYC Score: '
        f'<span style="color:{score_color};font-size:20px;">{kyc_score}/100</span></strong></p>'
    ) if kyc_score else ""

    summary_html = f'<p style="color:#555;font-size:13px;">{kyc_summary}</p>' if kyc_summary else ""
    issues_html = (
        f'<p style="background:#fef2f2;border-left:4px solid #dc2626;padding:8px 12px;'
        f'font-size:13px;color:#991b1b;">⚠️ Issues: {issues}</p>'
    ) if issues else ""

    html = f"""
        <p><strong>{lead_name}</strong> from <strong>{company_name}</strong> submitted KYC
        (Attempt #{attempt_number}) — requires manual review.</p>
        {score_html}{summary_html}{issues_html}
        <p>Review and take action:</p>
        <p>
            {_action_btn(approve_url, "✅ Approve KYC", "#16a34a")}
            {_action_btn(reject_url, "❌ Reject KYC", "#dc2626")}
        </p>
        <p style="color:#888;font-size:12px;">Onboarding ID: {onboarding_id}</p>
    """
    return _send_to_reviewers(subject, html)


def notify_team_signed_doc_received(
    lead_name: str,
    company_name: str,
    onboarding_id: str,
    doc_type: str,
    contract_id: str = "",
) -> bool:
    doc_lower = doc_type.lower()
    if doc_lower == "nda":
        approve_url = make_action_url(onboarding_id, "approve_nda_sign")
        reject_url = make_action_url(onboarding_id, "reject_nda_sign")
    else:
        approve_url = make_action_url(onboarding_id, "approve_agreement_sign")
        reject_url = make_action_url(onboarding_id, "reject_agreement_sign")

    zoho_link = ""
    if contract_id:
        url = f"https://contracts.zoho.in/janeaerospace#/contracts/{contract_id}"
        zoho_link = (
            f'<p><a href="{url}" style="color:#1a56db;">📄 View Signed {doc_type} in Zoho Contracts</a></p>'
        )

    subject = f"✍️ Signed {doc_type} Received — {company_name}"
    html = f"""
        <p><strong>{lead_name}</strong> from <strong>{company_name}</strong>
        has signed the {doc_type} via Zoho Contracts.</p>
        {zoho_link}
        <p>Review and take action:</p>
        <p>
            {_action_btn(approve_url, f"✅ Approve Signed {doc_type}", "#16a34a")}
            {_action_btn(reject_url, "❌ Reject Signature", "#dc2626")}
        </p>
        <p style="color:#888;font-size:12px;">Onboarding ID: {onboarding_id}</p>
    """
    return _send_to_reviewers(subject, html)


def notify_team_nda_draft_ready(
    lead_name: str,
    company_name: str,
    onboarding_id: str,
    preview_url: str = "",
    contract_id: str = "",
) -> bool:
    approve_url = make_action_url(onboarding_id, "approve_nda_draft")

    preview_btn = ""
    if preview_url:
        preview_btn = (
            f'<p><a href="{preview_url}" style="display:inline-block;background:#1e40af;color:#fff;'
            f'padding:10px 22px;border-radius:6px;text-decoration:none;font-size:14px;'
            f'font-weight:bold;margin:4px 6px 4px 0;">✏️ Review, Edit &amp; Preview NDA</a></p>'
        )

    subject = f"📋 NDA Draft Ready — {company_name} (Review & Approve)"
    html = f"""
        <p>The NDA for <strong>{lead_name}</strong> from <strong>{company_name}</strong>
        has been pre-filled with the verified KYC data.</p>
        <p><strong>Step 1:</strong> Open the editor — verify company name, registration number and address,
        adjust anything if needed, and preview the final PDF.</p>
        {preview_btn}
        <p><strong>Step 2:</strong> Send it to the lead for e-signature — either from the editor page,
        or directly with this button:</p>
        <p>{_action_btn(approve_url, "✅ Approve NDA & Send to Lead", "#16a34a")}</p>
        <p style="color:#888;font-size:12px;">Onboarding ID: {onboarding_id}
        {f" | Contract: {contract_id}" if contract_id else ""}</p>
    """
    return _send_to_reviewers(subject, html)


def notify_team_agreement_draft_ready(
    lead_name: str,
    company_name: str,
    onboarding_id: str,
    preview_url: str = "",
    contract_id: str = "",
) -> bool:
    approve_url = make_action_url(onboarding_id, "approve_agreement_draft")

    preview_btn = ""
    if preview_url:
        preview_btn = (
            f'<p><a href="{preview_url}" style="display:inline-block;background:#1e40af;color:#fff;'
            f'padding:10px 22px;border-radius:6px;text-decoration:none;font-size:14px;'
            f'font-weight:bold;margin:4px 6px 4px 0;">✏️ Review, Edit &amp; Preview Agreement</a></p>'
        )

    subject = f"📋 Agreement Draft Ready — {company_name} (Review & Approve)"
    html = f"""
        <p>The Supply / Customer Agreement for <strong>{lead_name}</strong> from
        <strong>{company_name}</strong> has been pre-filled with the verified KYC data.</p>
        <p><strong>Step 1:</strong> Open the editor — verify company details and fill the commercial
        terms (security deposit, contract term, etc.), then preview the final PDF.</p>
        {preview_btn}
        <p><strong>Step 2:</strong> Send it to the lead for e-signature — either from the editor page,
        or directly with this button:</p>
        <p>{_action_btn(approve_url, "✅ Approve Agreement & Send to Lead", "#16a34a")}</p>
        <p style="color:#888;font-size:12px;">Onboarding ID: {onboarding_id}
        {f" | Contract: {contract_id}" if contract_id else ""}</p>
    """
    return _send_to_reviewers(subject, html)


def notify_team_onboarding_started(
    lead_name: str,
    company_name: str,
    lead_email: str,
    onboarding_id: str,
    company_type: str = "",
) -> bool:
    """Sent to team the moment onboarding is initiated — via CRM button, email link, or API."""
    subject = f"Onboarding Started — {company_name}"
    type_badge = f'<span style="background:#e0f2fe;color:#0369a1;padding:2px 8px;border-radius:4px;font-size:12px;">{company_type.upper()}</span>' if company_type else ""
    html = f"""
        <p>Onboarding has been initiated for the following lead:</p>
        <table style="border-collapse:collapse;width:100%;max-width:480px;">
            <tr><td style="padding:6px 0;color:#555;width:130px;">Company</td>
                <td><strong>{company_name}</strong> {type_badge}</td></tr>
            <tr><td style="padding:6px 0;color:#555;">Contact</td>
                <td>{lead_name}</td></tr>
            <tr><td style="padding:6px 0;color:#555;">Email</td>
                <td>{lead_email}</td></tr>
            <tr><td style="padding:6px 0;color:#555;">Onboarding ID</td>
                <td style="font-family:monospace;font-size:12px;">{onboarding_id}</td></tr>
        </table>
        <br>
        <p style="color:#555;">The KYC form has been sent to <strong>{lead_email}</strong>.
        You will receive another email when the lead submits it.</p>
        <p style="color:#aaa;font-size:12px;margin-top:24px;">
            Pipeline: KYC → NDA → Agreement → Complete
        </p>
    """
    return _send_html_email(settings.ORGANIZER_EMAIL, subject, html)


def notify_team_booking_start_onboarding(
    lead_name: str,
    company_name: str,
    lead_email: str,
    lead_id: str,
    slot_display: str,
) -> bool:
    start_url = make_action_url(lead_id, "start_onboarding", expires_days=30)
    subject = f"New Meeting Booked — Start Onboarding for {company_name}"
    html = f"""
        <p>A new meeting has been booked:</p>
        <ul>
            <li><strong>Lead:</strong> {lead_name} from {company_name}</li>
            <li><strong>Email:</strong> {lead_email}</li>
            <li><strong>Meeting:</strong> {slot_display}</li>
        </ul>
        <p>Once the meeting is complete, click below to start the onboarding process.
        This will automatically detect the company type and send the KYC form to the lead.</p>
        <p>
            {_action_btn(start_url, "▶ Start Onboarding", "#1a56db")}
        </p>
        <p style="color:#888;font-size:12px;">This link is valid for 30 days.</p>
    """
    return _send_html_email(settings.ORGANIZER_EMAIL, subject, html)
