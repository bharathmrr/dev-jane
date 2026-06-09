"""Celery tasks for the customer onboarding pipeline.

Tasks:
  initiate_onboarding_task      — detect company type, create record, send KYC email
  send_kyc_rejection_task       — AI rejection email → lead
  kyc_daily_reminder_task       — daily beat for unpaid KYC forms
  generate_nda_draft_task       — fetch template, AI fill, store draft
  revise_nda_draft_task         — AI revise based on team notes
  send_nda_to_lead_task         — email NDA to lead
  send_nda_sign_rejection_task  — email rejection of signed NDA
  nda_daily_reminder_task       — daily reminder for unsigned NDA
  generate_agreement_draft_task — same as NDA
  revise_agreement_draft_task
  send_agreement_to_lead_task
  send_agreement_sign_rejection_task
  agreement_daily_reminder_task
  export_onboarding_to_sheets   — real-time Google Sheets export
  check_signed_docs_inbox       — IMAP: detect signed NDA/Agreement replies
  sweep_onboarding_reminders    — beat task that fires all overdue reminders
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

    # Idempotency check
    existing = (await db.execute(
        select(OnboardingRecord).where(OnboardingRecord.lead_id == uuid.UUID(lead_id))
    )).scalar_one_or_none()
    if existing:
        return

    # Detect company type via AI
    from app.services.onboarding_ai import detect_company_type
    company_type = detect_company_type(lead.business_name, lead.summary or "")

    # Create onboarding record
    from app.services.onboarding_email import make_kyc_token
    rec = OnboardingRecord(
        lead_id=uuid.UUID(lead_id),
        company_type=company_type,
        kyc_status=KYCStatus.FORM_SENT,
    )
    db.add(rec)
    await db.flush()  # get rec.id

    token = make_kyc_token(str(rec.id))
    rec.kyc_form_token = token

    from app.services.onboarding_email import make_kyc_url
    kyc_url = make_kyc_url(str(rec.id), token)

    now = _now_ist()
    rec.kyc_form_sent_at = now
    rec.kyc_status_display = f"KYC Form Sent ({_fmt(now)})"
    await db.commit()

    log_pipeline("ONBOARDING_STARTED", company=lead.business_name, email=lead.email,
                 detail=f"Company type detected: {company_type}")

    # Send KYC form email
    from app.services.onboarding_email import send_kyc_form_email
    send_kyc_form_email(lead.email, lead.contact_name or lead.business_name, lead.business_name, kyc_url)
    log_pipeline("KYC_FORM_SENT", company=lead.business_name, email=lead.email,
                 detail="KYC form emailed to lead")

    # Notify team immediately that onboarding has started
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

    # Sync to Zoho CRM
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

    # Export to sheets
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
# KYC: auto-verification (free API)
# ---------------------------------------------------------------------------

async def _auto_verify_kyc(db: AsyncSession, submission_id: str, onboarding_id: str) -> None:
    from app.db.models import KYCSubmission as KYCSubmissionModel

    submission = await db.get(KYCSubmissionModel, uuid.UUID(submission_id))
    if not submission:
        return

    rec = await db.get(OnboardingRecord, uuid.UUID(onboarding_id))
    if not rec:
        return

    # Run verification with up to 3 retries on transient API errors
    from app.services.kyc_verify import run_kyc_verification
    import time as _time
    _prev = submission.kyc_verification_result or {}
    extra = _prev.get("extra_fields", {})
    ocr_result = _prev.get("ocr_result")  # stored by background OCR task

    result: dict = {}
    for _trial in range(3):
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
        _gstin_chk = (result.get("gstin_check") or {})
        if _gstin_chk.get("valid") is not None or _trial >= 2:
            break
        _time.sleep(2 ** _trial)

    if ocr_result:
        result["ocr_result"] = ocr_result

    submission.kyc_verification_result = result
    submission.auto_verified = True
    now = _now_ist()

    log_pipeline("KYC_SUBMITTED", company=submission.company_name,
                 detail=f"GSTIN:{submission.gstin_number or '—'} PAN:{submission.pan_number or '—'}")

    lead = await db.get(LeadV2, rec.lead_id)

    if result.get("auto_approvable"):
        # All checks passed + API confirmed → auto-approve
        rec.kyc_status = KYCStatus.APPROVED
        rec.kyc_approved_at = now
        rec.kyc_status_display = f"KYC Auto-Verified & Approved ✓ ({_fmt(now)})"
        await db.commit()

        gstin_src = (result.get("gstin_check") or {}).get("source", "")
        log_pipeline("KYC_AUTO_APPROVED", company=submission.company_name,
                     detail=f"GSTIN verified via {gstin_src} | NDA generation triggered")

        if lead:
            from app.services.onboarding_email import send_kyc_approved_email
            send_kyc_approved_email(lead.email, lead.contact_name or "", lead.business_name)

            from app.services.zoho_crm import sync_onboarding_stage
            sync_onboarding_stage(
                email=lead.email,
                contact_name=lead.contact_name or submission.company_name,
                company_name=submission.company_name,
                stage="KYC Auto-Approved",
                detail=f"GSTIN: {submission.gstin_number or '—'} verified via {gstin_src}",
                phone=submission.contact_number or "",
                company_type=submission.company_type,
                onboarding_id=onboarding_id,
            )

        generate_nda_draft_task.delay(onboarding_id)

    elif result.get("overall_passed"):
        # Format checks passed but API did not fully confirm → manual review
        notes_txt = "; ".join(result.get("notes", []))
        rec.kyc_status_display = (
            f"KYC Format Verified — Under Manual Review ({_fmt(now)})"
            + (f" | {notes_txt[:150]}" if notes_txt else "")
        )
        await db.commit()
        log_pipeline("KYC_MANUAL_REVIEW", company=submission.company_name,
                     detail="Format valid, GST API unconfirmed — manual review needed")

        if lead:
            from app.services.zoho_crm import sync_onboarding_stage
            sync_onboarding_stage(
                email=lead.email,
                contact_name=lead.contact_name or submission.company_name,
                company_name=submission.company_name,
                stage="KYC Under Manual Review",
                detail=f"Format valid. Notes: {notes_txt[:200]}",
                phone=submission.contact_number or "",
                company_type=submission.company_type,
                onboarding_id=onboarding_id,
            )

        _send_kyc_reviewer_email(submission, result, lead, onboarding_id)

    else:
        # Format-level failures → flag for team review
        issues_txt = "; ".join(result.get("issues", []))
        rec.kyc_status_display = (
            f"KYC Verification Issues Found ({_fmt(now)}): {issues_txt[:200]}"
        )
        await db.commit()
        log_pipeline("KYC_ISSUES_FOUND", company=submission.company_name,
                     detail=f"Issues: {issues_txt[:200]}")

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

        _send_kyc_reviewer_email(submission, result, lead, onboarding_id)


def _send_kyc_reviewer_email(submission, result: dict, lead, onboarding_id: str) -> None:
    """Send AI-generated KYC review email to the team reviewer with approve/reject buttons."""
    try:
        from app.services.kyc_ocr import generate_kyc_review_email
        from app.services.onboarding_email import _send_to_reviewers, make_action_url

        approve_url = make_action_url(onboarding_id, "approve_kyc")
        reject_url = make_action_url(onboarding_id, "reject_kyc")

        html_body = generate_kyc_review_email(
            lead_name=submission.contact_name or submission.company_name,
            lead_email=lead.email if lead else "",
            company_name=submission.company_name,
            kyc_result=result,
            ocr_result=result.get("ocr_result"),
        )
        html_body += f"""<br>
<p>
  <a href="{approve_url}" style="background:#22c55e;color:#fff;padding:10px 22px;border-radius:6px;text-decoration:none;margin-right:12px;font-weight:bold;">&#10003; Approve KYC</a>
  <a href="{reject_url}" style="background:#ef4444;color:#fff;padding:10px 22px;border-radius:6px;text-decoration:none;font-weight:bold;">&#10007; Reject KYC</a>
</p>"""
        _send_to_reviewers(f"KYC Review Required — {submission.company_name}", html_body)
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

    from app.services.zoho_contracts import get_contract_type_id, create_contract
    contract_type_id = get_contract_type_id("nda", company_type)

    now = _now_ist()
    company_name = kyc.company_name
    nda_label = "Indian NDA" if not is_overseas else "Overseas NDA"

    extra = (kyc.kyc_verification_result or {}).get("extra_fields", {}) if kyc else {}

    def _mf(*keys):
        for k in keys:
            v = extra.get(k)
            if v:
                return v
        return ""

    merge_fields: dict = {
        "company_name":          kyc.company_name,
        "contact_name":          kyc.contact_name or (lead.contact_name if lead else "") or company_name,
        "contact_number":        kyc.contact_number or "",
        "effective_date":        now.strftime("%d %B %Y"),
        "signatory_email":       lead.email if lead else "",
        "entity_type":           _mf("entity_type"),
        "date_of_incorporation": _mf("date_of_incorporation"),
        "registered_address":    _mf("registered_address"),
        "city":                  _mf("city"),
        "state":                 _mf("state"),
        "nature_of_business":    _mf("nature_of_business"),
        "signatory1_designation":_mf("signatory1_designation"),
    }

    if not is_overseas:
        merge_fields.update({k: v for k, v in {
            "pan_number":   kyc.pan_number or "",
            "gstin_number": kyc.gstin_number or "",
            "cin_number":   kyc.cin_number or "",
        }.items() if v})
    else:
        merge_fields.update({k: v for k, v in {
            "country_of_incorporation": _mf("country_of_incorporation"),
            "company_reg_number":       _mf("company_reg_number"),
            "country_of_tax_residence": _mf("country_of_tax_residence"),
            "tax_id_tin":               _mf("tax_id_tin"),
            "lei_number":               _mf("lei_number"),
            "vat_gst_number":           _mf("vat_gst_number"),
            "country":                  _mf("country"),
            "signatory1_nationality":   _mf("signatory1_nationality"),
            "signatory1_passport_id":   _mf("signatory1_passport_id"),
        }.items() if v})

    # Create actual draft in Zoho Contracts (not sent yet — team reviews first)
    contract_api_name = ""
    preview_url = f"https://contracts.zoho.in/janeaerospace#/contracttypes/{contract_type_id}"
    contract_status = "Draft pending creation"
    try:
        _cid, contract_api_name = create_contract(
            contract_type_id=contract_type_id,
            contract_name=f"NDA - {company_name}",
            lead_name=kyc.contact_name or company_name,
            lead_email=lead.email if lead else "",
            merge_fields=merge_fields,
        )
        rec.nda_zoho_contract_id = contract_api_name
        preview_url = f"https://contracts.zoho.in/janeaerospace#/contracts/{contract_api_name}"
        contract_status = f"Draft created in Zoho Contracts (ID: {contract_api_name})"
    except Exception as e:
        logger.warning("nda_draft_create_failed", error=str(e), onboarding_id=onboarding_id)
        contract_status = f"Draft creation failed: {str(e)[:120]}"

    rec.nda_draft_content = (
        f"=== NDA READY FOR TEAM REVIEW ===\n"
        f"Company       : {company_name}\n"
        f"Contact       : {kyc.contact_name} | {kyc.contact_number or '—'}\n"
        f"Template Type : {nda_label} (Contract Type: {contract_type_id})\n"
        f"Effective Date: {now.strftime('%d %B %Y')}\n"
        f"Send To       : {lead.email if lead else '—'}\n\n"
        f"Status: {contract_status}\n"
        f"Preview in Zoho Contracts: {preview_url}\n\n"
        f"Merge fields pre-filled:\n"
        + "\n".join(f"  {k} = {v}" for k, v in merge_fields.items() if v)
        + "\n\nClick Approve to send this NDA to the lead for e-signature."
    )
    rec.nda_status = DocumentStatus.TEAM_REVIEW
    rec.nda_status_display = f"NDA Ready — Pending Team Review ({_fmt(now)})"

    log_pipeline("NDA_READY", company=company_name,
                 detail=f"NDA ({nda_label}) draft created in Zoho Contracts | {contract_status}")

    await db.commit()
    await _export_to_sheets(db, onboarding_id)

    try:
        from app.services.onboarding_email import notify_team_nda_draft_ready
        notify_team_nda_draft_ready(
            lead_name=(lead.contact_name or company_name) if lead else company_name,
            company_name=company_name,
            onboarding_id=onboarding_id,
            preview_url=preview_url,
            contract_id=contract_api_name,
        )
    except Exception as _e:
        pass


@celery_app.task(bind=True, max_retries=3, default_retry_delay=30)
def generate_nda_draft_task(self, onboarding_id: str) -> None:
    try:
        run_async(_generate_nda_draft, onboarding_id)
    except Exception as exc:
        raise self.retry(exc=exc)


# ---------------------------------------------------------------------------
# NDA: revise draft
# ---------------------------------------------------------------------------

async def _revise_nda_draft(db: AsyncSession, onboarding_id: str, notes: str) -> None:
    rec = await db.get(OnboardingRecord, uuid.UUID(onboarding_id))
    if not rec:
        return

    kyc_result = await db.execute(
        select(KYCSubmission)
        .where(KYCSubmission.onboarding_id == rec.id)
        .order_by(KYCSubmission.attempt_number.desc())
    )
    kyc = kyc_result.scalars().first()
    kyc_data = {
        "company_name": kyc.company_name if kyc else "",
        "contact_name": kyc.contact_name if kyc else "",
        "contact_number": kyc.contact_number if kyc else "",
        "company_type": rec.company_type or "indian",
        "date": _now_ist().strftime("%d %B %Y"),
    }

    from app.services.onboarding_ai import revise_document
    revised = revise_document(rec.nda_draft_content or "", notes, kyc_data, "NDA")

    now = _now_ist()
    rec.nda_draft_content = revised
    rec.nda_status = DocumentStatus.TEAM_REVIEW
    rec.nda_status_display = f"NDA Revised (v{rec.nda_draft_revision}) — Pending Team Review ({_fmt(now)})"

    # Upload revised draft
    if kyc:
        try:
            from app.services.zoho_workdrive import upload_file
            file_id = upload_file(
                revised.encode("utf-8"),
                f"NDA_Draft_v{rec.nda_draft_revision}_{kyc.company_name}_{onboarding_id[:8]}.txt",
                mime_type="text/plain",
            )
            rec.nda_draft_zoho_file_id = file_id
        except Exception:
            pass

    await db.commit()
    await _export_to_sheets(db, onboarding_id)


@celery_app.task(bind=True, max_retries=3, default_retry_delay=30)
def revise_nda_draft_task(self, onboarding_id: str, notes: str) -> None:
    try:
        run_async(_revise_nda_draft, onboarding_id, notes)
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
    company_type = rec.company_type or "INDIAN"
    is_overseas = company_type.upper() in ("OVERSEAS", "FOREIGN", "INTERNATIONAL")

    kyc_result = await db.execute(
        select(KYCSubmission)
        .where(KYCSubmission.onboarding_id == rec.id)
        .order_by(KYCSubmission.attempt_number.desc())
    )
    kyc = kyc_result.scalars().first()

    extra = (kyc.kyc_verification_result or {}).get("extra_fields", {}) if kyc else {}

    def _mf(*keys):
        for k in keys:
            v = extra.get(k)
            if v:
                return v
        return ""

    merge_fields: dict = {
        "company_name":          (kyc.company_name if kyc else None) or lead.business_name,
        "contact_name":          (kyc.contact_name if kyc else None) or lead.contact_name or lead.business_name,
        "contact_number":        (kyc.contact_number if kyc else "") or "",
        "effective_date":        now.strftime("%d %B %Y"),
        "signatory_email":       lead.email,
        "entity_type":           _mf("entity_type"),
        "date_of_incorporation": _mf("date_of_incorporation"),
        "registered_address":    _mf("registered_address"),
        "city":                  _mf("city"),
        "state":                 _mf("state"),
        "nature_of_business":    _mf("nature_of_business"),
        "signatory1_designation":_mf("signatory1_designation"),
    }
    if not is_overseas:
        merge_fields.update({k: v for k, v in {
            "pan_number":   (kyc.pan_number or "") if kyc else "",
            "gstin_number": (kyc.gstin_number or "") if kyc else "",
            "cin_number":   (kyc.cin_number or "") if kyc else "",
        }.items() if v})
    else:
        merge_fields.update({k: v for k, v in {
            "country_of_incorporation": _mf("country_of_incorporation"),
            "company_reg_number":       _mf("company_reg_number"),
            "country_of_tax_residence": _mf("country_of_tax_residence"),
            "tax_id_tin":               _mf("tax_id_tin"),
            "lei_number":               _mf("lei_number"),
            "vat_gst_number":           _mf("vat_gst_number"),
            "country":                  _mf("country"),
            "signatory1_nationality":   _mf("signatory1_nationality"),
            "signatory1_passport_id":   _mf("signatory1_passport_id"),
        }.items() if v})

    company_name = merge_fields["company_name"]
    contact_name = merge_fields["contact_name"]

    # Generate NDA document and send via email
    # (Zoho Contracts API contract-creation is not available on the current plan)
    from app.services.nda_template import render_nda_html
    from app.services.onboarding_email import send_nda_to_lead as _send_nda_email
    nda_html = render_nda_html(merge_fields, is_overseas)
    _send_nda_email(
        to_email=lead.email,
        lead_name=contact_name,
        company_name=company_name,
        nda_content_html=nda_html,
    )

    rec.nda_status = DocumentStatus.SENT_TO_LEAD
    rec.nda_sent_at = now
    rec.nda_status_display = f"NDA sent via email for signature ({_fmt(now)})"
    await db.commit()
    log_pipeline("NDA_SENT", company=company_name, email=lead.email,
                 detail="NDA sent via email (sign and return). Awaiting signed copy.")

    from app.services.zoho_crm import sync_onboarding_stage
    sync_onboarding_stage(
        email=lead.email,
        contact_name=contact_name,
        company_name=company_name,
        stage="NDA Sent for E-Sign",
        detail="NDA sent via email for wet signature. Lead to sign and return.",
        company_type=rec.company_type or "",
        onboarding_id=onboarding_id,
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
# Customer Agreement: generate draft (mirrors NDA)
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

    from app.services.zoho_contracts import get_contract_type_id, create_contract
    contract_type_id = get_contract_type_id("agreement", company_type)

    now = _now_ist()
    company_name = kyc.company_name

    extra = (kyc.kyc_verification_result or {}).get("extra_fields", {}) if kyc else {}

    def _mf(*keys):
        for k in keys:
            v = extra.get(k)
            if v:
                return v
        return ""

    merge_fields: dict = {
        "company_name":             kyc.company_name,
        "contact_name":             kyc.contact_name or (lead.contact_name if lead else "") or company_name,
        "contact_number":           kyc.contact_number or "",
        "effective_date":           now.strftime("%d %B %Y"),
        "signatory_email":          lead.email if lead else "",
        "entity_type":              _mf("entity_type"),
        "date_of_incorporation":    _mf("date_of_incorporation"),
        "registered_address":       _mf("registered_address"),
        "city":                     _mf("city"),
        "state":                    _mf("state"),
        "nature_of_business":       _mf("nature_of_business"),
        "annual_turnover":          _mf("annual_turnover"),
        "signatory1_designation":   _mf("signatory1_designation"),
        "escalation_contact_name":  _mf("escalation_contact_name"),
        "escalation_contact_email": _mf("escalation_contact_email"),
        "escalation_contact_phone": _mf("escalation_contact_phone"),
        "escalation_contact_title": _mf("escalation_contact_title"),
    }

    if not is_overseas:
        merge_fields.update({k: v for k, v in {
            "pan_number":   kyc.pan_number or "",
            "gstin_number": kyc.gstin_number or "",
            "cin_number":   kyc.cin_number or "",
            "bank_name":    _mf("bank_name"),
            "ifsc_code":    _mf("ifsc_code"),
            "account_type": _mf("account_type"),
        }.items() if v})
    else:
        merge_fields.update({k: v for k, v in {
            "country_of_incorporation":  _mf("country_of_incorporation"),
            "company_reg_number":        _mf("company_reg_number"),
            "country_of_tax_residence":  _mf("country_of_tax_residence"),
            "tax_id_tin":                _mf("tax_id_tin"),
            "lei_number":                _mf("lei_number"),
            "vat_gst_number":            _mf("vat_gst_number"),
            "country":                   _mf("country"),
            "signatory1_nationality":    _mf("signatory1_nationality"),
            "signatory1_passport_id":    _mf("signatory1_passport_id"),
            "swift_code":                _mf("swift_code"),
            "iban_number":               _mf("iban_number"),
            "bank_country":              _mf("bank_country"),
            "account_currency":          _mf("account_currency"),
        }.items() if v})

    # Create actual draft in Zoho Contracts (not sent yet — team reviews first)
    contract_api_name = ""
    preview_url = f"https://contracts.zoho.in/janeaerospace#/contracttypes/{contract_type_id}"
    contract_status = "Draft pending creation"
    try:
        _cid, contract_api_name = create_contract(
            contract_type_id=contract_type_id,
            contract_name=f"Customer Agreement - {company_name}",
            lead_name=kyc.contact_name or company_name,
            lead_email=lead.email if lead else "",
            merge_fields=merge_fields,
        )
        rec.agreement_zoho_contract_id = contract_api_name
        preview_url = f"https://contracts.zoho.in/janeaerospace#/contracts/{contract_api_name}"
        contract_status = f"Draft created in Zoho Contracts (ID: {contract_api_name})"
    except Exception as e:
        logger.warning("agreement_draft_create_failed", error=str(e), onboarding_id=onboarding_id)
        contract_status = f"Draft creation failed: {str(e)[:120]}"

    rec.agreement_draft_content = (
        f"=== CUSTOMER AGREEMENT READY FOR TEAM REVIEW ===\n"
        f"Company       : {company_name}\n"
        f"Contact       : {kyc.contact_name} | {kyc.contact_number or '—'}\n"
        f"Template Type : Customer Agreement (Contract Type: {contract_type_id})\n"
        f"Effective Date: {now.strftime('%d %B %Y')}\n"
        f"Send To       : {lead.email if lead else '—'}\n\n"
        f"Status: {contract_status}\n"
        f"Preview in Zoho Contracts: {preview_url}\n\n"
        f"Merge fields pre-filled:\n"
        + "\n".join(f"  {k} = {v}" for k, v in merge_fields.items() if v)
        + "\n\nClick Approve to send this Agreement to the lead for e-signature."
    )
    rec.agreement_status = DocumentStatus.TEAM_REVIEW
    rec.agreement_status_display = f"Agreement Ready — Pending Team Review ({_fmt(now)})"

    log_pipeline("AGREEMENT_READY", company=company_name,
                 detail=f"Customer Agreement draft created in Zoho Contracts | {contract_status}")
    await db.commit()
    await _export_to_sheets(db, onboarding_id)

    try:
        from app.services.onboarding_email import notify_team_agreement_draft_ready
        notify_team_agreement_draft_ready(
            lead_name=(lead.contact_name or company_name) if lead else company_name,
            company_name=company_name,
            onboarding_id=onboarding_id,
            preview_url=preview_url,
            contract_id=contract_api_name,
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
# Customer Agreement: revise draft
# ---------------------------------------------------------------------------

async def _revise_agreement_draft(db: AsyncSession, onboarding_id: str, notes: str) -> None:
    rec = await db.get(OnboardingRecord, uuid.UUID(onboarding_id))
    if not rec:
        return

    kyc_result = await db.execute(
        select(KYCSubmission)
        .where(KYCSubmission.onboarding_id == rec.id)
        .order_by(KYCSubmission.attempt_number.desc())
    )
    kyc = kyc_result.scalars().first()
    kyc_data = {
        "company_name": kyc.company_name if kyc else "",
        "contact_name": kyc.contact_name if kyc else "",
        "company_type": rec.company_type or "indian",
        "date": _now_ist().strftime("%d %B %Y"),
    }

    from app.services.onboarding_ai import revise_document
    revised = revise_document(rec.agreement_draft_content or "", notes, kyc_data, "Customer Agreement")

    now = _now_ist()
    rec.agreement_draft_content = revised
    rec.agreement_status = DocumentStatus.TEAM_REVIEW
    rec.agreement_status_display = (
        f"Agreement Revised (v{rec.agreement_draft_revision}) — Pending Team Review ({_fmt(now)})"
    )

    if kyc:
        try:
            from app.services.zoho_workdrive import upload_file
            file_id = upload_file(
                revised.encode("utf-8"),
                f"Agreement_Draft_v{rec.agreement_draft_revision}_{kyc.company_name}_{onboarding_id[:8]}.txt",
                mime_type="text/plain",
            )
            rec.agreement_draft_zoho_file_id = file_id
        except Exception:
            pass

    await db.commit()
    await _export_to_sheets(db, onboarding_id)


@celery_app.task(bind=True, max_retries=3, default_retry_delay=30)
def revise_agreement_draft_task(self, onboarding_id: str, notes: str) -> None:
    try:
        run_async(_revise_agreement_draft, onboarding_id, notes)
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

    from app.services.zoho_contracts import send_for_signature, create_and_send, get_contract_type_id

    now = _now_ist()

    if rec.agreement_zoho_contract_id:
        # Draft already created at preview step — just send for signature
        send_for_signature(rec.agreement_zoho_contract_id)
        contract_id = rec.agreement_zoho_contract_id
    else:
        # Fallback: create + send in one step (draft creation had failed earlier)
        kyc_result = await db.execute(
            select(KYCSubmission)
            .where(KYCSubmission.onboarding_id == rec.id)
            .order_by(KYCSubmission.attempt_number.desc())
        )
        kyc = kyc_result.scalars().first()
        company_type = rec.company_type or "INDIAN"
        is_overseas = company_type.upper() in ("OVERSEAS", "FOREIGN", "INTERNATIONAL")
        contract_type_id = get_contract_type_id("agreement", company_type)
        extra = (kyc.kyc_verification_result or {}).get("extra_fields", {}) if kyc else {}

        def _mf(*keys):
            for k in keys:
                v = extra.get(k)
                if v:
                    return v
            return ""

        merge_fields: dict = {
            "company_name":             kyc.company_name if kyc else lead.business_name,
            "contact_name":             kyc.contact_name if kyc else lead.contact_name or lead.business_name,
            "contact_number":           kyc.contact_number if kyc else "",
            "effective_date":           now.strftime("%d %B %Y"),
            "signatory_email":          lead.email,
            "entity_type":              _mf("entity_type"),
            "date_of_incorporation":    _mf("date_of_incorporation"),
            "registered_address":       _mf("registered_address"),
            "city":                     _mf("city"),
            "state":                    _mf("state"),
            "nature_of_business":       _mf("nature_of_business"),
            "annual_turnover":          _mf("annual_turnover"),
            "signatory1_designation":   _mf("signatory1_designation"),
            "escalation_contact_name":  _mf("escalation_contact_name"),
            "escalation_contact_email": _mf("escalation_contact_email"),
            "escalation_contact_phone": _mf("escalation_contact_phone"),
            "escalation_contact_title": _mf("escalation_contact_title"),
        }
        if not is_overseas:
            merge_fields.update({k: v for k, v in {
                "pan_number":   (kyc.pan_number or "") if kyc else "",
                "gstin_number": (kyc.gstin_number or "") if kyc else "",
                "cin_number":   (kyc.cin_number or "") if kyc else "",
                "bank_name":    _mf("bank_name"),
                "ifsc_code":    _mf("ifsc_code"),
                "account_type": _mf("account_type"),
            }.items() if v})
        else:
            merge_fields.update({k: v for k, v in {
                "country_of_incorporation":  _mf("country_of_incorporation"),
                "company_reg_number":        _mf("company_reg_number"),
                "country_of_tax_residence":  _mf("country_of_tax_residence"),
                "tax_id_tin":                _mf("tax_id_tin"),
                "lei_number":                _mf("lei_number"),
                "vat_gst_number":            _mf("vat_gst_number"),
                "country":                   _mf("country"),
                "signatory1_nationality":    _mf("signatory1_nationality"),
                "signatory1_passport_id":    _mf("signatory1_passport_id"),
                "swift_code":                _mf("swift_code"),
                "iban_number":               _mf("iban_number"),
                "bank_country":              _mf("bank_country"),
                "account_currency":          _mf("account_currency"),
            }.items() if v})

        contract_id = create_and_send(
            contract_type_id=contract_type_id,
            contract_name=f"Customer Agreement - {lead.business_name}",
            lead_name=lead.contact_name or lead.business_name,
            lead_email=lead.email,
            merge_fields=merge_fields,
        )
        rec.agreement_zoho_contract_id = contract_id

    rec.agreement_status = DocumentStatus.SENT_TO_LEAD
    rec.agreement_sent_at = now
    rec.agreement_status_display = f"Customer Agreement sent via Zoho Contracts for e-signature ({_fmt(now)})"
    await db.commit()
    log_pipeline("AGREEMENT_SENT", company=lead.business_name, email=lead.email,
                 detail=f"Customer Agreement sent via Zoho Contracts e-sign (contract_id={contract_id})")

    from app.services.zoho_crm import sync_onboarding_stage
    sync_onboarding_stage(
        email=lead.email,
        contact_name=lead.contact_name or lead.business_name,
        company_name=lead.business_name,
        stage="Agreement Sent for E-Sign",
        detail=f"Customer Agreement sent via Zoho Contracts. Contract ID: {contract_id}",
        company_type=rec.company_type or "",
        onboarding_id=onboarding_id,
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
# Daily reminder sweep (beat task — fires every 24h)
# ---------------------------------------------------------------------------

async def _sweep_reminders(db: AsyncSession) -> int:
    """Send daily reminders to leads who haven't responded."""
    now = _now_ist()
    sent = 0

    result = await db.execute(select(OnboardingRecord))
    records = result.scalars().all()

    for rec in records:
        lead = await db.get(LeadV2, rec.lead_id)
        if not lead:
            continue

        # KYC reminder: form sent but not submitted
        if rec.kyc_status in (KYCStatus.FORM_SENT, KYCStatus.REJECTED):
            last = rec.kyc_last_followup_at or rec.kyc_form_sent_at
            if last and (now - last).total_seconds() >= 86400:  # 24h
                days = (now - rec.kyc_form_sent_at).days if rec.kyc_form_sent_at else 1
                rec.kyc_followup_count = (rec.kyc_followup_count or 0) + 1
                rec.kyc_last_followup_at = now
                rec.kyc_status_display = (
                    f"KYC Follow-up #{rec.kyc_followup_count} sent — Day {days} ({_fmt(now)})"
                )

                from app.services.onboarding_ai import generate_kyc_reminder_email
                from app.services.onboarding_email import (
                    make_kyc_token, make_kyc_url, send_kyc_reminder_email
                )
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

        # NDA reminder: sent to lead but not signed
        if rec.nda_status == DocumentStatus.SENT_TO_LEAD:
            last = rec.nda_last_followup_at or rec.nda_sent_at
            if last and (now - last).total_seconds() >= 86400:
                days = (now - rec.nda_sent_at).days if rec.nda_sent_at else 1
                rec.nda_followup_count = (rec.nda_followup_count or 0) + 1
                rec.nda_last_followup_at = now
                rec.nda_status_display = (
                    f"NDA Follow-up #{rec.nda_followup_count} sent — Day {days} ({_fmt(now)})"
                )

                from app.services.onboarding_ai import generate_doc_reminder_email
                from app.services.onboarding_email import send_nda_reminder
                body = generate_doc_reminder_email(
                    lead.contact_name or "", lead.business_name, rec.nda_followup_count, "NDA", days
                )
                send_nda_reminder(
                    lead.email, lead.contact_name or "", lead.business_name, body, rec.nda_followup_count
                )
                log_pipeline("NDA_REMINDER_SENT", company=lead.business_name, email=lead.email,
                             detail=f"Follow-up #{rec.nda_followup_count} — Day {days}")
                sent += 1

        # Agreement reminder: sent to lead but not signed
        if rec.agreement_status == DocumentStatus.SENT_TO_LEAD:
            last = rec.agreement_last_followup_at or rec.agreement_sent_at
            if last and (now - last).total_seconds() >= 86400:
                days = (now - rec.agreement_sent_at).days if rec.agreement_sent_at else 1
                rec.agreement_followup_count = (rec.agreement_followup_count or 0) + 1
                rec.agreement_last_followup_at = now
                rec.agreement_status_display = (
                    f"Agreement Follow-up #{rec.agreement_followup_count} sent — Day {days} ({_fmt(now)})"
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
# Zoho Contracts: poll pending signatures (webhook fallback)
# ---------------------------------------------------------------------------

async def _poll_contract_statuses(db: AsyncSession) -> int:
    """Check Zoho Contracts for any NDA/Agreement that has been signed.

    Runs every 2 hours. Finds all records where a contract was sent to the
    lead but not yet marked as received. Polls the Zoho Contracts API for
    each and updates the DB if signed.
    """
    from app.services.zoho_contracts import get_contract_status
    from app.core.pipeline_logger import log_pipeline

    result = await db.execute(select(OnboardingRecord))
    records = result.scalars().all()

    updated = 0
    now = _now_ist()

    for rec in records:
        lead = await db.get(LeadV2, rec.lead_id)

        # --- Poll NDA ---
        if (
            rec.nda_status == DocumentStatus.SENT_TO_LEAD
            and rec.nda_zoho_contract_id
        ):
            try:
                status = get_contract_status(rec.nda_zoho_contract_id)
                if status["is_signed"]:
                    rec.nda_status = DocumentStatus.SIGN_UNDER_REVIEW
                    rec.nda_signed_received_at = now
                    rec.nda_status_display = (
                        f"NDA Signed (detected via poll) — Pending Team Review ({_fmt(now)})"
                    )
                    updated += 1
                    log_pipeline(
                        "NDA_SIGNED_RECEIVED",
                        company=lead.business_name if lead else "—",
                        email=lead.email if lead else "—",
                        detail=f"Signed NDA detected via Zoho Contracts poll (stage={status['stage']})",
                    )
                    if lead:
                        from app.services.onboarding_email import notify_team_signed_doc_received
                        notify_team_signed_doc_received(
                            lead.contact_name or "", lead.business_name,
                            str(rec.id), "NDA",
                        )
            except Exception as _e:
                logger.warning("nda_poll_failed", onboarding_id=str(rec.id), error=str(_e))

        # --- Poll Agreement ---
        if (
            rec.agreement_status == DocumentStatus.SENT_TO_LEAD
            and rec.agreement_zoho_contract_id
        ):
            try:
                status = get_contract_status(rec.agreement_zoho_contract_id)
                if status["is_signed"]:
                    rec.agreement_status = DocumentStatus.SIGN_UNDER_REVIEW
                    rec.agreement_signed_received_at = now
                    rec.agreement_status_display = (
                        f"Agreement Signed (detected via poll) — Pending Team Review ({_fmt(now)})"
                    )
                    updated += 1
                    log_pipeline(
                        "AGREEMENT_SIGNED_RECEIVED",
                        company=lead.business_name if lead else "—",
                        email=lead.email if lead else "—",
                        detail=f"Signed Agreement detected via poll (stage={status['stage']})",
                    )
                    if lead:
                        from app.services.onboarding_email import notify_team_signed_doc_received
                        notify_team_signed_doc_received(
                            lead.contact_name or "", lead.business_name,
                            str(rec.id), "Customer Agreement",
                        )
            except Exception as _e:
                logger.warning("agreement_poll_failed", onboarding_id=str(rec.id), error=str(_e))

    if updated:
        await db.commit()
    return updated


@celery_app.task(bind=True, max_retries=2)
def poll_contract_statuses_task(self) -> int:
    """Poll Zoho Contracts every 2 hours to detect signed NDA/Agreements."""
    try:
        return run_async(_poll_contract_statuses)
    except Exception as exc:
        raise self.retry(exc=exc)


# ---------------------------------------------------------------------------
# Google Sheets export
# ---------------------------------------------------------------------------

async def _export_to_sheets(db: AsyncSession, onboarding_id: str) -> None:
    """Append / update a row in the Onboarding worksheet."""
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

    from app.api.v1.onboarding_endpoints import _stage_score
    stage_score = _stage_score(rec)

    # Extract KYC verification details for extra columns
    _vr: dict = (kyc.kyc_verification_result or {}) if kyc else {}
    _gstin_chk = _vr.get("gstin_check") or {}
    _pan_chk = _vr.get("pan_check") or {}
    _cin_chk = _vr.get("cin_check") or {}
    _ocr = _vr.get("ocr_result") or {}
    _ocr_docs = ", ".join(
        d.get("type", "") for d in _ocr.get("documents_processed", []) if d.get("type")
    )

    row = [
        str(rec.id),
        str(rec.lead_id),
        lead.email if lead else "",
        lead.business_name if lead else "",
        lead.contact_name if lead else "",
        rec.company_type or "",
        rec.kyc_status or "",
        rec.kyc_status_display or "",
        str(rec.kyc_followup_count),
        kyc.company_name if kyc else "",
        kyc.contact_name if kyc else "",
        kyc.contact_number if kyc else "",
        rec.nda_status or "",
        rec.nda_status_display or "",
        str(rec.nda_followup_count),
        rec.agreement_status or "",
        rec.agreement_status_display or "",
        str(rec.agreement_followup_count),
        _fmt(rec.created_at),
        _fmt(rec.updated_at),
        str(stage_score),
        # KYC verification detail columns (V–AD)
        str(_gstin_chk.get("valid", "")),
        _gstin_chk.get("source", ""),
        _gstin_chk.get("business_name", ""),
        str(_pan_chk.get("valid", "")),
        str(_cin_chk.get("valid", "")),
        str(_vr.get("overall_passed", "")),
        str(_vr.get("auto_approvable", "")),
        _ocr.get("company_name", ""),
        _ocr_docs,
        "; ".join(_vr.get("issues", []))[:300],
    ]

    try:
        import json as _json
        import gspread
        from google.oauth2.service_account import Credentials

        creds_dict = _json.loads(settings.GOOGLE_SHEETS_CREDENTIALS_JSON)
        scopes = ["https://spreadsheets.google.com/feeds", "https://www.googleapis.com/auth/drive"]
        creds = Credentials.from_service_account_info(creds_dict, scopes=scopes)
        gc = gspread.authorize(creds)
        sh = gc.open_by_key(settings.GOOGLE_SHEETS_SPREADSHEET_ID)

        # Get or create the worksheet
        try:
            ws = sh.worksheet(settings.ONBOARDING_WORKSHEET_NAME)
        except gspread.WorksheetNotFound:
            ws = sh.add_worksheet(title=settings.ONBOARDING_WORKSHEET_NAME, rows=1000, cols=35)
            headers = [
                "Onboarding ID", "Lead ID", "Email", "Business Name", "Contact Name",
                "Company Type", "KYC Status", "KYC Status Detail", "KYC Follow-ups",
                "KYC Company Name", "KYC Contact Name", "KYC Contact Number",
                "NDA Status", "NDA Status Detail", "NDA Follow-ups",
                "Agreement Status", "Agreement Status Detail", "Agreement Follow-ups",
                "Created At", "Updated At", "Stage Score (0-11)",
                "GSTIN Valid", "GSTIN Source", "GSTIN API Name",
                "PAN Valid", "CIN Valid",
                "KYC Overall Passed", "KYC Auto Approvable",
                "OCR Company Name", "OCR Doc Types", "KYC Issues",
            ]
            ws.append_row(headers)

        clean_row = [str(v) if v is not None else "" for v in row]

        # Find existing row by onboarding_id and update, or append
        all_rows = ws.get_all_values()
        for i, r in enumerate(all_rows[1:], start=2):  # skip header
            if r and r[0] == str(rec.id):
                ws.update([clean_row], f"A{i}:AE{i}")
                return

        ws.append_row(clean_row)
    except Exception as exc:
        logger.warning("onboarding_sheets_export_failed", error=str(exc))


@celery_app.task(bind=True, max_retries=2, default_retry_delay=30)
def export_onboarding_to_sheets(self, onboarding_id: str) -> None:
    try:
        run_async(_export_to_sheets, onboarding_id)
    except Exception as exc:
        raise self.retry(exc=exc)


# ---------------------------------------------------------------------------
# IMAP: detect signed document replies
# ---------------------------------------------------------------------------

async def _process_signed_doc_reply(
    db: AsyncSession,
    from_email: str,
    attachment_bytes: bytes,
    attachment_filename: str,
) -> None:
    """Called when IMAP detects a reply with an attachment from a lead."""
    # Find lead by email
    lead_result = await db.execute(
        select(LeadV2).where(LeadV2.email == from_email.lower().strip())
    )
    lead = lead_result.scalar_one_or_none()
    if not lead:
        return

    # Find their onboarding record
    rec_result = await db.execute(
        select(OnboardingRecord).where(OnboardingRecord.lead_id == lead.id)
    )
    rec = rec_result.scalar_one_or_none()
    if not rec:
        return

    now = _now_ist()
    doc_type = None

    if rec.nda_status == DocumentStatus.SENT_TO_LEAD:
        doc_type = "NDA"
        try:
            from app.services.zoho_workdrive import upload_file
            file_id = upload_file(
                attachment_bytes,
                f"NDA_Signed_{lead.business_name}_{str(rec.id)[:8]}_{attachment_filename}",
                mime_type="application/pdf",
            )
            rec.nda_signed_zoho_file_id = file_id
        except Exception as exc:
            logger.warning("signed_nda_upload_failed", error=str(exc))

        rec.nda_status = DocumentStatus.SIGN_UNDER_REVIEW
        rec.nda_signed_received_at = now
        rec.nda_status_display = f"Signed NDA Received — Pending Team Review ({_fmt(now)})"

    elif rec.agreement_status == DocumentStatus.SENT_TO_LEAD:
        doc_type = "Customer Agreement"
        try:
            from app.services.zoho_workdrive import upload_file
            file_id = upload_file(
                attachment_bytes,
                f"Agreement_Signed_{lead.business_name}_{str(rec.id)[:8]}_{attachment_filename}",
                mime_type="application/pdf",
            )
            rec.agreement_signed_zoho_file_id = file_id
        except Exception as exc:
            logger.warning("signed_agreement_upload_failed", error=str(exc))

        rec.agreement_status = DocumentStatus.SIGN_UNDER_REVIEW
        rec.agreement_signed_received_at = now
        rec.agreement_status_display = f"Signed Agreement Received — Pending Team Review ({_fmt(now)})"

    if doc_type:
        await db.commit()
        event_type = "NDA_SIGNED_RECEIVED" if doc_type == "NDA" else "AGREEMENT_SIGNED_RECEIVED"
        log_pipeline(event_type, company=lead.business_name, email=from_email,
                     detail=f"Signed {doc_type} received — pending team review")
        from app.services.onboarding_email import notify_team_signed_doc_received
        notify_team_signed_doc_received(lead.contact_name or "", lead.business_name, str(rec.id), doc_type)
        await _export_to_sheets(db, str(rec.id))


@celery_app.task(bind=True, max_retries=2)
def process_signed_doc_reply(
    self,
    from_email: str,
    attachment_bytes: bytes,
    attachment_filename: str,
) -> None:
    try:
        run_async(_process_signed_doc_reply, from_email, attachment_bytes, attachment_filename)
    except Exception as exc:
        raise self.retry(exc=exc)


# ---------------------------------------------------------------------------
# Default templates (fallback when no Zoho template is configured)
# ---------------------------------------------------------------------------

def _default_nda_template(company_type: str) -> str:
    gst_clause = (
        "\n5. GST Registration: {{company_name}} confirms its GST number is on file with Jane Aerospace.\n"
        if company_type == "indian" else ""
    )
    return f"""NON-DISCLOSURE AGREEMENT

This Non-Disclosure Agreement ("Agreement") is entered into as of {{date}},
between Jane Aerospace ("Disclosing Party") and {{company_name}} ("Receiving Party").

1. Parties
   Disclosing Party: Jane Aerospace
   Receiving Party: {{company_name}}
   Contact: {{contact_name}}, {{contact_number}}

2. Confidential Information
   The Receiving Party agrees to keep confidential all technical, commercial, and business
   information shared by Jane Aerospace in connection with a potential partnership.

3. Obligations
   The Receiving Party shall not disclose Confidential Information to any third party
   without prior written consent from Jane Aerospace.

4. Term
   This Agreement shall remain in effect for a period of two (2) years from the date above.
{gst_clause}
IN WITNESS WHEREOF, the parties have executed this Agreement as of the date first written above.

Jane Aerospace                          {{company_name}}
Signature: ___________________          Signature: ___________________
Name: {settings.ORGANIZER_NAME}        Name: {{contact_name}}
Title: Founder & Managing Director      Title: ___________________
Date: {{date}}                          Date: ___________________
"""


def _default_agreement_template(company_type: str) -> str:
    return f"""CUSTOMER AGREEMENT

This Customer Agreement ("Agreement") is entered into as of {{date}},
between Jane Aerospace ("Service Provider") and {{company_name}} ("Customer").

1. Parties
   Service Provider: Jane Aerospace
   Customer: {{company_name}}
   Contact: {{contact_name}}, {{contact_number}}

2. Services
   Jane Aerospace agrees to provide aerospace component supply and related services
   as mutually agreed upon in the Statement of Work (SOW).

3. Payment Terms
   Payment terms shall be as specified in individual purchase orders.

4. Confidentiality
   Both parties agree to maintain confidentiality as per the previously executed NDA.

5. Term
   This Agreement shall remain in effect until terminated by either party with 30 days written notice.

6. Governing Law
   {"This Agreement shall be governed by the laws of India." if company_type == "indian" else "This Agreement shall be governed by applicable international commercial law."}

IN WITNESS WHEREOF, the parties have executed this Agreement.

Jane Aerospace                          {{company_name}}
Signature: ___________________          Signature: ___________________
Name: {settings.ORGANIZER_NAME}        Name: {{contact_name}}
Title: Founder & Managing Director      Title: ___________________
Date: {{date}}                          Date: ___________________
"""
