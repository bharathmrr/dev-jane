"""
Pipeline event logger.

Writes clean, human-readable events to logs/pipeline.log.
Each line: timestamp | [EVENT_TYPE] | company | email | detail

Also writes a checklist log to logs/pipeline_checklist.log that shows
only completed stages — one clean line per milestone, no noise.

Usage:
    from app.core.pipeline_logger import log_pipeline
    log_pipeline("KYC_AUTO_APPROVED", company="TechCorp", email="ceo@tech.com", detail="GSTIN confirmed")
"""
from __future__ import annotations

import datetime
import logging
import os
from zoneinfo import ZoneInfo

_IST = ZoneInfo("Asia/Kolkata")
_LOG_DIR = "logs"

# Status category for each event type
EVENT_STATUS: dict[str, str] = {
    "ONBOARDING_STARTED":        "info",
    "KYC_FORM_SENT":             "info",
    "KYC_SUBMITTED":             "info",
    "KYC_AUTO_APPROVED":         "ok",
    "KYC_MANUAL_REVIEW":         "warn",
    "KYC_ISSUES_FOUND":          "warn",
    "KYC_MANUALLY_APPROVED":     "ok",
    "KYC_REJECTED":              "error",
    "KYC_REMINDER_SENT":         "info",
    "NDA_READY":                 "info",
    "NDA_GENERATED":             "info",
    "NDA_SENT":                  "ok",
    "NDA_SIGNED_RECEIVED":       "ok",
    "NDA_SIGN_REJECTED":         "warn",
    "NDA_APPROVED":              "ok",
    "NDA_REMINDER_SENT":         "info",
    "AGREEMENT_READY":           "info",
    "AGREEMENT_GENERATED":       "info",
    "AGREEMENT_SENT":            "ok",
    "AGREEMENT_SIGNED_RECEIVED": "ok",
    "AGREEMENT_SIGN_REJECTED":   "warn",
    "AGREEMENT_APPROVED":        "ok",
    "AGREEMENT_REMINDER_SENT":   "info",
    "ONBOARDING_COMPLETE":       "ok",
    "EMAIL_SENT":                "info",
    "EMAIL_REMINDER":            "info",
    "BOOKING_COMPLETED":         "ok",
    "ERROR":                     "error",
}

# Human-readable label for each "ok" stage shown in the checklist
_STAGE_LABEL: dict[str, str] = {
    "BOOKING_COMPLETED":         "Meeting Booked",
    "KYC_AUTO_APPROVED":         "KYC Approved (auto)",
    "KYC_MANUALLY_APPROVED":     "KYC Approved (manual)",
    "NDA_SENT":                  "NDA Sent to Lead",
    "NDA_SIGNED_RECEIVED":       "NDA Signed by Lead",
    "NDA_APPROVED":              "NDA Approved by Team",
    "AGREEMENT_SENT":            "Agreement Sent to Lead",
    "AGREEMENT_SIGNED_RECEIVED": "Agreement Signed by Lead",
    "AGREEMENT_APPROVED":        "Agreement Approved by Team",
    "ONBOARDING_COMPLETE":       "Onboarding Complete",
}


def _get_logger() -> logging.Logger:
    logger = logging.getLogger("pipeline_events")
    if not logger.handlers:
        os.makedirs(_LOG_DIR, exist_ok=True)
        handler = logging.FileHandler(f"{_LOG_DIR}/pipeline.log", encoding="utf-8")
        handler.setFormatter(logging.Formatter("%(message)s"))
        logger.addHandler(handler)
        logger.setLevel(logging.INFO)
        logger.propagate = False
    return logger


def _get_checklist_logger() -> logging.Logger:
    logger = logging.getLogger("pipeline_checklist")
    if not logger.handlers:
        os.makedirs(_LOG_DIR, exist_ok=True)
        handler = logging.FileHandler(f"{_LOG_DIR}/pipeline_checklist.log", encoding="utf-8")
        handler.setFormatter(logging.Formatter("%(message)s"))
        logger.addHandler(handler)
        logger.setLevel(logging.INFO)
        logger.propagate = False
    return logger


def log_pipeline(
    event_type: str,
    company: str = "",
    email: str = "",
    detail: str = "",
) -> None:
    """Write one structured event line to logs/pipeline.log.
    Also writes to pipeline_checklist.log for completed stages.
    Thread-safe — Python's logging module serialises writes.
    """
    ts = datetime.datetime.now(_IST).strftime("%Y-%m-%d %H:%M:%S IST")
    company_col = (company or "—")[:42]
    email_col = (email or "—")[:46]
    event_col = f"[{event_type}]"
    line = f"{ts}  {event_col:<32}  {company_col:<42}  {email_col:<46}  {detail}"
    _get_logger().info(line)

    # Write to checklist log for completed milestones only
    if EVENT_STATUS.get(event_type) == "ok" and event_type in _STAGE_LABEL:
        ts_short = datetime.datetime.now(_IST).strftime("%Y-%m-%d %H:%M IST")
        label = _STAGE_LABEL[event_type]
        co = (company or "—")[:40]
        checklist_line = f"[✓] {ts_short}  {label:<35}  {co}"
        _get_checklist_logger().info(checklist_line)
