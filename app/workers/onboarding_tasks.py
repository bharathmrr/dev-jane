"""Celery tasks for the customer onboarding pipeline.

Tasks:
  initiate_onboarding_task      — detect company type, create record, send KYC email
  send_kyc_rejection_task       — rejection email → lead
  auto_verify_kyc_task          — format-only KYC check, route to auto-approve or team review
  generate_nda_draft_task       — build NDA draft content, notify team to review
  send_nda_to_lead_task         — email NDA HTML to lead
  send_nda_sign_rejection_task  — rejection email for signed NDA
  generate_agreement_draft_task — build Agreement draft content, notify team
  send_agreement_to_lead_task   — email Agreement HTML to lead
  send_agreement_sign_rejection_task
  export_onboarding_to_sheets   — live Google Sheets export (single row per lead, all stages)
  sweep_onboarding_reminders    — daily beat task for overdue reminders
"""
from __future__ import annotations

import datetime as dt
import uuid
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.logging import get_logger
from app.core.pipeline_logger import log_pipeline
from app.db.base import CompanyType, DocumentStatus, KYCStatus
from app.db.models import KYCSubmission, LeadV2, OnboardingRecord
from app.workers.celery_app import celery_app
from app.workers.runtime import run_async

logger = get_logger("onboarding")

_IST = ZoneInfo("Asia/Kolkata")


def _now_ist() -> dt.datetime:
    return dt.datetime.now(_IST)


def _fmt(d: dt.datetime | None) -> str:
    if not d:
        return ""
    return d.astimezone(_IST).strftime("%d %b %Y %H:%M IST")


# ---------------------------------------------------------------------------
# Initiate onboarding
# ---------------------------------------------------------------------------

async def _initiate_onboarding(db: AsyncSession, lead_id: str) -> None:
    lead = await db.get(LeadV2, uuid.UUID(lead_id))
    if not lead:
        return

    existing = (await db.execute(
        select(OnboardingRecord).where(OnboardingRecord.lead_id == uuid.UUID(lead_id))
    )).scalar_one_or_none()
    if existing:
        return

    from app.services.onboarding_ai import detect_company_type
    company_type = detect_company_type(lead.business_name, lead.summary or "")

    from app.services.onboarding_email import make_kyc_token, make_kyc_url
    rec = OnboardingRecord(
        lead_id=uuid.UUID(lead_id),
        company_type=company_type,
        kyc_status=KYCStatus.FORM_SENT,
    )
    db.add(rec)
    await db.flush()

    token = make_kyc_token(str(rec.id))
    rec.kyc_form_token = token
    kyc_url = make_kyc_url(str(rec.id), token)

    now = _now_ist()
    rec.kyc_form_sent_at = now
    rec.kyc_status_display = f"KYC Form Sent ({_fmt(now)})"
    await db.commit()

    log_pipeline("ONBOARDING_STARTED", company=lead.business_name, email=lead.email,
                 detail=f"Company type: {company_type}")

    from app.services.onboarding_email import send_kyc_form_email
    send_kyc_form_email(lead.email, lead.contact_name or lead.business_name, lead.business_name, kyc_url)
    log_pipeline("KYC_FORM_SENT", company=lead.business_name, email=lead.email)

    try:
        from app.services.onboarding_email import notify_team_onboarding_started
        notify_team_onboarding_started(
            lead_name=lead.contact_name or lead.business_name,
            company_name=lead.business_name,
            lead_email=lead.email,
            onboarding_id=str(rec.id),
            company_type=company_type or "",
        )
    except Exception as _e:
        logger.warning("notify_team_onboarding_started_failed", error=str(_e))

    from app.services.zoho_crm import sync_onboarding_stage
    sync_onboarding_stage(
        email=lead.email,
        contact_name=lead.contact_name or lead.business_name,
        company_name=lead.business_name,
        stage="Onboarding Started",
        detail=f"KYC form sent. Company type: {company_type}",
        company_type=company_type,
        onboarding_id=str(rec.id),
    )

    await _export_to_sheets(db, str(rec.id))


@celery_app.task(bind=True, max_retries=3, default_retry_delay=30)
def initiate_onboarding_task(self, lead_id: str) -> None:
    try:
        run_async(_initiate_onboarding, lead_id)
    except Exception as exc:
        raise self.retry(exc=exc)


@celery_app.task(bind=True, max_retries=3, default_retry_delay=30)
def send_kyc_email_task(self, email: str, contact_name: str, business_name: str, kyc_url: str) -> None:
    try:
        from app.services.onboarding_email import send_kyc_form_email
        send_kyc_form_email(email, contact_name, business_name, kyc_url)
    except Exception as exc:
        raise self.retry(exc=exc)


# ---------------------------------------------------------------------------
# KYC: format verification
# ---------------------------------------------------------------------------

async def _auto_verify_kyc(db: AsyncSession, submission_id: str, onboarding_id: str) -> None:
    submission = await db.get(KYCSubmission, uuid.UUID(submission_id))
    if not submission:
        return

    rec = await db.get(OnboardingRecord, uuid.UUID(onboarding_id))
    if not rec:
        return

    extra = (submission.kyc_verification_result or {}).get("extra_fields", {})

    from app.services.kyc_verify import run_kyc_verification
    result = run_kyc_verification(
        company_type=submission.company_type,
        company_name=submission.company_name,
        gstin=submission.gstin_number,
        pan=submission.pan_number,
        cin=submission.cin_number,
        ifsc=extra.get("ifsc_code"),
        lei_number=extra.get("lei_number"),
        country_of_incorporation=extra.get("country_of_incorporation"),
        company_reg_number=extra.get("company_reg_number"),
        tax_id_tin=extra.get("tax_id_tin"),
    )

    submission.kyc_verification_result = {**(submission.kyc_verification_result or {}), **result}
    submission.auto_verified = True
    now = _now_ist()

    lead = await db.get(LeadV2, rec.lead_id)
    issues_txt = "; ".join(result.get("issues", []))

    log_pipeline("KYC_SUBMITTED", company=submission.company_name,
                 detail=f"GSTIN:{submission.gstin_number or '—'} PAN:{submission.pan_number or '—'}")

    # Push all KYC fields to CRM immediately (regardless of pass/fail)
    if lead:
        try:
            from app.services.zoho_crm import sync_kyc_data
            kyc_payload = {
                "gstin":                  submission.gstin_number or "",
                "pan":                    submission.pan_number or "",
                "cin":                    submission.cin_number or "",
                "ifsc":                   extra.get("ifsc_code", ""),
                "bank_name":              extra.get("bank_name", ""),
                "registered_address":     extra.get("registered_address", ""),
                "city":                   extra.get("city", ""),
                "state":                  extra.get("state", ""),
                "nature_of_business":     extra.get("nature_of_business", ""),
                "entity_type":            extra.get("entity_type", ""),
                "date_of_incorporation":  extra.get("date_of_incorporation", ""),
                "annual_turnover":        extra.get("annual_turnover", ""),
                "designation":            extra.get("signatory1_designation", ""),
                "country_of_incorporation": extra.get("country_of_incorporation", ""),
                "company_reg_number":     extra.get("company_reg_number", ""),
                "tax_id_tin":             extra.get("tax_id_tin", ""),
                "lei_number":             extra.get("lei_number", ""),
            }
            sync_kyc_data(lead.email, submission.company_name, kyc_payload)
        except Exception as _kce:
            logger.warning("crm_kyc_sync_failed", error=str(_kce))

    if result.get("overall_passed"):
        rec.kyc_status = KYCStatus.UNDER_REVIEW
        rec.kyc_submitted_at = now
        rec.kyc_status_display = f"KYC Format Valid — Under Team Review ({_fmt(now)})"
        await db.commit()

        log_pipeline("KYC_FORMAT_VALID", company=submission.company_name,
                     detail="All format checks passed — pending team review")

        _send_kyc_reviewer_email(submission, result, lead, onboarding_id)

        if lead:
            from app.services.zoho_crm import sync_onboarding_stage
            sync_onboarding_stage(
                email=lead.email,
                contact_name=lead.contact_name or submission.company_name,
                company_name=submission.company_name,
                stage="KYC Submitted",
                detail="KYC format valid — pending team review",
                phone=submission.contact_number or "",
                company_type=submission.company_type,
                onboarding_id=onboarding_id,
            )
    else:
        rec.kyc_status_display = (
            f"KYC Format Issues ({_fmt(now)}): {issues_txt[:200]}"
        )
        await db.commit()

        log_pipeline("KYC_FORMAT_ISSUES", company=submission.company_name,
                     detail=f"Issues: {issues_txt[:200]}")

        _send_kyc_reviewer_email(submission, result, lead, onboarding_id)

        if lead:
            from app.services.zoho_crm import sync_onboarding_stage
            sync_onboarding_stage(
                email=lead.email,
                contact_name=lead.contact_name or submission.company_name,
                company_name=submission.company_name,
                stage="KYC Issues Found",
                detail=f"Issues: {issues_txt[:300]}",
                phone=submission.contact_number or "",
                company_type=submission.company_type,
                onboarding_id=onboarding_id,
            )

    await _export_to_sheets(db, onboarding_id)


def _send_kyc_reviewer_email(submission, result: dict, lead, onboarding_id: str) -> None:
    try:
        from app.services.onboarding_email import _send_to_reviewers, make_kyc_view_url

        view_url = make_kyc_view_url(onboarding_id)
        passed = result.get("overall_passed", False)
        issues = result.get("issues", [])

        status_color = "#16a34a" if passed else "#dc2626"
        status_label = "All Format Checks Passed" if passed else f"{len(issues)} Format Issue(s) Found"

        issues_summary = ""
        if issues:
            items = "".join(f"<li style='color:#991b1b;font-size:13px;margin-bottom:3px;'>{i}</li>" for i in issues)
            issues_summary = f"<ul style='margin:8px 0;padding-left:20px;'>{items}</ul>"

        company_type = (submission.company_type or "indian").title()
        lead_email = lead.email if lead else "—"

        html = f"""
        <div style="font-family:Arial,sans-serif;font-size:14px;color:#222;max-width:580px;">
          <h2 style="margin:0 0 4px;color:#1a3a6b;">KYC Submission Received</h2>
          <p style="color:#555;font-size:13px;margin:0 0 18px;">
            A new KYC form has been submitted and requires your review.
          </p>
          <table style="border-collapse:collapse;width:100%;font-size:13px;
                        border:1px solid #e5e7eb;border-radius:8px;overflow:hidden;margin-bottom:16px;">
            <tr style="background:#f0f4ff;">
              <td style="padding:8px 14px;font-weight:600;width:38%;">Company</td>
              <td style="padding:8px 14px;">{submission.company_name}</td>
            </tr>
            <tr>
              <td style="padding:8px 14px;font-weight:600;color:#555;">Company Type</td>
              <td style="padding:8px 14px;">{company_type}</td>
            </tr>
            <tr style="background:#f0f4ff;">
              <td style="padding:8px 14px;font-weight:600;color:#555;">Contact</td>
              <td style="padding:8px 14px;">{submission.contact_name or '—'} | {submission.contact_number or '—'}</td>
            </tr>
            <tr>
              <td style="padding:8px 14px;font-weight:600;color:#555;">Lead Email</td>
              <td style="padding:8px 14px;">{lead_email}</td>
            </tr>
            <tr style="background:#f0f4ff;">
              <td style="padding:8px 14px;font-weight:600;color:#555;">Format Check</td>
              <td style="padding:8px 14px;">
                <span style="background:{status_color};color:#fff;padding:3px 10px;
                  border-radius:4px;font-size:12px;font-weight:700;">{status_label}</span>
              </td>
            </tr>
          </table>
          {issues_summary}
          <p style="margin:20px 0 10px;font-size:14px;color:#374151;">
            Click below to view all submitted details and take action:
          </p>
          <a href="{view_url}"
             style="display:inline-block;background:#1a56db;color:#fff;padding:12px 28px;
                    border-radius:7px;text-decoration:none;font-weight:700;font-size:15px;">
            View KYC Details &amp; Review →
          </a>
          <p style="margin-top:20px;color:#aaa;font-size:11px;">
            Onboarding ID: {onboarding_id[:8]}… &nbsp;|&nbsp;
            Approve / Reject buttons are on the review page.
          </p>
        </div>"""

        _send_to_reviewers(f"KYC Review Required — {submission.company_name}", html)
    except Exception as _e:
        logger.warning("kyc_reviewer_email_failed", error=str(_e))


@celery_app.task(bind=True, max_retries=2, default_retry_delay=60)
def auto_verify_kyc_task(self, submission_id: str, onboarding_id: str) -> None:
    try:
        run_async(_auto_verify_kyc, submission_id, onboarding_id)
    except Exception as exc:
        raise self.retry(exc=exc)


# ---------------------------------------------------------------------------
# KYC: rejection email
# ---------------------------------------------------------------------------

async def _send_kyc_rejection(
    db: AsyncSession, onboarding_id: str, notes: str, attempt_number: int
) -> None:
    rec = await db.get(OnboardingRecord, uuid.UUID(onboarding_id))
    if not rec:
        return
    lead = await db.get(LeadV2, rec.lead_id)
    if not lead:
        return

    from app.services.onboarding_ai import generate_kyc_rejection_email
    email_body = generate_kyc_rejection_email(notes, lead.contact_name or "", lead.business_name, attempt_number)

    from app.services.onboarding_email import make_kyc_token, make_kyc_url, send_kyc_rejected_email
    token = rec.kyc_form_token or make_kyc_token(onboarding_id)
    kyc_url = make_kyc_url(onboarding_id, token)
    send_kyc_rejected_email(lead.email, lead.contact_name or "", lead.business_name, email_body, kyc_url)


@celery_app.task(bind=True, max_retries=3, default_retry_delay=30)
def send_kyc_rejection_task(self, onboarding_id: str, notes: str, attempt_number: int) -> None:
    try:
        run_async(_send_kyc_rejection, onboarding_id, notes, attempt_number)
    except Exception as exc:
        raise self.retry(exc=exc)


# ---------------------------------------------------------------------------
# NDA: generate draft
# ---------------------------------------------------------------------------

async def _generate_nda_draft(db: AsyncSession, onboarding_id: str) -> None:
    rec = await db.get(OnboardingRecord, uuid.UUID(onboarding_id))
    if not rec:
        return

    kyc_result = await db.execute(
        select(KYCSubmission)
        .where(KYCSubmission.onboarding_id == rec.id)
        .order_by(KYCSubmission.attempt_number.desc())
    )
    kyc = kyc_result.scalars().first()
    if not kyc:
        return

    lead = await db.get(LeadV2, rec.lead_id)

    company_type = rec.company_type or "indian"
    is_overseas = company_type.upper() in ("OVERSEAS", "FOREIGN", "INTERNATIONAL")
    now = _now_ist()
    company_name = kyc.company_name
    nda_label = "Overseas NDA" if is_overseas else "Indian NDA"

    extra = (kyc.kyc_verification_result or {}).get("extra_fields", {})

    # Pre-fill the official PDF template fields from KYC and store for the team editor
    import json as _json

    from app.services.onboarding_email import make_doc_edit_url
    from app.services.pdf_documents import default_replacements, kyc_fields_from_submission

    fields = kyc_fields_from_submission(
        company_name=kyc.company_name,
        cin_number=kyc.cin_number,
        extra=extra or {},
        is_overseas=is_overseas,
    )
    doc_data = {
        "replacements": default_replacements("nda", fields),
        "signatory_name": kyc.contact_name or (lead.contact_name if lead else "") or company_name,
        "signatory_email": lead.email if lead else "",
    }
    rec.nda_draft_content = _json.dumps(doc_data, ensure_ascii=False)
    view_link = make_doc_edit_url(onboarding_id, "nda")
    contract_id = ""
    log_pipeline("NDA_DRAFT_FILLED", company=company_name,
                 detail=f"{nda_label} PDF pre-filled from KYC — pending team review")

    rec.nda_status = DocumentStatus.TEAM_REVIEW
    rec.nda_status_display = f"NDA Ready — Pending Team Review ({_fmt(now)})"

    log_pipeline("NDA_READY", company=company_name,
                 detail=f"NDA ({nda_label}) draft ready for team review")

    await db.commit()
    await _export_to_sheets(db, onboarding_id)

    try:
        from app.services.onboarding_email import notify_team_nda_draft_ready
        notify_team_nda_draft_ready(
            lead_name=(lead.contact_name or company_name) if lead else company_name,
            company_name=company_name,
            onboarding_id=onboarding_id,
            preview_url=view_link,
            contract_id=contract_id,
        )
    except Exception as _e:
        logger.warning("notify_team_nda_draft_ready_failed", error=str(_e))


@celery_app.task(bind=True, max_retries=3, default_retry_delay=30)
def generate_nda_draft_task(self, onboarding_id: str) -> None:
    try:
        run_async(_generate_nda_draft, onboarding_id)
    except Exception as exc:
        raise self.retry(exc=exc)


# ---------------------------------------------------------------------------
# NDA: send to lead
# ---------------------------------------------------------------------------

async def _send_nda_to_lead(db: AsyncSession, onboarding_id: str) -> None:
    rec = await db.get(OnboardingRecord, uuid.UUID(onboarding_id))
    if not rec:
        return
    lead = await db.get(LeadV2, rec.lead_id)
    if not lead:
        return

    now = _now_ist()
    company_type = rec.company_type or "indian"
    is_overseas = company_type.upper() in ("OVERSEAS", "FOREIGN", "INTERNATIONAL")

    kyc_result = await db.execute(
        select(KYCSubmission)
        .where(KYCSubmission.onboarding_id == rec.id)
        .order_by(KYCSubmission.attempt_number.desc())
    )
    kyc = kyc_result.scalars().first()
    extra = (kyc.kyc_verification_result or {}).get("extra_fields", {}) if kyc else {}

    company_name = (kyc.company_name if kyc else None) or lead.business_name
    contact_name = (kyc.contact_name if kyc else None) or lead.contact_name or lead.business_name

    # Ensure PDF field data exists (built at draft stage; rebuild if missing)
    import json as _json

    from app.services.onboarding_email import make_doc_sign_url, send_document_sign_email
    from app.services.pdf_documents import default_replacements, kyc_fields_from_submission

    doc_data = None
    if rec.nda_draft_content:
        try:
            _d = _json.loads(rec.nda_draft_content)
            if isinstance(_d, dict) and "replacements" in _d:
                doc_data = _d
        except (ValueError, TypeError):
            pass
    if doc_data is None:
        fields = kyc_fields_from_submission(
            company_name=company_name,
            cin_number=kyc.cin_number if kyc else None,
            extra=extra or {},
            is_overseas=is_overseas,
        )
        doc_data = {
            "replacements": default_replacements("nda", fields),
            "signatory_name": contact_name,
            "signatory_email": lead.email,
        }
        rec.nda_draft_content = _json.dumps(doc_data, ensure_ascii=False)

    sign_url = make_doc_sign_url(onboarding_id, "nda")
    send_document_sign_email(
        to_email=str(doc_data.get("signatory_email") or lead.email),
        lead_name=str(doc_data.get("signatory_name") or contact_name),
        company_name=company_name,
        doc_type="nda",
        sign_url=sign_url,
    )
    contract_id = ""

    rec.nda_status = DocumentStatus.SENT_TO_LEAD
    rec.nda_sent_at = now
    rec.nda_status_display = f"NDA Sent to Lead ({_fmt(now)}) — Awaiting E-Signature"
    await db.commit()

    log_pipeline("NDA_SENT", company=company_name, email=lead.email,
                 detail="NDA signing link emailed to lead.")

    await _export_to_sheets(db, onboarding_id)

    from app.services.zoho_crm import sync_onboarding_stage
    sync_onboarding_stage(
        email=lead.email,
        contact_name=contact_name,
        company_name=company_name,
        stage="NDA Sent for E-Sign",
        detail="NDA sent via email for signature.",
        company_type=rec.company_type or "",
        onboarding_id=onboarding_id,
        nda_contract_id=contract_id or "",
    )


@celery_app.task(bind=True, max_retries=3, default_retry_delay=30)
def send_nda_to_lead_task(self, onboarding_id: str) -> None:
    try:
        run_async(_send_nda_to_lead, onboarding_id)
    except Exception as exc:
        raise self.retry(exc=exc)


# ---------------------------------------------------------------------------
# NDA: sign rejection
# ---------------------------------------------------------------------------

async def _send_nda_sign_rejection(db: AsyncSession, onboarding_id: str, notes: str) -> None:
    rec = await db.get(OnboardingRecord, uuid.UUID(onboarding_id))
    if not rec:
        return
    lead = await db.get(LeadV2, rec.lead_id)
    if not lead:
        return

    from app.services.onboarding_ai import generate_sign_rejection_email
    body = generate_sign_rejection_email(notes, lead.contact_name or "", lead.business_name, "NDA")

    from app.services.onboarding_email import send_nda_sign_rejection
    send_nda_sign_rejection(lead.email, lead.contact_name or "", lead.business_name, body)


@celery_app.task(bind=True, max_retries=3, default_retry_delay=30)
def send_nda_sign_rejection_task(self, onboarding_id: str, notes: str) -> None:
    try:
        run_async(_send_nda_sign_rejection, onboarding_id, notes)
    except Exception as exc:
        raise self.retry(exc=exc)


# ---------------------------------------------------------------------------
# Customer Agreement: generate draft
# ---------------------------------------------------------------------------

async def _generate_agreement_draft(db: AsyncSession, onboarding_id: str) -> None:
    rec = await db.get(OnboardingRecord, uuid.UUID(onboarding_id))
    if not rec:
        return

    kyc_result = await db.execute(
        select(KYCSubmission)
        .where(KYCSubmission.onboarding_id == rec.id)
        .order_by(KYCSubmission.attempt_number.desc())
    )
    kyc = kyc_result.scalars().first()
    if not kyc:
        return

    lead = await db.get(LeadV2, rec.lead_id)

    company_type = rec.company_type or "indian"
    is_overseas = company_type.upper() in ("OVERSEAS", "FOREIGN", "INTERNATIONAL")
    now = _now_ist()
    company_name = kyc.company_name

    extra = (kyc.kyc_verification_result or {}).get("extra_fields", {})

    # Pre-fill the official PDF template fields from KYC and store for the team editor
    import json as _json

    from app.services.onboarding_email import make_doc_edit_url
    from app.services.pdf_documents import default_replacements, kyc_fields_from_submission

    fields = kyc_fields_from_submission(
        company_name=kyc.company_name,
        cin_number=kyc.cin_number,
        extra=extra or {},
        is_overseas=is_overseas,
    )
    doc_data = {
        "replacements": default_replacements("agreement", fields),
        "signatory_name": kyc.contact_name or (lead.contact_name if lead else "") or company_name,
        "signatory_email": lead.email if lead else "",
    }
    rec.agreement_draft_content = _json.dumps(doc_data, ensure_ascii=False)
    agr_view_link = make_doc_edit_url(onboarding_id, "agreement")
    agr_contract_id = ""
    log_pipeline("AGREEMENT_DRAFT_FILLED", company=company_name,
                 detail="Supply Agreement PDF pre-filled from KYC — pending team review")

    rec.agreement_status = DocumentStatus.TEAM_REVIEW
    rec.agreement_status_display = f"Agreement Ready — Pending Team Review ({_fmt(now)})"

    log_pipeline("AGREEMENT_READY", company=company_name,
                 detail="Customer Agreement draft ready for team review")

    await db.commit()
    await _export_to_sheets(db, onboarding_id)

    try:
        from app.services.onboarding_email import notify_team_agreement_draft_ready
        notify_team_agreement_draft_ready(
            lead_name=(lead.contact_name or company_name) if lead else company_name,
            company_name=company_name,
            onboarding_id=onboarding_id,
            preview_url=agr_view_link,
            contract_id=agr_contract_id,
        )
    except Exception:
        pass


@celery_app.task(bind=True, max_retries=3, default_retry_delay=30)
def generate_agreement_draft_task(self, onboarding_id: str) -> None:
    try:
        run_async(_generate_agreement_draft, onboarding_id)
    except Exception as exc:
        raise self.retry(exc=exc)


# ---------------------------------------------------------------------------
# Customer Agreement: send to lead
# ---------------------------------------------------------------------------

async def _send_agreement_to_lead(db: AsyncSession, onboarding_id: str) -> None:
    rec = await db.get(OnboardingRecord, uuid.UUID(onboarding_id))
    if not rec:
        return
    lead = await db.get(LeadV2, rec.lead_id)
    if not lead:
        return

    now = _now_ist()
    company_type = rec.company_type or "indian"
    is_overseas = company_type.upper() in ("OVERSEAS", "FOREIGN", "INTERNATIONAL")

    kyc_result = await db.execute(
        select(KYCSubmission)
        .where(KYCSubmission.onboarding_id == rec.id)
        .order_by(KYCSubmission.attempt_number.desc())
    )
    kyc = kyc_result.scalars().first()
    extra = (kyc.kyc_verification_result or {}).get("extra_fields", {}) if kyc else {}

    company_name = (kyc.company_name if kyc else None) or lead.business_name
    contact_name = (kyc.contact_name if kyc else None) or lead.contact_name or lead.business_name

    # Ensure PDF field data exists (built at draft stage; rebuild if missing)
    import json as _json

    from app.services.onboarding_email import make_doc_sign_url, send_document_sign_email
    from app.services.pdf_documents import default_replacements, kyc_fields_from_submission

    doc_data = None
    if rec.agreement_draft_content:
        try:
            _d = _json.loads(rec.agreement_draft_content)
            if isinstance(_d, dict) and "replacements" in _d:
                doc_data = _d
        except (ValueError, TypeError):
            pass
    if doc_data is None:
        fields = kyc_fields_from_submission(
            company_name=company_name,
            cin_number=kyc.cin_number if kyc else None,
            extra=extra or {},
            is_overseas=is_overseas,
        )
        doc_data = {
            "replacements": default_replacements("agreement", fields),
            "signatory_name": contact_name,
            "signatory_email": lead.email,
        }
        rec.agreement_draft_content = _json.dumps(doc_data, ensure_ascii=False)

    sign_url = make_doc_sign_url(onboarding_id, "agreement")
    send_document_sign_email(
        to_email=str(doc_data.get("signatory_email") or lead.email),
        lead_name=str(doc_data.get("signatory_name") or contact_name),
        company_name=company_name,
        doc_type="agreement",
        sign_url=sign_url,
    )
    agr_contract_id = ""

    rec.agreement_status = DocumentStatus.SENT_TO_LEAD
    rec.agreement_sent_at = now
    rec.agreement_status_display = f"Customer Agreement Sent ({_fmt(now)}) — Awaiting E-Signature"
    await db.commit()

    log_pipeline("AGREEMENT_SENT", company=company_name, email=lead.email,
                 detail="Agreement signing link emailed to lead.")

    await _export_to_sheets(db, onboarding_id)

    from app.services.zoho_crm import sync_onboarding_stage
    sync_onboarding_stage(
        email=lead.email,
        contact_name=contact_name,
        company_name=company_name,
        stage="Agreement Sent for E-Sign",
        detail="Customer Agreement sent via email for signature.",
        company_type=rec.company_type or "",
        onboarding_id=onboarding_id,
        agreement_contract_id=agr_contract_id or "",
    )


@celery_app.task(bind=True, max_retries=3, default_retry_delay=30)
def send_agreement_to_lead_task(self, onboarding_id: str) -> None:
    try:
        run_async(_send_agreement_to_lead, onboarding_id)
    except Exception as exc:
        raise self.retry(exc=exc)


# ---------------------------------------------------------------------------
# Customer Agreement: sign rejection
# ---------------------------------------------------------------------------

async def _send_agreement_sign_rejection(db: AsyncSession, onboarding_id: str, notes: str) -> None:
    rec = await db.get(OnboardingRecord, uuid.UUID(onboarding_id))
    if not rec:
        return
    lead = await db.get(LeadV2, rec.lead_id)
    if not lead:
        return

    from app.services.onboarding_ai import generate_sign_rejection_email
    body = generate_sign_rejection_email(notes, lead.contact_name or "", lead.business_name, "Customer Agreement")

    from app.services.onboarding_email import send_agreement_sign_rejection
    send_agreement_sign_rejection(lead.email, lead.contact_name or "", lead.business_name, body)


@celery_app.task(bind=True, max_retries=3, default_retry_delay=30)
def send_agreement_sign_rejection_task(self, onboarding_id: str, notes: str) -> None:
    try:
        run_async(_send_agreement_sign_rejection, onboarding_id, notes)
    except Exception as exc:
        raise self.retry(exc=exc)


# ---------------------------------------------------------------------------
# Daily reminder sweep (beat task)
# ---------------------------------------------------------------------------

async def _sweep_reminders(db: AsyncSession) -> int:
    now = _now_ist()
    sent = 0

    result = await db.execute(select(OnboardingRecord))
    records = result.scalars().all()

    for rec in records:
        lead = await db.get(LeadV2, rec.lead_id)
        if not lead:
            continue

        # KYC reminder
        if rec.kyc_status in (KYCStatus.FORM_SENT, KYCStatus.REJECTED):
            last = rec.kyc_last_followup_at or rec.kyc_form_sent_at
            if last and (now - last).total_seconds() >= 86400:
                days = (now - rec.kyc_form_sent_at).days if rec.kyc_form_sent_at else 1
                rec.kyc_followup_count = (rec.kyc_followup_count or 0) + 1
                rec.kyc_last_followup_at = now
                rec.kyc_status_display = f"KYC Follow-up #{rec.kyc_followup_count} — Day {days} ({_fmt(now)})"

                from app.services.onboarding_ai import generate_kyc_reminder_email
                from app.services.onboarding_email import make_kyc_token, make_kyc_url, send_kyc_reminder_email
                token = rec.kyc_form_token or make_kyc_token(str(rec.id))
                kyc_url = make_kyc_url(str(rec.id), token)
                body = generate_kyc_reminder_email(
                    lead.contact_name or "", lead.business_name, rec.kyc_followup_count, days
                )
                send_kyc_reminder_email(
                    lead.email, lead.contact_name or "", lead.business_name,
                    kyc_url, rec.kyc_followup_count, body
                )
                log_pipeline("KYC_REMINDER_SENT", company=lead.business_name, email=lead.email,
                             detail=f"Follow-up #{rec.kyc_followup_count} — Day {days}")
                sent += 1

        # NDA reminder
        if rec.nda_status == DocumentStatus.SENT_TO_LEAD:
            last = rec.nda_last_followup_at or rec.nda_sent_at
            if last and (now - last).total_seconds() >= 86400:
                days = (now - rec.nda_sent_at).days if rec.nda_sent_at else 1
                rec.nda_followup_count = (rec.nda_followup_count or 0) + 1
                rec.nda_last_followup_at = now
                rec.nda_status_display = f"NDA Follow-up #{rec.nda_followup_count} — Day {days} ({_fmt(now)})"

                from app.services.onboarding_ai import generate_doc_reminder_email
                from app.services.onboarding_email import send_nda_reminder
                body = generate_doc_reminder_email(
                    lead.contact_name or "", lead.business_name, rec.nda_followup_count, "NDA", days
                )
                send_nda_reminder(lead.email, lead.contact_name or "", lead.business_name, body, rec.nda_followup_count)
                log_pipeline("NDA_REMINDER_SENT", company=lead.business_name, email=lead.email,
                             detail=f"Follow-up #{rec.nda_followup_count} — Day {days}")
                sent += 1

        # Agreement reminder
        if rec.agreement_status == DocumentStatus.SENT_TO_LEAD:
            last = rec.agreement_last_followup_at or rec.agreement_sent_at
            if last and (now - last).total_seconds() >= 86400:
                days = (now - rec.agreement_sent_at).days if rec.agreement_sent_at else 1
                rec.agreement_followup_count = (rec.agreement_followup_count or 0) + 1
                rec.agreement_last_followup_at = now
                rec.agreement_status_display = (
                    f"Agreement Follow-up #{rec.agreement_followup_count} — Day {days} ({_fmt(now)})"
                )

                from app.services.onboarding_ai import generate_doc_reminder_email
                from app.services.onboarding_email import send_agreement_reminder
                body = generate_doc_reminder_email(
                    lead.contact_name or "", lead.business_name,
                    rec.agreement_followup_count, "Customer Agreement", days
                )
                send_agreement_reminder(
                    lead.email, lead.contact_name or "", lead.business_name, body, rec.agreement_followup_count
                )
                log_pipeline("AGREEMENT_REMINDER_SENT", company=lead.business_name, email=lead.email,
                             detail=f"Follow-up #{rec.agreement_followup_count} — Day {days}")
                sent += 1

    if sent:
        await db.commit()
    return sent


@celery_app.task(bind=True, max_retries=2)
def sweep_onboarding_reminders(self) -> int:
    try:
        return run_async(_sweep_reminders)
    except Exception as exc:
        raise self.retry(exc=exc)


# ---------------------------------------------------------------------------
# Google Sheets export — single row per lead, all pipeline stages
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# Sheet1 — Pipeline Tracking headers
# Col:   A              B               C         D               E              F
# Sheet 1 — "Lead Information": basic lead + outreach pipeline columns
_LEAD_INFO_HEADERS = [
    "Lead Email", "Company Name", "Summary", "Company Type",
    "Contact Name", "Contact Phone",
    "Lead Status", "Sent At", "Replied At", "Booked At",
    "Last Updated",
]

# Sheet 3 — "Onboarding Track": KYC → NDA → Agreement pipeline
_ONBOARDING_TRACK_HEADERS = [
    "Lead Email", "Company Name",
    "Overall Stage", "KYC Status", "KYC Submitted At", "KYC Approved At",
    "NDA Status", "NDA Sent At", "Agreement Status", "Agreement Sent At",
    "GSTIN", "PAN", "CIN", "Last Updated",
]

# Keep legacy alias so any remaining references compile
_SHEET_HEADERS = _LEAD_INFO_HEADERS

# Sheet 2 — "KYC Data": raw KYC submission data
_KYC_HEADERS = [
    "Lead Email", "Company Name", "Company Type", "Contact Name", "Contact Phone",
    "GSTIN", "PAN", "CIN", "IFSC", "Bank Name",
    "Registered Address", "City", "State", "Nature of Business", "Entity Type",
    "Date of Incorporation", "Annual Turnover", "Signatory Designation",
    "Country of Incorporation", "Company Reg No", "Tax ID / TIN", "LEI Number",
    "KYC Format Check", "Issues Found", "Submitted At",
]

# Color palette for status cells
_GREEN_DARK  = {"red": 0.420, "green": 0.655, "blue": 0.314}   # header text not used
_GREEN_BG    = {"red": 0.714, "green": 0.843, "blue": 0.659}   # complete / approved
_GREEN_LIGHT = {"red": 0.851, "green": 0.918, "blue": 0.827}   # sent / submitted
_YELLOW_BG   = {"red": 1.0,   "green": 0.949, "blue": 0.8}     # pending / review
_RED_BG      = {"red": 0.988, "green": 0.898, "blue": 0.886}   # rejected / fail
_GREY_BG     = {"red": 0.95,  "green": 0.95,  "blue": 0.95}    # form sent / new
_HEADER_BG   = {"red": 0.102, "green": 0.227, "blue": 0.420}   # dark navy
_WHITE       = {"red": 1.0,   "green": 1.0,   "blue": 1.0}


def _status_bg(value: str) -> dict:
    v = (value or "").upper()
    if any(x in v for x in ("COMPLETE", "APPROVED", "PROCEED", "PASS")):
        return _GREEN_BG
    if any(x in v for x in ("SENT", "SUBMITTED")):
        return _GREEN_LIGHT
    if any(x in v for x in ("REVIEW", "PENDING", "DRAFT", "NDA APPROVED")):
        return _YELLOW_BG
    if any(x in v for x in ("REJECT", "FAIL", "ISSUE")):
        return _RED_BG
    if v and v not in ("", "NONE"):
        return _GREY_BG
    return _WHITE


def _overall_stage(rec: OnboardingRecord) -> str:
    a = rec.agreement_status
    if a in (DocumentStatus.APPROVED, DocumentStatus.PROCEED_NEXT):
        return "Onboarding Complete"
    if a == DocumentStatus.SENT_TO_LEAD:
        return "Agreement Sent — Awaiting Signature"
    if a in (DocumentStatus.TEAM_REVIEW, DocumentStatus.DRAFT_GENERATED):
        return "Agreement Draft — Pending Review"
    n = rec.nda_status
    if n == DocumentStatus.APPROVED:
        return "NDA Approved — Agreement Generation"
    if n in (DocumentStatus.SIGN_UNDER_REVIEW, DocumentStatus.SIGNED_RECEIVED):
        return "NDA Signed — Pending Review"
    if n == DocumentStatus.SENT_TO_LEAD:
        return "NDA Sent — Awaiting Signature"
    if n in (DocumentStatus.TEAM_REVIEW, DocumentStatus.DRAFT_GENERATED):
        return "NDA Draft — Pending Review"
    k = rec.kyc_status
    if k == KYCStatus.APPROVED:
        return "KYC Approved — NDA Generation"
    if k in (KYCStatus.SUBMITTED, KYCStatus.UNDER_REVIEW):
        return "KYC Submitted — Under Review"
    if k == KYCStatus.REJECTED:
        return "KYC Rejected — Resubmission Needed"
    return "KYC Form Sent"


def _kyc_stage_label(rec: OnboardingRecord) -> str:
    k = rec.kyc_status
    if k == KYCStatus.APPROVED:
        return "Approved"
    if k in (KYCStatus.SUBMITTED, KYCStatus.UNDER_REVIEW):
        return "Under Review"
    if k == KYCStatus.REJECTED:
        return "Rejected"
    return "Form Sent"


def _col(idx: int) -> str:
    """0-based column index → A, B, … Z, AA, …"""
    result = ""
    idx += 1
    while idx:
        idx, rem = divmod(idx - 1, 26)
        result = chr(65 + rem) + result
    return result


def _get_or_create_ws(sh, name: str, headers: list):
    """Return worksheet by name, creating it with a frozen header row if missing."""
    try:
        import gspread as _gs
        ws = sh.worksheet(name)
    except Exception:
        ws = sh.add_worksheet(title=name, rows=1000, cols=len(headers))
        ws.append_row(headers)
        try:
            _setup_tracking_sheet(sh, ws)
        except Exception:
            pass
    if not ws.get_all_values():
        ws.append_row(headers)
    return ws


def _setup_tracking_sheet(sh, ws) -> None:
    """Freeze header row and set column widths on Sheet1 (run once on creation)."""
    sheet_id = ws.id
    requests = [
        {"updateSheetProperties": {
            "properties": {"sheetId": sheet_id, "gridProperties": {"frozenRowCount": 1}},
            "fields": "gridProperties.frozenRowCount",
        }},
        *[
            {"updateDimensionProperties": {
                "range": {"sheetId": sheet_id, "dimension": "COLUMNS",
                          "startIndex": i, "endIndex": i + 1},
                "properties": {"pixelSize": px},
                "fields": "pixelSize",
            }}
            for i, px in enumerate([220, 180, 200, 90, 140, 110, 200, 120, 130, 130, 120, 130, 130, 130, 90, 80, 80, 130])
        ],
    ]
    sh.batch_update({"requests": requests})


async def _export_to_sheets(db: AsyncSession, onboarding_id: str) -> None:
    if not settings.GOOGLE_SHEETS_SPREADSHEET_ID or not settings.GOOGLE_SHEETS_CREDENTIALS_JSON:
        return

    rec = await db.get(OnboardingRecord, uuid.UUID(onboarding_id))
    if not rec:
        return
    lead = await db.get(LeadV2, rec.lead_id)

    kyc_result = await db.execute(
        select(KYCSubmission)
        .where(KYCSubmission.onboarding_id == rec.id)
        .order_by(KYCSubmission.attempt_number.desc())
    )
    kyc = kyc_result.scalars().first()

    _vr: dict = (kyc.kyc_verification_result or {}) if kyc else {}
    _extra = _vr.get("extra_fields") or {}

    kyc_status_label = _kyc_stage_label(rec)
    kyc_issues = "; ".join(_vr.get("issues", []))[:300]

    overall = _overall_stage(rec)

    _email = lead.email if lead else ""
    _company = kyc.company_name if kyc else (lead.business_name if lead else "")

    # ── Sheet 1 "Lead Information" row ──────────────────────────────────────
    lead_info_row = [
        _email,
        _company,
        (lead.summary or "") if lead else "",
        kyc.company_type if kyc else (rec.company_type or ""),
        kyc.contact_name if kyc else (lead.contact_name if lead else ""),
        kyc.contact_number if kyc else "",
        lead.status.value if lead and lead.status else "",
        _fmt(lead.sent_at) if lead else "",
        _fmt(lead.replied_at) if lead else "",
        _fmt(lead.booked_at) if lead else "",
        _fmt(_now_ist()),
    ]

    # ── Sheet 3 "Onboarding Track" row ──────────────────────────────────────
    onboarding_row = [
        _email,
        _company,
        overall,
        kyc_status_label,
        _fmt(kyc.created_at) if kyc else "",
        _fmt(rec.kyc_approved_at),
        rec.nda_status or "",
        _fmt(rec.nda_sent_at),
        rec.agreement_status or "",
        _fmt(rec.agreement_sent_at),
        kyc.gstin_number if kyc else "",
        kyc.pan_number if kyc else "",
        kyc.cin_number if kyc else "",
        _fmt(_now_ist()),
    ]

    def _upsert_ws(ws, headers: list, row: list) -> None:
        clean = [str(v) if v is not None else "" for v in row]
        all_rows = ws.get_all_values()
        nc = len(headers)
        end_col = _col(nc - 1)
        for i, r in enumerate(all_rows[1:], start=2):
            if r and r[0].lower() == clean[0].lower():
                ws.update([clean], f"A{i}:{end_col}{i}")
                return
        ws.append_row(clean)

    try:
        import json as _json
        import gspread
        from google.oauth2.service_account import Credentials

        creds_dict = _json.loads(settings.GOOGLE_SHEETS_CREDENTIALS_JSON)
        scopes = [
            "https://spreadsheets.google.com/feeds",
            "https://www.googleapis.com/auth/drive",
            "https://www.googleapis.com/auth/spreadsheets",
        ]
        creds = Credentials.from_service_account_info(creds_dict, scopes=scopes)
        gc = gspread.authorize(creds)
        sh = gc.open_by_key(settings.GOOGLE_SHEETS_SPREADSHEET_ID)

        ws1 = _get_or_create_ws(sh, "Lead Information", _LEAD_INFO_HEADERS)
        _upsert_ws(ws1, _LEAD_INFO_HEADERS, lead_info_row)

        ws3 = _get_or_create_ws(sh, "Onboarding Track", _ONBOARDING_TRACK_HEADERS)
        _upsert_ws(ws3, _ONBOARDING_TRACK_HEADERS, onboarding_row)

    except Exception as exc:
        logger.warning("onboarding_sheets_export_failed", error=str(exc))

    # ── KYC Data sheet ───────────────────────────────────────────────────────
    if kyc:
        try:
            import json as _json
            import gspread
            from google.oauth2.service_account import Credentials

            creds_dict = _json.loads(settings.GOOGLE_SHEETS_CREDENTIALS_JSON)
            scopes = [
                "https://spreadsheets.google.com/feeds",
                "https://www.googleapis.com/auth/drive",
                "https://www.googleapis.com/auth/spreadsheets",
            ]
            creds = Credentials.from_service_account_info(creds_dict, scopes=scopes)
            gc = gspread.authorize(creds)
            sh = gc.open_by_key(settings.GOOGLE_SHEETS_SPREADSHEET_ID)

            kws = _get_or_create_ws(sh, "KYC Data", _KYC_HEADERS)
            kyc_all = kws.get_all_values()
            if not kyc_all:
                kws.append_row(_KYC_HEADERS)
                kyc_all = [_KYC_HEADERS]

            lead_email_val = lead.email if lead else ""
            kyc_row = [
                lead_email_val,
                kyc.company_name or "",
                kyc.company_type or "",
                kyc.contact_name or "",
                kyc.contact_number or "",
                kyc.gstin_number or "",
                kyc.pan_number or "",
                kyc.cin_number or "",
                _extra.get("ifsc_code", ""),
                _extra.get("bank_name", ""),
                _extra.get("registered_address", ""),
                _extra.get("city", ""),
                _extra.get("state", ""),
                _extra.get("nature_of_business", ""),
                _extra.get("entity_type", ""),
                _extra.get("date_of_incorporation", ""),
                _extra.get("annual_turnover", ""),
                _extra.get("signatory1_designation", ""),
                _extra.get("country_of_incorporation", ""),
                _extra.get("company_reg_number", ""),
                _extra.get("tax_id_tin", ""),
                _extra.get("lei_number", ""),
                "Pass" if _vr.get("overall_passed") else ("Fail" if _vr.get("issues") else "Pending"),
                kyc_issues,
                _fmt(kyc.created_at),
            ]
            clean_kyc = [str(v) if v is not None else "" for v in kyc_row]

            # Upsert by Lead Email
            for i, r in enumerate(kyc_all[1:], start=2):
                if r and r[0] == lead_email_val:
                    kws.update([clean_kyc], f"A{i}:Y{i}")
                    return

            kws.append_row(clean_kyc, value_input_option="USER_ENTERED")
        except Exception as exc:
            logger.warning("kyc_sheets_export_failed", error=str(exc))


@celery_app.task(bind=True, max_retries=2, default_retry_delay=30)
def export_onboarding_to_sheets(self, onboarding_id: str) -> None:
    try:
        run_async(_export_to_sheets, onboarding_id)
    except Exception as exc:
        raise self.retry(exc=exc)


def write_csv_lead_to_sheet(
    email: str,
    company_name: str,
    summary: str = "",
    contact_name: str = "",
    phone: str = "",
) -> None:
    """Write a CSV-imported lead directly to Sheet1 (no OnboardingRecord needed).

    Called synchronously from the import-csv endpoint so the lead appears in the
    tracking sheet immediately — before the Celery worker creates an OnboardingRecord.
    If the lead already exists in the sheet (matched by email) its row is updated.
    """
    if not settings.GOOGLE_SHEETS_SPREADSHEET_ID or not settings.GOOGLE_SHEETS_CREDENTIALS_JSON:
        return
    try:
        import json as _json
        import gspread
        from google.oauth2.service_account import Credentials

        creds_dict = _json.loads(settings.GOOGLE_SHEETS_CREDENTIALS_JSON)
        scopes = [
            "https://spreadsheets.google.com/feeds",
            "https://www.googleapis.com/auth/drive",
            "https://www.googleapis.com/auth/spreadsheets",
        ]
        creds = Credentials.from_service_account_info(creds_dict, scopes=scopes)
        gc = gspread.authorize(creds)
        sh = gc.open_by_key(settings.GOOGLE_SHEETS_SPREADSHEET_ID)

        ws = _get_or_create_ws(sh, "Lead Information", _LEAD_INFO_HEADERS)

        # Row matches _LEAD_INFO_HEADERS (11 cols)
        row = [
            email,         # Lead Email
            company_name,  # Company Name
            summary,       # Summary
            "",            # Company Type (unknown at import)
            contact_name,  # Contact Name
            phone,         # Contact Phone
            "new",         # Lead Status
            "",            # Sent At
            "",            # Replied At
            "",            # Booked At
            _fmt(_now_ist()),  # Last Updated
        ]
        nc = len(_LEAD_INFO_HEADERS)
        end_col = _col(nc - 1)
        all_rows = ws.get_all_values()

        row_num = None
        for i, r in enumerate(all_rows[1:], start=2):
            if r and r[0].lower() == email.lower():
                row_num = i
                break

        if row_num:
            existing = all_rows[row_num - 1]
            merged = list(existing) + [""] * (nc - len(existing))
            for idx, val in enumerate(row):
                if idx >= nc:
                    break
                if not merged[idx] and val:
                    merged[idx] = val
            merged[10] = row[10]  # always refresh Last Updated
            ws.update([merged], f"A{row_num}:{end_col}{row_num}")
        else:
            ws.append_row(row)

        logger.info("csv_lead_written_to_sheet", email=email)
    except Exception as exc:
        logger.warning("csv_lead_sheet_write_failed", email=email, error=str(exc))
