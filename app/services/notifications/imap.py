"""IMAP inbound polling and reply parsing.

Polls the mailbox for unseen messages, extracts the signed thread token from
the To/Delivered-To address (reply+<token>@domain), pulls the plain-text body,
and strips quoted history so the LLM only sees what the person actually typed.

Returns lightweight dicts; persistence + booking actions happen in the email
processor / Celery task so this module stays I/O-only and testable.
"""
from __future__ import annotations

import email
import imaplib
import re
from email.message import Message
from email.utils import parseaddr

from app.core.config import settings
from app.core.logging import get_logger
from app.core.security import verify_thread_token

log = get_logger(__name__)

_TOKEN_RE = re.compile(r"reply\+([^@]+)@")
# Common reply separators used to trim quoted history.
_QUOTE_MARKERS = (
    re.compile(r"^On .+ wrote:$", re.MULTILINE),
    re.compile(r"^-----Original Message-----", re.MULTILINE),
    re.compile(r"^_{5,}$", re.MULTILINE),
)


def extract_token(msg: Message) -> str | None:
    for header in ("Delivered-To", "To", "X-Original-To", "Envelope-To"):
        value = msg.get(header, "")
        m = _TOKEN_RE.search(value)
        if m:
            return verify_thread_token(m.group(1))
    return None


def _get_plaintext(msg: Message) -> str:
    if msg.is_multipart():
        for part in msg.walk():
            if part.get_content_type() == "text/plain" and not part.get_filename():
                payload = part.get_payload(decode=True) or b""
                return payload.decode(part.get_content_charset() or "utf-8", "replace")
        return ""
    payload = msg.get_payload(decode=True) or b""
    return payload.decode(msg.get_content_charset() or "utf-8", "replace")


def strip_quoted(text: str) -> str:
    cut = len(text)
    for rx in _QUOTE_MARKERS:
        m = rx.search(text)
        if m:
            cut = min(cut, m.start())
    # Also drop leading ">" quote lines.
    lines = [ln for ln in text[:cut].splitlines() if not ln.lstrip().startswith(">")]
    return "\n".join(lines).strip()


def fetch_replies() -> list[dict]:
    """Fetch unseen messages, returning parsed reply dicts. Marks them seen."""
    conn = (
        imaplib.IMAP4_SSL(settings.IMAP_HOST, settings.IMAP_PORT)
        if settings.IMAP_USE_SSL
        else imaplib.IMAP4(settings.IMAP_HOST, settings.IMAP_PORT)
    )
    out: list[dict] = []
    try:
        conn.login(settings.IMAP_USERNAME, settings.IMAP_PASSWORD)
        conn.select(settings.IMAP_MAILBOX)
        typ, data = conn.search(None, "UNSEEN")
        if typ != "OK":
            return out
        for num in data[0].split():
            typ, raw = conn.fetch(num, "(RFC822)")
            if typ != "OK" or not raw or not raw[0]:
                continue
            msg = email.message_from_bytes(raw[0][1])
            token = extract_token(msg)
            from_name, from_addr = parseaddr(msg.get("From", ""))
            body = strip_quoted(_get_plaintext(msg))
            out.append(
                {
                    "thread_token": token,
                    "message_id": msg.get("Message-ID"),
                    "in_reply_to": msg.get("In-Reply-To"),
                    "from_addr": from_addr,
                    "from_name": from_name,
                    "subject": msg.get("Subject"),
                    "body": body,
                }
            )
            conn.store(num, "+FLAGS", "\\Seen")
    finally:
        try:
            conn.logout()
        except Exception:  # pragma: no cover
            pass
    log.info("imap_fetched", count=len(out))
    return out
