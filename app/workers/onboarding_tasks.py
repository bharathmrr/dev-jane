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
from app.db.base import CompanyType, DocumentStatus, KYCStatus
from app.db.models import KYCSubmission, LeadV2, OnboardingRecord
from app.workers.celery_app import celery_app
from app.workers.runtime import run_async

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

    # Send KYC form email
    from app.services.onboarding_email import send_kyc_form_email
    send_kyc_form_email(lead.email, lead.contact_name or lead.business_name, lead.business_name, kyc_url)

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

    # Get KYC data
    kyc_result = await db.execute(
        select(KYCSubmission)
        .where(KYCSubmission.onboarding_id == rec.id)
        .order_by(KYCSubmission.attempt_number.desc())
    )
    kyc = kyc_result.scalars().first()
    if not kyc:
        return

    kyc_data = {
        "company_name": kyc.company_name,
        "contact_name": kyc.contact_name,
        "contact_number": kyc.contact_number,
        "company_type": kyc.company_type,
        "date": _now_ist().strftime("%d %B %Y"),
    }

    # Fetch template from Zoho WorkDrive
    company_type = rec.company_type or "indian"
    template_id = (
        settings.ZOHO_NDA_TEMPLATE_ID_INDIAN
        if company_type == "indian"
        else settings.ZOHO_NDA_TEMPLATE_ID_OVERSEAS
    )

    template_content = ""
    if template_id:
        try:
            from app.services.zoho_workdrive import download_file
            raw = download_file(template_id)
            template_content = raw.decode("utf-8", errors="ignore")
        except Exception as exc:
            print(f"[NDA] Failed to fetch template from WorkDrive: {exc}")
            template_content = _default_nda_template(company_type)
    else:
        template_content = _default_nda_template(company_type)

    # AI fills the template
    from app.services.onboarding_ai import fill_document_template
    filled = fill_document_template(template_content, kyc_data, "NDA")

    now = _now_ist()
    rec.nda_draft_content = filled
    rec.nda_status = DocumentStatus.TEAM_REVIEW
    rec.nda_status_display = f"NDA Draft Generated — Pending Team Review ({_fmt(now)})"

    # Upload filled draft to Zoho WorkDrive
    lead = await db.get(LeadV2, rec.lead_id)
    company_name = kyc.company_name
    try:
        from app.services.zoho_workdrive import upload_file
        file_id = upload_file(
            filled.encode("utf-8"),
            f"NDA_Draft_{company_name}_{onboarding_id[:8]}.txt",
            mime_type="text/plain",
        )
        rec.nda_draft_zoho_file_id = file_id
    except Exception as exc:
        print(f"[NDA] Draft upload failed: {exc}")

    await db.commit()
    await _export_to_sheets(db, onboarding_id)


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
    if not rec or not rec.nda_draft_content:
        return
    lead = await db.get(LeadV2, rec.lead_id)
    if not lead:
        return

    from app.services.onboarding_email import send_nda_to_lead
    send_nda_to_lead(lead.email, lead.contact_name or "", lead.business_name, rec.nda_draft_content)


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

    kyc_data = {
        "company_name": kyc.company_name,
        "contact_name": kyc.contact_name,
        "contact_number": kyc.contact_number,
        "company_type": kyc.company_type,
        "date": _now_ist().strftime("%d %B %Y"),
    }

    company_type = rec.company_type or "indian"
    template_id = (
        settings.ZOHO_AGREEMENT_TEMPLATE_ID_INDIAN
        if company_type == "indian"
        else settings.ZOHO_AGREEMENT_TEMPLATE_ID_OVERSEAS
    )

    template_content = ""
    if template_id:
        try:
            from app.services.zoho_workdrive import download_file
            raw = download_file(template_id)
            template_content = raw.decode("utf-8", errors="ignore")
        except Exception as exc:
            print(f"[AGREEMENT] Failed to fetch template: {exc}")
            template_content = _default_agreement_template(company_type)
    else:
        template_content = _default_agreement_template(company_type)

    from app.services.onboarding_ai import fill_document_template
    filled = fill_document_template(template_content, kyc_data, "Customer Agreement")

    now = _now_ist()
    rec.agreement_draft_content = filled
    rec.agreement_status = DocumentStatus.TEAM_REVIEW
    rec.agreement_status_display = f"Agreement Draft Generated — Pending Team Review ({_fmt(now)})"

    try:
        from app.services.zoho_workdrive import upload_file
        file_id = upload_file(
            filled.encode("utf-8"),
            f"Agreement_Draft_{kyc.company_name}_{onboarding_id[:8]}.txt",
            mime_type="text/plain",
        )
        rec.agreement_draft_zoho_file_id = file_id
    except Exception:
        pass

    await db.commit()
    await _export_to_sheets(db, onboarding_id)


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
    if not rec or not rec.agreement_draft_content:
        return
    lead = await db.get(LeadV2, rec.lead_id)
    if not lead:
        return

    from app.services.onboarding_email import send_agreement_to_lead
    send_agreement_to_lead(
        lead.email, lead.contact_name or "", lead.business_name, rec.agreement_draft_content
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
            ws = sh.add_worksheet(title=settings.ONBOARDING_WORKSHEET_NAME, rows=1000, cols=25)
            headers = [
                "Onboarding ID", "Lead ID", "Email", "Business Name", "Contact Name",
                "Company Type", "KYC Status", "KYC Status Detail", "KYC Follow-ups",
                "KYC Company Name", "KYC Contact Name", "KYC Contact Number",
                "NDA Status", "NDA Status Detail", "NDA Follow-ups",
                "Agreement Status", "Agreement Status Detail", "Agreement Follow-ups",
                "Created At", "Updated At", "Stage Score (0-11)",
            ]
            ws.append_row(headers)

        # Find existing row by onboarding_id and update, or append
        all_rows = ws.get_all_values()
        for i, r in enumerate(all_rows[1:], start=2):  # skip header
            if r and r[0] == str(rec.id):
                ws.update(f"A{i}:U{i}", [row])
                return

        ws.append_row(row)
    except Exception as exc:
        print(f"[ONBOARDING SHEETS] Export failed: {exc}")


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
        # Upload signed copy to Zoho WorkDrive
        try:
            from app.services.zoho_workdrive import upload_file
            file_id = upload_file(
                attachment_bytes,
                f"NDA_Signed_{lead.business_name}_{str(rec.id)[:8]}_{attachment_filename}",
                mime_type="application/pdf",
            )
            rec.nda_signed_zoho_file_id = file_id
        except Exception as exc:
            print(f"[SIGNED DOC] Upload failed: {exc}")

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
            print(f"[SIGNED DOC] Upload failed: {exc}")

        rec.agreement_status = DocumentStatus.SIGN_UNDER_REVIEW
        rec.agreement_signed_received_at = now
        rec.agreement_status_display = f"Signed Agreement Received — Pending Team Review ({_fmt(now)})"

    if doc_type:
        await db.commit()
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
