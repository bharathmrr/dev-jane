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
import uuid
from typing import Annotated
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import HTMLResponse, JSONResponse
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.db.base import CompanyType, DocumentStatus, KYCStatus
from app.db.models import KYCSubmission, LeadV2, OnboardingRecord
from app.db.session import get_db
from app.services.onboarding_email import (
    make_kyc_token,
    make_kyc_url,
    notify_team_kyc_submitted,
    notify_team_signed_doc_received,
    send_kyc_form_email,
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

    # Upload files to Zoho WorkDrive
    from app.services.zoho_workdrive import upload_file

    gst_zoho_id = None
    gst_filename = None
    inc_zoho_id = None
    inc_filename = None

    try:
        if gst_certificate:
            gst_bytes = await gst_certificate.read()
            gst_zoho_id = upload_file(
                gst_bytes,
                f"GST_{company_name}_{onboarding_id[:8]}.{gst_certificate.filename.rsplit('.', 1)[-1]}",
                mime_type=gst_certificate.content_type or "application/octet-stream",
            )
            gst_filename = gst_certificate.filename

        inc_bytes = await incorporation_certificate.read()
        inc_zoho_id = upload_file(
            inc_bytes,
            f"INC_{company_name}_{onboarding_id[:8]}.{incorporation_certificate.filename.rsplit('.', 1)[-1]}",
            mime_type=incorporation_certificate.content_type or "application/octet-stream",
        )
        inc_filename = incorporation_certificate.filename

    except Exception as exc:
        print(f"[KYC UPLOAD] Zoho WorkDrive upload failed: {exc}")
        # Store None file IDs — team will notice during review

    # Count existing submissions for attempt number
    existing_count = (await db.execute(
        select(KYCSubmission).where(KYCSubmission.onboarding_id == rec.id)
    )).scalars().all()
    attempt = len(existing_count) + 1

    submission = KYCSubmission(
        onboarding_id=rec.id,
        attempt_number=attempt,
        company_type=company_type,
        company_name=company_name,
        contact_name=contact_name,
        contact_number=contact_number,
        gst_certificate_zoho_id=gst_zoho_id,
        gst_certificate_filename=gst_filename,
        incorporation_zoho_id=inc_zoho_id,
        incorporation_filename=inc_filename,
    )
    db.add(submission)

    now = _now_ist()
    rec.kyc_status = KYCStatus.UNDER_REVIEW
    rec.kyc_submitted_at = now
    rec.kyc_status_display = f"KYC Submitted — Under Review (Attempt #{attempt}, {_fmt(now)})"
    rec.company_type = company_type
    await db.commit()

    # Notify team
    lead = await db.get(LeadV2, rec.lead_id)
    if lead:
        notify_team_kyc_submitted(lead.contact_name or "", lead.business_name, onboarding_id, attempt)

    # Export to sheets
    from app.workers.onboarding_tasks import export_onboarding_to_sheets
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
# KYC form HTML pages
# ---------------------------------------------------------------------------

def _kyc_form_html(submit_url: str, company_name: str, contact_name: str, preselect: str) -> str:
    indian_checked = 'checked' if preselect == 'indian' else ''
    overseas_checked = 'checked' if preselect == 'overseas' else ''
    indian_style = '' if preselect != 'overseas' else 'display:none'
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>KYC Form — Jane Aerospace</title>
<style>
  body{{font-family:Arial,sans-serif;background:#f4f6fb;margin:0;padding:24px;}}
  .card{{background:#fff;max-width:620px;margin:0 auto;border-radius:12px;
         box-shadow:0 2px 16px rgba(0,0,0,.1);padding:36px 40px;}}
  h1{{color:#1a3a6b;font-size:22px;margin-bottom:4px;}}
  .sub{{color:#555;font-size:14px;margin-bottom:28px;}}
  label{{display:block;font-size:14px;font-weight:600;margin:16px 0 4px;color:#222;}}
  input[type=text],input[type=tel]{{width:100%;padding:10px 12px;border:1px solid #ccc;
    border-radius:6px;font-size:14px;box-sizing:border-box;}}
  input[type=file]{{padding:8px 0;font-size:13px;}}
  .radio-group{{display:flex;gap:20px;margin:8px 0 16px;}}
  .radio-group label{{font-weight:400;margin:0;display:flex;align-items:center;gap:6px;cursor:pointer;}}
  .file-note{{color:#888;font-size:12px;margin-top:2px;}}
  button{{background:#1a56db;color:#fff;border:none;padding:13px 32px;border-radius:6px;
          font-size:15px;font-weight:600;cursor:pointer;margin-top:24px;width:100%;}}
  button:hover{{background:#1344b0;}}
  .required{{color:#e53e3e;}}
  #gst-section{{transition:all .3s;}}
  .logo{{font-size:18px;font-weight:700;color:#1a3a6b;margin-bottom:20px;}}
</style>
</head>
<body>
<div class="card">
  <div class="logo">✈ Jane Aerospace</div>
  <h1>KYC Verification Form</h1>
  <p class="sub">Please complete all fields. All information is kept confidential.</p>

  <form method="POST" action="{submit_url}" enctype="multipart/form-data">

    <label>Company Type <span class="required">*</span></label>
    <div class="radio-group">
      <label><input type="radio" name="company_type" value="indian" {indian_checked}
             onchange="toggleGST(this.value)"> Indian Company</label>
      <label><input type="radio" name="company_type" value="overseas" {overseas_checked}
             onchange="toggleGST(this.value)"> Overseas Company</label>
    </div>

    <label>Company Name <span class="required">*</span></label>
    <input type="text" name="company_name" value="{company_name}" required placeholder="e.g. Acme Aerospace Pvt Ltd">

    <label>Contact Name <span class="required">*</span></label>
    <input type="text" name="contact_name" value="{contact_name}" required placeholder="Full name of primary contact">

    <label>Contact Number <span class="required">*</span></label>
    <input type="tel" name="contact_number" required placeholder="+91 9876543210">

    <div id="gst-section" style="{indian_style}">
      <label>GST Certificate <span class="required">*</span></label>
      <input type="file" name="gst_certificate" accept=".pdf,.jpg,.jpeg,.png">
      <p class="file-note">PDF, JPG, or PNG. Max 10MB.</p>
    </div>

    <label>Company Incorporation Certificate <span class="required">*</span></label>
    <input type="file" name="incorporation_certificate" accept=".pdf,.jpg,.jpeg,.png" required>
    <p class="file-note">PDF, JPG, or PNG. Max 10MB.</p>

    <button type="submit">Submit KYC</button>
  </form>
</div>
<script>
function toggleGST(val){{
  var s = document.getElementById('gst-section');
  var f = s.querySelector('input[type=file]');
  if(val === 'indian'){{
    s.style.display = '';
    f.required = true;
  }} else {{
    s.style.display = 'none';
    f.required = false;
  }}
}}
// Auto-init on page load
window.onload = function(){{
  var checked = document.querySelector('input[name=company_type]:checked');
  if(checked) toggleGST(checked.value);
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
