"""Customer Onboarding Pipeline API endpoints.

Routes:
  POST /onboarding/start/{lead_id}                   — team initiates onboarding
  GET  /onboarding/list                              — all onboarding records
  GET  /onboarding/{onboarding_id}                   — single record detail
  GET  /onboarding/kyc/form/{onboarding_id}/{token}  — lead fills KYC form (public, HMAC-signed)
  POST /onboarding/kyc/submit/{onboarding_id}/{token}— lead submits KYC form
  POST /onboarding/kyc/review/{onboarding_id}        — team approve/reject KYC
  GET  /onboarding/nda/preview/{onboarding_id}       — team views NDA draft HTML
  POST /onboarding/nda/draft-review/{onboarding_id}  — team approve/reject NDA draft → emails lead
  POST /onboarding/nda/sign-review/{onboarding_id}   — team marks NDA acknowledged → triggers Agreement
  GET  /onboarding/agreement/preview/{onboarding_id} — team views Agreement draft HTML
  POST /onboarding/agreement/draft-review/{onboarding_id} — team approve/reject Agreement → emails lead
  POST /onboarding/agreement/sign-review/{onboarding_id}  — team marks Agreement acknowledged → complete
  POST /onboarding/import-csv                             — bulk import leads from CSV (email,company_name,summary,contact_name,phone)
"""
from __future__ import annotations

import datetime as dt
import hashlib
import hmac as _hmac
import time
import uuid
from typing import Annotated
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import HTMLResponse, JSONResponse
from pydantic import BaseModel
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.logging import get_logger

logger = get_logger("onboarding_endpoints")
from app.db.base import CompanyType, DocumentStatus, KYCStatus
from app.db.models import KYCSubmission, LeadV2, OnboardingRecord
from app.db.session import get_db
from app.services.onboarding_email import (
    make_action_url,
    make_kyc_token,
    make_kyc_url,
    verify_kyc_token,
)

_IST = ZoneInfo("Asia/Kolkata")

router = APIRouter(prefix="/onboarding", tags=["onboarding"])


def _now_ist() -> dt.datetime:
    return dt.datetime.now(_IST)


def _fmt(d: dt.datetime | None) -> str:
    if not d:
        return ""
    return d.astimezone(_IST).strftime("%d %b %Y %H:%M IST")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

async def _get_onboarding(db: AsyncSession, onboarding_id: str) -> OnboardingRecord:
    rec = await db.get(OnboardingRecord, uuid.UUID(onboarding_id))
    if not rec:
        raise HTTPException(404, "Onboarding record not found")
    return rec


async def _get_lead(db: AsyncSession, lead_id: str) -> LeadV2:
    lead = await db.get(LeadV2, uuid.UUID(lead_id))
    if not lead:
        raise HTTPException(404, "Lead not found")
    return lead


def _stage_score(rec: OnboardingRecord) -> int:
    """0 (not started) → 11 (fully complete). Used for ranking."""
    a = rec.agreement_status
    if a in (DocumentStatus.APPROVED, DocumentStatus.PROCEED_NEXT):
        return 11
    if a in (DocumentStatus.SIGN_UNDER_REVIEW, DocumentStatus.SIGNED_RECEIVED, DocumentStatus.SIGN_REJECTED):
        return 10
    if a == DocumentStatus.SENT_TO_LEAD:
        return 9
    if a in (DocumentStatus.TEAM_REVIEW, DocumentStatus.DRAFT_GENERATED, DocumentStatus.DRAFT_REJECTED):
        return 8
    n = rec.nda_status
    if n == DocumentStatus.APPROVED:
        return 7
    if n in (DocumentStatus.SIGN_UNDER_REVIEW, DocumentStatus.SIGNED_RECEIVED, DocumentStatus.SIGN_REJECTED):
        return 6
    if n == DocumentStatus.SENT_TO_LEAD:
        return 5
    if n in (DocumentStatus.TEAM_REVIEW, DocumentStatus.DRAFT_GENERATED, DocumentStatus.DRAFT_REJECTED):
        return 4
    k = rec.kyc_status
    if k == KYCStatus.APPROVED:
        return 3
    if k in (KYCStatus.SUBMITTED, KYCStatus.UNDER_REVIEW, KYCStatus.REJECTED):
        return 2
    if k == KYCStatus.FORM_SENT:
        return 1
    return 0


def _pending_action(rec: OnboardingRecord) -> dict:
    """Returns what the team needs to do next for this record."""
    k, n, a = rec.kyc_status, rec.nda_status, rec.agreement_status
    if k in (KYCStatus.UNDER_REVIEW, KYCStatus.SUBMITTED):
        return {"type": "kyc_review", "label": "Review KYC Submission"}
    if k == KYCStatus.APPROVED and n in (DocumentStatus.TEAM_REVIEW, DocumentStatus.DRAFT_GENERATED, DocumentStatus.DRAFT_REJECTED):
        return {"type": "nda_draft_review", "label": "Review NDA Draft"}
    if n in (DocumentStatus.SIGN_UNDER_REVIEW, DocumentStatus.SIGNED_RECEIVED):
        return {"type": "nda_sign_review", "label": "Review Signed NDA"}
    if n == DocumentStatus.APPROVED and a in (DocumentStatus.TEAM_REVIEW, DocumentStatus.DRAFT_GENERATED, DocumentStatus.DRAFT_REJECTED):
        return {"type": "agreement_draft_review", "label": "Review Agreement Draft"}
    if a in (DocumentStatus.SIGN_UNDER_REVIEW, DocumentStatus.SIGNED_RECEIVED):
        return {"type": "agreement_sign_review", "label": "Review Signed Agreement"}
    if a in (DocumentStatus.APPROVED, DocumentStatus.PROCEED_NEXT):
        return {"type": "complete", "label": "Onboarding Complete"}
    return {"type": "waiting", "label": "Waiting for lead response"}


def _serialize(rec: OnboardingRecord) -> dict:
    return {
        "id": str(rec.id),
        "lead_id": str(rec.lead_id),
        "company_type": rec.company_type,
        "kyc_status": rec.kyc_status,
        "kyc_status_display": rec.kyc_status_display,
        "kyc_followup_count": rec.kyc_followup_count,
        "nda_status": rec.nda_status,
        "nda_status_display": rec.nda_status_display,
        "nda_followup_count": rec.nda_followup_count,
        "agreement_status": rec.agreement_status,
        "agreement_status_display": rec.agreement_status_display,
        "agreement_followup_count": rec.agreement_followup_count,
        "stage_score": _stage_score(rec),
        "pending_action": _pending_action(rec),
        "created_at": rec.created_at.isoformat() if rec.created_at else None,
    }


# ---------------------------------------------------------------------------
# Action link — one-click approve/reject via HMAC-signed email links
# ---------------------------------------------------------------------------

def _verify_action_token(record_id: str, action: str, expires: int, token: str) -> bool:
    if int(time.time()) > expires:
        return False
    msg = f"{record_id}:{action}:{expires}".encode()
    expected = _hmac.new(settings.ONBOARDING_HMAC_SECRET.encode(), msg, hashlib.sha256).hexdigest()
    return _hmac.compare_digest(expected, token)


def _action_page(title: str, message: str, ok: bool = True) -> str:
    color = "#16a34a" if ok else "#dc2626"
    icon = "✅" if ok else "❌"
    return f"""<!DOCTYPE html><html><head><meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{title}</title></head>
<body style="font-family:Arial,sans-serif;background:#f5f7fb;display:flex;
  align-items:center;justify-content:center;min-height:100vh;margin:0;">
<div style="background:#fff;border-radius:12px;padding:40px 48px;max-width:480px;
  text-align:center;box-shadow:0 2px 16px rgba(0,0,0,.1);">
  <div style="font-size:52px;margin-bottom:16px;">{icon}</div>
  <h2 style="color:{color};margin:0 0 12px;">{title}</h2>
  <p style="color:#555;font-size:15px;margin:0;">{message}</p>
  <p style="color:#aaa;font-size:12px;margin-top:24px;">Jane Aerospace Onboarding System</p>
</div></body></html>"""


@router.get("/action", response_class=HTMLResponse, include_in_schema=False)
async def action_link(
    id: str,
    action: str,
    token: str,
    expires: int,
    db: AsyncSession = Depends(get_db),
):
    """One-click HMAC-signed action link used in team notification emails."""
    if not _verify_action_token(id, action, expires, token):
        return HTMLResponse(_action_page("Link Invalid or Expired",
            "This link has expired or is invalid. Please use the Zoho CRM widget to take action.", ok=False))

    now = _now_ist()

    try:
        # ── start_onboarding: id = lead_id ──────────────────────────────────
        if action == "start_onboarding":
            lead = await db.get(LeadV2, uuid.UUID(id))
            if not lead:
                return HTMLResponse(_action_page("Not Found", "Lead not found.", ok=False))
            existing = (await db.execute(
                select(OnboardingRecord).where(OnboardingRecord.lead_id == lead.id)
            )).scalar_one_or_none()
            if existing:
                return HTMLResponse(_action_page("Already Started",
                    f"Onboarding for {lead.business_name} is already in progress."))
            from app.workers.onboarding_tasks import initiate_onboarding_task
            initiate_onboarding_task.delay(str(lead.id))
            from app.workers.onboarding_tasks import log_pipeline
            log_pipeline("ONBOARDING_STARTED_EMAIL_LINK", company=lead.business_name, email=lead.email,
                         detail="Started via email action link")
            return HTMLResponse(_action_page("Onboarding Started",
                f"Onboarding has been initiated for {lead.business_name}. "
                f"KYC form will be sent to {lead.email} shortly."))

        # ── all other actions: id = onboarding_id ───────────────────────────
        rec = await db.get(OnboardingRecord, uuid.UUID(id))
        if not rec:
            return HTMLResponse(_action_page("Not Found", "Onboarding record not found.", ok=False))
        lead = await db.get(LeadV2, rec.lead_id)
        if not lead:
            return HTMLResponse(_action_page("Not Found", "Lead not found.", ok=False))

        if action == "approve_kyc":
            if rec.kyc_status not in (KYCStatus.SUBMITTED, KYCStatus.UNDER_REVIEW):
                return HTMLResponse(_action_page("Already Processed",
                    f"KYC is already at status: {rec.kyc_status}"))
            rec.kyc_status = KYCStatus.APPROVED
            rec.kyc_approved_at = now
            rec.kyc_status_display = f"KYC Approved ✓ via email link ({_fmt(now)})"
            await db.commit()
            from app.services.onboarding_email import send_kyc_approved_email
            send_kyc_approved_email(lead.email, lead.contact_name or "", lead.business_name)
            from app.workers.onboarding_tasks import generate_nda_draft_task, export_onboarding_to_sheets
            generate_nda_draft_task.delay(id)
            export_onboarding_to_sheets.delay(id)
            from app.services.zoho_crm import sync_onboarding_stage
            sync_onboarding_stage(email=lead.email, contact_name=lead.contact_name or lead.business_name,
                company_name=lead.business_name, stage="KYC Manually Approved",
                company_type=rec.company_type or "", onboarding_id=id)
            return HTMLResponse(_action_page("KYC Approved",
                f"KYC for {lead.business_name} approved. NDA draft generation triggered."))

        elif action == "reject_kyc":
            rec.kyc_status = KYCStatus.REJECTED
            rec.kyc_followup_count = (rec.kyc_followup_count or 0) + 1
            rec.kyc_status_display = f"KYC Rejected via email link ({_fmt(now)})"
            await db.commit()
            from app.workers.onboarding_tasks import send_kyc_rejection_task, export_onboarding_to_sheets
            send_kyc_rejection_task.delay(id, "Rejected by team.", 1)
            export_onboarding_to_sheets.delay(id)
            from app.services.zoho_crm import sync_onboarding_stage
            sync_onboarding_stage(email=lead.email, contact_name=lead.contact_name or lead.business_name,
                company_name=lead.business_name, stage="KYC Rejected",
                company_type=rec.company_type or "", onboarding_id=id)
            return HTMLResponse(_action_page("KYC Rejected",
                f"KYC for {lead.business_name} rejected. Lead will receive a rejection email."))

        elif action == "approve_nda_draft":
            if rec.nda_status not in (DocumentStatus.TEAM_REVIEW, DocumentStatus.DRAFT_GENERATED,
                                       DocumentStatus.DRAFT_REJECTED):
                return HTMLResponse(_action_page("Already Processed",
                    f"NDA draft is already at status: {rec.nda_status}"))
            rec.nda_status = DocumentStatus.SENT_TO_LEAD
            rec.nda_sent_at = now
            rec.nda_status_display = f"NDA Sent to Lead via email link ({_fmt(now)})"
            await db.commit()
            from app.workers.onboarding_tasks import send_nda_to_lead_task, export_onboarding_to_sheets
            send_nda_to_lead_task.delay(id)
            export_onboarding_to_sheets.delay(id)
            return HTMLResponse(_action_page("NDA Sent",
                f"NDA approved and sent to {lead.business_name} for e-signature."))

        elif action == "approve_nda_sign":
            if rec.nda_status not in (DocumentStatus.SIGN_UNDER_REVIEW, DocumentStatus.SIGNED_RECEIVED):
                return HTMLResponse(_action_page("Already Processed",
                    f"NDA signature is already at status: {rec.nda_status}"))
            rec.nda_status = DocumentStatus.APPROVED
            rec.nda_approved_at = now
            rec.nda_status_display = f"NDA Signed & Approved ✓ via email link ({_fmt(now)})"
            await db.commit()
            from app.services.onboarding_email import send_nda_approved_email
            send_nda_approved_email(lead.email, lead.contact_name or "", lead.business_name)
            from app.workers.onboarding_tasks import generate_agreement_draft_task, export_onboarding_to_sheets
            generate_agreement_draft_task.delay(id)
            export_onboarding_to_sheets.delay(id)
            from app.services.zoho_crm import sync_onboarding_stage
            sync_onboarding_stage(email=lead.email, contact_name=lead.contact_name or lead.business_name,
                company_name=lead.business_name, stage="NDA Approved",
                company_type=rec.company_type or "", onboarding_id=id)
            return HTMLResponse(_action_page("NDA Approved",
                f"Signed NDA approved. Customer Agreement generation triggered for {lead.business_name}."))

        elif action == "reject_nda_sign":
            rec.nda_status = DocumentStatus.SIGN_REJECTED
            rec.nda_followup_count = (rec.nda_followup_count or 0) + 1
            rec.nda_status_display = f"NDA Signature Rejected via email link ({_fmt(now)})"
            await db.commit()
            from app.workers.onboarding_tasks import send_nda_sign_rejection_task, export_onboarding_to_sheets
            send_nda_sign_rejection_task.delay(id, "Signature rejected by team.")
            export_onboarding_to_sheets.delay(id)
            return HTMLResponse(_action_page("NDA Signature Rejected",
                f"Lead {lead.business_name} will be notified to re-sign."))

        elif action == "approve_agreement_draft":
            if rec.agreement_status not in (DocumentStatus.TEAM_REVIEW, DocumentStatus.DRAFT_GENERATED,
                                             DocumentStatus.DRAFT_REJECTED):
                return HTMLResponse(_action_page("Already Processed",
                    f"Agreement draft is already at status: {rec.agreement_status}"))
            rec.agreement_status = DocumentStatus.SENT_TO_LEAD
            rec.agreement_sent_at = now
            rec.agreement_status_display = f"Agreement Sent to Lead via email link ({_fmt(now)})"
            await db.commit()
            from app.workers.onboarding_tasks import send_agreement_to_lead_task, export_onboarding_to_sheets
            send_agreement_to_lead_task.delay(id)
            export_onboarding_to_sheets.delay(id)
            return HTMLResponse(_action_page("Agreement Sent",
                f"Agreement approved and sent to {lead.business_name} for e-signature."))

        elif action == "approve_agreement_sign":
            if rec.agreement_status not in (DocumentStatus.SIGN_UNDER_REVIEW, DocumentStatus.SIGNED_RECEIVED):
                return HTMLResponse(_action_page("Already Processed",
                    f"Agreement signature is already at status: {rec.agreement_status}"))
            rec.agreement_status = DocumentStatus.PROCEED_NEXT
            rec.agreement_approved_at = now
            rec.agreement_status_display = f"Agreement Approved ✓ — Onboarding Complete ({_fmt(now)})"
            await db.commit()
            from app.services.onboarding_email import send_agreement_approved_email
            send_agreement_approved_email(lead.email, lead.contact_name or "", lead.business_name)
            from app.workers.onboarding_tasks import export_onboarding_to_sheets
            export_onboarding_to_sheets.delay(id)
            from app.services.zoho_crm import sync_onboarding_stage
            sync_onboarding_stage(email=lead.email, contact_name=lead.contact_name or lead.business_name,
                company_name=lead.business_name, stage="Onboarding Complete",
                company_type=rec.company_type or "", onboarding_id=id)
            return HTMLResponse(_action_page("Onboarding Complete! 🎉",
                f"{lead.business_name} onboarding is fully complete. Welcome email sent to lead."))

        elif action == "reject_agreement_sign":
            rec.agreement_status = DocumentStatus.SIGN_REJECTED
            rec.agreement_followup_count = (rec.agreement_followup_count or 0) + 1
            rec.agreement_status_display = f"Agreement Signature Rejected via email link ({_fmt(now)})"
            await db.commit()
            from app.workers.onboarding_tasks import send_agreement_sign_rejection_task, export_onboarding_to_sheets
            send_agreement_sign_rejection_task.delay(id, "Signature rejected by team.")
            export_onboarding_to_sheets.delay(id)
            return HTMLResponse(_action_page("Agreement Signature Rejected",
                f"Lead {lead.business_name} will be notified to re-sign."))

        return HTMLResponse(_action_page("Unknown Action", f"Action '{action}' is not recognised.", ok=False))

    except Exception as exc:
        return HTMLResponse(_action_page("Error", f"An error occurred: {exc}", ok=False))


# ---------------------------------------------------------------------------
# Pipeline summary — for Zoho CRM widget and Zoho Analytics
# ---------------------------------------------------------------------------

async def _pipeline_stats(db: AsyncSession) -> dict:
    recs_result = await db.execute(select(OnboardingRecord))
    recs = recs_result.scalars().all()

    total = len(recs)
    cutoff_48h = dt.datetime.now(dt.timezone.utc) - dt.timedelta(hours=48)

    counts: dict[str, int] = {
        "kyc_pending": 0, "kyc_review": 0, "kyc_approved": 0, "kyc_rejected": 0,
        "nda_draft": 0, "nda_sent": 0, "nda_signed": 0, "nda_approved": 0,
        "agreement_draft": 0, "agreement_sent": 0, "agreement_signed": 0, "complete": 0,
        "indian": 0, "overseas": 0,
    }
    stuck: list[dict] = []
    recent_complete: list[dict] = []

    for r in recs:
        ct = (r.company_type or "").lower()
        if ct == "indian":
            counts["indian"] += 1
        elif ct == "overseas":
            counts["overseas"] += 1

        pa = _pending_action(r)
        if pa["type"] == "complete":
            counts["complete"] += 1
            if r.created_at and r.created_at.replace(tzinfo=dt.timezone.utc) > cutoff_48h:
                recent_complete.append({"id": str(r.id), "lead_id": str(r.lead_id)})
            continue

        ks, ns, ag = r.kyc_status, r.nda_status, r.agreement_status

        if ag in (DocumentStatus.SIGN_UNDER_REVIEW, DocumentStatus.SIGNED_RECEIVED):
            counts["agreement_signed"] += 1
        elif ag in (DocumentStatus.TEAM_REVIEW, DocumentStatus.DRAFT_GENERATED, DocumentStatus.DRAFT_REJECTED):
            counts["agreement_draft"] += 1
        elif ag == DocumentStatus.SENT_TO_LEAD:
            counts["agreement_sent"] += 1
        elif ns == DocumentStatus.APPROVED:
            counts["nda_approved"] += 1
        elif ns in (DocumentStatus.SIGN_UNDER_REVIEW, DocumentStatus.SIGNED_RECEIVED):
            counts["nda_signed"] += 1
        elif ns in (DocumentStatus.TEAM_REVIEW, DocumentStatus.DRAFT_GENERATED, DocumentStatus.DRAFT_REJECTED):
            counts["nda_draft"] += 1
        elif ns == DocumentStatus.SENT_TO_LEAD:
            counts["nda_sent"] += 1
        elif ks == KYCStatus.APPROVED:
            counts["kyc_approved"] += 1
        elif ks in (KYCStatus.SUBMITTED, KYCStatus.UNDER_REVIEW):
            counts["kyc_review"] += 1
        elif ks == KYCStatus.REJECTED:
            counts["kyc_rejected"] += 1
        else:
            counts["kyc_pending"] += 1

        # Stuck: same TEAM_REVIEW/REVIEW status for >48h (proxy: created_at)
        if pa["type"] not in ("waiting", "complete") and r.created_at:
            age = dt.datetime.now(dt.timezone.utc) - r.created_at.replace(tzinfo=dt.timezone.utc)
            if age.total_seconds() > 48 * 3600:
                stuck.append({
                    "id": str(r.id),
                    "lead_id": str(r.lead_id),
                    "pending": pa["label"],
                    "hours_waiting": int(age.total_seconds() // 3600),
                })

    return {"total": total, "counts": counts, "stuck": stuck, "recent_complete": recent_complete}


@router.get("/track", response_class=HTMLResponse, include_in_schema=False)
async def lead_track_page(db: AsyncSession = Depends(get_db)):
    """Lead tracking lookup — enter email, Ctrl+S to auto-fill all lead details."""
    html = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Lead Tracker — Jane Aerospace</title>
<style>
  *{box-sizing:border-box;margin:0;padding:0;}
  body{font-family:Arial,sans-serif;background:#f4f6fb;padding:24px;}
  .card{background:#fff;max-width:820px;margin:0 auto;border-radius:12px;
        box-shadow:0 2px 16px rgba(0,0,0,.1);padding:32px 36px;}
  h1{color:#1a3a6b;font-size:20px;margin-bottom:20px;}
  .logo{font-size:16px;font-weight:700;color:#1a3a6b;margin-bottom:16px;}
  .search-row{display:flex;gap:10px;align-items:center;margin-bottom:24px;}
  input[type=email]{flex:1;padding:10px 14px;border:2px solid #d1d5db;border-radius:7px;
    font-size:14px;outline:none;transition:border .2s;}
  input[type=email]:focus{border-color:#1a3a6b;}
  .btn{padding:10px 22px;background:#1a3a6b;color:#fff;border:none;border-radius:7px;
    font-size:14px;font-weight:600;cursor:pointer;white-space:nowrap;}
  .btn:hover{background:#163060;}
  .hint{font-size:11px;color:#9ca3af;margin-top:4px;}
  #result{display:none;}
  .sec{font-size:11px;font-weight:700;color:#fff;background:#1a3a6b;
    text-transform:uppercase;letter-spacing:.06em;padding:7px 12px;border-radius:5px;
    margin:20px 0 10px;}
  .grid{display:grid;grid-template-columns:1fr 1fr;gap:0;border:1px solid #e5e7eb;border-radius:6px;overflow:hidden;margin-bottom:4px;}
  .grid-3{grid-template-columns:1fr 1fr 1fr;}
  .field{padding:9px 14px;border-bottom:1px solid #f0f2f8;}
  .field:nth-child(odd){background:#f8fafc;}
  .field label{display:block;font-size:11px;color:#6b7280;font-weight:600;margin-bottom:2px;text-transform:uppercase;}
  .field span{font-size:13px;color:#111;font-family:monospace;}
  .badge{display:inline-block;padding:3px 10px;border-radius:99px;font-size:11px;font-weight:700;}
  .badge-new{background:#f3f4f6;color:#374151;}
  .badge-sent{background:#dbeafe;color:#1e40af;}
  .badge-replied{background:#d1fae5;color:#065f46;}
  .badge-booked{background:#bbf7d0;color:#14532d;}
  .badge-kyc{background:#fef3c7;color:#92400e;}
  .badge-nda{background:#ede9fe;color:#5b21b6;}
  .badge-ok{background:#d1fae5;color:#065f46;}
  .badge-pending{background:#fef3c7;color:#92400e;}
  .badge-rejected{background:#fee2e2;color:#991b1b;}
  #err{display:none;background:#fee2e2;color:#991b1b;padding:10px 14px;border-radius:7px;
    font-size:13px;margin-bottom:16px;}
  #loading{display:none;color:#6b7280;font-size:13px;margin-bottom:12px;}
</style>
</head>
<body>
<div class="card">
  <div class="logo">✈ Jane Aerospace</div>
  <h1>Lead Tracker</h1>

  <div class="search-row">
    <input type="email" id="emailInput" placeholder="Enter lead email address…" autocomplete="off">
    <button class="btn" onclick="lookupLead()">Look Up</button>
  </div>
  <p class="hint">Type the lead email and press <strong>Enter</strong> or click Look Up</p>

  <div id="err"></div>
  <div id="loading">Looking up lead…</div>

  <div id="result">
    <!-- Lead Info -->
    <div class="sec">Lead Information</div>
    <div class="grid">
      <div class="field"><label>Email</label><span id="f-email">—</span></div>
      <div class="field"><label>Company</label><span id="f-company">—</span></div>
      <div class="field"><label>Contact Name</label><span id="f-contact">—</span></div>
      <div class="field"><label>Phone</label><span id="f-phone">—</span></div>
      <div class="field"><label>Lead Status</label><span id="f-status">—</span></div>
      <div class="field"><label>Summary</label><span id="f-summary">—</span></div>
      <div class="field"><label>Sent At</label><span id="f-sent">—</span></div>
      <div class="field"><label>Replied At</label><span id="f-replied">—</span></div>
      <div class="field"><label>Booked At</label><span id="f-booked">—</span></div>
      <div class="field"><label>Onboarding Started</label><span id="f-ob-started">—</span></div>
    </div>

    <!-- Onboarding Pipeline -->
    <div id="ob-section" style="display:none;">
      <div class="sec">Onboarding Pipeline</div>
      <div class="grid">
        <div class="field"><label>Overall Stage</label><span id="f-stage">—</span></div>
        <div class="field"><label>Company Type</label><span id="f-ctype">—</span></div>
        <div class="field"><label>KYC Status</label><span id="f-kyc">—</span></div>
        <div class="field"><label>KYC Submitted At</label><span id="f-kyc-sub">—</span></div>
        <div class="field"><label>KYC Approved At</label><span id="f-kyc-app">—</span></div>
        <div class="field"><label>NDA Status</label><span id="f-nda">—</span></div>
        <div class="field"><label>NDA Sent At</label><span id="f-nda-sent">—</span></div>
        <div class="field"><label>Agreement Status</label><span id="f-agr">—</span></div>
        <div class="field"><label>Agreement Sent At</label><span id="f-agr-sent">—</span></div>
        <div class="field"><label>Onboarding ID</label><span id="f-ob-id">—</span></div>
      </div>

      <!-- KYC Submission Details -->
      <div id="kyc-sub-section" style="display:none;">
        <div class="sec">KYC Submission</div>
        <div class="grid grid-3">
          <div class="field"><label>Company Name (KYC)</label><span id="f-kyc-company">—</span></div>
          <div class="field"><label>Contact (KYC)</label><span id="f-kyc-contact">—</span></div>
          <div class="field"><label>Contact Number</label><span id="f-kyc-phone">—</span></div>
          <div class="field"><label>GSTIN</label><span id="f-gstin">—</span></div>
          <div class="field"><label>PAN</label><span id="f-pan">—</span></div>
          <div class="field"><label>CIN</label><span id="f-cin">—</span></div>
          <div class="field"><label>GST Certificate</label><span id="f-gst-file">—</span></div>
          <div class="field"><label>Incorporation Cert.</label><span id="f-inc-file">—</span></div>
          <div class="field"><label>KYC Attempt #</label><span id="f-kyc-att">—</span></div>
        </div>
      </div>
    </div>
  </div>
</div>

<script>
const BASE = '/api/v1/onboarding';

function badge(val, map) {
  if (!val) return '<span style="color:#9ca3af">—</span>';
  const cls = map[val.toLowerCase()] || 'badge-new';
  return `<span class="badge ${cls}">${val}</span>`;
}

function fmt(v) { return v || '—'; }

async function lookupLead() {
  const email = document.getElementById('emailInput').value.trim();
  if (!email) return;

  document.getElementById('err').style.display = 'none';
  document.getElementById('result').style.display = 'none';
  document.getElementById('loading').style.display = 'block';

  try {
    const r = await fetch(`${BASE}/status-by-email?email=${encodeURIComponent(email)}`);
    document.getElementById('loading').style.display = 'none';

    if (!r.ok) {
      const errBox = document.getElementById('err');
      errBox.textContent = r.status === 404 ? `No lead found for: ${email}` : `Error ${r.status}`;
      errBox.style.display = 'block';
      return;
    }

    const d = await r.json();

    // Lead info
    document.getElementById('f-email').textContent   = fmt(d.lead_email);
    document.getElementById('f-company').textContent = fmt(d.lead_business_name);
    document.getElementById('f-contact').textContent = fmt(d.lead_contact_name);
    document.getElementById('f-phone').textContent   = fmt(d.lead_phone_number || '—');
    document.getElementById('f-sent').textContent    = fmt(d.lead_sent_at);
    document.getElementById('f-replied').textContent = fmt(d.lead_replied_at);
    document.getElementById('f-booked').textContent  = fmt(d.lead_booked_at);
    document.getElementById('f-summary').textContent = fmt(d.lead_summary);
    document.getElementById('f-status').innerHTML    = badge(d.lead_status,{
      new:'badge-new',sent:'badge-sent',replied:'badge-replied',booked:'badge-booked'});
    document.getElementById('f-ob-started').innerHTML = d.onboarding_started
      ? '<span class="badge badge-ok">Yes</span>'
      : '<span class="badge badge-new">No</span>';

    // Onboarding
    if (d.onboarding_started) {
      document.getElementById('ob-section').style.display = 'block';
      document.getElementById('f-stage').textContent    = fmt(d.overall_stage || d.kyc_status_display);
      document.getElementById('f-ctype').textContent    = fmt(d.company_type);
      document.getElementById('f-kyc').innerHTML        = badge(d.kyc_status, {
        approved:'badge-ok',rejected:'badge-rejected',under_review:'badge-kyc',pending:'badge-pending'});
      document.getElementById('f-kyc-sub').textContent  = fmt(d.kyc_submitted_at);
      document.getElementById('f-kyc-app').textContent  = fmt(d.kyc_approved_at);
      document.getElementById('f-nda').innerHTML        = badge(d.nda_status, {
        approved:'badge-ok',sent_to_lead:'badge-nda',signed_received:'badge-nda',pending:'badge-pending'});
      document.getElementById('f-nda-sent').textContent = fmt(d.nda_sent_at);
      document.getElementById('f-agr').innerHTML        = badge(d.agreement_status, {
        approved:'badge-ok',sent_to_lead:'badge-nda',signed_received:'badge-nda',pending:'badge-pending'});
      document.getElementById('f-agr-sent').textContent = fmt(d.agreement_sent_at);
      document.getElementById('f-ob-id').textContent    = (d.id||'').substring(0,8)+'…';

      if (d.kyc_submission) {
        const k = d.kyc_submission;
        document.getElementById('kyc-sub-section').style.display = 'block';
        document.getElementById('f-kyc-company').textContent = fmt(k.company_name);
        document.getElementById('f-kyc-contact').textContent = fmt(k.contact_name);
        document.getElementById('f-kyc-phone').textContent   = fmt(k.contact_number);
        document.getElementById('f-gstin').textContent       = fmt(k.gstin_number);
        document.getElementById('f-pan').textContent         = fmt(k.pan_number);
        document.getElementById('f-cin').textContent         = fmt(k.cin_number);
        document.getElementById('f-gst-file').innerHTML      = k.has_gst ? '<span class="badge badge-ok">Uploaded</span>' : '—';
        document.getElementById('f-inc-file').innerHTML      = k.has_incorporation ? '<span class="badge badge-ok">Uploaded</span>' : '—';
        document.getElementById('f-kyc-att').textContent     = fmt(k.attempt_number);
      }
    }

    document.getElementById('result').style.display = 'block';

  } catch(e) {
    document.getElementById('loading').style.display = 'none';
    const errBox = document.getElementById('err');
    errBox.textContent = 'Network error — ' + e.message;
    errBox.style.display = 'block';
  }
}

// Trigger on Enter in the input
document.getElementById('emailInput').addEventListener('keydown', e => {
  if (e.key === 'Enter') lookupLead();
});
</script>
</body></html>"""
    return HTMLResponse(html)


@router.get("/pipeline-summary", response_class=HTMLResponse, include_in_schema=False)
async def pipeline_summary_html(db: AsyncSession = Depends(get_db)):
    stats = await _pipeline_stats(db)
    c = stats["counts"]
    stuck = stats["stuck"]

    def row(label: str, val: int, color: str = "#1a3a6b") -> str:
        return (f'<div style="display:flex;justify-content:space-between;padding:6px 0;'
                f'border-bottom:1px solid #f0f2f8;">'
                f'<span style="font-size:13px;color:#555;">{label}</span>'
                f'<span style="font-size:13px;font-weight:700;color:{color};">{val}</span></div>')

    stuck_rows = ""
    for s in stuck[:10]:
        stuck_rows += (f'<div style="padding:6px 0;border-bottom:1px solid #f0f2f8;font-size:12px;">'
                       f'<span style="color:#dc2626;font-weight:600;">{s["hours_waiting"]}h</span>'
                       f' — {s["pending"]}'
                       f' <span style="color:#aaa;font-size:10px;">ID:{s["id"][:8]}</span></div>')

    html = f"""<!DOCTYPE html><html><head><meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Onboarding Pipeline</title></head>
<body style="font-family:Arial,sans-serif;background:#f5f7fb;padding:16px;margin:0;">
<h2 style="font-size:16px;color:#1a3a6b;margin:0 0 14px;">Onboarding Pipeline — {stats["total"]} Active</h2>

<div style="display:flex;gap:10px;margin-bottom:14px;flex-wrap:wrap;">
  <div style="background:#fff;border-radius:8px;padding:12px 16px;flex:1;min-width:90px;text-align:center;box-shadow:0 1px 4px rgba(0,0,0,.07);">
    <div style="font-size:22px;font-weight:700;color:#1a3a6b;">{stats["total"]}</div>
    <div style="font-size:11px;color:#888;">Total</div></div>
  <div style="background:#fff;border-radius:8px;padding:12px 16px;flex:1;min-width:90px;text-align:center;box-shadow:0 1px 4px rgba(0,0,0,.07);">
    <div style="font-size:22px;font-weight:700;color:#dc2626;">{len(stuck)}</div>
    <div style="font-size:11px;color:#888;">Stuck &gt;48h</div></div>
  <div style="background:#fff;border-radius:8px;padding:12px 16px;flex:1;min-width:90px;text-align:center;box-shadow:0 1px 4px rgba(0,0,0,.07);">
    <div style="font-size:22px;font-weight:700;color:#16a34a;">{c["complete"]}</div>
    <div style="font-size:11px;color:#888;">Complete</div></div>
</div>

<div style="background:#fff;border-radius:8px;padding:14px;box-shadow:0 1px 4px rgba(0,0,0,.07);margin-bottom:12px;">
  <div style="font-size:12px;font-weight:700;color:#1a3a6b;margin-bottom:8px;">STAGE BREAKDOWN</div>
  {row("KYC Pending", c["kyc_pending"])}
  {row("KYC Under Review", c["kyc_review"], "#f59e0b")}
  {row("KYC Rejected", c["kyc_rejected"], "#dc2626")}
  {row("KYC Approved", c["kyc_approved"], "#16a34a")}
  {row("NDA Draft Ready", c["nda_draft"], "#f59e0b")}
  {row("NDA Sent to Lead", c["nda_sent"])}
  {row("NDA Signed (Pending Review)", c["nda_signed"], "#f59e0b")}
  {row("NDA Approved", c["nda_approved"], "#16a34a")}
  {row("Agreement Draft Ready", c["agreement_draft"], "#f59e0b")}
  {row("Agreement Sent to Lead", c["agreement_sent"])}
  {row("Agreement Signed (Pending Review)", c["agreement_signed"], "#f59e0b")}
  {row("Complete", c["complete"], "#16a34a")}
</div>

<div style="background:#fff;border-radius:8px;padding:14px;box-shadow:0 1px 4px rgba(0,0,0,.07);margin-bottom:12px;">
  <div style="font-size:12px;font-weight:700;color:#1a3a6b;margin-bottom:8px;">COMPANY TYPE</div>
  {row("🇮🇳 Indian", c["indian"])}
  {row("🌐 Overseas", c["overseas"])}
</div>

{"" if not stuck else f'''<div style="background:#fff;border-radius:8px;padding:14px;box-shadow:0 1px 4px rgba(0,0,0,.07);">
  <div style="font-size:12px;font-weight:700;color:#dc2626;margin-bottom:8px;">⚠ NEEDS ATTENTION (&gt;48h)</div>
  {stuck_rows}
</div>'''}

<p style="color:#aaa;font-size:10px;text-align:center;margin-top:12px;">
  Updated: {dt.datetime.now(_IST).strftime("%d %b %Y %H:%M IST")}</p>
</body></html>"""
    return HTMLResponse(html)


@router.get("/pipeline-summary.json")
async def pipeline_summary_json(db: AsyncSession = Depends(get_db)):
    return await _pipeline_stats(db)


# ---------------------------------------------------------------------------
# Initiate onboarding
# ---------------------------------------------------------------------------

@router.delete("/cancel/{lead_id}")
async def cancel_onboarding(lead_id: str, db: AsyncSession = Depends(get_db)):
    """Team cancels / removes onboarding for a lead."""
    rec = (await db.execute(
        select(OnboardingRecord).where(OnboardingRecord.lead_id == uuid.UUID(lead_id))
    )).scalar_one_or_none()
    if not rec:
        raise HTTPException(404, "No onboarding record found for this lead")
    await db.delete(rec)
    await db.commit()
    return {"message": "Onboarding cancelled and removed"}


@router.post("/start/{lead_id}")
async def start_onboarding(lead_id: str, db: AsyncSession = Depends(get_db)):
    lead = await _get_lead(db, lead_id)

    # Check if onboarding already exists
    existing = (await db.execute(
        select(OnboardingRecord).where(OnboardingRecord.lead_id == uuid.UUID(lead_id))
    )).scalar_one_or_none()
    if existing:
        return {"message": "Onboarding already started", "onboarding_id": str(existing.id)}

    # Create record immediately so the dashboard shows KYC status right away
    from app.services.onboarding_email import make_kyc_token, make_kyc_url
    now = _now_ist()
    rec = OnboardingRecord(
        lead_id=uuid.UUID(lead_id),
        company_type=CompanyType.INDIAN,  # default; task will detect and update
        kyc_status=KYCStatus.FORM_SENT,
        kyc_status_display=f"KYC Form Sending… ({_fmt(now)})",
        kyc_form_sent_at=now,
    )
    db.add(rec)
    await db.flush()

    token = make_kyc_token(str(rec.id))
    rec.kyc_form_token = token
    kyc_url = make_kyc_url(str(rec.id), token)
    rec.kyc_status_display = f"KYC Form Sent ({_fmt(now)})"
    await db.commit()

    # Send KYC email + AI company detection in background
    from app.workers.onboarding_tasks import send_kyc_email_task
    send_kyc_email_task.delay(lead.email, lead.contact_name or lead.business_name, lead.business_name or "", kyc_url)

    return {"message": "Onboarding started. KYC form is being sent to the lead.", "onboarding_id": str(rec.id)}


# ---------------------------------------------------------------------------
# CRM Button — start onboarding directly from Zoho CRM Lead/Contact record
# ---------------------------------------------------------------------------

class _CRMStartBody(BaseModel):
    email: str
    contact_name: str = ""
    company_name: str = ""
    zoho_crm_id: str = ""


@router.post("/crm-start")
async def crm_start_onboarding(body: _CRMStartBody, db: AsyncSession = Depends(get_db)):
    """Called by the Zoho CRM Deluge button on Lead/Contact detail pages.
    Looks up the lead by email, creates a minimal record if missing,
    then fires the onboarding pipeline. Returns JSON for the Deluge alert.
    """
    email = body.email.lower().strip()

    # Find lead by email
    result = await db.execute(select(LeadV2).where(LeadV2.email == email))
    lead = result.scalar_one_or_none()

    if not lead:
        # Create a minimal lead from CRM data so the pipeline can run
        lead = LeadV2(
            email=email,
            contact_name=body.contact_name or None,
            business_name=body.company_name or email.split("@")[0],
        )
        db.add(lead)
        await db.flush()
        await db.commit()
        await db.refresh(lead)

    # Idempotency — don't double-start
    existing = (await db.execute(
        select(OnboardingRecord).where(OnboardingRecord.lead_id == lead.id)
    )).scalar_one_or_none()
    if existing:
        return {
            "status": "already_started",
            "message": f"Onboarding for {lead.business_name} is already in progress.",
            "onboarding_id": str(existing.id),
        }

    from app.workers.onboarding_tasks import initiate_onboarding_task
    initiate_onboarding_task.delay(str(lead.id))

    from app.core.pipeline_logger import log_pipeline
    log_pipeline("ONBOARDING_STARTED", company=lead.business_name, email=lead.email,
                 detail=f"Started via Zoho CRM button (crm_id={body.zoho_crm_id})")

    return {
        "status": "ok",
        "message": f"Onboarding started for {lead.business_name}. KYC form will be sent to {email} shortly.",
    }


# ---------------------------------------------------------------------------
# List / detail
# ---------------------------------------------------------------------------

@router.get("/list")
async def list_onboarding(db: AsyncSession = Depends(get_db)):
    result = await db.execute(
        select(OnboardingRecord).order_by(OnboardingRecord.created_at.desc())
    )
    records = result.scalars().all()

    # Enrich with lead info
    out = []
    for rec in records:
        lead = await db.get(LeadV2, rec.lead_id)
        data = _serialize(rec)
        if lead:
            data["lead_email"] = lead.email
            data["lead_contact_name"] = lead.contact_name
            data["lead_business_name"] = lead.business_name
        out.append(data)
    return out


@router.get("/status-by-email")
async def status_by_email(email: str, db: AsyncSession = Depends(get_db)):
    """Zoho CRM widget uses this to look up onboarding status by lead email."""
    lead = (await db.execute(
        select(LeadV2).where(LeadV2.email == email)
    )).scalar_one_or_none()
    if not lead:
        raise HTTPException(404, "Lead not found in system")

    rec = (await db.execute(
        select(OnboardingRecord).where(OnboardingRecord.lead_id == lead.id)
    )).scalar_one_or_none()

    base = {
        "lead_id": str(lead.id),
        "lead_email": lead.email,
        "lead_contact_name": lead.contact_name,
        "lead_business_name": lead.business_name,
        "lead_status": lead.status.value if hasattr(lead.status, "value") else lead.status,
        "lead_phone_number": lead.phone_number or "",
        "lead_sent_at": _fmt(lead.sent_at),
        "lead_replied_at": _fmt(lead.replied_at),
        "lead_booked_at": _fmt(lead.booked_at),
        "lead_summary": lead.summary or "",
        "onboarding_started": rec is not None,
    }
    if not rec:
        return base

    data = _serialize(rec)
    data.update(base)

    # Add missing date/stage fields from the onboarding record
    data["kyc_submitted_at"] = _fmt(rec.kyc_submitted_at)
    data["kyc_approved_at"] = _fmt(rec.kyc_approved_at)
    data["nda_sent_at"] = _fmt(rec.nda_sent_at)
    data["agreement_sent_at"] = _fmt(rec.agreement_sent_at)
    data["overall_stage"] = _pending_action(rec)["label"]

    kyc_result = await db.execute(
        select(KYCSubmission)
        .where(KYCSubmission.onboarding_id == rec.id)
        .order_by(KYCSubmission.attempt_number.desc())
    )
    latest_kyc = kyc_result.scalars().first()
    if latest_kyc:
        data["kyc_submission"] = {
            "company_type": latest_kyc.company_type,
            "company_name": latest_kyc.company_name,
            "contact_name": latest_kyc.contact_name,
            "contact_number": latest_kyc.contact_number,
            "gstin_number": latest_kyc.gstin_number or "",
            "pan_number": latest_kyc.pan_number or "",
            "cin_number": latest_kyc.cin_number or "",
            "has_gst": bool(latest_kyc.gst_certificate_zoho_id),
            "has_incorporation": bool(latest_kyc.incorporation_zoho_id),
            "attempt_number": latest_kyc.attempt_number,
        }
    data["nda_draft_content"] = rec.nda_draft_content
    data["agreement_draft_content"] = rec.agreement_draft_content
    return data


@router.get("/{onboarding_id}")
async def get_onboarding(onboarding_id: str, db: AsyncSession = Depends(get_db)):
    rec = await _get_onboarding(db, onboarding_id)
    lead = await db.get(LeadV2, rec.lead_id)
    data = _serialize(rec)
    if lead:
        data["lead_email"] = lead.email
        data["lead_contact_name"] = lead.contact_name
        data["lead_business_name"] = lead.business_name

    # Latest KYC submission
    kyc_result = await db.execute(
        select(KYCSubmission)
        .where(KYCSubmission.onboarding_id == rec.id)
        .order_by(KYCSubmission.attempt_number.desc())
    )
    latest_kyc = kyc_result.scalars().first()
    if latest_kyc:
        _vr = latest_kyc.kyc_verification_result or {}
        _ef = _vr.get("extra_fields") or {}
        data["kyc_submission"] = {
            "company_type": latest_kyc.company_type,
            "company_name": latest_kyc.company_name,
            "contact_name": latest_kyc.contact_name,
            "contact_number": latest_kyc.contact_number,
            "gstin_number": latest_kyc.gstin_number,
            "pan_number": latest_kyc.pan_number,
            "cin_number": latest_kyc.cin_number,
            "attempt_number": latest_kyc.attempt_number,
            "auto_verified": latest_kyc.auto_verified,
            "has_gst": bool(latest_kyc.gst_certificate_zoho_id),
            "has_incorporation": bool(latest_kyc.incorporation_zoho_id),
            "reviewer_notes": latest_kyc.reviewer_notes,
            "verification_passed": _vr.get("overall_passed"),
            "verification_issues": _vr.get("issues", []),
            "extra_fields": _ef,
        }

    data["nda_draft_content"] = rec.nda_draft_content
    data["agreement_draft_content"] = rec.agreement_draft_content
    data["nda_team_notes"] = rec.nda_team_notes
    data["agreement_team_notes"] = rec.agreement_team_notes
    return data


# ---------------------------------------------------------------------------
# KYC file download proxy (team-facing, token-gated)
# ---------------------------------------------------------------------------

@router.get("/kyc/file/{onboarding_id}/{token}/{file_type}")
async def kyc_file_download(
    onboarding_id: str,
    token: str,
    file_type: str,          # "gst" or "incorporation"
    db: AsyncSession = Depends(get_db),
):
    """Stream a KYC attachment from Zoho CRM. Uses the same view token as kyc_view_page."""
    from app.services.onboarding_email import verify_kyc_view_token
    if not verify_kyc_view_token(onboarding_id, token):
        raise HTTPException(403, "Invalid or expired link")

    rec = await _get_onboarding(db, onboarding_id)
    kyc_result = await db.execute(
        select(KYCSubmission)
        .where(KYCSubmission.onboarding_id == rec.id)
        .order_by(KYCSubmission.attempt_number.desc())
    )
    kyc = kyc_result.scalars().first()
    if not kyc:
        raise HTTPException(404, "No KYC submission found")

    if file_type == "gst":
        att_id = kyc.gst_certificate_zoho_id
        filename = kyc.gst_certificate_filename or "gst_certificate"
    elif file_type == "incorporation":
        att_id = kyc.incorporation_zoho_id
        filename = kyc.incorporation_filename or "incorporation_certificate"
    else:
        raise HTTPException(400, "Invalid file_type. Use 'gst' or 'incorporation'")

    if not att_id:
        raise HTTPException(404, "File not uploaded to CRM yet")

    lead = await db.get(LeadV2, rec.lead_id)
    if not lead:
        raise HTTPException(404, "Lead not found")

    from app.services.zoho_crm import find_lead_by_email, download_lead_attachment
    crm_lead_id = find_lead_by_email(lead.email)
    if not crm_lead_id:
        raise HTTPException(404, "Lead not found in CRM")

    file_bytes = download_lead_attachment(crm_lead_id, att_id)
    if not file_bytes:
        raise HTTPException(502, "Could not retrieve file from CRM")

    import mimetypes
    mime = mimetypes.guess_type(filename)[0] or "application/octet-stream"
    from fastapi.responses import Response
    return Response(
        content=file_bytes,
        media_type=mime,
        headers={"Content-Disposition": f'inline; filename="{filename}"'},
    )


# ---------------------------------------------------------------------------
# KYC form (public — lead-facing, HMAC-signed)
# ---------------------------------------------------------------------------

@router.get("/kyc/form/{onboarding_id}/{token}", response_class=HTMLResponse)
async def kyc_form_page(
    onboarding_id: str,
    token: str,
    db: AsyncSession = Depends(get_db),
):
    if not verify_kyc_token(onboarding_id, token):
        raise HTTPException(403, "Invalid or expired link")

    rec = await _get_onboarding(db, onboarding_id)
    if rec.kyc_status == KYCStatus.APPROVED:
        return HTMLResponse(_kyc_already_done_page())

    lead = await db.get(LeadV2, rec.lead_id)
    company_name = lead.business_name if lead else ""
    contact_name = lead.contact_name if lead else ""

    # Pre-select company type if already known
    preselect = rec.company_type or ""

    submit_url = f"{settings.APP_URL.rstrip('/')}/api/v1/onboarding/kyc/submit/{onboarding_id}/{token}"
    return HTMLResponse(_kyc_form_html(submit_url, company_name, contact_name, preselect))




@router.post("/kyc/submit/{onboarding_id}/{token}", response_class=HTMLResponse)
async def kyc_form_submit(
    onboarding_id: str,
    token: str,
    request: Request,
    db: AsyncSession = Depends(get_db),
    company_type: str = Form(...),
    company_name: str = Form(...),
    contact_name: str = Form(...),
    contact_number: str = Form(...),
    gstin_number: str | None = Form(None),
    pan_number: str | None = Form(None),
    cin_number: str | None = Form(None),
    # Section A extras
    trade_name: str | None = Form(None),
    entity_type: str | None = Form(None),
    date_of_incorporation: str | None = Form(None),
    registered_address: str | None = Form(None),
    city: str | None = Form(None),
    pin_code: str | None = Form(None),
    state: str | None = Form(None),
    principal_business_address: str | None = Form(None),
    nature_of_business: str | None = Form(None),
    # Section B — signatories
    signatory1_designation: str | None = Form(None),
    signatory1_pan: str | None = Form(None),
    signatory1_aadhaar: str | None = Form(None),
    signatory2_name: str | None = Form(None),
    signatory2_designation: str | None = Form(None),
    signatory2_pan: str | None = Form(None),
    signatory2_aadhaar: str | None = Form(None),
    director_names: str | None = Form(None),
    ubo_name: str | None = Form(None),
    ubo_pan: str | None = Form(None),
    ubo_nationality: str | None = Form(None),
    pep_status: str | None = Form(None),
    # Section D — bank details
    bank_name: str | None = Form(None),
    account_number: str | None = Form(None),
    ifsc_code: str | None = Form(None),
    account_type: str | None = Form(None),
    bank_branch_address: str | None = Form(None),
    annual_turnover: str | None = Form(None),
    # Section D — overseas bank
    swift_code: str | None = Form(None),
    iban_number: str | None = Form(None),
    bank_country: str | None = Form(None),
    account_currency: str | None = Form(None),
    # Overseas A — identity
    country_of_incorporation: str | None = Form(None),
    company_reg_number: str | None = Form(None),
    country_of_tax_residence: str | None = Form(None),
    tax_id_tin: str | None = Form(None),
    lei_number: str | None = Form(None),
    vat_gst_number: str | None = Form(None),
    company_website: str | None = Form(None),
    primary_business_activity: str | None = Form(None),
    countries_of_operation: str | None = Form(None),
    country: str | None = Form(None),
    # Overseas B — signatory passport
    signatory1_nationality: str | None = Form(None),
    signatory1_dob: str | None = Form(None),
    signatory1_passport_id: str | None = Form(None),
    signatory1_country_of_residence: str | None = Form(None),
    signatory1_shareholding_pct: str | None = Form(None),
    # Overseas F — escalation contact
    escalation_contact_name: str | None = Form(None),
    escalation_contact_title: str | None = Form(None),
    escalation_contact_email: str | None = Form(None),
    escalation_contact_phone: str | None = Form(None),
    escalation_contact_dept: str | None = Form(None),
    escalation_contact_relationship: str | None = Form(None),
    # Overseas G — directors
    director1_name: str | None = Form(None),
    director1_nationality: str | None = Form(None),
    director1_dob: str | None = Form(None),
    director1_passport_id: str | None = Form(None),
    director1_country_of_residence: str | None = Form(None),
    director1_shareholding_pct: str | None = Form(None),
    director2_name: str | None = Form(None),
    director2_nationality: str | None = Form(None),
    director2_dob: str | None = Form(None),
    director2_passport_id: str | None = Form(None),
    director2_country_of_residence: str | None = Form(None),
    director2_shareholding_pct: str | None = Form(None),
    # Overseas H — compliance
    sanctions_check: str | None = Form(None),
    criminal_investigation_check: str | None = Form(None),
    regulated_licensed: str | None = Form(None),
    licensed_regulator: str | None = Form(None),
    # Declaration
    declaration_agreed: str | None = Form(None),
    gst_certificate: UploadFile | None = File(None),
    incorporation_certificate: UploadFile = File(...),
):
    if not verify_kyc_token(onboarding_id, token):
        raise HTTPException(403, "Invalid or expired link")

    rec = await _get_onboarding(db, onboarding_id)
    if rec.kyc_status == KYCStatus.APPROVED:
        return HTMLResponse(_kyc_already_done_page())

    # Validate company type
    if company_type not in ("indian", "overseas"):
        raise HTTPException(400, "Invalid company type")

    if company_type == "indian" and not gst_certificate:
        raise HTTPException(400, "GST certificate is required for Indian companies")

    gst_filename: str | None = gst_certificate.filename if gst_certificate else None
    inc_filename: str | None = incorporation_certificate.filename

    # Read file bytes now — UploadFile is unavailable after response is sent
    gst_bytes: bytes = await gst_certificate.read() if gst_certificate else b""
    inc_bytes: bytes = await incorporation_certificate.read()

    # Count existing submissions for attempt number
    existing_count = (await db.execute(
        select(KYCSubmission).where(KYCSubmission.onboarding_id == rec.id)
    )).scalars().all()
    attempt = len(existing_count) + 1

    def _clean(v: str | None) -> str | None:
        return (v or "").strip() or None

    extra_fields = {
        "trade_name": _clean(trade_name),
        "entity_type": _clean(entity_type),
        "date_of_incorporation": _clean(date_of_incorporation),
        "registered_address": _clean(registered_address),
        "city": _clean(city),
        "pin_code": _clean(pin_code),
        "state": _clean(state),
        "principal_business_address": _clean(principal_business_address),
        "nature_of_business": _clean(nature_of_business),
        "signatory1_designation": _clean(signatory1_designation),
        "signatory1_pan": (_clean(signatory1_pan) or "").upper() or None,
        "signatory1_aadhaar": _clean(signatory1_aadhaar),
        "signatory2_name": _clean(signatory2_name),
        "signatory2_designation": _clean(signatory2_designation),
        "signatory2_pan": (_clean(signatory2_pan) or "").upper() or None,
        "signatory2_aadhaar": _clean(signatory2_aadhaar),
        "director_names": _clean(director_names),
        "ubo_name": _clean(ubo_name),
        "ubo_pan": (_clean(ubo_pan) or "").upper() or None,
        "ubo_nationality": _clean(ubo_nationality),
        "pep_status": _clean(pep_status),
        "bank_name": _clean(bank_name),
        "account_number": _clean(account_number),
        "ifsc_code": (_clean(ifsc_code) or "").upper() or None,
        "account_type": _clean(account_type),
        "bank_branch_address": _clean(bank_branch_address),
        "annual_turnover": _clean(annual_turnover),
        # Overseas bank
        "swift_code": (_clean(swift_code) or "").upper() or None,
        "iban_number": _clean(iban_number),
        "bank_country": _clean(bank_country),
        "account_currency": _clean(account_currency),
        # Overseas identity
        "country_of_incorporation": _clean(country_of_incorporation),
        "company_reg_number": _clean(company_reg_number),
        "country_of_tax_residence": _clean(country_of_tax_residence),
        "tax_id_tin": _clean(tax_id_tin),
        "lei_number": _clean(lei_number),
        "vat_gst_number": _clean(vat_gst_number),
        "company_website": _clean(company_website),
        "primary_business_activity": _clean(primary_business_activity),
        "countries_of_operation": _clean(countries_of_operation),
        "country": _clean(country),
        # Overseas signatory passport
        "signatory1_nationality": _clean(signatory1_nationality),
        "signatory1_dob": _clean(signatory1_dob),
        "signatory1_passport_id": _clean(signatory1_passport_id),
        "signatory1_country_of_residence": _clean(signatory1_country_of_residence),
        "signatory1_shareholding_pct": _clean(signatory1_shareholding_pct),
        # Overseas escalation contact
        "escalation_contact_name": _clean(escalation_contact_name),
        "escalation_contact_title": _clean(escalation_contact_title),
        "escalation_contact_email": _clean(escalation_contact_email),
        "escalation_contact_phone": _clean(escalation_contact_phone),
        "escalation_contact_dept": _clean(escalation_contact_dept),
        "escalation_contact_relationship": _clean(escalation_contact_relationship),
        # Overseas directors
        "director1_name": _clean(director1_name),
        "director1_nationality": _clean(director1_nationality),
        "director1_dob": _clean(director1_dob),
        "director1_passport_id": _clean(director1_passport_id),
        "director1_country_of_residence": _clean(director1_country_of_residence),
        "director1_shareholding_pct": _clean(director1_shareholding_pct),
        "director2_name": _clean(director2_name),
        "director2_nationality": _clean(director2_nationality),
        "director2_dob": _clean(director2_dob),
        "director2_passport_id": _clean(director2_passport_id),
        "director2_country_of_residence": _clean(director2_country_of_residence),
        "director2_shareholding_pct": _clean(director2_shareholding_pct),
        # Overseas compliance
        "sanctions_check": _clean(sanctions_check),
        "criminal_investigation_check": _clean(criminal_investigation_check),
        "regulated_licensed": _clean(regulated_licensed),
        "licensed_regulator": _clean(licensed_regulator),
        "declaration_agreed": declaration_agreed == "yes",
    }

    submission = KYCSubmission(
        onboarding_id=rec.id,
        attempt_number=attempt,
        company_type=company_type,
        company_name=company_name,
        contact_name=contact_name,
        contact_number=contact_number,
        gstin_number=(gstin_number or "").upper().strip() or None,
        pan_number=(pan_number or "").upper().strip() or None,
        cin_number=(cin_number or "").upper().strip() or None,
        gst_certificate_zoho_id=None,   # set by background task after upload
        gst_certificate_filename=gst_filename,
        incorporation_zoho_id=None,
        incorporation_filename=inc_filename,
        kyc_verification_result={"extra_fields": extra_fields},
    )
    db.add(submission)
    await db.flush()  # get submission.id before commit
    submission_id = str(submission.id)

    now = _now_ist()
    rec.kyc_status = KYCStatus.UNDER_REVIEW
    rec.kyc_submitted_at = now
    rec.kyc_status_display = f"KYC Submitted — Verifying… (Attempt #{attempt}, {_fmt(now)})"
    rec.company_type = company_type
    await db.commit()

    # Upload KYC files to Zoho CRM as Lead attachments
    try:
        from app.services.zoho_crm import find_lead_by_email, upload_lead_attachment
        _lead_for_files = await db.get(LeadV2, rec.lead_id)
        if _lead_for_files:
            crm_lead_id = find_lead_by_email(_lead_for_files.email)
            if crm_lead_id:
                import mimetypes as _mt
                if gst_bytes and gst_filename:
                    _mime = _mt.guess_type(gst_filename)[0] or "application/octet-stream"
                    gst_att_id = upload_lead_attachment(crm_lead_id, gst_filename, gst_bytes, _mime)
                    if gst_att_id:
                        submission.gst_certificate_zoho_id = gst_att_id
                if inc_bytes and inc_filename:
                    _mime = _mt.guess_type(inc_filename)[0] or "application/octet-stream"
                    inc_att_id = upload_lead_attachment(crm_lead_id, inc_filename, inc_bytes, _mime)
                    if inc_att_id:
                        submission.incorporation_zoho_id = inc_att_id
                await db.commit()
    except Exception as _fe:
        logger.warning("kyc_file_upload_crm_failed", onboarding_id=onboarding_id, error=str(_fe))

    # Format verification + team notification via Celery
    from app.workers.onboarding_tasks import auto_verify_kyc_task, export_onboarding_to_sheets
    auto_verify_kyc_task.delay(submission_id, onboarding_id)
    export_onboarding_to_sheets.delay(onboarding_id)

    return HTMLResponse(_kyc_submitted_page(company_name))


# ---------------------------------------------------------------------------
# KYC review (team)
# ---------------------------------------------------------------------------

class ReviewRequest(BaseModel):
    action: str  # "approve" or "reject"
    notes: str = ""


@router.post("/kyc/review/{onboarding_id}")
async def kyc_review(
    onboarding_id: str,
    body: ReviewRequest,
    db: AsyncSession = Depends(get_db),
):
    rec = await _get_onboarding(db, onboarding_id)
    lead = await db.get(LeadV2, rec.lead_id)
    if not lead:
        raise HTTPException(404, "Lead not found")

    # Get latest submission
    kyc_result = await db.execute(
        select(KYCSubmission)
        .where(KYCSubmission.onboarding_id == rec.id)
        .order_by(KYCSubmission.attempt_number.desc())
    )
    latest_kyc = kyc_result.scalars().first()

    now = _now_ist()

    if body.action == "approve":
        rec.kyc_status = KYCStatus.APPROVED
        rec.kyc_approved_at = now
        rec.kyc_status_display = f"KYC Approved ✓ ({_fmt(now)})"
        await db.commit()

        # Notify lead of approval
        from app.services.onboarding_email import send_kyc_approved_email
        send_kyc_approved_email(lead.email, lead.contact_name or "", lead.business_name)

        # Trigger NDA generation
        from app.workers.onboarding_tasks import generate_nda_draft_task
        generate_nda_draft_task.delay(onboarding_id)

        from app.workers.onboarding_tasks import export_onboarding_to_sheets
        export_onboarding_to_sheets.delay(onboarding_id)

        from app.services.zoho_crm import sync_onboarding_stage
        sync_onboarding_stage(
            email=lead.email,
            contact_name=lead.contact_name or lead.business_name,
            company_name=lead.business_name,
            stage="KYC Manually Approved",
            detail=f"Approved by team. Notes: {body.notes}" if body.notes else "Approved by team.",
            company_type=rec.company_type or "",
            onboarding_id=onboarding_id,
        )

        return {"message": "KYC approved. NDA draft generation triggered."}

    elif body.action == "reject":
        if latest_kyc:
            latest_kyc.reviewer_notes = body.notes
        rec.kyc_status = KYCStatus.REJECTED
        rec.kyc_followup_count = (rec.kyc_followup_count or 0) + 1
        rec.kyc_last_followup_at = now
        rec.kyc_status_display = (
            f"KYC Rejected — Follow-up #{rec.kyc_followup_count} sent ({_fmt(now)})"
        )
        await db.commit()

        # AI rejection email + send
        from app.workers.onboarding_tasks import send_kyc_rejection_task
        send_kyc_rejection_task.delay(
            onboarding_id,
            body.notes,
            latest_kyc.attempt_number if latest_kyc else 1,
        )

        from app.workers.onboarding_tasks import export_onboarding_to_sheets
        export_onboarding_to_sheets.delay(onboarding_id)

        from app.services.zoho_crm import sync_onboarding_stage
        sync_onboarding_stage(
            email=lead.email,
            contact_name=lead.contact_name or lead.business_name,
            company_name=lead.business_name,
            stage="KYC Rejected",
            detail=f"Rejected by team. Reason: {body.notes}",
            company_type=rec.company_type or "",
            onboarding_id=onboarding_id,
        )

        return {"message": "KYC rejected. Rejection email sent to lead."}

    raise HTTPException(400, "action must be 'approve' or 'reject'")


# ---------------------------------------------------------------------------
# KYC read-only view page (reviewer sees what lead submitted, then approves/rejects)
# ---------------------------------------------------------------------------

@router.get("/kyc/view/{onboarding_id}/{token}", response_class=HTMLResponse)
async def kyc_view_page(onboarding_id: str, token: str, db: AsyncSession = Depends(get_db)):
    from app.services.onboarding_email import verify_kyc_view_token, make_action_url
    if not verify_kyc_view_token(onboarding_id, token):
        raise HTTPException(403, "Invalid or expired link")

    rec = await _get_onboarding(db, onboarding_id)
    lead = await db.get(LeadV2, rec.lead_id)

    kyc_result = await db.execute(
        select(KYCSubmission)
        .where(KYCSubmission.onboarding_id == rec.id)
        .order_by(KYCSubmission.attempt_number.desc())
    )
    kyc = kyc_result.scalars().first()

    if not kyc:
        return HTMLResponse("<p style='font-family:sans-serif;padding:40px;'>No KYC submission found.</p>")

    vr = kyc.kyc_verification_result or {}
    extra = vr.get("extra_fields", {}) or {}
    issues = vr.get("issues", [])
    passed = vr.get("overall_passed", False)

    def _row(label: str, val: str) -> str:
        val = val or "—"
        return (
            f"<tr><td style='padding:8px 14px;background:#f8fafc;font-weight:600;font-size:13px;"
            f"color:#374151;width:36%;vertical-align:top;'>{label}</td>"
            f"<td style='padding:8px 14px;font-size:13px;color:#111;font-family:monospace;"
            f"word-break:break-all;'>{val}</td></tr>"
        )

    def _sec(title: str) -> str:
        return (
            f"<tr><td colspan='2' style='padding:10px 14px;background:#1a3a6b;color:#fff;"
            f"font-weight:700;font-size:12px;text-transform:uppercase;letter-spacing:.05em;"
            f"'>{title}</td></tr>"
        )

    is_overseas = (kyc.company_type or "").lower() == "overseas"

    # ── A. Business Identity ──────────────────────────────────────────────────
    rows = _sec("A. Business Identity")
    rows += _row("Company Name", kyc.company_name or "")
    rows += _row("Company Type", (kyc.company_type or "").title())
    rows += _row("Trade Name / Brand", extra.get("trade_name", ""))
    rows += _row("Entity Type", extra.get("entity_type", ""))
    rows += _row("Date of Incorporation", extra.get("date_of_incorporation", ""))
    rows += _row("Nature of Business", extra.get("nature_of_business", ""))
    if extra.get("principal_business_address"):
        rows += _row("Principal Business Address", extra.get("principal_business_address", ""))

    # ── A. Registered Address ────────────────────────────────────────────────
    rows += _sec("Registered Address")
    rows += _row("Address", extra.get("registered_address", ""))
    rows += _row("City", extra.get("city", ""))
    rows += _row("State / Province", extra.get("state", ""))
    rows += _row("PIN / Postal Code", extra.get("pin_code", ""))
    if is_overseas:
        rows += _row("Country", extra.get("country", ""))

    # ── A. KYC Identifiers ───────────────────────────────────────────────────
    if not is_overseas:
        rows += _sec("Indian KYC Identifiers")
        rows += _row("GSTIN", kyc.gstin_number or "")
        rows += _row("PAN (Entity)", kyc.pan_number or "")
        rows += _row("CIN", kyc.cin_number or "")
    else:
        rows += _sec("Overseas Identifiers")
        rows += _row("Country of Incorporation", extra.get("country_of_incorporation", ""))
        rows += _row("Company Reg. No.", extra.get("company_reg_number", ""))
        rows += _row("Country of Tax Residence", extra.get("country_of_tax_residence", ""))
        rows += _row("Tax ID / TIN", extra.get("tax_id_tin", ""))
        rows += _row("LEI Number", extra.get("lei_number", ""))
        rows += _row("VAT / GST Number", extra.get("vat_gst_number", ""))
        rows += _row("Company Website", extra.get("company_website", ""))
        rows += _row("Primary Business Activity", extra.get("primary_business_activity", ""))
        rows += _row("Countries of Operation", extra.get("countries_of_operation", ""))

    # ── B. Authorised Signatories ────────────────────────────────────────────
    rows += _sec("B. Authorised Signatories")
    rows += _row("Signatory 1 — Name", kyc.contact_name or "")
    rows += _row("Signatory 1 — Designation", extra.get("signatory1_designation", ""))
    rows += _row("Signatory 1 — PAN", extra.get("signatory1_pan", ""))
    rows += _row("Signatory 1 — Aadhaar (last 4)", extra.get("signatory1_aadhaar", ""))
    if is_overseas:
        rows += _row("Signatory 1 — Nationality", extra.get("signatory1_nationality", ""))
        rows += _row("Signatory 1 — DOB", extra.get("signatory1_dob", ""))
        rows += _row("Signatory 1 — Passport / ID", extra.get("signatory1_passport_id", ""))
        rows += _row("Signatory 1 — Country of Residence", extra.get("signatory1_country_of_residence", ""))
        rows += _row("Signatory 1 — Shareholding %", extra.get("signatory1_shareholding_pct", ""))
    if extra.get("signatory2_name"):
        rows += _row("Signatory 2 — Name", extra.get("signatory2_name", ""))
        rows += _row("Signatory 2 — Designation", extra.get("signatory2_designation", ""))
        rows += _row("Signatory 2 — PAN", extra.get("signatory2_pan", ""))
        rows += _row("Signatory 2 — Aadhaar (last 4)", extra.get("signatory2_aadhaar", ""))
    if extra.get("director_names"):
        rows += _row("Director / Partner Name(s)", extra.get("director_names", ""))

    # ── UBO ──────────────────────────────────────────────────────────────────
    if extra.get("ubo_name"):
        rows += _sec("Ultimate Beneficial Owner (UBO)")
        rows += _row("UBO Name", extra.get("ubo_name", ""))
        rows += _row("UBO PAN", extra.get("ubo_pan", ""))
        rows += _row("UBO Nationality", extra.get("ubo_nationality", ""))

    rows += _row("PEP Status", extra.get("pep_status", ""))

    # ── D. Bank Details ──────────────────────────────────────────────────────
    rows += _sec("D. Bank & Financial Details")
    rows += _row("Bank Name", extra.get("bank_name", ""))
    rows += _row("Account Number", extra.get("account_number", ""))
    if not is_overseas:
        rows += _row("IFSC Code", extra.get("ifsc_code", ""))
        rows += _row("Account Type", extra.get("account_type", ""))
    else:
        rows += _row("SWIFT Code / BIC", extra.get("swift_code", ""))
        rows += _row("IBAN / Account No.", extra.get("iban_number", ""))
        rows += _row("Bank Country", extra.get("bank_country", ""))
        rows += _row("Account Currency", extra.get("account_currency", ""))
    rows += _row("Annual Turnover", extra.get("annual_turnover", ""))
    if extra.get("bank_branch_address"):
        rows += _row("Bank Branch Address", extra.get("bank_branch_address", ""))

    # ── Overseas: Escalation Contact ─────────────────────────────────────────
    if is_overseas and extra.get("escalation_contact_name"):
        rows += _sec("F. Escalation Contact")
        rows += _row("Name", extra.get("escalation_contact_name", ""))
        rows += _row("Title / Designation", extra.get("escalation_contact_title", ""))
        rows += _row("Email", extra.get("escalation_contact_email", ""))
        rows += _row("Phone", extra.get("escalation_contact_phone", ""))
        rows += _row("Department", extra.get("escalation_contact_dept", ""))
        rows += _row("Relationship", extra.get("escalation_contact_relationship", ""))

    # ── Overseas: Directors ───────────────────────────────────────────────────
    if is_overseas and extra.get("director1_name"):
        rows += _sec("G. Directors & Key Controllers")
        rows += _row("Director 1 — Name", extra.get("director1_name", ""))
        rows += _row("Director 1 — Nationality", extra.get("director1_nationality", ""))
        rows += _row("Director 1 — DOB", extra.get("director1_dob", ""))
        rows += _row("Director 1 — Passport / ID", extra.get("director1_passport_id", ""))
        rows += _row("Director 1 — Country of Residence", extra.get("director1_country_of_residence", ""))
        rows += _row("Director 1 — Shareholding %", extra.get("director1_shareholding_pct", ""))
        if extra.get("director2_name"):
            rows += _row("Director 2 — Name", extra.get("director2_name", ""))
            rows += _row("Director 2 — Nationality", extra.get("director2_nationality", ""))
            rows += _row("Director 2 — DOB", extra.get("director2_dob", ""))
            rows += _row("Director 2 — Passport / ID", extra.get("director2_passport_id", ""))
            rows += _row("Director 2 — Country of Residence", extra.get("director2_country_of_residence", ""))
            rows += _row("Director 2 — Shareholding %", extra.get("director2_shareholding_pct", ""))

    # ── Compliance ────────────────────────────────────────────────────────────
    if any(extra.get(k) for k in ("sanctions_check", "criminal_investigation_check", "regulated_licensed")):
        rows += _sec("H. Compliance")
        rows += _row("Sanctions Check", extra.get("sanctions_check", ""))
        rows += _row("Criminal Investigation", extra.get("criminal_investigation_check", ""))
        rows += _row("Regulated / Licensed", extra.get("regulated_licensed", ""))
        rows += _row("Licensing Regulator", extra.get("licensed_regulator", ""))

    # ── C. Documents ─────────────────────────────────────────────────────────
    # Build file view URLs (same token used for this page)
    _base_url = settings.APP_URL.rstrip("/")
    _file_base = f"{_base_url}/api/v1/onboarding/kyc/file/{onboarding_id}/{token}"

    def _file_row(label: str, filename: str | None, zoho_id: str | None, file_type: str) -> str:
        if not filename:
            return _row(label, "Not uploaded")
        if zoho_id:
            view_url = f"{_file_base}/{file_type}"
            link = (
                f"{filename} &nbsp;"
                f"<a href='{view_url}' target='_blank' "
                f"style='display:inline-block;padding:3px 10px;background:#1a3a6b;color:#fff;"
                f"border-radius:4px;font-size:11px;text-decoration:none;font-family:Arial;'>View File</a>"
            )
        else:
            link = f"{filename} &nbsp;<span style='color:#f59e0b;font-size:11px;'>(uploading to CRM…)</span>"
        return (
            f"<tr><td style='padding:8px 14px;background:#f8fafc;font-weight:600;font-size:13px;"
            f"color:#374151;width:36%;vertical-align:top;'>{label}</td>"
            f"<td style='padding:8px 14px;font-size:13px;color:#111;'>{link}</td></tr>"
        )

    rows += _sec("C. Documents Uploaded")
    if not is_overseas:
        rows += _file_row("GST Certificate", kyc.gst_certificate_filename, kyc.gst_certificate_zoho_id, "gst")
    rows += _file_row("Incorporation Cert.", kyc.incorporation_filename, kyc.incorporation_zoho_id, "incorporation")
    rows += _row("Declaration Agreed", "Yes" if extra.get("declaration_agreed") else "No")

    status_color = "#16a34a" if passed else "#dc2626"
    status_label = "All Format Checks Passed" if passed else f"{len(issues)} Format Issue(s) Found"
    issues_html = ""
    if issues:
        items = "".join(f"<li style='color:#991b1b;margin-bottom:4px;'>{i}</li>" for i in issues)
        issues_html = f"<ul style='margin:10px 0;padding-left:20px;font-size:13px;'>{items}</ul>"

    approve_url = make_action_url(onboarding_id, "approve_kyc")
    reject_url  = make_action_url(onboarding_id, "reject_kyc")

    html = f"""<!DOCTYPE html>
<html lang="en">
<head><meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>KYC Review — {kyc.company_name}</title>
<style>
  body{{font-family:Arial,sans-serif;background:#f4f6fb;margin:0;padding:24px;}}
  .card{{background:#fff;max-width:720px;margin:0 auto;border-radius:12px;
         box-shadow:0 2px 16px rgba(0,0,0,.1);padding:36px 40px;}}
  h1{{color:#1a3a6b;font-size:22px;margin:0 0 4px;}}
  table{{width:100%;border-collapse:collapse;border:1px solid #e5e7eb;margin-bottom:20px;}}
  .badge{{display:inline-block;padding:6px 16px;border-radius:5px;color:#fff;
          font-weight:700;font-size:13px;margin-bottom:20px;}}
  .btn{{display:inline-block;padding:13px 28px;border-radius:7px;color:#fff;
        text-decoration:none;font-weight:700;font-size:15px;margin:4px 8px 4px 0;}}
  .logo{{font-size:16px;font-weight:700;color:#1a3a6b;margin-bottom:20px;}}
  @media(max-width:600px){{.card{{padding:20px 16px;}}}}
</style></head>
<body>
<div class="card">
  <div class="logo">✈ Jane Aerospace — KYC Review</div>
  <h1>KYC Submission — {kyc.company_name}</h1>
  <p style="color:#555;font-size:13px;margin:4px 0 12px;">
    Submitted by: <strong>{lead.email if lead else '—'}</strong> &nbsp;|&nbsp;
    Attempt #{kyc.attempt_number or 1} &nbsp;|&nbsp;
    Onboarding ID: {onboarding_id[:8]}…
  </p>
  <div style="margin-bottom:16px;">
    <span style="display:inline-block;padding:4px 14px;border-radius:99px;font-size:12px;font-weight:700;
      background:{'#dbeafe' if not is_overseas else '#fef3c7'};
      color:{'#1e40af' if not is_overseas else '#92400e'};">
      {'🇮🇳 Indian Company' if not is_overseas else '🌐 Overseas Company'}
    </span>
  </div>
  <div class="badge" style="background:{status_color};">{status_label}</div>
  {issues_html}
  <table>{rows}</table>
  <p style="margin:24px 0 10px;font-weight:600;font-size:15px;color:#1a3a6b;">Take Action:</p>
  <a href="{approve_url}" class="btn" style="background:#16a34a;">✓ Approve KYC</a>
  <a href="{reject_url}" class="btn" style="background:#dc2626;">✗ Reject KYC</a>
  <p style="margin-top:24px;color:#aaa;font-size:11px;">
    This link is valid for 7 days. Do not forward — it contains a secure action token.
  </p>
</div>
</body></html>"""
    return HTMLResponse(html)


# ---------------------------------------------------------------------------
# NDA draft preview + review (team)
# ---------------------------------------------------------------------------

@router.get("/nda/preview/{onboarding_id}")
async def nda_preview(onboarding_id: str, db: AsyncSession = Depends(get_db)):
    rec = await _get_onboarding(db, onboarding_id)
    return {
        "nda_status": rec.nda_status,
        "nda_status_display": rec.nda_status_display,
        "nda_draft_content": rec.nda_draft_content,
        "nda_draft_revision": rec.nda_draft_revision,
        "nda_draft_zoho_file_id": rec.nda_draft_zoho_file_id,
        "nda_team_notes": rec.nda_team_notes,
    }


@router.post("/nda/draft-review/{onboarding_id}")
async def nda_draft_review(
    onboarding_id: str,
    body: ReviewRequest,
    db: AsyncSession = Depends(get_db),
):
    rec = await _get_onboarding(db, onboarding_id)
    lead = await db.get(LeadV2, rec.lead_id)
    if not lead:
        raise HTTPException(404, "Lead not found")

    now = _now_ist()

    if body.action == "approve":
        rec.nda_status = DocumentStatus.SENT_TO_LEAD
        rec.nda_sent_at = now
        rec.nda_status_display = f"NDA Sent to Lead ({_fmt(now)}) — Awaiting Signature"
        await db.commit()

        from app.workers.onboarding_tasks import send_nda_to_lead_task
        send_nda_to_lead_task.delay(onboarding_id)

        from app.workers.onboarding_tasks import export_onboarding_to_sheets
        export_onboarding_to_sheets.delay(onboarding_id)

        return {"message": "NDA approved and sent to lead."}

    elif body.action == "reject":
        rec.nda_status = DocumentStatus.DRAFT_REJECTED
        rec.nda_team_notes = body.notes
        rec.nda_draft_revision = (rec.nda_draft_revision or 0) + 1
        rec.nda_status_display = f"NDA Draft Rejected — Revision #{rec.nda_draft_revision} ({_fmt(now)})"
        await db.commit()

        return {"message": "NDA draft rejected. Edit the template and re-trigger if needed."}

    raise HTTPException(400, "action must be 'approve' or 'reject'")


# ---------------------------------------------------------------------------
# NDA signed copy review (team)
# ---------------------------------------------------------------------------

@router.post("/nda/sign-review/{onboarding_id}")
async def nda_sign_review(
    onboarding_id: str,
    body: ReviewRequest,
    db: AsyncSession = Depends(get_db),
):
    rec = await _get_onboarding(db, onboarding_id)
    lead = await db.get(LeadV2, rec.lead_id)
    if not lead:
        raise HTTPException(404, "Lead not found")

    now = _now_ist()

    if body.action == "approve":
        rec.nda_status = DocumentStatus.APPROVED
        rec.nda_approved_at = now
        rec.nda_status_display = f"NDA Signed & Approved ✓ ({_fmt(now)}) — Proceeding to Agreement"
        await db.commit()

        from app.services.onboarding_email import send_nda_approved_email
        send_nda_approved_email(lead.email, lead.contact_name or "", lead.business_name)

        from app.workers.onboarding_tasks import generate_agreement_draft_task
        generate_agreement_draft_task.delay(onboarding_id)

        from app.workers.onboarding_tasks import export_onboarding_to_sheets
        export_onboarding_to_sheets.delay(onboarding_id)

        return {"message": "Signed NDA approved. Customer Agreement generation triggered."}

    elif body.action == "reject":
        rec.nda_status = DocumentStatus.SIGN_REJECTED
        rec.nda_team_notes = body.notes
        rec.nda_followup_count = (rec.nda_followup_count or 0) + 1
        rec.nda_status_display = (
            f"NDA Signature Rejected #{rec.nda_followup_count} — Lead Notified ({_fmt(now)})"
        )
        await db.commit()

        from app.workers.onboarding_tasks import send_nda_sign_rejection_task
        send_nda_sign_rejection_task.delay(onboarding_id, body.notes)

        from app.workers.onboarding_tasks import export_onboarding_to_sheets
        export_onboarding_to_sheets.delay(onboarding_id)

        return {"message": "Sign rejection sent to lead."}

    raise HTTPException(400, "action must be 'approve' or 'reject'")


# ---------------------------------------------------------------------------
# NDA template upload
# ---------------------------------------------------------------------------



# ---------------------------------------------------------------------------
# Customer Agreement (mirrors NDA flow exactly)
# ---------------------------------------------------------------------------

@router.get("/agreement/preview/{onboarding_id}")
async def agreement_preview(onboarding_id: str, db: AsyncSession = Depends(get_db)):
    rec = await _get_onboarding(db, onboarding_id)
    return {
        "agreement_status": rec.agreement_status,
        "agreement_status_display": rec.agreement_status_display,
        "agreement_draft_content": rec.agreement_draft_content,
        "agreement_draft_revision": rec.agreement_draft_revision,
        "agreement_draft_zoho_file_id": rec.agreement_draft_zoho_file_id,
        "agreement_team_notes": rec.agreement_team_notes,
    }


@router.post("/agreement/draft-review/{onboarding_id}")
async def agreement_draft_review(
    onboarding_id: str,
    body: ReviewRequest,
    db: AsyncSession = Depends(get_db),
):
    rec = await _get_onboarding(db, onboarding_id)
    lead = await db.get(LeadV2, rec.lead_id)
    if not lead:
        raise HTTPException(404, "Lead not found")

    now = _now_ist()

    if body.action == "approve":
        rec.agreement_status = DocumentStatus.SENT_TO_LEAD
        rec.agreement_sent_at = now
        rec.agreement_status_display = f"Agreement Sent to Lead ({_fmt(now)}) — Awaiting Signature"
        await db.commit()

        from app.workers.onboarding_tasks import send_agreement_to_lead_task
        send_agreement_to_lead_task.delay(onboarding_id)

        from app.workers.onboarding_tasks import export_onboarding_to_sheets
        export_onboarding_to_sheets.delay(onboarding_id)

        return {"message": "Agreement approved and sent to lead."}

    elif body.action == "reject":
        rec.agreement_status = DocumentStatus.DRAFT_REJECTED
        rec.agreement_team_notes = body.notes
        rec.agreement_draft_revision = (rec.agreement_draft_revision or 0) + 1
        rec.agreement_status_display = (
            f"Agreement Draft Rejected — Revision #{rec.agreement_draft_revision} ({_fmt(now)})"
        )
        await db.commit()

        return {"message": "Agreement draft rejected. Edit the template and re-trigger if needed."}

    raise HTTPException(400, "action must be 'approve' or 'reject'")


@router.post("/agreement/sign-review/{onboarding_id}")
async def agreement_sign_review(
    onboarding_id: str,
    body: ReviewRequest,
    db: AsyncSession = Depends(get_db),
):
    rec = await _get_onboarding(db, onboarding_id)
    lead = await db.get(LeadV2, rec.lead_id)
    if not lead:
        raise HTTPException(404, "Lead not found")

    now = _now_ist()

    if body.action == "approve":
        rec.agreement_status = DocumentStatus.PROCEED_NEXT
        rec.agreement_approved_at = now
        rec.agreement_status_display = f"Agreement Approved ✓ — Proceed with Training ({_fmt(now)})"
        await db.commit()

        from app.services.onboarding_email import send_agreement_approved_email
        send_agreement_approved_email(lead.email, lead.contact_name or "", lead.business_name)

        from app.workers.onboarding_tasks import export_onboarding_to_sheets
        export_onboarding_to_sheets.delay(onboarding_id)

        return {"message": "Agreement approved. Customer onboarding complete — proceed with training."}

    elif body.action == "reject":
        rec.agreement_status = DocumentStatus.SIGN_REJECTED
        rec.agreement_team_notes = body.notes
        rec.agreement_followup_count = (rec.agreement_followup_count or 0) + 1
        rec.agreement_status_display = (
            f"Agreement Signature Rejected #{rec.agreement_followup_count} — Lead Notified ({_fmt(now)})"
        )
        await db.commit()

        from app.workers.onboarding_tasks import send_agreement_sign_rejection_task
        send_agreement_sign_rejection_task.delay(onboarding_id, body.notes)

        from app.workers.onboarding_tasks import export_onboarding_to_sheets
        export_onboarding_to_sheets.delay(onboarding_id)

        return {"message": "Sign rejection sent to lead."}

    raise HTTPException(400, "action must be 'approve' or 'reject'")


# ---------------------------------------------------------------------------
# CSV bulk lead import → creates leads, syncs CRM, starts onboarding
# POST /onboarding/import-csv
# Required CSV columns: email, company_name
# Optional:  contact_name, summary, phone
# ---------------------------------------------------------------------------

@router.post("/import-csv")
async def import_leads_csv(
    file: UploadFile = File(...),
    db: AsyncSession = Depends(get_db),
):
    """Upload a CSV of leads. Each row creates a lead, syncs to Zoho CRM, and starts onboarding.

    Minimum required CSV columns: email, company_name
    Optional columns: contact_name, summary, phone
    """
    import csv
    import io

    content = await file.read()
    try:
        text = content.decode("utf-8-sig")  # handles BOM
    except UnicodeDecodeError:
        text = content.decode("latin-1")

    reader = csv.DictReader(io.StringIO(text))
    if not reader.fieldnames:
        raise HTTPException(400, "CSV has no headers")

    # Normalise header names (lowercase, strip)
    norm = {k.lower().strip().replace(" ", "_"): k for k in (reader.fieldnames or [])}

    def _get(row: dict, *keys: str) -> str:
        for k in keys:
            v = row.get(norm.get(k, k), "").strip()
            if v:
                return v
        return ""

    created, skipped, errors = [], [], []

    for raw in reader:
        email = _get(raw, "email", "lead_email").lower()
        company = _get(raw, "company_name", "company", "business_name")
        if not email or not company:
            skipped.append({"row": dict(raw), "reason": "missing email or company_name"})
            continue

        contact_name = _get(raw, "contact_name", "name", "contact")
        summary      = _get(raw, "summary", "interest", "notes", "message")
        phone        = _get(raw, "phone", "contact_phone", "mobile")

        try:
            # Create or find lead
            existing = (await db.execute(
                select(LeadV2).where(LeadV2.email == email)
            )).scalar_one_or_none()

            if existing:
                # Update fields if richer data available
                if contact_name and not existing.contact_name:
                    existing.contact_name = contact_name
                if summary and not existing.summary:
                    existing.summary = summary
                lead = existing
                is_new = False
            else:
                from app.db.base import LeadStatus
                lead = LeadV2(
                    email=email,
                    business_name=company,
                    contact_name=contact_name or None,
                    summary=summary or None,
                    status=LeadStatus.NEW,
                )
                db.add(lead)
                await db.flush()
                is_new = True

            await db.commit()
            await db.refresh(lead)

            # Sync to Zoho CRM — pass all CSV fields into the Lead record
            try:
                from app.services.zoho_crm import upsert_lead, add_note
                crm_lead_id = upsert_lead(
                    email=email,
                    contact_name=contact_name or company,
                    company_name=company,
                    phone=phone,
                    summary=summary,
                )
                if crm_lead_id and summary:
                    add_note(
                        crm_lead_id,
                        f"Lead Imported via CSV — {company}",
                        f"Email: {email}\nCompany: {company}\nContact: {contact_name or '—'}\n"
                        f"Phone: {phone or '—'}\nSummary: {summary}",
                    )
            except Exception as _crm_err:
                logger.warning("csv_import_crm_sync_failed", email=email, error=str(_crm_err))


            # Check if onboarding already started
            existing_ob = (await db.execute(
                select(OnboardingRecord).where(OnboardingRecord.lead_id == lead.id)
            )).scalar_one_or_none()

            if not existing_ob:
                from app.workers.onboarding_tasks import initiate_onboarding_task
                initiate_onboarding_task.delay(str(lead.id))
                created.append({"email": email, "company": company, "status": "onboarding_started"})
            else:
                created.append({"email": email, "company": company, "status": "already_onboarding"})

        except Exception as exc:
            await db.rollback()
            errors.append({"email": email, "company": company, "error": str(exc)})

    return {
        "imported": len(created),
        "skipped":  len(skipped),
        "errors":   len(errors),
        "details": {"created": created, "skipped": skipped, "errors": errors},
    }


# ---------------------------------------------------------------------------
# KYC form HTML pages
# ---------------------------------------------------------------------------

def _kyc_form_html(submit_url: str, company_name: str, contact_name: str, preselect: str) -> str:
    indian_checked = 'checked' if preselect == 'indian' else ''
    overseas_checked = 'checked' if preselect == 'overseas' else ''
    indian_style = '' if preselect != 'overseas' else 'display:none'
    overseas_style = 'display:none' if preselect != 'overseas' else ''
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Business KYC Form — Jane Aerospace</title>
<style>
  body{{font-family:Arial,sans-serif;background:#f4f6fb;margin:0;padding:24px;}}
  .card{{background:#fff;max-width:760px;margin:0 auto;border-radius:12px;
         box-shadow:0 2px 16px rgba(0,0,0,.1);padding:36px 40px;}}
  h1{{color:#1a3a6b;font-size:22px;margin-bottom:4px;}}
  .sub{{color:#555;font-size:13px;margin-bottom:28px;line-height:1.5;}}
  label{{display:block;font-size:13px;font-weight:600;margin:14px 0 4px;color:#222;}}
  input[type=text],input[type=tel],input[type=date],select,textarea{{
    width:100%;padding:9px 11px;border:1px solid #ccc;
    border-radius:6px;font-size:13px;box-sizing:border-box;}}
  input[type=text]{{text-transform:uppercase;}}
  .no-upper{{text-transform:none!important;}}
  input[type=file]{{padding:6px 0;font-size:12px;}}
  textarea{{height:70px;resize:vertical;text-transform:none;}}
  .radio-group{{display:flex;gap:16px;margin:8px 0 12px;flex-wrap:wrap;}}
  .radio-group label{{font-weight:400;margin:0;display:flex;align-items:center;gap:6px;cursor:pointer;font-size:13px;}}
  .file-note{{color:#888;font-size:11px;margin-top:2px;}}
  .field-note{{color:#2563eb;font-size:11px;margin-top:2px;}}
  .sec{{font-size:13px;font-weight:700;color:#fff;background:#1a3a6b;
    text-transform:uppercase;letter-spacing:.05em;padding:8px 14px;border-radius:6px;margin:28px 0 14px;}}
  .two-col{{display:grid;grid-template-columns:1fr 1fr;gap:14px;}}
  .three-col{{display:grid;grid-template-columns:1fr 1fr 1fr;gap:12px;}}
  button{{background:#1a56db;color:#fff;border:none;padding:13px 32px;border-radius:6px;
          font-size:15px;font-weight:600;cursor:pointer;margin-top:28px;width:100%;}}
  button:hover{{background:#1344b0;}}
  .required{{color:#e53e3e;}}
  .logo{{font-size:18px;font-weight:700;color:#1a3a6b;margin-bottom:20px;}}
  .badge{{display:inline-flex;align-items:center;gap:4px;background:#ecfdf5;
    color:#059669;font-size:11px;font-weight:600;padding:2px 8px;border-radius:99px;
    margin-left:8px;vertical-align:middle;}}
  .opt{{color:#888;font-weight:400;font-size:12px;}}
  .disclaimer{{font-size:12px;color:#555;background:#f8f9fa;padding:14px;border-radius:6px;
    border-left:3px solid #1a3a6b;margin:16px 0;line-height:1.6;}}
  .sub-label{{font-size:13px;color:#444;margin:16px 0 6px;font-weight:600;}}
  @media(max-width:600px){{.two-col,.three-col{{grid-template-columns:1fr;}}
    .card{{padding:20px 18px;}}}}
</style>
</head>
<body>
<div class="card">
  <div class="logo">✈ Jane Aerospace</div>
  <h1>Business KYC Form</h1>
  <p class="sub">Know Your Customer — Legal Entity &nbsp;|&nbsp; As per RBI Master Direction on KYC 2016 | PMLA 2002 | SEBI Regulations<br>
  Fields marked <span class="required">*</span> are mandatory. Details are auto-verified against government records.</p>

  <form id="kyc-form" method="POST" action="{submit_url}" enctype="multipart/form-data">

    <!-- ── A. BUSINESS IDENTITY ── -->
    <div class="sec">A. Business Identity</div>

    <label>Company Type <span class="required">*</span></label>
    <div class="radio-group">
      <label><input type="radio" name="company_type" value="indian" {indian_checked}
             onchange="toggleIndian(this.value)"> Indian Company</label>
      <label><input type="radio" name="company_type" value="overseas" {overseas_checked}
             onchange="toggleIndian(this.value)"> Overseas Company</label>
    </div>

    <div class="two-col">
      <div>
        <label>Legal / Registered Name of Entity <span class="required">*</span></label>
        <input type="text" name="company_name" value="{company_name}" required placeholder="ACME AEROSPACE PVT LTD">
      </div>
      <div>
        <label>Trade Name / Brand Name <span class="opt">(if any)</span></label>
        <input type="text" name="trade_name" placeholder="Brand or trading name">
      </div>
    </div>

    <div class="two-col">
      <div>
        <label>Type of Entity <span class="required">*</span></label>
        <select name="entity_type" required>
          <option value="">-- Select --</option>
          <option value="private_ltd">Private Ltd. / Pvt. Ltd.</option>
          <option value="public_ltd">Public Ltd. / PLC</option>
          <option value="llp">LLP / LLC</option>
          <option value="corporation">Corporation / Inc.</option>
          <option value="partnership">Partnership</option>
          <option value="proprietorship">Proprietorship / Sole Trader</option>
          <option value="gmbh">GmbH / SARL / BV</option>
          <option value="trust_ngo">Trust / NGO</option>
          <option value="others">Others</option>
        </select>
      </div>
      <div>
        <label>Date of Incorporation <span class="required">*</span></label>
        <input type="date" name="date_of_incorporation" required>
      </div>
    </div>

    <div class="two-col">
      <div>
        <label>PAN of Entity <span class="required">*</span></label>
        <input type="text" name="pan_number" maxlength="10" placeholder="AAAAA0000A"
               oninput="this.value=this.value.toUpperCase().replace(/\\s/g,'')">
      </div>
      <div>
        <label>Certificate of Incorporation No. <span class="opt">(optional)</span></label>
        <input type="text" name="cin_number" maxlength="21" placeholder="L17110MH1973PLC019786"
               oninput="this.value=this.value.toUpperCase().replace(/\\s/g,'')">
        <p class="file-note">CIN for Pvt Ltd / Ltd. Leave blank if not applicable.</p>
      </div>
    </div>

    <div id="indian-section" style="{indian_style}">
      <label>GSTIN Number <span class="required">*</span> <span class="badge">⚡ Auto-Verified</span></label>
      <input type="text" name="gstin_number" maxlength="15" placeholder="22AAAAA0000A1Z5"
             oninput="this.value=this.value.toUpperCase().replace(/\\s/g,'')">
      <p class="field-note">15-character GST Identification Number — verified automatically against GST portal.</p>
    </div>

    <!-- ── Overseas: Company Identity ── -->
    <div id="overseas-section" style="{overseas_style}">
      <div class="two-col">
        <div>
          <label>Country of Incorporation <span class="required">*</span></label>
          <input type="text" name="country_of_incorporation" placeholder="UNITED STATES / UNITED KINGDOM">
        </div>
        <div>
          <label>Company Registration No. <span class="required">*</span></label>
          <input type="text" name="company_reg_number" placeholder="Registration / File Number">
          <p class="file-note">Overseas equivalent of CIN — issued by the company registry in your country.</p>
        </div>
      </div>
      <div class="two-col">
        <div>
          <label>Country of Tax Residence <span class="required">*</span></label>
          <input type="text" name="country_of_tax_residence" placeholder="UNITED STATES">
        </div>
        <div>
          <label>Tax ID / TIN <span class="required">*</span></label>
          <input type="text" name="tax_id_tin" placeholder="e.g. 12-3456789 (EIN) / UTR / TIN">
        </div>
      </div>
      <div class="two-col">
        <div>
          <label>LEI Number <span class="opt">(optional)</span></label>
          <input type="text" name="lei_number" maxlength="20" placeholder="20-character LEI code">
        </div>
        <div>
          <label>VAT / GST Number <span class="opt">(if applicable)</span></label>
          <input type="text" name="vat_gst_number" placeholder="GB123456789">
        </div>
      </div>
      <div class="two-col">
        <div>
          <label>Company Website <span class="opt">(optional)</span></label>
          <input type="text" name="company_website" class="no-upper" placeholder="https://www.company.com">
        </div>
        <div>
          <label>Primary Business Activity</label>
          <input type="text" name="primary_business_activity" class="no-upper"
                 placeholder="Aerospace / Manufacturing / Trading">
        </div>
      </div>
      <label>Countries of Operation <span class="opt">(optional)</span></label>
      <input type="text" name="countries_of_operation" class="no-upper"
             placeholder="USA, UK, UAE, Singapore">
    </div>

    <label>Registered Office Address <span class="required">*</span></label>
    <textarea name="registered_address" required placeholder="Building, street, locality, area"></textarea>

    <div class="three-col">
      <div>
        <label>City <span class="required">*</span></label>
        <input type="text" name="city" required placeholder="Mumbai / New York">
      </div>
      <div>
        <label>PIN / Postal Code <span class="required">*</span></label>
        <input type="text" name="pin_code" maxlength="10" required placeholder="400001 / 10001">
      </div>
      <div>
        <label>State / Province</label>
        <input type="text" name="state" placeholder="Maharashtra / New York">
      </div>
    </div>

    <div id="overseas-country-row" style="{overseas_style}">
      <label>Country <span class="required">*</span></label>
      <input type="text" name="country" placeholder="UNITED STATES / UNITED KINGDOM">
    </div>

    <label>Principal Business Address <span class="opt">(if different from registered)</span></label>
    <textarea name="principal_business_address" placeholder="Leave blank if same as above"></textarea>

    <label>Nature / Description of Business <span class="required">*</span></label>
    <textarea name="nature_of_business" required placeholder="Brief description of main business activity, products, services"></textarea>

    <!-- ── B. AUTHORISED SIGNATORIES ── -->
    <div class="sec">B. Authorised Signatories &amp; Key Persons</div>

    <p class="sub-label">Authorised Signatory 1</p>
    <div class="two-col">
      <div>
        <label>Name <span class="required">*</span></label>
        <input type="text" name="contact_name" value="{contact_name}" required
               placeholder="Full name" class="no-upper">
      </div>
      <div>
        <label>Designation <span class="required">*</span></label>
        <input type="text" name="signatory1_designation" required
               placeholder="Director / CEO / Partner" class="no-upper">
      </div>
    </div>
    <div class="two-col">
      <div>
        <label>PAN (Signatory 1)</label>
        <input type="text" name="signatory1_pan" maxlength="10" placeholder="AAAAA0000A"
               oninput="this.value=this.value.toUpperCase().replace(/\\s/g,'')">
      </div>
      <div>
        <label>Aadhaar — last 4 digits <span class="opt">(optional)</span></label>
        <input type="text" name="signatory1_aadhaar" maxlength="4" placeholder="XXXX"
               oninput="this.value=this.value.replace(/[^0-9]/g,'')" class="no-upper">
        <p class="file-note">Last 4 digits only for identity verification</p>
      </div>
    </div>

    <p class="sub-label">Authorised Signatory 2 <span class="opt">(if applicable)</span></p>
    <div class="two-col">
      <div>
        <label>Name</label>
        <input type="text" name="signatory2_name" placeholder="Full name" class="no-upper">
      </div>
      <div>
        <label>Designation</label>
        <input type="text" name="signatory2_designation"
               placeholder="Director / CFO / Partner" class="no-upper">
      </div>
    </div>
    <div class="two-col">
      <div>
        <label>PAN (Signatory 2)</label>
        <input type="text" name="signatory2_pan" maxlength="10" placeholder="AAAAA0000A"
               oninput="this.value=this.value.toUpperCase().replace(/\\s/g,'')">
      </div>
      <div>
        <label>Aadhaar — last 4 digits <span class="opt">(optional)</span></label>
        <input type="text" name="signatory2_aadhaar" maxlength="4" placeholder="XXXX"
               oninput="this.value=this.value.replace(/[^0-9]/g,'')" class="no-upper">
      </div>
    </div>

    <label>Director / Partner / Trustee Name(s) <span class="opt">(optional)</span></label>
    <input type="text" name="director_names" placeholder="Names separated by comma" class="no-upper">

    <div class="two-col" style="margin-top:12px;">
      <div>
        <label>Ultimate Beneficial Owner (UBO) Name <span class="opt">(≥25% stake)</span></label>
        <input type="text" name="ubo_name" placeholder="Full name" class="no-upper">
      </div>
      <div>
        <label>UBO PAN</label>
        <input type="text" name="ubo_pan" maxlength="10" placeholder="AAAAA0000A"
               oninput="this.value=this.value.toUpperCase().replace(/\\s/g,'')">
      </div>
    </div>
    <div class="two-col">
      <div>
        <label>UBO Nationality</label>
        <input type="text" name="ubo_nationality"
               placeholder="Indian / American / etc." class="no-upper">
      </div>
    </div>

    <!-- ── Overseas: Authorised Signatory Passport Details ── -->
    <div id="overseas-signatory-section" style="{overseas_style}">
      <p class="sub-label">Authorised Signatory — Passport / ID Details <span class="opt">(overseas entities)</span></p>
      <div class="three-col">
        <div>
          <label>Nationality <span class="required">*</span></label>
          <input type="text" name="signatory1_nationality" placeholder="AMERICAN / BRITISH">
        </div>
        <div>
          <label>Date of Birth</label>
          <input type="date" name="signatory1_dob">
        </div>
        <div>
          <label>Passport / National ID No. <span class="required">*</span></label>
          <input type="text" name="signatory1_passport_id" placeholder="A12345678">
        </div>
      </div>
      <div class="two-col">
        <div>
          <label>Country of Residence</label>
          <input type="text" name="signatory1_country_of_residence" placeholder="UNITED STATES">
        </div>
        <div>
          <label>% Shareholding <span class="opt">(if applicable)</span></label>
          <input type="text" name="signatory1_shareholding_pct" placeholder="25%" class="no-upper">
        </div>
      </div>
    </div>

    <label>Politically Exposed Person (PEP)?</label>
    <div class="radio-group">
      <label><input type="radio" name="pep_status" value="none" checked> None</label>
      <label><input type="radio" name="pep_status" value="director_pep"> Director is PEP</label>
      <label><input type="radio" name="pep_status" value="ubo_pep"> UBO is PEP</label>
    </div>

    <!-- ── C. DOCUMENTS ── -->
    <div class="sec">C. Documents Submitted</div>

    <div id="gst-doc-section" style="{indian_style}">
      <label>GST Registration Certificate <span class="required">*</span></label>
      <input type="file" name="gst_certificate" accept=".pdf,.jpg,.jpeg,.png">
      <p class="file-note">PDF, JPG, or PNG. Max 10 MB.</p>
    </div>

    <label>Certificate of Incorporation <span class="required">*</span></label>
    <input type="file" name="incorporation_certificate" accept=".pdf,.jpg,.jpeg,.png" required>
    <p class="file-note">PDF, JPG, or PNG. Max 10 MB.</p>

    <label>MoA &amp; AoA <span class="opt">(optional — for Pvt Ltd / Ltd)</span></label>
    <input type="file" name="moa_aoa" accept=".pdf,.jpg,.jpeg,.png">
    <p class="file-note">Memorandum &amp; Articles of Association.</p>

    <!-- ── Overseas: Additional Documents ── -->
    <div id="overseas-docs-section" style="{overseas_style}">
      <label>Proof of Registered Business Address <span class="required">*</span></label>
      <input type="file" name="proof_of_address" accept=".pdf,.jpg,.jpeg,.png">
      <p class="file-note">Utility bill, bank statement, or official letter showing registered address. Max 10 MB.</p>
      <label>Valid Trade Licence / Business Permit <span class="opt">(if applicable)</span></label>
      <input type="file" name="trade_licence" accept=".pdf,.jpg,.jpeg,.png">
      <p class="file-note">PDF, JPG, or PNG. Max 10 MB.</p>
    </div>

    <label>Signatory ID Proof <span class="opt">(Aadhaar / PAN Card / Passport)</span></label>
    <input type="file" name="signatory_id_proof" accept=".pdf,.jpg,.jpeg,.png">

    <!-- ── D. BANK & FINANCIAL DETAILS ── -->
    <div class="sec">D. Bank &amp; Financial Details</div>

    <div class="two-col">
      <div>
        <label>Bank Name <span class="required">*</span></label>
        <input type="text" name="bank_name" required placeholder="HDFC Bank" class="no-upper">
      </div>
      <div>
        <label>Account Number <span class="required">*</span></label>
        <input type="text" name="account_number" required placeholder="00000000000000"
               oninput="this.value=this.value.replace(/[^0-9]/g,'')" class="no-upper">
      </div>
    </div>
    <div id="indian-bank-section" style="{indian_style}">
      <div class="two-col">
        <div>
          <label>IFSC Code <span class="required">*</span> <span class="badge">⚡ Auto-Verified</span></label>
          <input type="text" name="ifsc_code" maxlength="11" placeholder="HDFC0001234"
                 oninput="this.value=this.value.toUpperCase().replace(/\\s/g,'')">
        </div>
        <div>
          <label>Account Type <span class="required">*</span></label>
          <select name="account_type">
            <option value="">-- Select --</option>
            <option value="current">Current</option>
            <option value="savings">Savings</option>
            <option value="cc">Cash Credit</option>
            <option value="od">Overdraft</option>
          </select>
        </div>
      </div>
    </div>

    <div id="overseas-bank-section" style="{overseas_style}">
      <div class="two-col">
        <div>
          <label>SWIFT Code / BIC <span class="required">*</span></label>
          <input type="text" name="swift_code" maxlength="11" placeholder="BOFAUS3NXXX"
                 oninput="this.value=this.value.toUpperCase().replace(/\\s/g,'')">
        </div>
        <div>
          <label>IBAN / Account No. <span class="required">*</span></label>
          <input type="text" name="iban_number" placeholder="GB29NWBK60161331926819" class="no-upper">
        </div>
      </div>
      <div class="two-col">
        <div>
          <label>Bank Country <span class="required">*</span></label>
          <input type="text" name="bank_country" placeholder="UNITED STATES">
        </div>
        <div>
          <label>Account Currency <span class="required">*</span></label>
          <select name="account_currency">
            <option value="">-- Select --</option>
            <option value="USD">USD — US Dollar</option>
            <option value="EUR">EUR — Euro</option>
            <option value="GBP">GBP — British Pound</option>
            <option value="SGD">SGD — Singapore Dollar</option>
            <option value="AED">AED — UAE Dirham</option>
            <option value="INR">INR — Indian Rupee</option>
            <option value="other">Other</option>
          </select>
        </div>
      </div>
    </div>

    <label>Contact Number <span class="required">*</span></label>
    <input type="tel" name="contact_number" required placeholder="+91 9876543210" class="no-upper">

    <label>Bank Branch Address</label>
    <textarea name="bank_branch_address" placeholder="Full branch address"></textarea>

    <label>Annual Turnover <span class="required">*</span></label>
    <div class="radio-group">
      <label><input type="radio" name="annual_turnover" value="lt_1cr"> &lt; ₹1 Cr</label>
      <label><input type="radio" name="annual_turnover" value="1_10cr"> ₹1–10 Cr</label>
      <label><input type="radio" name="annual_turnover" value="10_50cr"> ₹10–50 Cr</label>
      <label><input type="radio" name="annual_turnover" value="50_100cr"> ₹50–100 Cr</label>
      <label><input type="radio" name="annual_turnover" value="gt_100cr"> &gt; ₹100 Cr</label>
    </div>

    <!-- ── F. ESCALATION CONTACT (Overseas only) ── -->
    <div id="overseas-escalation-section" style="{overseas_style}">
      <div class="sec">F. Escalation Contact</div>
      <div class="two-col">
        <div>
          <label>Full Name <span class="required">*</span></label>
          <input type="text" name="escalation_contact_name" placeholder="Full name" class="no-upper">
        </div>
        <div>
          <label>Job Title / Designation <span class="required">*</span></label>
          <input type="text" name="escalation_contact_title" placeholder="VP Operations / CFO" class="no-upper">
        </div>
      </div>
      <div class="two-col">
        <div>
          <label>Direct Email <span class="required">*</span></label>
          <input type="text" name="escalation_contact_email" placeholder="name@company.com" class="no-upper">
        </div>
        <div>
          <label>Direct Phone / Mobile <span class="required">*</span></label>
          <input type="tel" name="escalation_contact_phone" placeholder="+1 555 0100" class="no-upper">
        </div>
      </div>
      <div class="two-col">
        <div>
          <label>Department <span class="opt">(optional)</span></label>
          <input type="text" name="escalation_contact_dept" placeholder="Finance / Legal" class="no-upper">
        </div>
        <div>
          <label>Relationship to Company <span class="opt">(optional)</span></label>
          <input type="text" name="escalation_contact_relationship" placeholder="Employee / Director" class="no-upper">
        </div>
      </div>
    </div>

    <!-- ── G. DIRECTORS & KEY CONTROLLERS (Overseas only) ── -->
    <div id="overseas-directors-section" style="{overseas_style}">
      <div class="sec">G. Directors &amp; Key Controllers</div>
      <p class="sub-label">Director / Key Controller 1</p>
      <div class="three-col">
        <div>
          <label>Full Name <span class="required">*</span></label>
          <input type="text" name="director1_name" placeholder="Full legal name" class="no-upper">
        </div>
        <div>
          <label>Nationality <span class="required">*</span></label>
          <input type="text" name="director1_nationality" placeholder="AMERICAN / BRITISH">
        </div>
        <div>
          <label>Date of Birth</label>
          <input type="date" name="director1_dob">
        </div>
      </div>
      <div class="three-col">
        <div>
          <label>Passport / National ID No. <span class="required">*</span></label>
          <input type="text" name="director1_passport_id" placeholder="A12345678">
        </div>
        <div>
          <label>Country of Residence</label>
          <input type="text" name="director1_country_of_residence" placeholder="UNITED STATES">
        </div>
        <div>
          <label>% Shareholding <span class="opt">(optional)</span></label>
          <input type="text" name="director1_shareholding_pct" placeholder="25%" class="no-upper">
        </div>
      </div>
      <p class="sub-label">Director / Key Controller 2 <span class="opt">(if applicable)</span></p>
      <div class="three-col">
        <div>
          <label>Full Name</label>
          <input type="text" name="director2_name" placeholder="Full legal name" class="no-upper">
        </div>
        <div>
          <label>Nationality</label>
          <input type="text" name="director2_nationality" placeholder="BRITISH / SINGAPOREAN">
        </div>
        <div>
          <label>Date of Birth</label>
          <input type="date" name="director2_dob">
        </div>
      </div>
      <div class="three-col">
        <div>
          <label>Passport / National ID No.</label>
          <input type="text" name="director2_passport_id" placeholder="B98765432">
        </div>
        <div>
          <label>Country of Residence</label>
          <input type="text" name="director2_country_of_residence" placeholder="UNITED KINGDOM">
        </div>
        <div>
          <label>% Shareholding <span class="opt">(optional)</span></label>
          <input type="text" name="director2_shareholding_pct" placeholder="20%" class="no-upper">
        </div>
      </div>
    </div>

    <!-- ── H. REGULATORY & COMPLIANCE DECLARATIONS (Overseas only) ── -->
    <div id="overseas-compliance-section" style="{overseas_style}">
      <div class="sec">H. Regulatory &amp; Compliance Declarations</div>
      <label>Is the entity or any director subject to sanctions (UN, OFAC, EU, etc.)?</label>
      <div class="radio-group">
        <label><input type="radio" name="sanctions_check" value="no" checked> No</label>
        <label><input type="radio" name="sanctions_check" value="yes"> Yes</label>
      </div>
      <label>Is the entity under any criminal investigation or prosecution?</label>
      <div class="radio-group">
        <label><input type="radio" name="criminal_investigation_check" value="no" checked> No</label>
        <label><input type="radio" name="criminal_investigation_check" value="yes"> Yes</label>
      </div>
      <label>Is the entity regulated or licensed in its home country?</label>
      <div class="radio-group">
        <label><input type="radio" name="regulated_licensed" value="no" checked> No</label>
        <label><input type="radio" name="regulated_licensed" value="yes"> Yes</label>
      </div>
      <label>Regulator Name / Licence No. <span class="opt">(if regulated)</span></label>
      <input type="text" name="licensed_regulator" placeholder="FAA / EASA / CAA / FCA / etc." class="no-upper">
    </div>

    <!-- ── E. DECLARATION ── -->
    <div class="sec">E. Declaration &amp; Authorised Signatory</div>

    <div class="disclaimer">
      We hereby declare that all information provided above is true, accurate, and complete to the best of our
      knowledge. We authorise Jane Aerospace Pvt Ltd to verify the above details against government records
      (GST portal, MCA, PAN database) as required under RBI KYC Master Direction 2016 and PMLA 2002.
    </div>

    <label style="font-weight:400;display:flex;align-items:flex-start;gap:8px;cursor:pointer;">
      <input type="checkbox" name="declaration_agreed" value="yes" required
             style="width:auto;margin-top:2px;flex-shrink:0;">
      <span>I confirm the above declaration is true and accurate. I am authorised to submit this form
      on behalf of the entity. <span class="required">*</span></span>
    </label>

    <div style="margin-top:12px;color:#555;font-size:12px;">
      Submitting as: <strong>{contact_name}</strong>
    </div>

    <button type="button" onclick="kycReview()">Review &amp; Submit ›</button>
  </form>
</div>

<!-- ── Confirm Review Overlay ── -->
<div id="kyc-confirm-overlay" style="display:none;position:fixed;top:0;left:0;width:100%;height:100%;
     background:rgba(0,0,0,.5);z-index:999;overflow-y:auto;">
  <div style="background:#fff;max-width:680px;margin:40px auto;border-radius:12px;
               padding:36px 40px;position:relative;">
    <h2 style="color:#1a3a6b;margin:0 0 6px;font-size:20px;">Review Your KYC Details</h2>
    <p style="color:#555;font-size:13px;margin:0 0 24px;">
      Please verify all information before submitting. You cannot edit after submission.
    </p>
    <div id="kyc-review-body"></div>
    <div style="margin-top:28px;display:flex;gap:12px;flex-wrap:wrap;">
      <button type="button" onclick="kycEdit()"
        style="flex:1;background:#f3f4f6;color:#374151;border:1px solid #d1d5db;
               padding:12px 24px;border-radius:6px;font-size:15px;font-weight:600;cursor:pointer;">
        ← Edit Details
      </button>
      <button type="button" id="kyc-confirm-btn" onclick="kycSubmit()"
        style="flex:2;background:#1a56db;color:#fff;border:none;
               padding:12px 24px;border-radius:6px;font-size:15px;font-weight:600;cursor:pointer;">
        Confirm &amp; Submit KYC ›
      </button>
    </div>
  </div>
</div>

<script>
/* ── Section toggle ── */
function _sh(ids, show){{
  for(var i=0;i<ids.length;i++){{
    var el=document.getElementById(ids[i]);
    if(el) el.style.display=show?'':'none';
  }}
}}
function _req(names, required){{
  for(var i=0;i<names.length;i++){{
    var els=document.querySelectorAll('[name="'+names[i]+'"]');
    for(var j=0;j<els.length;j++) els[j].required=required;
  }}
}}
function toggleIndian(val){{
  var isIndian=(val==='indian');
  _sh(['indian-section','gst-doc-section','indian-bank-section'], isIndian);
  _sh(['overseas-section','overseas-country-row','overseas-signatory-section',
       'overseas-docs-section','overseas-bank-section','overseas-directors-section',
       'overseas-escalation-section','overseas-compliance-section'], !isIndian);
  if(isIndian){{
    _req(['gstin_number'], true);
    _req(['ifsc_code','account_type'], true);
    _req(['country_of_incorporation','company_reg_number','country_of_tax_residence','tax_id_tin',
          'country','swift_code','iban_number','bank_country','account_currency',
          'signatory1_nationality','signatory1_passport_id',
          'director1_name','director1_nationality','director1_passport_id',
          'escalation_contact_name','escalation_contact_title',
          'escalation_contact_email','escalation_contact_phone'], false);
  }} else {{
    _req(['gstin_number'], false);
    _req(['ifsc_code','account_type'], false);
    _req(['country_of_incorporation','company_reg_number','country_of_tax_residence','tax_id_tin',
          'country','swift_code','iban_number','bank_country','account_currency',
          'signatory1_nationality','signatory1_passport_id',
          'director1_name','director1_nationality','director1_passport_id',
          'escalation_contact_name','escalation_contact_title',
          'escalation_contact_email','escalation_contact_phone'], true);
  }}
}}

/* ── Real-time format validators ── */
var _GSTIN_RE = /^[0-9]{{2}}[A-Z]{{5}}[0-9]{{4}}[A-Z]{{1}}[1-9A-Z]{{1}}Z[0-9A-Z]{{1}}$/;
var _PAN_RE   = /^[A-Z]{{5}}[0-9]{{4}}[A-Z]{{1}}$/;
var _CIN_RE   = /^[LU][0-9]{{5}}[A-Z]{{2}}[0-9]{{4}}[A-Z]{{3}}[0-9]{{6}}$/;
var _IFSC_RE  = /^[A-Z]{{4}}0[A-Z0-9]{{6}}$/;

function _err(fieldName, msg){{
  var el = document.querySelector('[name="'+fieldName+'"]');
  if(!el) return;
  var errId = 'err_'+fieldName;
  var existing = document.getElementById(errId);
  if(msg){{
    el.style.borderColor='#dc2626';
    if(!existing){{
      var d=document.createElement('div');
      d.id=errId;
      d.style.cssText='color:#dc2626;font-size:11px;margin-top:3px;';
      el.parentNode.insertBefore(d, el.nextSibling);
    }}
    document.getElementById(errId).textContent=msg;
  }} else {{
    el.style.borderColor='#16a34a';
    if(existing) existing.remove();
  }}
}}

function validateGSTIN(val){{
  if(!val) return;
  var v=val.toUpperCase().replace(/\s/g,'');
  if(!_GSTIN_RE.test(v)) _err('gstin_number','Invalid GSTIN — must be 15 characters, e.g. 22AAAAA0000A1Z5');
  else _err('gstin_number','');
}}
function validatePAN(val){{
  if(!val) return;
  var v=val.toUpperCase().replace(/\s/g,'');
  if(!_PAN_RE.test(v)) _err('pan_number','Invalid PAN — must be 10 characters, e.g. AAAAA1234A');
  else _err('pan_number','');
}}
function validateCIN(val){{
  if(!val) return;
  var v=val.toUpperCase().replace(/\s/g,'');
  if(!_CIN_RE.test(v)) _err('cin_number','Invalid CIN — must be 21 characters, e.g. L12345AB1234ABC123456');
  else _err('cin_number','');
}}
function validateIFSC(val){{
  if(!val) return;
  var v=val.toUpperCase().replace(/\s/g,'');
  if(!_IFSC_RE.test(v)) _err('ifsc_code','Invalid IFSC — must be 11 characters, e.g. HDFC0001234');
  else _err('ifsc_code','');
}}

/* ── Confirm review screen ── */
function _fv(name){{
  var el=document.querySelector('[name="'+name+'"]');
  if(!el) return '—';
  return el.value||'—';
}}
function _row(label, val){{
  if(!val||val==='—') return '';
  return '<tr><td style="padding:7px 12px;background:#f8fafc;font-weight:600;font-size:13px;'+
         'color:#374151;width:38%;vertical-align:top;">'+label+'</td>'+
         '<td style="padding:7px 12px;font-size:13px;color:#111;">'+val+'</td></tr>';
}}
function kycReview(){{
  var form=document.getElementById('kyc-form');
  if(form&&!form.checkValidity()){{ form.reportValidity(); return; }}
  var isIndian=document.querySelector('input[name=company_type]:checked')&&
               document.querySelector('input[name=company_type]:checked').value==='indian';
  var rows='';
  rows+=_row('Company Name',_fv('company_name'));
  rows+=_row('Company Type',isIndian?'Indian Company':'Overseas Company');
  rows+=_row('Trade Name',_fv('trade_name'));
  rows+=_row('Contact Person',_fv('contact_name'));
  rows+=_row('Contact Number',_fv('contact_number'));
  if(isIndian){{
    rows+=_row('GSTIN',_fv('gstin_number'));
    rows+=_row('PAN',_fv('pan_number'));
    rows+=_row('CIN',_fv('cin_number'));
    rows+=_row('IFSC Code',_fv('ifsc_code'));
  }} else {{
    rows+=_row('Country of Incorporation',_fv('country_of_incorporation'));
    rows+=_row('Company Reg. No.',_fv('company_reg_number'));
    rows+=_row('Tax ID / TIN',_fv('tax_id_tin'));
    rows+=_row('LEI Number',_fv('lei_number'));
  }}
  rows+=_row('Bank Name',_fv('bank_name'));
  rows+=_row('Account Number',_fv('account_number'));
  rows+=_row('Annual Turnover',(function(){{
    var r=document.querySelector('input[name=annual_turnover]:checked');
    return r?r.value:'—';
  }})());
  document.getElementById('kyc-review-body').innerHTML=
    '<table style="width:100%;border-collapse:collapse;border:1px solid #e5e7eb;'+
    'border-radius:8px;overflow:hidden;">'+rows+'</table>';
  document.getElementById('kyc-confirm-overlay').style.display='block';
  document.body.style.overflow='hidden';
}}
function kycEdit(){{
  document.getElementById('kyc-confirm-overlay').style.display='none';
  document.body.style.overflow='';
}}
function kycSubmit(){{
  document.getElementById('kyc-confirm-btn').textContent='Submitting…';
  document.getElementById('kyc-confirm-btn').disabled=true;
  document.getElementById('kyc-form').submit();
}}

/* ── Attach live validators on load ── */
window.onload=function(){{
  var c=document.querySelector('input[name=company_type]:checked');
  if(c) toggleIndian(c.value);
  var g=document.querySelector('[name=gstin_number]');
  if(g) g.addEventListener('blur',function(){{validateGSTIN(this.value);}});
  var p=document.querySelector('[name=pan_number]');
  if(p) p.addEventListener('blur',function(){{validatePAN(this.value);}});
  var ci=document.querySelector('[name=cin_number]');
  if(ci) ci.addEventListener('blur',function(){{validateCIN(this.value);}});
  var ifs=document.querySelector('[name=ifsc_code]');
  if(ifs) ifs.addEventListener('blur',function(){{validateIFSC(this.value);}});
}};
</script>
</body>
</html>"""


def _kyc_submitted_page(company_name: str) -> str:
    return f"""<!DOCTYPE html>
<html><head><meta charset="UTF-8"><title>KYC Submitted</title>
<style>body{{font-family:Arial,sans-serif;background:#f4f6fb;display:flex;
align-items:center;justify-content:center;height:100vh;margin:0;}}
.box{{background:#fff;padding:40px;border-radius:12px;text-align:center;
max-width:480px;box-shadow:0 2px 16px rgba(0,0,0,.1);}}
h2{{color:#1a3a6b;}} p{{color:#444;line-height:1.6;}}
.check{{font-size:48px;color:#38a169;margin-bottom:16px;}}
</style></head>
<body><div class="box">
<div class="check">✓</div>
<h2>KYC Submitted Successfully</h2>
<p>Thank you for submitting your KYC documents for <strong>{company_name}</strong>.</p>
<p>Our team will review your submission and notify you via email within 1–2 business days.</p>
<p style="color:#888;font-size:13px;">You may close this window.</p>
</div></body></html>"""


def _kyc_already_done_page() -> str:
    return """<!DOCTYPE html>
<html><head><meta charset="UTF-8"><title>KYC Complete</title>
<style>body{{font-family:Arial,sans-serif;background:#f4f6fb;display:flex;
align-items:center;justify-content:center;height:100vh;margin:0;}}
.box{{background:#fff;padding:40px;border-radius:12px;text-align:center;
max-width:480px;box-shadow:0 2px 16px rgba(0,0,0,.1);}}
h2{{color:#1a3a6b;}} p{{color:#444;}}
</style></head>
<body><div class="box">
<h2>KYC Already Approved</h2>
<p>Your KYC verification is complete. No further action is needed here.</p>
</div></body></html>"""
