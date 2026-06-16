"""Jane Aerospace — independent tracking dashboard.

Single-page app served at /api/v1/dashboard (HTML lives in app/templates/dashboard.html).

Roles:
  ADMIN      — everything: users, approvals, lead actions, bulk send
  ORGANIZER  — "Approver": approvals + lead actions (no user management)
  AGENT      — "Viewer": read-only

Default super-admin (seeded on first login attempt):
  admin@janeaerospace.co.in / admin123

JSON APIs (Bearer token):
  POST /dashboard/api/login
  GET  /dashboard/api/overview        — funnel, counters, charts, integration health
  GET  /dashboard/api/leads           — per-lead journey rows
  GET  /dashboard/api/lead/{id}       — full detail (KYC data, docs, booking, journey)
  POST /dashboard/api/lead/{id}/action — start_onboarding | cancel_onboarding | drop | restore
  POST /dashboard/api/add-lead        — single lead add (+CRM +Google Sheet append)
  GET  /dashboard/api/bookings        — month calendar: bookings, open slots, holidays, blocked days
  POST /dashboard/api/day-block       — block/unblock a calendar day
  GET  /dashboard/api/approvals       — pending approvals (KYC / NDA / Agreement)
  POST /dashboard/api/approvals/act   — approve / reject
  GET  /dashboard/api/logs            — pipeline activity feed
  POST /dashboard/api/bulk-start      — bulk onboarding, duplicates skipped
  GET/POST/DELETE /dashboard/api/users
"""
from __future__ import annotations

import datetime as dt
import json
import re
import uuid
from pathlib import Path
from zoneinfo import ZoneInfo

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException
from fastapi.responses import HTMLResponse
from pydantic import BaseModel
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.deps import get_current_user
from app.core.logging import get_logger
from app.core.security import create_access_token, hash_password, verify_password
from app.db.base import DocumentStatus, KYCStatus, LeadStatus, SlotStatus, UserRole
from app.db.models import (
    AvailableDateV2,
    KYCSubmission,
    LeadV2,
    OnboardingRecord,
    User,
    ZohoSlot,
)
from app.db.session import get_db

logger = get_logger("dashboard")
_IST = ZoneInfo("Asia/Kolkata")

router = APIRouter(prefix="/dashboard", tags=["dashboard"])

DEFAULT_ADMIN_EMAIL = "admin@janeaerospace.co.in"
DEFAULT_ADMIN_PASSWORD = "admin123"

_HTML_PATH = Path(__file__).resolve().parents[2] / "templates" / "dashboard.html"

# Role labels shown in the UI
_ROLE_LABEL = {"admin": "admin", "organizer": "approver", "agent": "viewer"}

# Indian public holidays 2026 (approximate where lunar)
INDIA_HOLIDAYS_2026 = {
    "2026-01-26": "Republic Day",
    "2026-03-04": "Holi",
    "2026-03-21": "Eid al-Fitr",
    "2026-04-03": "Good Friday",
    "2026-05-01": "May Day",
    "2026-05-27": "Eid al-Adha",
    "2026-08-15": "Independence Day",
    "2026-10-02": "Gandhi Jayanti",
    "2026-10-20": "Dussehra",
    "2026-11-08": "Diwali",
    "2026-12-25": "Christmas",
}


def _fmt(d: dt.datetime | None) -> str:
    return d.astimezone(_IST).strftime("%d %b %Y %H:%M") if d else ""


def _require_admin(user: User = Depends(get_current_user)) -> User:
    if user.role != UserRole.ADMIN:
        raise HTTPException(403, "Admin access required")
    return user


def _require_approver(user: User = Depends(get_current_user)) -> User:
    if user.role not in (UserRole.ADMIN, UserRole.ORGANIZER):
        raise HTTPException(403, "Approver or admin access required")
    return user


# ---------------------------------------------------------------------------
# Auth
# ---------------------------------------------------------------------------

class _LoginBody(BaseModel):
    email: str
    password: str


@router.post("/api/login")
async def dashboard_login(body: _LoginBody, db: AsyncSession = Depends(get_db)):
    has_admin = (await db.execute(
        select(func.count()).select_from(User).where(User.role == UserRole.ADMIN)
    )).scalar() or 0
    if not has_admin:
        db.add(User(
            email=DEFAULT_ADMIN_EMAIL,
            full_name="Super Admin",
            hashed_password=hash_password(DEFAULT_ADMIN_PASSWORD),
            role=UserRole.ADMIN,
            is_active=True,
        ))
        await db.commit()
        logger.info("dashboard_default_admin_seeded", email=DEFAULT_ADMIN_EMAIL)

    user = (await db.execute(
        select(User).where(User.email == body.email.lower().strip())
    )).scalar_one_or_none()
    if not user or not user.is_active or not verify_password(body.password, user.hashed_password):
        raise HTTPException(401, "Invalid email or password")

    return {
        "access_token": create_access_token(str(user.id), user.role.value),
        "role": _ROLE_LABEL.get(user.role.value, user.role.value),
        "full_name": user.full_name,
        "email": user.email,
    }


# ---------------------------------------------------------------------------
# User management (admin only)
# ---------------------------------------------------------------------------

class _UserCreate(BaseModel):
    email: str
    full_name: str
    password: str
    role: str = "viewer"   # "admin" | "approver" | "viewer"


@router.get("/api/users")
async def list_users(db: AsyncSession = Depends(get_db), _: User = Depends(_require_admin)):
    users = (await db.execute(select(User).order_by(User.created_at))).scalars().all()
    return [{
        "id": str(u.id), "email": u.email, "full_name": u.full_name,
        "role": _ROLE_LABEL.get(u.role.value, u.role.value), "is_active": u.is_active,
        "created_at": _fmt(u.created_at),
    } for u in users]


@router.post("/api/users")
async def create_user(body: _UserCreate, db: AsyncSession = Depends(get_db),
                      _: User = Depends(_require_admin)):
    email = body.email.lower().strip()
    if not re.match(r"^[^@\s]+@[^@\s]+\.[^@\s]+$", email):
        raise HTTPException(400, "Invalid email address")
    if len(body.password) < 6:
        raise HTTPException(400, "Password must be at least 6 characters")
    exists = (await db.execute(select(User).where(User.email == email))).scalar_one_or_none()
    if exists:
        raise HTTPException(409, "A user with this email already exists")
    role = {"admin": UserRole.ADMIN, "approver": UserRole.ORGANIZER}.get(body.role, UserRole.AGENT)
    db.add(User(email=email, full_name=body.full_name.strip() or email,
                hashed_password=hash_password(body.password), role=role, is_active=True))
    await db.commit()
    return {"message": f"User {email} created ({_ROLE_LABEL.get(role.value, role.value)})"}


@router.delete("/api/users/{user_id}")
async def delete_user(user_id: str, db: AsyncSession = Depends(get_db),
                      admin: User = Depends(_require_admin)):
    if str(admin.id) == user_id:
        raise HTTPException(400, "You cannot delete your own account")
    u = await db.get(User, uuid.UUID(user_id))
    if not u:
        raise HTTPException(404, "User not found")
    await db.delete(u)
    await db.commit()
    return {"message": f"User {u.email} removed"}


# ---------------------------------------------------------------------------
# Stage computation
# ---------------------------------------------------------------------------

STAGES = [
    "New Lead", "Email Sent", "Replied", "Meeting Booked",
    "KYC Form Sent", "KYC Approved", "NDA Sent", "NDA Signed",
    "Agreement Sent", "Completed",
]


def _stage_index(lead: LeadV2, rec: OnboardingRecord | None) -> int:
    if rec:
        a, n, k = rec.agreement_status, rec.nda_status, rec.kyc_status
        if a in (DocumentStatus.APPROVED, DocumentStatus.PROCEED_NEXT):
            return 9
        if a in (DocumentStatus.SENT_TO_LEAD, DocumentStatus.SIGN_UNDER_REVIEW,
                 DocumentStatus.SIGNED_RECEIVED, DocumentStatus.TEAM_REVIEW,
                 DocumentStatus.DRAFT_GENERATED):
            return 8
        if n in (DocumentStatus.APPROVED, DocumentStatus.SIGN_UNDER_REVIEW,
                 DocumentStatus.SIGNED_RECEIVED):
            return 7
        if n in (DocumentStatus.SENT_TO_LEAD, DocumentStatus.TEAM_REVIEW,
                 DocumentStatus.DRAFT_GENERATED):
            return 6
        if k == KYCStatus.APPROVED:
            return 5
        return 4
    if lead.status == LeadStatus.BOOKED:
        return 3
    if lead.status == LeadStatus.REPLIED:
        return 2
    if lead.status == LeadStatus.SENT:
        return 1
    return 0


def _signed(raw: str | None) -> bool:
    try:
        return bool((json.loads(raw or "") or {}).get("signature"))
    except (ValueError, TypeError):
        return False


def _sig_info(raw: str | None) -> dict | None:
    try:
        return (json.loads(raw or "") or {}).get("signature")
    except (ValueError, TypeError):
        return None


def _doc_stage_label(rec: OnboardingRecord, doc_type: str) -> str:
    """Granular stage label for the Documents dashboard column.

    Returns one of: <prefix>_sent | <prefix>_modify | lead_accept |
                    sign_sent | signed | (raw status as fallback)
    """
    prefix = "nda" if doc_type == "nda" else "agr"
    status = rec.nda_status if doc_type == "nda" else rec.agreement_status
    content = rec.nda_draft_content if doc_type == "nda" else rec.agreement_draft_content
    approved = (
        status == DocumentStatus.APPROVED
        if doc_type == "nda"
        else status in (DocumentStatus.APPROVED, DocumentStatus.PROCEED_NEXT)
    )
    if approved:
        return "signed"
    try:
        data = json.loads(content or "")
        if data.get("signature"):
            return "signed"
        if data.get("internal_signature") or data.get("stage") == "awaiting_lead_sign":
            return "sign_sent"
        stage = data.get("stage") or ""
        if stage == "accepted":
            return "lead_accept"
        if stage == "changes_requested":
            return f"{prefix}_modify"
        if stage == "review":
            return f"{prefix}_sent"
    except (ValueError, TypeError):
        pass
    if status == DocumentStatus.TEAM_REVIEW:
        return f"{prefix}_modify"
    if status == DocumentStatus.SENT_TO_LEAD:
        return f"{prefix}_sent"
    return f"{status}" if status else ""


# ---------------------------------------------------------------------------
# Overview
# ---------------------------------------------------------------------------

@router.get("/api/overview")
async def overview(db: AsyncSession = Depends(get_db), _: User = Depends(get_current_user)):
    leads = (await db.execute(select(LeadV2))).scalars().all()
    recs = (await db.execute(select(OnboardingRecord))).scalars().all()
    rec_by_lead = {r.lead_id: r for r in recs}

    funnel = [0] * len(STAGES)
    for lead in leads:
        funnel[_stage_index(lead, rec_by_lead.get(lead.id))] += 1

    by_status: dict[str, int] = {}
    emails_sent = 0
    for lead in leads:
        s = lead.status.value if hasattr(lead.status, "value") else str(lead.status)
        by_status[s] = by_status.get(s, 0) + 1
        if lead.sent_at:
            emails_sent += 1 + (lead.follow_up_count or 0)

    # last-14-days activity (emails sent / replies / bookings per day)
    today = dt.datetime.now(_IST).date()
    days = [(today - dt.timedelta(days=i)) for i in range(13, -1, -1)]
    daily = {d.isoformat(): {"sent": 0, "replied": 0, "booked": 0} for d in days}
    for lead in leads:
        for attr, key in (("sent_at", "sent"), ("replied_at", "replied"), ("booked_at", "booked")):
            v = getattr(lead, attr)
            if v:
                k = v.astimezone(_IST).date().isoformat()
                if k in daily:
                    daily[k][key] += 1

    signed_nda = sum(1 for r in recs if r.nda_approved_at)
    signed_agr = sum(1 for r in recs if r.agreement_approved_at)
    lead_by_id = {l.id: l for l in leads}
    pending_approvals = 0
    waiting_on_lead = 0
    for r in recs:
        _lead = lead_by_id.get(r.lead_id)
        if _lead:
            _a, _w = _approval_items(r, _lead)
            pending_approvals += len(_a)
            waiting_on_lead += len(_w)

    integrations = {
        "Google Sheets (lead source)": bool(settings.GOOGLE_SHEETS_SPREADSHEET_ID),
        "Zoho CRM sync": bool(settings.ZOHO_CRM_REFRESH_TOKEN),
        "Zoho Mail SMTP (outbound)": bool(settings.SMTP_HOST and settings.SMTP_USERNAME),
        "Zoho Mail IMAP (replies)": bool(settings.IMAP_HOST and settings.IMAP_USERNAME),
        "Zoho Bookings (slots)": bool(settings.ZOHO_REFRESH_TOKEN),
        "Claude AI (email drafting)": bool(settings.ANTHROPIC_API_KEY),
        "PDF Documents (NDA/Agreement)": True,
    }

    return {
        "stages": STAGES,
        "funnel": funnel,
        "totals": {
            "leads": len(leads),
            "emails_sent": emails_sent,
            "replied": by_status.get("REPLIED", 0) + by_status.get("BOOKED", 0),
            "booked": sum(1 for l in leads if l.booked_at),
            "onboarding": len(recs),
            "nda_signed": signed_nda,
            "agreement_signed": signed_agr,
            "pending_approvals": pending_approvals,
            "waiting_on_lead": waiting_on_lead,
        },
        "by_status": by_status,
        "daily": daily,
        "integrations": integrations,
        "sheet_name": settings.GOOGLE_SHEETS_WORKSHEET_NAME,
        "app_url": settings.APP_URL,
        "generated_at": dt.datetime.now(_IST).strftime("%d %b %Y %H:%M:%S IST"),
    }


# ---------------------------------------------------------------------------
# Leads list + full detail
# ---------------------------------------------------------------------------

def _lead_row(lead: LeadV2, rec: OnboardingRecord | None) -> dict:
    from app.services.onboarding_email import make_doc_edit_url, make_doc_sign_url, make_kyc_view_url
    idx = _stage_index(lead, rec)
    row = {
        "lead_id": str(lead.id),
        "email": lead.email,
        "company": lead.business_name,
        "contact": lead.contact_name or "",
        "phone": lead.phone_number or "",
        "status": lead.status.value if hasattr(lead.status, "value") else str(lead.status),
        "stage_index": idx,
        "stage": STAGES[idx],
        "sent_at": _fmt(lead.sent_at),
        "replied_at": _fmt(lead.replied_at),
        "booked_at": _fmt(lead.booked_at),
        "selected_slot": lead.selected_slot or "",
        "followups": lead.follow_up_count or 0,
        "dropped": lead.opted_out,
        "onboarding": None,
    }
    if rec:
        oid = str(rec.id)
        row["onboarding"] = {
            "id": oid,
            "company_type": rec.company_type or "",
            "kyc_status": rec.kyc_status,
            "kyc_display": rec.kyc_status_display or "",
            "kyc_view_url": make_kyc_view_url(oid),
            "nda_status": rec.nda_status,
            "nda_display": rec.nda_status_display or "",
            "nda_signed": _signed(rec.nda_draft_content),
            "nda_stage": _doc_stage_label(rec, "nda"),
            "nda_edit_url": make_doc_edit_url(oid, "nda"),
            "nda_sign_url": make_doc_sign_url(oid, "nda"),
            "agreement_status": rec.agreement_status,
            "agreement_display": rec.agreement_status_display or "",
            "agreement_signed": _signed(rec.agreement_draft_content),
            "agreement_stage": _doc_stage_label(rec, "agreement"),
            "agreement_edit_url": make_doc_edit_url(oid, "agreement"),
            "agreement_sign_url": make_doc_sign_url(oid, "agreement"),
        }
    return row


@router.get("/api/leads")
async def dashboard_leads(db: AsyncSession = Depends(get_db), _: User = Depends(get_current_user)):
    leads = (await db.execute(
        select(LeadV2).order_by(LeadV2.created_at.desc())
    )).scalars().all()
    recs = (await db.execute(select(OnboardingRecord))).scalars().all()
    rec_by_lead = {r.lead_id: r for r in recs}
    return [_lead_row(lead, rec_by_lead.get(lead.id)) for lead in leads]


@router.get("/api/lead/{lead_id}")
async def lead_detail(lead_id: str, db: AsyncSession = Depends(get_db),
                      _: User = Depends(get_current_user)):
    lead = await db.get(LeadV2, uuid.UUID(lead_id))
    if not lead:
        raise HTTPException(404, "Lead not found")
    rec = (await db.execute(
        select(OnboardingRecord).where(OnboardingRecord.lead_id == lead.id)
    )).scalar_one_or_none()

    data = _lead_row(lead, rec)
    data["summary"] = lead.summary or ""
    data["location"] = lead.location or ""
    data["designation"] = lead.designation or ""
    data["meeting_link"] = lead.zoho_meeting_link or ""
    data["booking_id"] = lead.booking_id or ""
    data["created_at"] = _fmt(lead.created_at)

    # Journey timeline
    timeline = []
    if lead.created_at:
        timeline.append({"label": "Lead Created", "at": _fmt(lead.created_at)})
    if lead.sent_at:
        timeline.append({"label": "Outreach Email Sent", "at": _fmt(lead.sent_at)})
    if lead.replied_at:
        timeline.append({"label": "Lead Replied", "at": _fmt(lead.replied_at)})
    if lead.booked_at:
        timeline.append({"label": f"Meeting Booked {('— ' + lead.selected_slot) if lead.selected_slot else ''}",
                         "at": _fmt(lead.booked_at)})
    if rec:
        if rec.kyc_form_sent_at:
            timeline.append({"label": "KYC Form Sent", "at": _fmt(rec.kyc_form_sent_at)})
        if rec.kyc_submitted_at:
            timeline.append({"label": "KYC Submitted", "at": _fmt(rec.kyc_submitted_at)})
        if rec.kyc_approved_at:
            timeline.append({"label": "KYC Approved", "at": _fmt(rec.kyc_approved_at)})
        if rec.nda_sent_at:
            timeline.append({"label": "NDA Sent for Signature", "at": _fmt(rec.nda_sent_at)})
        if rec.nda_approved_at:
            timeline.append({"label": "NDA Signed", "at": _fmt(rec.nda_approved_at)})
        if rec.agreement_sent_at:
            timeline.append({"label": "Agreement Sent for Signature", "at": _fmt(rec.agreement_sent_at)})
        if rec.agreement_approved_at:
            timeline.append({"label": "Agreement Signed — Complete", "at": _fmt(rec.agreement_approved_at)})
    data["timeline"] = timeline

    # Full KYC submission data
    if rec:
        kyc = (await db.execute(
            select(KYCSubmission)
            .where(KYCSubmission.onboarding_id == rec.id)
            .order_by(KYCSubmission.attempt_number.desc())
        )).scalars().first()
        if kyc:
            extra = (kyc.kyc_verification_result or {}).get("extra_fields", {}) or {}
            kyc_fields = {
                "Company Name": kyc.company_name,
                "Company Type": (kyc.company_type or "").title(),
                "Contact Name": kyc.contact_name,
                "Contact Number": kyc.contact_number,
                "GSTIN": kyc.gstin_number or "",
                "PAN": kyc.pan_number or "",
                "CIN": kyc.cin_number or "",
                "Attempt #": kyc.attempt_number,
            }
            label_map = {
                "trade_name": "Trade Name", "entity_type": "Entity Type",
                "date_of_incorporation": "Date of Incorporation",
                "registered_address": "Registered Address", "city": "City",
                "pin_code": "PIN Code", "state": "State", "country": "Country",
                "nature_of_business": "Nature of Business",
                "signatory1_designation": "Signatory Designation",
                "bank_name": "Bank Name", "account_number": "Account No",
                "ifsc_code": "IFSC", "swift_code": "SWIFT", "iban_number": "IBAN",
                "annual_turnover": "Annual Turnover",
                "country_of_incorporation": "Country of Incorporation",
                "company_reg_number": "Company Reg No", "tax_id_tin": "Tax ID / TIN",
                "lei_number": "LEI", "vat_gst_number": "VAT/GST",
                "company_website": "Website",
            }
            for k, lbl in label_map.items():
                kyc_fields[lbl] = extra.get(k) or ""
            data["kyc_data"] = {k: (str(v) if v else "—") for k, v in kyc_fields.items()}
            data["kyc_files"] = {
                "gst": bool(kyc.gst_certificate_zoho_id),
                "incorporation": bool(kyc.incorporation_zoho_id),
            }
        nda_sig = _sig_info(rec.nda_draft_content)
        agr_sig = _sig_info(rec.agreement_draft_content)
        if nda_sig:
            data["nda_signature"] = nda_sig
        if agr_sig:
            data["agreement_signature"] = agr_sig
    return data


# ---------------------------------------------------------------------------
# Lead actions (admin + approver)
# ---------------------------------------------------------------------------

class _ActionBody(BaseModel):
    action: str   # start_onboarding | cancel_onboarding | drop | restore


@router.post("/api/lead/{lead_id}/action")
async def lead_action(lead_id: str, body: _ActionBody, db: AsyncSession = Depends(get_db),
                      user: User = Depends(_require_approver)):
    lead = await db.get(LeadV2, uuid.UUID(lead_id))
    if not lead:
        raise HTTPException(404, "Lead not found")
    rec = (await db.execute(
        select(OnboardingRecord).where(OnboardingRecord.lead_id == lead.id)
    )).scalar_one_or_none()

    if body.action == "start_onboarding":
        if rec:
            return {"message": "Onboarding already in progress"}
        from app.workers.onboarding_tasks import initiate_onboarding_task
        initiate_onboarding_task.delay(str(lead.id))
        from app.core.pipeline_logger import log_pipeline
        log_pipeline("ONBOARDING_STARTED", company=lead.business_name, email=lead.email,
                     detail=f"Started from dashboard by {user.email}")
        return {"message": f"Onboarding started — KYC form is being sent to {lead.email}"}

    if body.action == "cancel_onboarding":
        if not rec:
            raise HTTPException(404, "No onboarding in progress for this lead")
        await db.delete(rec)
        await db.commit()
        from app.core.pipeline_logger import log_pipeline
        log_pipeline("ONBOARDING_CANCELLED", company=lead.business_name, email=lead.email,
                     detail=f"Cancelled from dashboard by {user.email}")
        return {"message": f"Onboarding cancelled for {lead.business_name}"}

    if body.action == "drop":
        lead.opted_out = True
        await db.commit()
        from app.core.pipeline_logger import log_pipeline
        log_pipeline("LEAD_DROPPED", company=lead.business_name, email=lead.email,
                     detail=f"Dropped from dashboard by {user.email}")
        return {"message": f"Lead {lead.email} dropped — no further emails will be sent"}

    if body.action == "restore":
        lead.opted_out = False
        await db.commit()
        return {"message": f"Lead {lead.email} restored"}

    raise HTTPException(400, "Unknown action")


# ---------------------------------------------------------------------------
# Add single lead (+ icon) — DB + CRM + Google Sheet append
# ---------------------------------------------------------------------------

class _AddLeadBody(BaseModel):
    email: str
    company_name: str = ""
    contact_name: str = ""
    phone: str = ""
    summary: str = ""
    start_onboarding: bool = False


def _append_to_input_sheet(row: dict) -> None:
    """Append the new lead to the Google 'Lead Information' worksheet (best effort)."""
    try:
        import gspread
        from google.oauth2.service_account import Credentials
        creds = Credentials.from_service_account_info(
            json.loads(settings.GOOGLE_SHEETS_CREDENTIALS_JSON),
            scopes=["https://www.googleapis.com/auth/spreadsheets"],
        )
        gc = gspread.authorize(creds)
        ws = gc.open_by_key(settings.GOOGLE_SHEETS_SPREADSHEET_ID).worksheet(
            settings.GOOGLE_SHEETS_WORKSHEET_NAME)
        from gspread.utils import ValueInputOption
        headers = [h.strip().lower().replace(" ", "_") for h in ws.row_values(1)]
        ws.append_row([str(row.get(h, "")) for h in headers],
                      value_input_option=ValueInputOption.user_entered)
        logger.info("dashboard_lead_appended_to_sheet", email=row.get("email"))
    except Exception as exc:
        logger.warning("dashboard_sheet_append_failed", error=str(exc))


def _crm_push(email: str, contact: str, company: str, phone: str, summary: str) -> None:
    try:
        from app.services.zoho_crm import upsert_lead
        upsert_lead(email=email, contact_name=contact or company, company_name=company,
                    phone=phone, summary=summary)
    except Exception as exc:
        logger.warning("dashboard_crm_push_failed", error=str(exc))


@router.post("/api/add-lead")
async def add_lead(body: _AddLeadBody, background: BackgroundTasks,
                   db: AsyncSession = Depends(get_db), user: User = Depends(_require_approver)):
    email = body.email.lower().strip()
    if not re.match(r"^[^@\s]+@[^@\s]+\.[^@\s]+$", email):
        raise HTTPException(400, "Invalid email address")

    existing = (await db.execute(select(LeadV2).where(LeadV2.email == email))).scalar_one_or_none()
    if existing:
        raise HTTPException(409, f"Lead {email} already exists ({existing.business_name})")

    company = body.company_name.strip() or email.split("@")[0].title()
    lead = LeadV2(
        email=email,
        business_name=company,
        contact_name=body.contact_name.strip() or None,
        phone_number=body.phone.strip() or None,
        summary=body.summary.strip() or None,
        status=LeadStatus.NEW,
    )
    db.add(lead)
    await db.flush()
    await db.commit()
    await db.refresh(lead)

    background.add_task(_crm_push, email, body.contact_name.strip(), company,
                        body.phone.strip(), body.summary.strip())
    background.add_task(_append_to_input_sheet, {
        "lead_type": "dashboard", "list_name": "Dashboard",
        "contact_person": body.contact_name.strip(),
        "name_of_the_company": company, "phone_number": body.phone.strip(),
        "email": email, "summary": body.summary.strip(),
    })

    msg = f"Lead {email} added — synced to CRM and Google Sheet."
    if body.start_onboarding:
        from app.workers.onboarding_tasks import initiate_onboarding_task
        initiate_onboarding_task.delay(str(lead.id))
        msg += " Onboarding started (KYC form being sent)."

    from app.core.pipeline_logger import log_pipeline
    log_pipeline("LEAD_ADDED", company=company, email=email,
                 detail=f"Added via dashboard by {user.email}")
    return {"message": msg, "lead_id": str(lead.id)}


# ---------------------------------------------------------------------------
# Bookings calendar
# ---------------------------------------------------------------------------

@router.get("/api/bookings")
async def bookings_calendar(year: int = 0, month: int = 0,
                            db: AsyncSession = Depends(get_db),
                            _: User = Depends(get_current_user)):
    now = dt.datetime.now(_IST)
    year = year or now.year
    month = month or now.month

    leads = (await db.execute(
        select(LeadV2).where(LeadV2.booked_at.isnot(None)).order_by(LeadV2.booked_at.desc())
    )).scalars().all()
    bookings = []
    for l in leads:
        if not l.booked_at:
            continue
        booked = l.booked_at.astimezone(_IST)
        bookings.append({
            "lead_id": str(l.id),
            "date": booked.date().isoformat(),
            "time": booked.strftime("%H:%M"),
            "company": l.business_name,
            "email": l.email,
            "slot": l.selected_slot or "",
            "meeting_link": l.zoho_meeting_link or "",
            "booked_at": _fmt(l.booked_at),
        })

    # Open slots from Zoho slot cache
    slots = (await db.execute(
        select(ZohoSlot).where(ZohoSlot.status == SlotStatus.AVAILABLE)
    )).scalars().all()
    open_by_day: dict[str, int] = {}
    for s in slots:
        d = s.slot_time.astimezone(_IST).date().isoformat()
        open_by_day[d] = open_by_day.get(d, 0) + 1

    # Blocked days (stored as midnight rows in available_dates_v2 with is_available=False)
    blocked = (await db.execute(
        select(AvailableDateV2).where(AvailableDateV2.is_available.is_(False))
    )).scalars().all()
    blocked_days = sorted({b.slot_datetime.astimezone(_IST).date().isoformat() for b in blocked})

    return {
        "year": year, "month": month,
        "bookings": bookings,
        "open_slots": open_by_day,
        "holidays": INDIA_HOLIDAYS_2026,
        "blocked_days": blocked_days,
        "organizer": settings.ORGANIZER_NAME,
    }


class _DayBlockBody(BaseModel):
    date: str          # YYYY-MM-DD
    block: bool


@router.post("/api/day-block")
async def day_block(body: _DayBlockBody, db: AsyncSession = Depends(get_db),
                    user: User = Depends(_require_approver)):
    try:
        day = dt.date.fromisoformat(body.date)
    except ValueError:
        raise HTTPException(400, "Invalid date — use YYYY-MM-DD")
    midnight = dt.datetime(day.year, day.month, day.day, tzinfo=_IST)

    existing = (await db.execute(
        select(AvailableDateV2).where(AvailableDateV2.slot_datetime == midnight)
    )).scalar_one_or_none()
    if body.block:
        if existing:
            existing.is_available = False
        else:
            db.add(AvailableDateV2(slot_datetime=midnight, is_available=False))
        msg = f"{body.date} marked as unavailable (holiday/blocked)"
    else:
        if existing:
            await db.delete(existing)
        msg = f"{body.date} unblocked"
    await db.commit()
    logger.info("dashboard_day_block", date=body.date, block=body.block, by=user.email)
    return {"message": msg}


def _doc_json(r: OnboardingRecord, doc_type: str) -> dict:
    raw = r.nda_draft_content if doc_type == "nda" else r.agreement_draft_content
    if raw:
        try:
            d = json.loads(raw)
            if isinstance(d, dict):
                return d
        except (ValueError, TypeError):
            pass
    return {}


def _approval_items(r: OnboardingRecord, lead: LeadV2) -> tuple[list[dict], list[dict]]:
    """Everything in flight for one onboarding record.

    Returns (action_required, waiting_on_lead).
    """
    from app.services.onboarding_email import (
        make_doc_edit_url,
        make_doc_sign_url,
        make_kyc_view_url,
    )
    oid = str(r.id)
    base = {"onboarding_id": oid, "company": lead.business_name, "email": lead.email,
            "company_type": r.company_type or ""}
    action: list[dict] = []
    waiting: list[dict] = []
    k, n, a = r.kyc_status, r.nda_status, r.agreement_status

    # KYC
    if k in (KYCStatus.SUBMITTED, KYCStatus.UNDER_REVIEW):
        action.append({**base, "kind": "kyc", "label": "KYC Submission",
                       "since": _fmt(r.kyc_submitted_at), "view_url": make_kyc_view_url(oid),
                       "display": r.kyc_status_display or ""})
    elif k in (KYCStatus.PENDING, KYCStatus.FORM_SENT):
        waiting.append({**base, "kind": "kyc_wait", "label": "KYC Form — lead filling",
                        "since": _fmt(r.kyc_form_sent_at), "view_url": "",
                        "display": r.kyc_status_display or "KYC form sent, awaiting submission"})
    elif k == KYCStatus.REJECTED:
        waiting.append({**base, "kind": "kyc_wait", "label": "KYC Rejected — lead must resubmit",
                        "since": _fmt(r.kyc_last_followup_at), "view_url": make_kyc_view_url(oid),
                        "display": r.kyc_status_display or ""})

    def _doc_items(doc_type: str, status, prev_ok: bool, sent_at, signed_at,
                   status_display, approved_at) -> None:
        """Stage-aware items for one document (NDA or Agreement)."""
        short = "NDA" if doc_type == "nda" else "Agreement"
        d = _doc_json(r, doc_type)
        stage = d.get("stage") or ""
        comments = d.get("comments") or []
        display = status_display or ""

        if prev_ok and status in (DocumentStatus.PENDING, DocumentStatus.TEAM_REVIEW,
                                  DocumentStatus.DRAFT_GENERATED):
            action.append({**base, "kind": f"{doc_type}_draft", "label": f"{short} — Review & Send",
                           "since": _fmt(approved_at), "view_url": make_doc_edit_url(oid, doc_type),
                           "display": display})
        if status == DocumentStatus.DRAFT_REJECTED:
            label = (f"{short} — Changes requested by lead ({len(comments)} comment{'s' if len(comments) != 1 else ''})"
                     if stage == "changes_requested" else f"{short} — Review & Send")
            action.append({**base, "kind": f"{doc_type}_draft", "label": label,
                           "since": _fmt(sent_at or approved_at),
                           "view_url": make_doc_edit_url(oid, doc_type),
                           "display": display, "comments": len(comments)})
        if status in (DocumentStatus.SIGNED_RECEIVED, DocumentStatus.SIGN_UNDER_REVIEW):
            action.append({**base, "kind": f"{doc_type}_sign", "label": f"Signed {short} — Review",
                           "since": _fmt(signed_at), "view_url": make_doc_edit_url(oid, doc_type),
                           "display": display})
        if status == DocumentStatus.SENT_TO_LEAD:
            if stage == "review":
                waiting.append({**base, "kind": f"{doc_type}_wait", "label": f"{short} — out for T&C review",
                                "since": _fmt(sent_at), "view_url": make_doc_sign_url(oid, doc_type),
                                "display": display})
            elif stage == "accepted":
                action.append({**base, "kind": f"{doc_type}_internal_sign",
                               "label": f"{short} — Terms accepted, internal signature required",
                               "since": _fmt(sent_at), "view_url": make_doc_edit_url(oid, doc_type),
                               "display": display})
            elif stage == "awaiting_lead_sign":
                waiting.append({**base, "kind": f"{doc_type}_wait",
                                "label": f"{short} — countersigned, awaiting lead signature",
                                "since": _fmt(sent_at), "view_url": make_doc_sign_url(oid, doc_type),
                                "display": display})
            else:  # legacy direct-sign links sent before the review flow existed
                waiting.append({**base, "kind": f"{doc_type}_wait", "label": f"{short} — out for signature",
                                "since": _fmt(sent_at), "view_url": make_doc_sign_url(oid, doc_type),
                                "display": display})

    _doc_items("nda", n, k == KYCStatus.APPROVED, r.nda_sent_at,
               r.nda_signed_received_at, r.nda_status_display, r.kyc_approved_at)
    _doc_items("agreement", a, n in (DocumentStatus.APPROVED, DocumentStatus.PROCEED_NEXT),
               r.agreement_sent_at, r.agreement_signed_received_at,
               r.agreement_status_display, r.nda_approved_at)
    return action, waiting


@router.get("/api/approvals")
async def approvals_list(db: AsyncSession = Depends(get_db), _: User = Depends(get_current_user)):
    recs = (await db.execute(select(OnboardingRecord))).scalars().all()
    action_all: list[dict] = []
    waiting_all: list[dict] = []
    for r in recs:
        lead = await db.get(LeadV2, r.lead_id)
        if not lead:
            continue
        action, waiting = _approval_items(r, lead)
        action_all.extend(action)
        waiting_all.extend(waiting)
    return {"action": action_all, "waiting": waiting_all}


class _ApprovalBody(BaseModel):
    onboarding_id: str
    kind: str       # kyc | nda_draft | agreement_draft
    action: str     # approve | reject
    notes: str = ""


@router.post("/api/approvals/act")
async def approvals_act(body: _ApprovalBody, db: AsyncSession = Depends(get_db),
                        user: User = Depends(_require_approver)):
    from app.api.v1.onboarding_endpoints import (
        ReviewRequest,
        agreement_draft_review,
        kyc_review,
        nda_draft_review,
    )
    review = ReviewRequest(action=body.action,
                           notes=(body.notes or f"Via dashboard by {user.email}"))
    if body.kind == "kyc":
        return await kyc_review(body.onboarding_id, review, db)
    if body.kind == "nda_draft":
        return await nda_draft_review(body.onboarding_id, review, db)
    if body.kind == "agreement_draft":
        return await agreement_draft_review(body.onboarding_id, review, db)
    raise HTTPException(400, "Unknown approval kind")


# ---------------------------------------------------------------------------
# Voice co-pilot — read-only assistant (brain behind the mic button)
# ---------------------------------------------------------------------------

class _AssistantBody(BaseModel):
    message: str
    history: list[dict] = []
    context: dict | None = None


@router.post("/api/assistant")
async def assistant_chat(body: _AssistantBody, db: AsyncSession = Depends(get_db),
                         user: User = Depends(get_current_user)):
    """One turn with the read-only voice co-pilot. Returns a spoken reply plus an
    optional UI navigation directive for the browser to execute. The agent cannot act."""
    from app.services.assistant import AssistantService
    svc = AssistantService(db, user)
    return await svc.run(body.message, body.history, body.context)


@router.get("/api/comments")
async def comments_feed(db: AsyncSession = Depends(get_db), _: User = Depends(get_current_user)):
    """Every lead document-change comment across all onboarding records — for the
    dashboard comments sidebar + notifications panel. Tracks which lead, which
    document, and when."""
    recs = (await db.execute(select(OnboardingRecord))).scalars().all()
    out: list[dict] = []
    for r in recs:
        lead = await db.get(LeadV2, r.lead_id)
        if not lead:
            continue
        for doc_type in ("nda", "agreement"):
            data = _doc_json(r, doc_type)
            for c in reversed(data.get("comments") or []):
                if not isinstance(c, dict):
                    continue
                out.append({
                    "company": lead.business_name or lead.email,
                    "email": lead.email,
                    "doc_type": doc_type,
                    "by": c.get("by") or "Lead",
                    "text": (c.get("text") or "")[:1000],
                    "at": c.get("at") or "",
                    "done": bool(c.get("done")),
                    "onboarding_id": str(r.id),
                })
    return {"comments": out, "open": sum(1 for c in out if not c["done"])}


# ---------------------------------------------------------------------------
# Google Sheet view — read + edit the Lead Information worksheet from the app
# ---------------------------------------------------------------------------

def _open_sheet():
    import gspread
    from google.oauth2.service_account import Credentials
    creds = Credentials.from_service_account_info(
        json.loads(settings.GOOGLE_SHEETS_CREDENTIALS_JSON),
        scopes=["https://www.googleapis.com/auth/spreadsheets"],
    )
    gc = gspread.authorize(creds)
    return gc.open_by_key(settings.GOOGLE_SHEETS_SPREADSHEET_ID).worksheet(
        settings.GOOGLE_SHEETS_WORKSHEET_NAME)


def _trim_headers(raw: list[str]) -> list[str]:
    last = -1
    for i, h in enumerate(raw):
        if h.strip():
            last = i
    return raw[:last + 1]


def _sheet_read() -> dict:
    ws = _open_sheet()
    values = ws.get_all_values()
    headers = _trim_headers(values[0]) if values else []
    n = len(headers)
    keys = [h.strip().lower().replace(" ", "_") for h in headers]
    rows = [r[:n] + [""] * (n - len(r[:n]))
            for r in values[1:] if any(c.strip() for c in r[:n])]
    return {
        "headers": headers,
        "keys": keys,
        "rows": rows,
        "sheet_url": f"https://docs.google.com/spreadsheets/d/{settings.GOOGLE_SHEETS_SPREADSHEET_ID}",
        "worksheet": settings.GOOGLE_SHEETS_WORKSHEET_NAME,
    }


def _sheet_save(values_by_key: dict) -> str:
    """Append a row, or update the existing row with the same email."""
    from gspread.utils import ValueInputOption
    ws = _open_sheet()
    all_values = ws.get_all_values()
    headers = _trim_headers(all_values[0]) if all_values else []
    keys = [h.strip().lower().replace(" ", "_") for h in headers]
    row_vals = [str(values_by_key.get(k, "")) for k in keys]

    email = (values_by_key.get("email") or "").strip().lower()
    if email and "email" in keys:
        ecol = keys.index("email")
        for i, row in enumerate(all_values[1:], start=2):
            if ecol < len(row) and row[ecol].strip().lower() == email:
                # keep non-empty old cells if the new value is blank
                old = row + [""] * (len(keys) - len(row))
                merged = [nv if nv.strip() else old[j] for j, nv in enumerate(row_vals)]
                ws.update(values=[merged], range_name=f"A{i}",
                          value_input_option=ValueInputOption.user_entered)
                return "updated"
    ws.append_row(row_vals, value_input_option=ValueInputOption.user_entered)
    return "appended"


@router.get("/api/sheet")
async def sheet_view(_: User = Depends(get_current_user)):
    import asyncio
    try:
        return await asyncio.to_thread(_sheet_read)
    except Exception as exc:
        raise HTTPException(502, f"Could not read Google Sheet: {str(exc)[:200]}")


class _SheetRowBody(BaseModel):
    values: dict   # normalized_header_key -> cell value


@router.post("/api/sheet")
async def sheet_save(body: _SheetRowBody, db: AsyncSession = Depends(get_db),
                     user: User = Depends(_require_approver)):
    import asyncio
    vals = {str(k): str(v or "").strip() for k, v in (body.values or {}).items()}
    email = (vals.get("email") or "").lower().strip()
    if not email or not re.match(r"^[^@\s]+@[^@\s]+\.[^@\s]+$", email):
        raise HTTPException(400, "A valid email is required to save a row")

    try:
        result = await asyncio.to_thread(_sheet_save, vals)
    except Exception as exc:
        raise HTTPException(502, f"Could not write to Google Sheet: {str(exc)[:200]}")

    # Mirror into leads DB (same field mapping as the worker sync) + CRM
    company = vals.get("name_of_the_company") or vals.get("contact_person") or email.split("@")[0].title()
    contact = vals.get("contact_person") or ""
    phone = vals.get("phone_number") or ""
    designation = vals.get("designation") or ""
    location = vals.get("location") or ""
    summary = vals.get("summary") or ""

    lead = (await db.execute(select(LeadV2).where(LeadV2.email == email))).scalar_one_or_none()
    if not lead:
        lead = LeadV2(email=email, business_name=company, contact_name=contact or None,
                      phone_number=phone or None, designation=designation or None,
                      location=location or None, summary=summary or None, status=LeadStatus.NEW)
        db.add(lead)
        db_result = "lead created"
    else:
        if company:
            lead.business_name = company
        if contact:
            lead.contact_name = contact
        if phone:
            lead.phone_number = phone
        if designation:
            lead.designation = designation
        if location:
            lead.location = location
        if summary:
            lead.summary = summary
        db_result = "lead updated"
    await db.commit()

    try:
        from app.services.zoho_crm import upsert_lead
        upsert_lead(email=email, contact_name=contact or company, company_name=company,
                    phone=phone, summary=summary, designation=designation, city=location)
    except Exception as exc:
        logger.warning("dashboard_sheet_crm_failed", email=email, error=str(exc))

    from app.core.pipeline_logger import log_pipeline
    log_pipeline("LEAD_ADDED" if db_result == "lead created" else "LEAD_UPDATED",
                 company=company, email=email,
                 detail=f"Sheet row {result} via dashboard by {user.email}")
    return {"message": f"Row {result} in Google Sheet · {db_result} · CRM synced"}


# ---------------------------------------------------------------------------
# Activity log
# ---------------------------------------------------------------------------

@router.get("/api/logs")
async def dashboard_logs(limit: int = 100, _: User = Depends(get_current_user)):
    path = Path("logs/pipeline.log")
    if not path.exists():
        return []
    lines = path.read_text(encoding="utf-8", errors="replace").splitlines()[-min(limit, 300):]
    out = []
    for line in reversed(lines):
        m = re.match(r"^(\S+ \S+ IST)\s+\[(\w+)\]\s+(.{0,42}?)\s{2,}(.{0,46}?)\s{2,}(.*)$", line)
        if m:
            out.append({"ts": m.group(1), "event": m.group(2),
                        "company": m.group(3).strip(" —"), "email": m.group(4).strip(" —"),
                        "detail": m.group(5).strip()})
        elif line.strip():
            out.append({"ts": "", "event": "LOG", "company": "", "email": "", "detail": line.strip()})
    return out


# ---------------------------------------------------------------------------
# Bulk start
# ---------------------------------------------------------------------------

class _BulkBody(BaseModel):
    emails: str


@router.post("/api/bulk-start")
async def bulk_start(body: _BulkBody, db: AsyncSession = Depends(get_db),
                     _: User = Depends(_require_approver)):
    raw = re.split(r"[\s,;]+", body.emails or "")
    emails, seen = [], set()
    for e in raw:
        e = e.strip().lower()
        if e and re.match(r"^[^@\s]+@[^@\s]+\.[^@\s]+$", e) and e not in seen:
            seen.add(e)
            emails.append(e)
    if not emails:
        raise HTTPException(400, "No valid email addresses found")
    if len(emails) > 100:
        raise HTTPException(400, "Maximum 100 emails per batch")

    results = []
    for email in emails:
        try:
            lead = (await db.execute(
                select(LeadV2).where(LeadV2.email == email)
            )).scalar_one_or_none()
            if not lead:
                lead = LeadV2(email=email, business_name=email.split("@")[0].title(),
                              status=LeadStatus.NEW)
                db.add(lead)
                await db.flush()
                await db.commit()
                await db.refresh(lead)

            existing = (await db.execute(
                select(OnboardingRecord).where(OnboardingRecord.lead_id == lead.id)
            )).scalar_one_or_none()
            if existing:
                results.append({"email": email, "status": "skipped",
                                "detail": "Already in onboarding pipeline"})
                continue

            from app.workers.onboarding_tasks import initiate_onboarding_task
            initiate_onboarding_task.delay(str(lead.id))
            results.append({"email": email, "status": "started", "detail": "KYC form is being sent"})
        except Exception as exc:
            await db.rollback()
            results.append({"email": email, "status": "error", "detail": str(exc)[:160]})

    started = sum(1 for r in results if r["status"] == "started")
    skipped = sum(1 for r in results if r["status"] == "skipped")
    return {"started": started, "skipped": skipped,
            "errors": len(results) - started - skipped, "results": results}


# ---------------------------------------------------------------------------
# SPA
# ---------------------------------------------------------------------------

@router.get("", response_class=HTMLResponse, include_in_schema=False)
@router.get("/", response_class=HTMLResponse, include_in_schema=False)
async def dashboard_page():
    try:
        return HTMLResponse(_HTML_PATH.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return HTMLResponse("<h3>dashboard.html not found</h3>", status_code=500)
