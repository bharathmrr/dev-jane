"""Outbound email helpers — sends via SMTP (Gmail or any provider).

Set SMTP_HOST / SMTP_PORT / SMTP_USERNAME / SMTP_PASSWORD in .env.
For Gmail, generate an App Password at https://myaccount.google.com/apppasswords.
"""
from __future__ import annotations

import smtplib
import ssl
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

from app.core.config import settings


# ------------------------------------------------------------------ #
#  Internal helper                                                     #
# ------------------------------------------------------------------ #

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
            if settings.SMTP_USE_TLS:
                server.starttls(context=context)
            if settings.SMTP_USERNAME:
                server.login(settings.SMTP_USERNAME, settings.SMTP_PASSWORD)
            server.send_message(msg)

        print(f"[EMAIL] ✓ Sent '{subject}' → {to_email}")
        return True
    except smtplib.SMTPAuthenticationError:
        print(
            "[EMAIL] ✗ Authentication failed. "
            "For Gmail, use an App Password (not your account password). "
            "See https://myaccount.google.com/apppasswords"
        )
    except Exception as exc:
        print(f"[EMAIL] ✗ Failed to send '{subject}' → {to_email}: {exc}")
    return False


# ------------------------------------------------------------------ #
#  Public API                                                          #
# ------------------------------------------------------------------ #

def send_booking_email(to_email: str, recipient_name: str) -> bool:
    """Sends the Calendly booking link to a lead after they fill the form."""
    subject = "Let's schedule a call!"
    html_content = f"""
    <!DOCTYPE html>
    <html>
    <head>
        <meta charset="utf-8">
        <style>
            body {{
                font-family: 'Inter', -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto,
                             Helvetica, Arial, sans-serif;
                background-color: #f9fafb;
                margin: 0;
                padding: 0;
                -webkit-font-smoothing: antialiased;
            }}
            .container {{
                max-width: 600px;
                margin: 40px auto;
                background-color: #ffffff;
                border: 1px solid #e5e7eb;
                border-radius: 8px;
                padding: 40px;
                box-shadow: 0 4px 6px -1px rgba(0,0,0,.05), 0 2px 4px -1px rgba(0,0,0,.03);
            }}
            .logo {{
                font-size: 20px;
                font-weight: 700;
                color: #0f172a;
                margin-bottom: 24px;
            }}
            .content {{
                font-size: 16px;
                line-height: 1.6;
                color: #334155;
            }}
            .btn-container {{ margin: 32px 0; text-align: center; }}
            .btn {{
                background-color: #2563eb;
                color: #ffffff !important;
                text-decoration: none;
                padding: 12px 32px;
                border-radius: 6px;
                font-weight: 600;
                font-size: 16px;
                display: inline-block;
            }}
            .footer {{
                margin-top: 40px;
                border-top: 1px solid #e5e7eb;
                padding-top: 24px;
                font-size: 14px;
                color: #64748b;
            }}
        </style>
    </head>
    <body>
        <div class="container">
            <div class="logo">Outreach Automation</div>
            <div class="content">
                <p>Hi {recipient_name},</p>
                <p>Thanks for connecting on LinkedIn! It sounds like there is some great
                synergy between our businesses, and I would love to explore how we can
                work together.</p>
                <p>Please use my Calendly link below to pick a convenient slot. It only
                takes a minute and we'll both receive a calendar invite automatically.</p>

                <div class="btn-container">
                    <a href="{settings.CALENDLY_LINK}" class="btn" target="_blank">
                        Schedule a Meeting
                    </a>
                </div>

                <p>Looking forward to speaking with you soon!</p>
                <p>Best regards,<br>Bharath Reddy</p>
            </div>
            <div class="footer">
                <p>You're receiving this because you expressed interest on LinkedIn.</p>
            </div>
        </div>
    </body>
    </html>
    """
    print(f"[EMAIL] Sending booking email → {to_email}")
    return _send_html_email(to_email, subject, html_content)


def send_admin_notification(lead_name: str, lead_email: str, lead_timezone: str) -> bool:
    """Notifies the organizer when a lead submits the details form."""
    subject = f"New Lead Alert: {lead_name} submitted details!"
    html_content = f"""
    <!DOCTYPE html>
    <html>
    <head>
        <meta charset="utf-8">
        <style>
            body {{
                font-family: 'Inter', -apple-system, sans-serif;
                background-color: #f1f5f9;
                margin: 0; padding: 0;
            }}
            .container {{
                max-width: 600px;
                margin: 40px auto;
                background: #fff;
                border: 1px solid #cbd5e1;
                border-radius: 8px;
                padding: 40px;
            }}
            .header {{
                border-bottom: 2px solid #3b82f6;
                padding-bottom: 16px;
                margin-bottom: 24px;
                font-size: 20px;
                font-weight: 700;
                color: #1e3a8a;
            }}
            table {{ width: 100%; border-collapse: collapse; margin-top: 16px; }}
            th, td {{ padding: 12px; border-bottom: 1px solid #e2e8f0; text-align: left; }}
            th {{ background: #f8fafc; color: #475569; font-weight: 600; width: 30%; }}
            td {{ color: #0f172a; }}
            .footer {{ margin-top: 32px; font-size: 12px; color: #94a3b8; text-align: center; }}
        </style>
    </head>
    <body>
        <div class="container">
            <div class="header">Lead Form Submission Captured</div>
            <p>Hello Bharath,</p>
            <p>A new lead has submitted their details. A booking email with your
            Calendly link has been sent to them.</p>
            <table>
                <tr><th>Full Name</th><td>{lead_name}</td></tr>
                <tr><th>Email</th><td>{lead_email}</td></tr>
                <tr><th>Timezone</th><td>{lead_timezone}</td></tr>
            </table>
            <p style="margin-top:24px;">Please follow up if needed.</p>
            <div class="footer">LinkedIn Outreach Automation</div>
        </div>
    </body>
    </html>
    """
    print(f"[EMAIL] Sending admin notification for lead: {lead_name}")
    return _send_html_email(settings.SMTP_FROM_EMAIL, subject, html_content)


def send_organizer_booking_notification(
    lead_name: str, lead_email: str, slot_start: str
) -> bool:
    """Notifies the organizer when a lead books via Calendly."""
    subject = f"New Meeting Booked: {lead_name}"
    slot_display = (
        slot_start.replace("T", " ").replace("Z", " UTC")[:19]
        if slot_start
        else "Time TBD"
    )
    html_content = f"""
    <div style="font-family:sans-serif;max-width:520px;margin:32px auto;
                background:#fff;border:1px solid #e5e7eb;border-radius:10px;padding:36px;">
        <h2 style="color:#1e3a8a;margin-bottom:8px;">A lead just booked a meeting!</h2>
        <table style="width:100%;border-collapse:collapse;margin-top:16px;">
            <tr>
                <td style="padding:10px;background:#f8fafc;font-weight:600;
                           color:#475569;width:35%;">Name</td>
                <td style="padding:10px;border-bottom:1px solid #e2e8f0;">{lead_name}</td>
            </tr>
            <tr>
                <td style="padding:10px;background:#f8fafc;font-weight:600;color:#475569;">
                    Email</td>
                <td style="padding:10px;border-bottom:1px solid #e2e8f0;">{lead_email}</td>
            </tr>
            <tr>
                <td style="padding:10px;background:#f8fafc;font-weight:600;color:#475569;">
                    Slot</td>
                <td style="padding:10px;">{slot_display}</td>
            </tr>
        </table>
        <p style="margin-top:24px;color:#6b7280;font-size:13px;">
            This booking has been recorded in your dashboard.</p>
    </div>
    """
    return _send_html_email(settings.SMTP_FROM_EMAIL, subject, html_content)


def send_booking_confirmation_to_lead(
    to_email: str, recipient_name: str, slot_start: str
) -> bool:
    """Sends a confirmation email to the lead after they book via Calendly."""
    slot_display = (
        slot_start.replace("T", " ").replace("Z", " UTC")[:19]
        if slot_start
        else "your selected time"
    )
    subject = "Your meeting is confirmed!"
    html_content = f"""
    <div style="font-family:sans-serif;max-width:520px;margin:32px auto;
                background:#fff;border:1px solid #e5e7eb;border-radius:10px;padding:36px;">
        <h2 style="color:#1e3a8a;margin-bottom:8px;">Meeting Confirmed!</h2>
        <p style="color:#374151;font-size:15px;">Hi {recipient_name},</p>
        <p style="color:#374151;font-size:15px;">
            Your meeting has been booked for <strong>{slot_display}</strong>.</p>
        <p style="color:#374151;font-size:15px;">
            You'll receive a calendar invite shortly. Looking forward to speaking with you!</p>
        <p style="margin-top:24px;color:#6b7280;font-size:13px;">
            To reschedule, use the link in your Calendly confirmation email.</p>
    </div>
    """
    return _send_html_email(to_email, subject, html_content)
