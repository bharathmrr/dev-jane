"""Outbound email helpers — sends via Gmail SMTP.

Email Pipeline:
  Stage 1 (send_week_selection_email)  — Cold outreach: plain long-form text + week cards
  Stage 2 (send_v2_slots_email)        — Slot cards: clickable time slot cards
  Stage 3 (send_booking_confirmation_to_lead) — Booking confirmed, plain text
  Reminder (send_v2_reminder_email)    — Follow-up, plain text + week cards
"""
from __future__ import annotations

import hashlib
import hmac as _hmac
import smtplib
import ssl
from datetime import datetime
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from zoneinfo import ZoneInfo

from app.core.config import settings

_IST = ZoneInfo("Asia/Kolkata")

# ---------------------------------------------------------------------------
# After-hours detection (#11)
# Business hours: 9:00 AM – 9:00 PM IST  Mon–Sat
# Replies arriving outside these hours are queued for 9am IST next business day
# ---------------------------------------------------------------------------

_BUSINESS_START_HOUR = 9    # 9:00 AM IST
_BUSINESS_END_HOUR   = 21   # 9:00 PM IST


def is_after_hours() -> bool:
    """Return True if current IST time is outside business hours or it's Sunday."""
    now_ist = datetime.now(_IST)
    if now_ist.weekday() == 6:   # Sunday
        return True
    return not (_BUSINESS_START_HOUR <= now_ist.hour < _BUSINESS_END_HOUR)


def make_confirm_booking_url(lead_id: str, slot_idx: int) -> str:
    """Signed URL for confirming a pending slot booking (#17)."""
    msg = f"confirm:{lead_id}:{slot_idx}".encode()
    sig = _hmac.new(settings.EMAIL_HMAC_SECRET.encode(), msg, hashlib.sha256).hexdigest()[:16]
    return f"{settings.APP_URL.rstrip('/')}/api/v1/v2/confirm-booking/{lead_id}/{slot_idx}/{sig}"


# ---------------------------------------------------------------------------
# Location → UTC offset mapping for dual-timezone display (#21)
# ---------------------------------------------------------------------------

_LOCATION_TZ: dict[str, str] = {
    # Indian cities → IST (+05:30) — all map to same display offset
    "mumbai": "IST (UTC+5:30)", "delhi": "IST (UTC+5:30)", "bangalore": "IST (UTC+5:30)",
    "bengaluru": "IST (UTC+5:30)", "chennai": "IST (UTC+5:30)", "hyderabad": "IST (UTC+5:30)",
    "pune": "IST (UTC+5:30)", "kolkata": "IST (UTC+5:30)", "ahmedabad": "IST (UTC+5:30)",
    "jaipur": "IST (UTC+5:30)", "surat": "IST (UTC+5:30)", "lucknow": "IST (UTC+5:30)",
    "kanpur": "IST (UTC+5:30)", "nagpur": "IST (UTC+5:30)", "indore": "IST (UTC+5:30)",
    "thane": "IST (UTC+5:30)", "bhopal": "IST (UTC+5:30)", "visakhapatnam": "IST (UTC+5:30)",
    "pimpri": "IST (UTC+5:30)", "patna": "IST (UTC+5:30)", "coimbatore": "IST (UTC+5:30)",
    "gurugram": "IST (UTC+5:30)", "gurgaon": "IST (UTC+5:30)", "noida": "IST (UTC+5:30)",
    "kochi": "IST (UTC+5:30)", "chandigarh": "IST (UTC+5:30)",
    # International
    "london": "GMT (UTC+0)", "uk": "GMT (UTC+0)", "dubai": "GST (UTC+4)",
    "singapore": "SGT (UTC+8)", "new york": "EST (UTC-5)", "usa": "EST (UTC-5)",
    "sydney": "AEST (UTC+10)", "tokyo": "JST (UTC+9)", "berlin": "CET (UTC+1)",
    "paris": "CET (UTC+1)", "toronto": "EST (UTC-5)", "hong kong": "HKT (UTC+8)",
}

_IST_OFFSET_MINS = 330   # 5h 30m

_TZ_OFFSET_MINS: dict[str, int] = {
    "IST (UTC+5:30)": 330,
    "GMT (UTC+0)": 0,
    "GST (UTC+4)": 240,
    "SGT (UTC+8)": 480,
    "EST (UTC-5)": -300,
    "AEST (UTC+10)": 600,
    "JST (UTC+9)": 540,
    "CET (UTC+1)": 60,
    "HKT (UTC+8)": 480,
}


def _local_tz_label(location: str | None) -> str | None:
    """Return a TZ label for the lead's location, or None if unknown."""
    if not location:
        return None
    loc = location.lower().strip()
    for key, label in _LOCATION_TZ.items():
        if key in loc:
            return label
    return None


def _convert_slot_to_local(slot_display: str, local_tz_label: str) -> str | None:
    """Convert 'Monday, Jun 10 at 09:00 AM' (IST) to local time string."""
    try:
        import dateutil.parser as _dup
        dt = _dup.parse(slot_display)
        offset_mins = _TZ_OFFSET_MINS.get(local_tz_label, _IST_OFFSET_MINS)
        delta_mins = offset_mins - _IST_OFFSET_MINS
        from datetime import timedelta
        local_dt = dt + timedelta(minutes=delta_mins)
        return local_dt.strftime("%I:%M %p") + f" {local_tz_label.split(' ')[0]}"
    except Exception:
        return None


def _slot_cards_dual_tz(slots: list[str], book_urls: list[str], lead_location: str | None) -> str:
    """Render slot cards with dual timezone (IST + lead's local time) (#21)."""
    local_label = _local_tz_label(lead_location)
    cards = ""
    for slot, url in zip(slots, book_urls):
        local_time_str = ""
        if local_label and local_label != "IST (UTC+5:30)":
            converted = _convert_slot_to_local(slot, local_label)
            if converted:
                local_time_str = f'<span style="color:#6b7280;font-size:13px;"> / {converted}</span>'

        cards += f"""
  <tr>
    <td style="padding-bottom:10px;">
      <a href="{url}" style="text-decoration:none;display:block;">
        <table cellpadding="0" cellspacing="0" style="width:100%;border:1px solid #d1d5db;border-radius:8px;background:#f9fafb;">
          <tr>
            <td style="padding:16px 20px;font-family:Arial,sans-serif;font-size:15px;font-weight:600;color:#111111;">
              &#128336;&nbsp; {slot} IST{local_time_str}
            </td>
            <td style="padding:16px 20px;text-align:right;font-family:Arial,sans-serif;font-size:15px;color:#1155cc;font-weight:600;white-space:nowrap;">
              Confirm &rarr;
            </td>
          </tr>
        </table>
      </a>
    </td>
  </tr>"""
    return f'<table cellpadding="0" cellspacing="0" style="margin:24px 0 8px 0;width:100%;max-width:480px;">{cards}</table>'


# ---------------------------------------------------------------------------
# URL helpers
# ---------------------------------------------------------------------------

def make_book_url(lead_id: str, slot_idx: int) -> str:
    msg = f"{lead_id}:{slot_idx}".encode()
    sig = _hmac.new(settings.EMAIL_HMAC_SECRET.encode(), msg, hashlib.sha256).hexdigest()[:16]
    return f"{settings.APP_URL.rstrip('/')}/api/v1/v2/book/{lead_id}/{slot_idx}/{sig}"


def make_week_url(lead_id: str, week: str) -> str:
    msg = f"{lead_id}:week:{week}".encode()
    sig = _hmac.new(settings.EMAIL_HMAC_SECRET.encode(), msg, hashlib.sha256).hexdigest()[:16]
    return f"{settings.APP_URL.rstrip('/')}/api/v1/v2/week/{lead_id}/{week}/{sig}"


# ---------------------------------------------------------------------------
# Internal send helper
# ---------------------------------------------------------------------------

def _send_html_email(
    to_email: str,
    subject: str,
    html_content: str,
    lead_id: str | None = None,
    check_throttle: bool = True,
) -> bool:
    # SMTP daily throttle check (#40)
    if check_throttle:
        try:
            from app.services.email_tracker import smtp_can_send, smtp_record_send
            if not smtp_can_send():
                print(f"[EMAIL] SMTP daily limit reached — queuing '{subject}' for tomorrow")
                return False
        except Exception:
            pass  # if tracker unavailable, allow send

    try:
        msg = MIMEMultipart("alternative")
        msg["Subject"] = subject
        msg["From"] = f"{settings.ORGANIZER_NAME} <{settings.SMTP_FROM_EMAIL}>"
        msg["To"] = to_email

        # Inject tracking pixel if lead_id provided (#3)
        final_html = html_content
        if lead_id:
            try:
                from app.services.email_tracker import pixel_html
                px = pixel_html(lead_id)
                # Insert just before </body>
                if "</body>" in final_html:
                    final_html = final_html.replace("</body>", f"{px}</body>")
                else:
                    final_html += px
            except Exception:
                pass

        msg.attach(MIMEText(final_html, "html"))

        context = ssl.create_default_context()
        with smtplib.SMTP(settings.SMTP_HOST, settings.SMTP_PORT, timeout=20) as server:
            server.ehlo()
            if settings.SMTP_USE_TLS:
                server.starttls(context=context)
                server.ehlo()
            if settings.SMTP_USERNAME:
                server.login(settings.SMTP_USERNAME, settings.SMTP_PASSWORD)
            server.send_message(msg)

        # Record send for throttle counter
        try:
            from app.services.email_tracker import smtp_record_send
            smtp_record_send()
        except Exception:
            pass

        print(f"[EMAIL] Sent '{subject}' -> {to_email}")
        return True
    except smtplib.SMTPAuthenticationError:
        print("[EMAIL] Auth failed — check SMTP_PASSWORD app password in .env")
    except Exception as exc:
        print(f"[EMAIL] Failed '{subject}' -> {to_email}: {exc}")
    return False


def _signature() -> str:
    name = settings.ORGANIZER_NAME
    org_email = settings.ORGANIZER_EMAIL
    return (
        f'<p style="margin:32px 0 4px 0;font-family:Arial,sans-serif;font-size:15px;color:#111111;">Kind regards,</p>'
        f'<p style="margin:0 0 2px 0;font-family:Arial,sans-serif;font-size:15px;color:#111111;"><strong>{name}</strong></p>'
        f'<p style="margin:0 0 2px 0;font-family:Arial,sans-serif;font-size:13px;color:#555555;">Founder &amp; Managing Director</p>'
        f'<p style="margin:0 0 2px 0;font-family:Arial,sans-serif;font-size:13px;color:#555555;">Jane Aerospace</p>'
        f'<p style="margin:0;"><a href="mailto:{org_email}" style="color:#1155cc;font-family:Arial,sans-serif;font-size:13px;text-decoration:none;">{org_email}</a></p>'
    )


def _week_cards(this_week_url: str, next_week_url: str) -> str:
    """Render This week / Next week as clickable cards."""
    return f"""
<table cellpadding="0" cellspacing="0" style="margin:24px 0 8px 0;width:100%;max-width:480px;">
  <tr>
    <td style="padding-bottom:12px;">
      <a href="{this_week_url}" style="text-decoration:none;display:block;">
        <table cellpadding="0" cellspacing="0" style="width:100%;border:1px solid #d1d5db;border-radius:8px;background:#f9fafb;">
          <tr>
            <td style="padding:16px 20px;font-family:Arial,sans-serif;font-size:15px;font-weight:600;color:#111111;">
              &#128197;&nbsp; This week
            </td>
            <td style="padding:16px 20px;text-align:right;font-family:Arial,sans-serif;font-size:15px;color:#1155cc;font-weight:600;">
              Click to select &rarr;
            </td>
          </tr>
        </table>
      </a>
    </td>
  </tr>
  <tr>
    <td>
      <a href="{next_week_url}" style="text-decoration:none;display:block;">
        <table cellpadding="0" cellspacing="0" style="width:100%;border:1px solid #d1d5db;border-radius:8px;background:#f9fafb;">
          <tr>
            <td style="padding:16px 20px;font-family:Arial,sans-serif;font-size:15px;font-weight:600;color:#111111;">
              &#128197;&nbsp; Next week
            </td>
            <td style="padding:16px 20px;text-align:right;font-family:Arial,sans-serif;font-size:15px;color:#1155cc;font-weight:600;">
              Click to select &rarr;
            </td>
          </tr>
        </table>
      </a>
    </td>
  </tr>
</table>"""


def _slot_cards(slots: list[str], book_urls: list[str]) -> str:
    """Render each slot as a clickable card."""
    cards = ""
    for slot, url in zip(slots, book_urls):
        cards += f"""
  <tr>
    <td style="padding-bottom:10px;">
      <a href="{url}" style="text-decoration:none;display:block;">
        <table cellpadding="0" cellspacing="0" style="width:100%;border:1px solid #d1d5db;border-radius:8px;background:#f9fafb;">
          <tr>
            <td style="padding:16px 20px;font-family:Arial,sans-serif;font-size:15px;font-weight:600;color:#111111;">
              &#128336;&nbsp; {slot}
            </td>
            <td style="padding:16px 20px;text-align:right;font-family:Arial,sans-serif;font-size:15px;color:#1155cc;font-weight:600;white-space:nowrap;">
              Confirm &rarr;
            </td>
          </tr>
        </table>
      </a>
    </td>
  </tr>"""
    return f'<table cellpadding="0" cellspacing="0" style="margin:24px 0 8px 0;width:100%;max-width:480px;">{cards}</table>'


def _body_wrap(paragraphs: list[str], cards_html: str, closing_lines: list[str]) -> str:
    """Assemble the full email HTML — plain text paragraphs + cards + signature."""
    paras_html = "".join(
        f'<p style="margin:0 0 18px 0;font-family:Arial,sans-serif;font-size:15px;color:#111111;line-height:1.75;">{p}</p>'
        for p in paragraphs if p != ""
    )
    closing_html = "".join(
        f'<p style="margin:0 0 18px 0;font-family:Arial,sans-serif;font-size:15px;color:#111111;line-height:1.75;">{p}</p>'
        for p in closing_lines if p != ""
    )
    return f"""<!DOCTYPE html>
<html>
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width,initial-scale=1">
</head>
<body style="margin:0;padding:0;background:#ffffff;">
  <div style="max-width:600px;margin:40px auto;padding:0 24px 60px;">
    {paras_html}
    {cards_html}
    {closing_html}
    {_signature()}
  </div>
</body>
</html>"""


# ---------------------------------------------------------------------------
# Stage 1 — Week selection outreach
# ---------------------------------------------------------------------------

def send_week_selection_email(
    to_email: str,
    recipient_name: str,
    this_week_url: str,
    next_week_url: str,
    body_text: str | None = None,
    contact_name: str | None = None,
    subject_override: str | None = None,
    lead_id: str | None = None,
) -> bool:
    greeting_name = (contact_name or recipient_name).split()[0].capitalize()
    company_name = recipient_name
    subject = subject_override or f"Partnership Opportunity — Jane Aerospace × {company_name}"

    # AI-generated intro paragraph (personalized) or default
    if body_text:
        intro_paras = [p.strip() for p in body_text.split("\n\n") if p.strip()]
    else:
        intro_paras = [
            f"I came across {company_name} while exploring companies doing meaningful work in procurement and supply chain — and your name kept coming up in the right conversations.",
        ]

    body_paras = [f"Hi {greeting_name},", ""] + intro_paras + [
        "",
        f"My name is {settings.ORGANIZER_NAME}, Founder and Managing Director of Jane Aerospace, where we focus on unmanned aviation products, supply chain integration, vendor network intelligence, and AI-enabled procurement solutions aligned with the Atmanirbhar Bharat mission.",
        f"We have been working with companies like yours to help them streamline procurement cycles, build resilient supplier networks, and stay ahead of industry shifts. I believe there is a genuine opportunity for us to explore together.",
        "I am not looking to pitch anything heavy — just a focused 20-minute call to understand where you are headed and share what we are building. No obligations whatsoever.",
        "Please click one of the options below to let me know which week works better for you:",
    ]

    closing = [
        "Alternatively, feel free to reply directly to this email and I will work around your schedule.",
        "Thank you for your time and I look forward to hearing from you.",
    ]

    print(f"[EMAIL] Sending week-selection email -> {to_email}")
    return _send_html_email(
        to_email, subject,
        _body_wrap(body_paras, _week_cards(this_week_url, next_week_url), closing),
        lead_id=lead_id,
    )


# ---------------------------------------------------------------------------
# Stage 2 — Slot cards email
# ---------------------------------------------------------------------------

def send_v2_slots_email(
    to_email: str,
    recipient_name: str,
    slots: list[str],
    book_urls: list[str] | None = None,
    body_text: str | None = None,
    contact_name: str | None = None,
    lead_id: str | None = None,
    lead_location: str | None = None,
) -> bool:
    greeting_name = (contact_name or recipient_name).split()[0].capitalize()
    subject = f"Available Times — Jane Aerospace"

    if body_text:
        intro_paras = [p.strip() for p in body_text.split("\n\n") if p.strip()]
    else:
        intro_paras = [
            "Thank you for getting back to me — I really appreciate it.",
            "I have checked our calendar and put together a few time slots that are open right now. Each of the times listed below is confirmed available. Simply click the one that works best for you and your spot will be locked in instantly — no back and forth needed.",
        ]

    body_paras = [f"Hi {greeting_name},", ""] + intro_paras

    closing = [
        "If none of these times work for you, please do not hesitate to reply to this email with a date and time that suits you better and I will do my best to accommodate.",
        "Looking forward to speaking with you.",
    ]

    if book_urls:
        # Use dual-timezone cards (#21)
        cards_html = _slot_cards_dual_tz(slots, book_urls, lead_location)
    else:
        items = "".join(f"<li style='margin-bottom:8px;font-family:Arial,sans-serif;font-size:15px;'>{s}</li>" for s in slots)
        cards_html = f'<ul style="margin:20px 0;padding-left:20px;">{items}</ul>'

    print(f"[EMAIL] Sending slots email -> {to_email}")
    return _send_html_email(
        to_email, subject,
        _body_wrap(body_paras, cards_html, closing),
        lead_id=lead_id,
    )


# ---------------------------------------------------------------------------
# Stage 3 — Booking confirmation
# ---------------------------------------------------------------------------

def send_booking_confirmation_to_lead(
    to_email: str,
    recipient_name: str,
    slot_start: str,
    meeting_link: str | None = None,
    contact_name: str | None = None,
) -> bool:
    greeting_name = (contact_name or recipient_name).split()[0].capitalize()
    subject = f"Meeting Confirmed — Jane Aerospace"

    link_line = (
        f'You can join the call using the following link: <a href="{meeting_link}" style="color:#1155cc;">{meeting_link}</a>'
        if meeting_link else
        "A calendar invitation with the full meeting details will be sent to your inbox shortly."
    )

    body_paras = [
        f"Hi {greeting_name},",
        "",
        "I am delighted to confirm that your meeting with Jane Aerospace has been successfully scheduled. We are genuinely looking forward to speaking with you.",
        f"Your confirmed meeting time is: <strong>{slot_start} IST</strong>",
        link_line,
        "Please add this to your calendar so you do not miss it. If for any reason you need to reschedule or if anything changes on your end, please do not hesitate to reply to this email and we will find another time that works — there is absolutely no trouble at all.",
        "We are excited about this conversation and believe it will be a valuable one for both sides.",
        "Thank you once again for your time and we look forward to speaking with you soon.",
    ]

    print(f"[EMAIL] Sending booking confirmation -> {to_email}")
    return _send_html_email(
        to_email, subject,
        _body_wrap(body_paras, "", [])
    )


# ---------------------------------------------------------------------------
# Reminder — Follow-up
# ---------------------------------------------------------------------------

def send_v2_reminder_email(
    to_email: str,
    recipient_name: str,
    this_week_url: str,
    next_week_url: str,
    follow_up_count: int = 1,
    contact_name: str | None = None,
) -> bool:
    greeting_name = (contact_name or recipient_name).split()[0].capitalize()
    subject = f"Following Up — Jane Aerospace"

    if follow_up_count == 1:
        body_paras = [
            f"Hi {greeting_name},",
            "",
            "I wanted to follow up on my earlier note in case it got buried — inboxes can get very busy and I completely understand.",
            "I reached out because I genuinely believe there is a real opportunity for Jane Aerospace and your organisation to explore a conversation together. We work with companies on supply chain integration, drone procurement, and AI-enabled vendor intelligence, and the work your team is doing is exactly the kind of initiative we love to support.",
            "I am only asking for 20 minutes of your time — no heavy pitch, just an open conversation about where things are heading and whether there is a fit worth exploring.",
            "If any of these weeks work for you, please click below to pick a time:",
        ]
        closing = [
            "Or simply reply to this email and we will sort something out.",
            "Thank you for your time and I hope to hear from you soon.",
        ]
    else:
        body_paras = [
            f"Hi {greeting_name},",
            "",
            "I hope you are doing well. This will be my last follow-up and I want to keep it brief.",
            "I have genuinely enjoyed learning about what your organisation is doing and I still believe there is something worth exploring together. That said, I also respect that timing is everything in business and now may simply not be the right moment.",
            "If you are ever open to a conversation in the future — whether it is this month or six months from now — please know that the door is always open at Jane Aerospace.",
            "If now works, please click below:",
        ]
        closing = [
            "Wishing you and your team all the very best.",
        ]

    print(f"[EMAIL] Sending follow-up #{follow_up_count} -> {to_email}")
    return _send_html_email(
        to_email, subject,
        _body_wrap(body_paras, _week_cards(this_week_url, next_week_url), closing)
    )


# ---------------------------------------------------------------------------
# Organizer notification — internal alert when a lead books
# ---------------------------------------------------------------------------

def send_organizer_booking_notification(
    lead_name: str, lead_email: str, slot_start: str, meeting_link: str | None = None
) -> bool:
    subject = f"New Booking: {lead_name}"
    link_row = (
        f"<tr><td style='padding:10px 14px;background:#f8fafc;font-weight:600;color:#475569;"
        f"font-family:sans-serif;font-size:13px;'>Meeting Link</td>"
        f"<td style='padding:10px 14px;font-family:sans-serif;font-size:13px;'>"
        f"<a href='{meeting_link}' style='color:#1d4ed8;'>{meeting_link}</a></td></tr>"
        if meeting_link else ""
    )
    html_content = f"""
    <div style="font-family:sans-serif;max-width:520px;margin:32px auto;
                background:#fff;border:1px solid #e5e7eb;border-radius:10px;padding:36px;">
        <h2 style="color:#1e3a8a;margin:0 0 20px;font-size:20px;">New meeting booked!</h2>
        <table style="width:100%;border-collapse:collapse;border:1px solid #e2e8f0;border-radius:8px;overflow:hidden;">
            <tr>
                <td style="padding:10px 14px;background:#f8fafc;font-weight:600;color:#475569;
                           font-size:13px;width:30%;">Name</td>
                <td style="padding:10px 14px;border-bottom:1px solid #e2e8f0;font-size:13px;">{lead_name}</td>
            </tr>
            <tr>
                <td style="padding:10px 14px;background:#f8fafc;font-weight:600;color:#475569;font-size:13px;">Email</td>
                <td style="padding:10px 14px;border-bottom:1px solid #e2e8f0;font-size:13px;">{lead_email}</td>
            </tr>
            <tr>
                <td style="padding:10px 14px;background:#f8fafc;font-weight:600;color:#475569;font-size:13px;">Slot</td>
                <td style="padding:10px 14px;border-bottom:1px solid #e2e8f0;font-size:13px;
                           font-weight:600;color:#1e3a8a;">{slot_start} IST</td>
            </tr>
            {link_row}
        </table>
        <p style="margin-top:20px;color:#6b7280;font-size:12px;">Recorded in your dashboard automatically.</p>
    </div>"""
    return _send_html_email(settings.ORGANIZER_EMAIL, subject, html_content)


# ---------------------------------------------------------------------------
# Legacy admin notification (used by old v1 flow)
# ---------------------------------------------------------------------------

def send_escalation_alert(
    lead_name: str,
    lead_email: str,
    designation: str | None,
    reason: str,
    reply_body: str | None = None,
) -> bool:
    """Alert organizer that a senior lead or flagged reply needs human attention (#38, #12)."""
    subject = f"[ACTION NEEDED] {reason} — {lead_name}"
    snippet = (reply_body or "")[:400].replace("\n", "<br>")
    html = f"""
    <div style="font-family:sans-serif;max-width:600px;margin:32px auto;border:2px solid #dc2626;
                border-radius:10px;padding:32px;background:#fff;">
      <h2 style="color:#dc2626;margin:0 0 16px;">&#9888; Human Action Required</h2>
      <p><strong>Reason:</strong> {reason}</p>
      <table style="width:100%;border-collapse:collapse;margin:16px 0;">
        <tr><td style="padding:8px;background:#f8fafc;font-weight:600;width:30%;">Lead</td>
            <td style="padding:8px;">{lead_name}</td></tr>
        <tr><td style="padding:8px;background:#f8fafc;font-weight:600;">Email</td>
            <td style="padding:8px;">{lead_email}</td></tr>
        <tr><td style="padding:8px;background:#f8fafc;font-weight:600;">Designation</td>
            <td style="padding:8px;">{designation or '—'}</td></tr>
      </table>
      {"<p><strong>Their reply:</strong></p><blockquote style='border-left:3px solid #dc2626;padding-left:12px;color:#374151;'>" + snippet + "</blockquote>" if snippet else ""}
      <p style="color:#6b7280;font-size:12px;margin-top:24px;">Do NOT send an AI reply. Review and respond manually.</p>
    </div>"""
    return _send_html_email(settings.ORGANIZER_EMAIL, subject, html, check_throttle=False)


def send_zoho_down_alert(lead_name: str, lead_email: str) -> bool:
    """Alert organizer that Zoho API is down and a lead needs manual confirmation (#19)."""
    subject = f"[ZOHO DOWN] Manual booking needed — {lead_name}"
    html = f"""
    <div style="font-family:sans-serif;max-width:600px;margin:32px auto;border:2px solid #f59e0b;
                border-radius:10px;padding:32px;">
      <h2 style="color:#f59e0b;">&#9888; Zoho Booking System Offline</h2>
      <p>Lead <strong>{lead_name}</strong> ({lead_email}) tried to book a slot but Zoho API is unavailable.</p>
      <p>Please book manually and send them a confirmation email.</p>
    </div>"""
    return _send_html_email(settings.ORGANIZER_EMAIL, subject, html, check_throttle=False)


def send_admin_notification(lead_name: str, lead_email: str, lead_timezone: str) -> bool:
    subject = f"New Lead: {lead_name} submitted details"
    html_content = f"""
    <div style="font-family:sans-serif;max-width:600px;margin:40px auto;background:#fff;
                border:1px solid #cbd5e1;border-radius:8px;padding:40px;">
        <div style="border-bottom:2px solid #3b82f6;padding-bottom:16px;margin-bottom:24px;
                    font-size:20px;font-weight:700;color:#1e3a8a;">New Lead Form Submission</div>
        <p>Hello {settings.ORGANIZER_NAME},</p>
        <p>A new lead has submitted their details.</p>
        <table style="width:100%;border-collapse:collapse;margin-top:16px;">
            <tr><td style="padding:12px;background:#f8fafc;color:#475569;font-weight:600;width:30%;">Full Name</td>
                <td style="padding:12px;color:#0f172a;">{lead_name}</td></tr>
            <tr><td style="padding:12px;background:#f8fafc;color:#475569;font-weight:600;">Email</td>
                <td style="padding:12px;color:#0f172a;">{lead_email}</td></tr>
            <tr><td style="padding:12px;background:#f8fafc;color:#475569;font-weight:600;">Location</td>
                <td style="padding:12px;color:#0f172a;">{lead_timezone}</td></tr>
        </table>
    </div>"""
    return _send_html_email(settings.SMTP_FROM_EMAIL, subject, html_content)
