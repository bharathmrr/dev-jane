"""Outbound email helpers — sends via Gmail SMTP."""
from __future__ import annotations

import hashlib
import hmac as _hmac
import smtplib
import ssl
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

from app.core.config import settings


def make_book_url(lead_id: str, slot_idx: int) -> str:
    """Generate a one-click booking URL signed with HMAC."""
    msg = f"{lead_id}:{slot_idx}".encode()
    sig = _hmac.new(settings.EMAIL_HMAC_SECRET.encode(), msg, hashlib.sha256).hexdigest()[:16]
    base = settings.APP_URL.rstrip("/")
    return f"{base}/api/v1/v2/book/{lead_id}/{slot_idx}/{sig}"


def _send_html_email(to_email: str, subject: str, html_content: str) -> bool:
    """Send an HTML email via SMTP. Returns True on success."""
    try:
        msg = MIMEMultipart("alternative")
        msg["Subject"] = subject
        msg["From"] = f"{settings.SMTP_FROM_NAME} <{settings.SMTP_FROM_EMAIL}>"
        msg["To"] = to_email
        msg.attach(MIMEText(html_content, "html"))

        context = ssl.create_default_context()
        with smtplib.SMTP(settings.SMTP_HOST, settings.SMTP_PORT, timeout=20) as server:
            server.ehlo()
            if settings.SMTP_USE_TLS:
                server.starttls(context=context)
                server.ehlo()
            if settings.SMTP_USERNAME:
                server.login(settings.SMTP_USERNAME, settings.SMTP_PASSWORD)
            server.send_message(msg)

        print(f"[EMAIL] Sent '{subject}' -> {to_email}")
        return True
    except smtplib.SMTPAuthenticationError:
        print(
            "[EMAIL] Auth failed. Go to myaccount.google.com/apppasswords "
            "and generate an App Password, then set SMTP_PASSWORD in .env"
        )
    except Exception as exc:
        print(f"[EMAIL] Failed to send '{subject}' -> {to_email}: {exc}")
    return False


def send_v2_slots_email(
    to_email: str,
    recipient_name: str,
    slots: list[str],
    book_urls: list[str] | None = None,
) -> bool:
    """Sends an email with clickable slot cards (or plain list as fallback)."""
    subject = "Let's schedule a call!"

    if book_urls:
        cards_html = ""
        for slot, url in zip(slots, book_urls):
            cards_html += f"""
            <a href="{url}" style="display:block;text-decoration:none;margin-bottom:14px;">
              <table width="100%" cellpadding="0" cellspacing="0"
                     style="border:2px solid #3b82f6;border-radius:10px;background:#eff6ff;">
                <tr>
                  <td style="padding:18px 22px;">
                    <div style="font-size:15px;font-weight:700;color:#1e3a8a;">{slot}</div>
                    <div style="font-size:13px;color:#2563eb;margin-top:6px;">
                      1-hour meeting &nbsp;&middot;&nbsp; <strong>Click to confirm &rarr;</strong>
                    </div>
                  </td>
                </tr>
              </table>
            </a>"""
        slots_section = f"""
            <p style="font-size:15px;color:#334155;margin-bottom:16px;">
              Choose a time that works for you — just click to instantly confirm:
            </p>
            {cards_html}"""
    else:
        items = "".join(f"<li style='margin-bottom:8px;'><strong>{s}</strong></li>" for s in slots)
        slots_section = f"""
            <p style="font-size:15px;color:#334155;">
              Please reply with which time works best:
            </p>
            <ul style="background:#f8fafc;padding:16px 32px;border-radius:6px;border:1px solid #e2e8f0;">
              {items}
            </ul>"""

    html_content = f"""<!DOCTYPE html>
<html>
<head><meta charset="utf-8"></head>
<body style="font-family:-apple-system,sans-serif;background:#f9fafb;margin:0;padding:0;">
  <div style="max-width:600px;margin:40px auto;background:#fff;border:1px solid #e5e7eb;
              border-radius:10px;padding:40px;">
    <div style="font-size:20px;font-weight:700;color:#0f172a;margin-bottom:24px;">
      Jane Aerospace
    </div>
    <p style="font-size:16px;color:#334155;">Hi {recipient_name},</p>
    <p style="font-size:15px;color:#334155;line-height:1.6;">
      Thanks for your interest! I'd love to connect and explore how we can work together.
    </p>
    {slots_section}
    <p style="font-size:15px;color:#334155;margin-top:24px;">
      Looking forward to speaking with you soon!<br><br>
      Best regards,<br><strong>Bharath Reddy</strong>
    </p>
    <div style="margin-top:32px;border-top:1px solid #e5e7eb;padding-top:20px;
                font-size:13px;color:#94a3b8;">
      You're receiving this because you expressed interest in connecting.
    </div>
  </div>
</body>
</html>"""
    print(f"[EMAIL] Sending V2 slots email -> {to_email}")
    return _send_html_email(to_email, subject, html_content)


def send_admin_notification(lead_name: str, lead_email: str, lead_timezone: str) -> bool:
    """Notifies the organizer when a lead submits the details form."""
    subject = f"New Lead: {lead_name} submitted details"
    html_content = f"""
    <!DOCTYPE html>
    <html>
    <head>
        <meta charset="utf-8">
        <style>
            body {{ font-family: 'Inter', -apple-system, sans-serif; background-color: #f1f5f9; margin: 0; padding: 0; }}
            .container {{ max-width: 600px; margin: 40px auto; background: #fff; border: 1px solid #cbd5e1; border-radius: 8px; padding: 40px; }}
            .header {{ border-bottom: 2px solid #3b82f6; padding-bottom: 16px; margin-bottom: 24px; font-size: 20px; font-weight: 700; color: #1e3a8a; }}
            table {{ width: 100%; border-collapse: collapse; margin-top: 16px; }}
            th, td {{ padding: 12px; border-bottom: 1px solid #e2e8f0; text-align: left; }}
            th {{ background: #f8fafc; color: #475569; font-weight: 600; width: 30%; }}
            td {{ color: #0f172a; }}
        </style>
    </head>
    <body>
        <div class="container">
            <div class="header">New Lead Form Submission</div>
            <p>Hello Bharath,</p>
            <p>A new lead has submitted their details. A booking email with your Calendly link has been sent to them.</p>
            <table>
                <tr><th>Full Name</th><td>{lead_name}</td></tr>
                <tr><th>Email</th><td>{lead_email}</td></tr>
                <tr><th>Location</th><td>{lead_timezone}</td></tr>
            </table>
        </div>
    </body>
    </html>
    """
    print(f"[EMAIL] Sending admin notification for lead: {lead_name}")
    return _send_html_email(settings.SMTP_FROM_EMAIL, subject, html_content)


def send_organizer_booking_notification(
    lead_name: str, lead_email: str, slot_start: str, meeting_link: str | None = None
) -> bool:
    """Notifies the organizer when a lead books a meeting."""
    subject = f"New Meeting Booked: {lead_name}"
    link_row = (
        f"<tr><td style='padding:10px;background:#f8fafc;font-weight:600;color:#475569;'>Meeting Link</td>"
        f"<td style='padding:10px;'><a href='{meeting_link}'>{meeting_link}</a></td></tr>"
        if meeting_link else ""
    )
    html_content = f"""
    <div style="font-family:sans-serif;max-width:520px;margin:32px auto;
                background:#fff;border:1px solid #e5e7eb;border-radius:10px;padding:36px;">
        <h2 style="color:#1e3a8a;margin-bottom:8px;">A lead just booked a meeting!</h2>
        <table style="width:100%;border-collapse:collapse;margin-top:16px;">
            <tr>
                <td style="padding:10px;background:#f8fafc;font-weight:600;color:#475569;width:35%;">Name</td>
                <td style="padding:10px;border-bottom:1px solid #e2e8f0;">{lead_name}</td>
            </tr>
            <tr>
                <td style="padding:10px;background:#f8fafc;font-weight:600;color:#475569;">Email</td>
                <td style="padding:10px;border-bottom:1px solid #e2e8f0;">{lead_email}</td>
            </tr>
            <tr>
                <td style="padding:10px;background:#f8fafc;font-weight:600;color:#475569;">Slot</td>
                <td style="padding:10px;border-bottom:1px solid #e2e8f0;">{slot_start}</td>
            </tr>
            {link_row}
        </table>
        <p style="margin-top:24px;color:#6b7280;font-size:13px;">This booking has been recorded in your dashboard.</p>
    </div>
    """
    return _send_html_email(settings.SMTP_FROM_EMAIL, subject, html_content)


def send_booking_confirmation_to_lead(
    to_email: str, recipient_name: str, slot_start: str, meeting_link: str | None = None
) -> bool:
    """Sends a confirmation email to the lead with the booked slot and meeting link."""
    subject = "Your meeting is confirmed!"
    join_button = (
        f'<p style="margin-top:20px;">'
        f'<a href="{meeting_link}" style="display:inline-block;padding:12px 24px;'
        f'background:#1d4ed8;color:#fff;text-decoration:none;border-radius:6px;font-weight:600;">'
        f'Join Meeting</a></p>'
        if meeting_link else
        "<p style='color:#6b7280;font-size:13px;margin-top:20px;'>A calendar invite will be sent to you shortly.</p>"
    )
    html_content = f"""
    <div style="font-family:sans-serif;max-width:520px;margin:32px auto;
                background:#fff;border:1px solid #e5e7eb;border-radius:10px;padding:36px;">
        <h2 style="color:#1e3a8a;margin-bottom:8px;">Meeting Confirmed!</h2>
        <p style="color:#374151;font-size:15px;">Hi {recipient_name},</p>
        <p style="color:#374151;font-size:15px;">
            Your meeting has been booked for <strong>{slot_start}</strong>.</p>
        <p style="color:#374151;font-size:15px;">Looking forward to speaking with you!</p>
        {join_button}
    </div>
    """
    return _send_html_email(to_email, subject, html_content)
