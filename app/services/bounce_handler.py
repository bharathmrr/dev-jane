"""Email bounce + job-change + shared-inbox detection.

Scenarios covered:
  #1  Hard bounce  → mark INVALID_EMAIL, stop all sends
  #2  Soft bounce  → retry 6h → 24h → 72h, then treat as hard after 3 soft bounces
  #5  Shared inbox → detect info@/contact@/hello@ etc., personalise with company name
  #16 Job change   → extract new contact, create new lead, mark old as JOB_CHANGED
"""
from __future__ import annotations

import re
from structlog import get_logger

logger = get_logger(__name__)

# ---------------------------------------------------------------------------
# Hard bounce patterns — permanent delivery failures
# ---------------------------------------------------------------------------
_HARD_PATTERNS = [
    r"user\s+unknown",
    r"no\s+such\s+user",
    r"mailbox\s+not\s+found",
    r"does\s+not\s+exist",
    r"invalid\s+address",
    r"address\s+rejected",
    r"account\s+does\s+not\s+exist",
    r"email\s+address\s+does\s+not\s+exist",
    r"undeliverable",
    r"delivery\s+failed\s+permanently",
    r"permanent\s+error",
    r"\b55[013-9]\b",   # 550, 551, 553, 554 SMTP codes
    r"recipient\s+not\s+found",
    r"bad\s+destination\s+mailbox",
    r"mailbox\s+unavailable",
]

# ---------------------------------------------------------------------------
# Soft bounce patterns — temporary delivery failures
# ---------------------------------------------------------------------------
_SOFT_PATTERNS = [
    r"mailbox\s+full",
    r"over\s+quota",
    r"quota\s+exceeded",
    r"try\s+again\s+later",
    r"service\s+temporarily\s+unavailable",
    r"connection\s+timed?\s+out",
    r"too\s+many\s+connections",
    r"temporarily\s+(rejected|deferred|unavailable)",
    r"\b4[2-5]\d\b",    # 421, 450, 451, 452 SMTP codes
    r"greylisted",
]

# ---------------------------------------------------------------------------
# Job-change auto-reply patterns
# ---------------------------------------------------------------------------
_JOB_CHANGE_PATTERNS = [
    r"i\s+no\s+longer\s+work",
    r"i\s+have\s+left\s+the\s+company",
    r"no\s+longer\s+employed",
    r"has\s+left\s+the\s+(company|organization|organisation)",
    r"please\s+(contact|reach\s+out\s+to|email)\s+([A-Z][a-z]+)",
    r"my\s+replacement\s+is",
    r"new\s+point\s+of\s+contact",
    r"i\s+(left|resigned|departed)",
    r"forwarding\s+to\s+([A-Z][a-z]+)",
    r"please\s+update\s+your\s+records",
]

# ---------------------------------------------------------------------------
# Shared / generic inbox prefixes
# ---------------------------------------------------------------------------
_SHARED_PREFIXES = {
    "info", "contact", "hello", "hi", "support", "admin", "sales",
    "team", "office", "mail", "help", "enquiry", "enquiries",
    "inquiry", "inquiries", "general", "noreply", "no-reply",
    "webmaster", "postmaster", "accounts", "billing", "hr",
    "marketing", "feedback", "business", "career", "careers",
    "jobs", "pr", "press", "media", "news",
}

# ---------------------------------------------------------------------------
# Senior designation keywords — triggers escalation (#38)
# ---------------------------------------------------------------------------
_SENIOR_TITLES = {
    "ceo", "chief executive", "founder", "co-founder", "managing director",
    "md", "chairman", "chairperson", "president", "owner", "proprietor",
    "partner", "director general", "executive director",
}


def classify_bounce(subject: str, body: str) -> dict:
    """Classify an inbound email as hard_bounce, soft_bounce, job_change, or normal.

    Returns:
        {
          "type": "hard_bounce" | "soft_bounce" | "job_change" | "normal",
          "new_contact_name": str | None,
          "new_contact_email": str | None,
        }
    """
    text = (subject + " " + body).lower()

    for pat in _HARD_PATTERNS:
        if re.search(pat, text, re.IGNORECASE):
            logger.info("bounce_classified_hard", snippet=text[:80])
            return {"type": "hard_bounce", "new_contact_name": None, "new_contact_email": None}

    for pat in _SOFT_PATTERNS:
        if re.search(pat, text, re.IGNORECASE):
            logger.info("bounce_classified_soft", snippet=text[:80])
            return {"type": "soft_bounce", "new_contact_name": None, "new_contact_email": None}

    for pat in _JOB_CHANGE_PATTERNS:
        if re.search(pat, text, re.IGNORECASE):
            new_name, new_email = _extract_new_contact(body)
            logger.info("bounce_classified_job_change", new_name=new_name, new_email=new_email)
            return {"type": "job_change", "new_contact_name": new_name, "new_contact_email": new_email}

    return {"type": "normal", "new_contact_name": None, "new_contact_email": None}


def _extract_new_contact(body: str) -> tuple[str | None, str | None]:
    """Extract new contact name + email from a job-change auto-reply."""
    email_pattern = r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b"
    emails = re.findall(email_pattern, body)
    new_email = emails[0] if emails else None

    name_pattern = (
        r"(?:contact|reach\s+out\s+to|please\s+email|speak\s+to|forwarded\s+to)"
        r"\s+([A-Z][a-z]+(?:\s+[A-Z][a-z]+)?)"
    )
    m = re.search(name_pattern, body, re.IGNORECASE)
    new_name = m.group(1).strip() if m else None

    return new_name, new_email


def is_shared_inbox(email: str) -> bool:
    """Return True if the email address looks like a shared/generic inbox."""
    local = email.split("@")[0].lower().split("+")[0].replace(".", "").replace("-", "")
    return local in _SHARED_PREFIXES or email.split("@")[0].lower() in _SHARED_PREFIXES


def is_senior_lead(designation: str | None) -> bool:
    """Return True if the designation indicates a C-suite / senior decision maker."""
    if not designation:
        return False
    d = designation.lower()
    return any(title in d for title in _SENIOR_TITLES)


def soft_bounce_retry_delay(soft_bounce_count: int) -> int:
    """Return seconds to wait before retrying after a soft bounce.

    1st soft bounce → 6 hours
    2nd soft bounce → 24 hours
    3rd soft bounce → 72 hours (then treat as hard bounce)
    """
    delays = {1: 6 * 3600, 2: 24 * 3600, 3: 72 * 3600}
    return delays.get(soft_bounce_count, 72 * 3600)
