"""SMTP outbound transport.

Sets RFC 5322 threading headers so replies stay in-thread, and a Reply-To that
carries a signed thread token (anti-spoof: we trust the token, not From).
Returns the generated Message-ID so callers can persist it for thread tracking.
"""
from __future__ import annotations

import smtplib
import ssl
from email.message import EmailMessage
from email.utils import format_datetime, make_msgid
from datetime import datetime, timezone

from app.core.config import settings
from app.core.logging import get_logger

log = get_logger(__name__)


def reply_to_address(reply_token: str) -> str:
    """Gmail plus-addressing: bharathreddyget+<token>@gmail.com
    Replies land in the real inbox; IMAP poller extracts the token from the To: header."""
    local = settings.SMTP_FROM_EMAIL.split("@")[0]
    return f"{local}+{reply_token}@{settings.INBOUND_DOMAIN}"


def send_email(
    *,
    to_addr: str,
    subject: str,
    body: str,
    reply_token: str,
    in_reply_to: str | None = None,
    references: list[str] | None = None,
) -> str:
    msg = EmailMessage()
    message_id = make_msgid(domain=settings.INBOUND_DOMAIN)
    msg["Message-ID"] = message_id
    msg["From"] = f"{settings.SMTP_FROM_NAME} <{settings.SMTP_FROM_EMAIL}>"
    msg["To"] = to_addr
    msg["Subject"] = subject
    msg["Reply-To"] = reply_to_address(reply_token)
    msg["Date"] = format_datetime(datetime.now(timezone.utc))
    if in_reply_to:
        msg["In-Reply-To"] = in_reply_to
        msg["References"] = " ".join((references or []) + [in_reply_to])
    msg.set_content(body)

    context = ssl.create_default_context()
    with smtplib.SMTP(settings.SMTP_HOST, settings.SMTP_PORT, timeout=20) as server:
        if settings.SMTP_USE_TLS:
            server.starttls(context=context)
        if settings.SMTP_USERNAME:
            server.login(settings.SMTP_USERNAME, settings.SMTP_PASSWORD)
        server.send_message(msg)

    log.info("email_sent", to=to_addr, subject=subject, message_id=message_id)
    return message_id
