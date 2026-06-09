"""Customer Onboarding Pipeline API endpoints.

Routes:
  POST /onboarding/start/{lead_id}                  — team initiates onboarding
  GET  /onboarding/list                             — all onboarding records
  GET  /onboarding/{onboarding_id}                  — single record detail
  GET  /onboarding/kyc/form/{onboarding_id}/{token} — lead fills KYC form (public, HMAC-signed)
  POST /onboarding/kyc/submit/{onboarding_id}/{token}— lead submits KYC form
  POST /onboarding/kyc/review/{onboarding_id}       — team approve/reject KYC
  GET  /onboarding/nda/preview/{onboarding_id}      — team views filled NDA
  POST /onboarding/nda/draft-review/{onboarding_id} — team approve/reject NDA draft
  GET  /onboarding/nda/signed/{onboarding_id}       — team views signed NDA from lead
  POST /onboarding/nda/sign-review/{onboarding_id}  — team approve/reject signed NDA
  POST /onboarding/nda/upload-template              — upload NDA template file
  GET  /onboarding/agreement/preview/{onboarding_id}
  POST /onboarding/agreement/draft-review/{onboarding_id}
  GET  /onboarding/agreement/signed/{onboarding_id}
  POST /onboarding/agreement/sign-review/{onboarding_id}
  POST /onboarding/agreement/upload-template
"""
from __future__ import annotations

import datetime as dt
import hashlib
import hmac as _hmac
import time
import uuid
from typing import Annotated
from zoneinfo import ZoneInfo

from fastapi import APIRouter, BackgroundTasks, Depends, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import HTMLResponse, JSONResponse
from pydantic import BaseModel
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.db.base import CompanyType, DocumentStatus, KYCStatus
from app.db.models import KYCSubmission, LeadV2, OnboardingRecord
from app.db.session import get_db
from app.services.onboarding_email import (
    make_action_url,
    make_kyc_token,
    make_kyc_url,
    notify_team_kyc_submitted,
    notify_team_signed_doc_received,
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
# WorkDrive: list & extract uploaded KYC documents
# ---------------------------------------------------------------------------

@router.get("/workdrive/files")
async def workdrive_list_files(folder_id: str | None = None):
    """List all files currently uploaded to the Zoho WorkDrive KYC folder.

    Pass ?folder_id=XXXX to inspect a specific sub-folder.
    Returns file name, size, upload time, and a direct download URL.
    """
    from app.services.zoho_workdrive import list_files, list_subfolders
    try:
        files = list_files(folder_id)
        subfolders = list_subfolders(folder_id)
        return {
            "folder_id": folder_id or "default (ZOHO_WORKDRIVE_FOLDER_ID)",
            "total_files": len(files),
            "total_subfolders": len(subfolders),
            "subfolders": subfolders,
            "files": files,
        }
    except Exception as exc:
        raise HTTPException(502, f"WorkDrive API error: {exc}")


@router.get("/workdrive/kyc-documents")
async def workdrive_kyc_documents(db: AsyncSession = Depends(get_db)):
    """Extract all KYC document references from the database
    and pair them with their Zoho WorkDrive download URLs.

    Returns every GST certificate and incorporation certificate
    that has been uploaded across all onboarding submissions.
    """
    from app.services.zoho_workdrive import get_download_url
    from app.db.models import KYCSubmission as KYCSubmissionModel

    rows = (await db.execute(
        select(KYCSubmissionModel).order_by(KYCSubmissionModel.created_at.desc())
    )).scalars().all()

    documents = []
    for sub in rows:
        entry: dict = {
            "onboarding_id": str(sub.onboarding_id),
            "submission_id": str(sub.id),
            "attempt": sub.attempt_number,
            "company": sub.company_name,
            "company_type": sub.company_type,
            "submitted_at": sub.created_at.isoformat() if sub.created_at else None,
            "gst_certificate": None,
            "incorporation_certificate": None,
        }
        if sub.gst_certificate_zoho_id:
            entry["gst_certificate"] = {
                "file_id": sub.gst_certificate_zoho_id,
                "filename": sub.gst_certificate_filename,
                "url": get_download_url(sub.gst_certificate_zoho_id),
            }
        if sub.incorporation_zoho_id:
            entry["incorporation_certificate"] = {
                "file_id": sub.incorporation_zoho_id,
                "filename": sub.incorporation_filename,
                "url": get_download_url(sub.incorporation_zoho_id),
            }
        documents.append(entry)

    return {
        "total_submissions": len(documents),
        "documents": documents,
    }


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
        "lead_status": lead.status,
        "onboarding_started": rec is not None,
    }
    if not rec:
        return base

    data = _serialize(rec)
    data.update(base)

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
        data["kyc_submission"] = {
            "company_type": latest_kyc.company_type,
            "company_name": latest_kyc.company_name,
            "contact_name": latest_kyc.contact_name,
            "contact_number": latest_kyc.contact_number,
            "gstin_number": latest_kyc.gstin_number,
            "pan_number": latest_kyc.pan_number,
            "cin_number": latest_kyc.cin_number,
            "kyc_verification_result": latest_kyc.kyc_verification_result,
            "auto_verified": latest_kyc.auto_verified,
            "has_gst": bool(latest_kyc.gst_certificate_zoho_id),
            "has_incorporation": bool(latest_kyc.incorporation_zoho_id),
            "attempt_number": latest_kyc.attempt_number,
            "reviewer_notes": latest_kyc.reviewer_notes,
        }

    data["nda_draft_content"] = rec.nda_draft_content
    data["agreement_draft_content"] = rec.agreement_draft_content
    data["nda_team_notes"] = rec.nda_team_notes
    data["agreement_team_notes"] = rec.agreement_team_notes
    return data


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


async def _kyc_post_submit_background(
    submission_id: str,
    onboarding_id: str,
    attempt: int,
    contact_name: str,
    company_name: str,
    gst_bytes: bytes | None,
    gst_filename: str | None,
    gst_content_type: str | None,
    inc_bytes: bytes,
    inc_filename: str,
    inc_content_type: str,
    company_name_for_file: str,
) -> None:
    """Upload KYC documents to WorkDrive and send team notification email.

    Runs after the HTTP response is already returned to the lead.
    Creates its own DB session to update file IDs on the submission.
    """
    from app.services.zoho_workdrive import upload_file
    from app.db.session import SessionLocal
    from app.db.models import KYCSubmission as _KYCSub
    import uuid as _uuid

    gst_zoho_id = None
    inc_zoho_id = None

    try:
        if gst_bytes and gst_filename:
            ext = gst_filename.rsplit(".", 1)[-1]
            gst_zoho_id = upload_file(
                gst_bytes,
                f"GST_{company_name_for_file}_{onboarding_id[:8]}.{ext}",
                mime_type=gst_content_type or "application/octet-stream",
            )
    except Exception as exc:
        print(f"[KYC UPLOAD] GST cert upload failed: {exc}")

    try:
        ext = inc_filename.rsplit(".", 1)[-1]
        inc_zoho_id = upload_file(
            inc_bytes,
            f"INC_{company_name_for_file}_{onboarding_id[:8]}.{ext}",
            mime_type=inc_content_type or "application/octet-stream",
        )
    except Exception as exc:
        print(f"[KYC UPLOAD] Inc cert upload failed: {exc}")

    if gst_zoho_id or inc_zoho_id:
        try:
            async with SessionLocal() as session:
                sub = await session.get(_KYCSub, _uuid.UUID(submission_id))
                if sub:
                    if gst_zoho_id:
                        sub.gst_certificate_zoho_id = gst_zoho_id
                    if inc_zoho_id:
                        sub.incorporation_zoho_id = inc_zoho_id
                    await session.commit()
        except Exception as exc:
            print(f"[KYC UPLOAD] DB file ID update failed: {exc}")

    # Claude Vision OCR — extract KYC fields from uploaded documents
    try:
        from app.services.kyc_ocr import extract_kyc_from_multiple_images
        _ocr_images = []
        if gst_bytes:
            _ocr_images.append({"data": gst_bytes, "mime_type": gst_content_type or "image/jpeg", "label": "GST Certificate"})
        _ocr_images.append({"data": inc_bytes, "mime_type": inc_content_type or "image/jpeg", "label": "Incorporation Certificate"})
        _ocr_result = extract_kyc_from_multiple_images(_ocr_images)
        async with SessionLocal() as _ocr_sess:
            _ocr_sub = await _ocr_sess.get(_KYCSub, _uuid.UUID(submission_id))
            if _ocr_sub:
                _vr = dict(_ocr_sub.kyc_verification_result or {})
                _vr["ocr_result"] = _ocr_result
                _ocr_sub.kyc_verification_result = _vr
                await _ocr_sess.commit()
    except Exception as exc:
        print(f"[KYC OCR] OCR extraction failed: {exc}")

    try:
        notify_team_kyc_submitted(contact_name, company_name, onboarding_id, attempt)
    except Exception as exc:
        print(f"[KYC NOTIFY] Team notification failed: {exc}")


@router.post("/kyc/submit/{onboarding_id}/{token}", response_class=HTMLResponse)
async def kyc_form_submit(
    onboarding_id: str,
    token: str,
    request: Request,
    background_tasks: BackgroundTasks,
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

    # Read file bytes now (fast async) — actual upload runs in background after response
    gst_bytes: bytes | None = None
    gst_filename: str | None = None
    gst_content_type: str | None = None
    if gst_certificate:
        gst_bytes = await gst_certificate.read()
        gst_filename = gst_certificate.filename
        gst_content_type = gst_certificate.content_type or "application/octet-stream"

    inc_bytes = await incorporation_certificate.read()
    inc_filename = incorporation_certificate.filename
    inc_content_type = incorporation_certificate.content_type or "application/octet-stream"

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

    lead = await db.get(LeadV2, rec.lead_id)
    lead_contact = lead.contact_name or "" if lead else ""
    lead_company = lead.business_name if lead else company_name

    # Upload docs + notify team after response is returned (non-blocking)
    background_tasks.add_task(
        _kyc_post_submit_background,
        submission_id, onboarding_id, attempt,
        lead_contact, lead_company,
        gst_bytes, gst_filename, gst_content_type,
        inc_bytes, inc_filename or "", inc_content_type,
        company_name,
    )

    # Auto-verification and sheet export via Celery
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

        from app.workers.onboarding_tasks import revise_nda_draft_task
        revise_nda_draft_task.delay(onboarding_id, body.notes)

        return {"message": "NDA revision triggered. Revised draft will appear in preview."}

    raise HTTPException(400, "action must be 'approve' or 'reject'")


# ---------------------------------------------------------------------------
# NDA signed copy review (team)
# ---------------------------------------------------------------------------

@router.get("/nda/signed/{onboarding_id}")
async def nda_signed_preview(onboarding_id: str, db: AsyncSession = Depends(get_db)):
    rec = await _get_onboarding(db, onboarding_id)
    from app.services.zoho_workdrive import get_download_url
    return {
        "nda_status": rec.nda_status,
        "nda_status_display": rec.nda_status_display,
        "signed_file_url": get_download_url(rec.nda_signed_zoho_file_id),
        "nda_signed_received_at": rec.nda_signed_received_at.isoformat() if rec.nda_signed_received_at else None,
    }


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

@router.post("/nda/upload-template")
async def upload_nda_template(
    company_type: str = Form(...),  # "indian" or "overseas"
    template_file: UploadFile = File(...),
):
    if company_type not in ("indian", "overseas"):
        raise HTTPException(400, "company_type must be 'indian' or 'overseas'")

    from app.services.zoho_workdrive import upload_file
    content = await template_file.read()
    file_id = upload_file(
        content,
        f"NDA_Template_{company_type}_{template_file.filename}",
        mime_type=template_file.content_type or "application/octet-stream",
    )

    # In production you'd persist this file_id to settings/DB
    return {"message": f"NDA template uploaded", "zoho_file_id": file_id, "company_type": company_type}


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

        from app.workers.onboarding_tasks import revise_agreement_draft_task
        revise_agreement_draft_task.delay(onboarding_id, body.notes)

        return {"message": "Agreement revision triggered."}

    raise HTTPException(400, "action must be 'approve' or 'reject'")


@router.get("/agreement/signed/{onboarding_id}")
async def agreement_signed_preview(onboarding_id: str, db: AsyncSession = Depends(get_db)):
    rec = await _get_onboarding(db, onboarding_id)
    from app.services.zoho_workdrive import get_download_url
    return {
        "agreement_status": rec.agreement_status,
        "agreement_status_display": rec.agreement_status_display,
        "signed_file_url": get_download_url(rec.agreement_signed_zoho_file_id),
        "signed_received_at": rec.agreement_signed_received_at.isoformat() if rec.agreement_signed_received_at else None,
    }


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


@router.post("/agreement/upload-template")
async def upload_agreement_template(
    company_type: str = Form(...),
    template_file: UploadFile = File(...),
):
    if company_type not in ("indian", "overseas"):
        raise HTTPException(400, "company_type must be 'indian' or 'overseas'")

    from app.services.zoho_workdrive import upload_file
    content = await template_file.read()
    file_id = upload_file(
        content,
        f"Agreement_Template_{company_type}_{template_file.filename}",
        mime_type=template_file.content_type or "application/octet-stream",
    )
    return {"message": "Agreement template uploaded", "zoho_file_id": file_id, "company_type": company_type}


# ---------------------------------------------------------------------------
# Zoho Contracts webhook — fired when a lead signs or declines
# ---------------------------------------------------------------------------

@router.post("/zoho-webhook")
async def zoho_contracts_webhook(request: Request, db: AsyncSession = Depends(get_db)):
    """Receive Zoho Contracts signature events.

    Expected payload structure (Zoho Contracts event):
      {
        "contract": {"id": "...", "status": "SIGNED" | "DECLINED" | ...},
        "event": "CONTRACT_SIGNED" | "CONTRACT_DECLINED" | ...
      }
    """
    from app.core.logging import get_logger
    log = get_logger("zoho_webhook")

    try:
        payload = await request.json()
    except Exception:
        payload = {}

    log.info("zoho_webhook_received", payload=payload)

    event = payload.get("event", "") or payload.get("type", "")
    contract = payload.get("contract", {}) or {}
    contract_id = str(contract.get("id", "") or payload.get("contractId", "") or "")

    if not contract_id:
        return {"status": "ignored", "reason": "no contract_id"}

    now = _now_ist()

    # Find the onboarding record that matches this contract_id
    nda_result = await db.execute(
        select(OnboardingRecord).where(OnboardingRecord.nda_zoho_contract_id == contract_id)
    )
    rec = nda_result.scalar_one_or_none()
    doc_type = "NDA" if rec else None

    if not rec:
        ag_result = await db.execute(
            select(OnboardingRecord).where(OnboardingRecord.agreement_zoho_contract_id == contract_id)
        )
        rec = ag_result.scalar_one_or_none()
        doc_type = "Agreement" if rec else None

    if not rec:
        log.warning("zoho_webhook_no_record", contract_id=contract_id)
        return {"status": "ignored", "reason": "contract not found"}

    lead = await db.get(LeadV2, rec.lead_id)

    signed = event.upper() in ("CONTRACT_SIGNED", "SIGNED", "COMPLETED")
    declined = event.upper() in ("CONTRACT_DECLINED", "DECLINED", "REJECTED")

    if signed:
        if doc_type == "NDA":
            rec.nda_status = DocumentStatus.SIGN_UNDER_REVIEW
            rec.nda_signed_received_at = now
            rec.nda_status_display = f"NDA Signed via Zoho Contracts — Pending Team Approval ({_fmt(now)})"
        else:
            rec.agreement_status = DocumentStatus.SIGN_UNDER_REVIEW
            rec.agreement_signed_received_at = now
            rec.agreement_status_display = f"Agreement Signed via Zoho Contracts — Pending Team Approval ({_fmt(now)})"

        await db.commit()
        log.info("zoho_webhook_signed_pending_review", doc_type=doc_type, contract_id=contract_id)

        if lead:
            from app.services.onboarding_email import notify_team_signed_doc_received
            notify_team_signed_doc_received(
                lead.contact_name or "", lead.business_name, str(rec.id),
                doc_type or "", contract_id=contract_id,
            )

            from app.services.zoho_crm import sync_onboarding_stage
            sync_onboarding_stage(
                email=lead.email,
                contact_name=lead.contact_name or lead.business_name,
                company_name=lead.business_name,
                stage=f"{doc_type} Signed — Pending Team Review",
                detail=f"{doc_type} signed via Zoho Contracts. Contract ID: {contract_id}",
                company_type=rec.company_type or "",
                onboarding_id=str(rec.id),
            )

        from app.workers.onboarding_tasks import export_onboarding_to_sheets
        export_onboarding_to_sheets.delay(str(rec.id))

    elif declined:
        if doc_type == "NDA":
            rec.nda_status = DocumentStatus.SENT_TO_LEAD
            rec.nda_status_display = f"NDA Declined by lead via Zoho Contracts ({_fmt(now)})"
        else:
            rec.agreement_status = DocumentStatus.SENT_TO_LEAD
            rec.agreement_status_display = f"Agreement Declined by lead via Zoho Contracts ({_fmt(now)})"

        await db.commit()
        log.info("zoho_webhook_declined", doc_type=doc_type, contract_id=contract_id)

    return {"status": "ok", "event": event, "doc_type": doc_type}


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

  <form method="POST" action="{submit_url}" enctype="multipart/form-data">

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

    <button type="submit">Submit KYC for Verification ›</button>
  </form>
</div>
<script>
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
window.onload=function(){{
  var c=document.querySelector('input[name=company_type]:checked');
  if(c) toggleIndian(c.value);
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
