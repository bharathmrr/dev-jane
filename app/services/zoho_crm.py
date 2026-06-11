"""Zoho CRM integration — sync onboarding contacts and status.

Creates/updates a Contact record in Zoho CRM at every onboarding stage:
  - Onboarding started       → create Contact
  - KYC submitted            → update + add Note
  - KYC approved/rejected    → update status field + Note
  - NDA sent / signed        → update + Note
  - Agreement sent / signed  → update + Note
  - Onboarding complete      → update + Note

API region: India (zohoapis.in)
Token URL : https://accounts.zoho.in/oauth/v2/token
"""
from __future__ import annotations

import time
from typing import Any

import httpx

from app.core.config import settings
from app.core.logging import get_logger

log = get_logger("zoho_crm")

_BASE = "https://www.zohoapis.in/crm/v2"
_TOKEN_URL = "https://accounts.zoho.in/oauth/v2/token"
_token_cache: dict[str, Any] = {}

_STAGE_TO_PICKLIST: dict[str, str] = {
    "KYC Form Sent": "KYC_Sent",
    "KYC Submitted": "KYC_Sent",
    "KYC Auto-Approved": "KYC_Approved",
    "KYC Manually Approved": "KYC_Approved",
    "KYC Approved": "KYC_Approved",
    "KYC Rejected": "KYC_Rejected",
    "NDA Sent for E-Sign": "NDA_Sent",
    "NDA Signed — Pending Team Review": "NDA_Signed",
    "NDA Signed — Pending Review": "NDA_Signed",
    "NDA Approved": "NDA_Approved",
    "Agreement Sent for E-Sign": "Agreement_Sent",
    "Agreement Signed — Pending Team Review": "Agreement_Signed",
    "Agreement Signed — Pending Review": "Agreement_Signed",
    "Agreement Approved": "Agreement_Signed",
    "Onboarding Complete": "Complete",
}

# Maps onboarding stage → Zoho CRM Lead_Status picklist value (actual CRM values)
_STAGE_TO_LEAD_STATUS: dict[str, str] = {
    "Onboarding Started": "Discovery Call Stage",
    "KYC Form Sent":      "Discovery Call Stage",
    "KYC Submitted":      "Discovery Call Stage",
    "KYC Auto-Approved":  "Discovery Call Stage",
    "KYC Manually Approved": "Discovery Call Stage",
    "KYC Approved":       "Discovery Call Stage",
    "KYC Rejected":       "Discovery Call Stage",
    "NDA Sent for E-Sign": "Discovery Call Stage",
    "NDA Approved":       "Discovery Call Stage",
    "Agreement Sent for E-Sign": "Discovery Call Stage",
    "Onboarding Complete": "Qualified",
}


def _get_access_token() -> str:
    now = time.time()
    if _token_cache.get("expires_at", 0) - now > 60:
        return _token_cache["token"]

    resp = httpx.post(
        _TOKEN_URL,
        data={
            "grant_type": "refresh_token",
            "client_id": settings.ZOHO_CLIENT_ID,
            "client_secret": settings.ZOHO_CLIENT_SECRET,
            "refresh_token": settings.ZOHO_CRM_REFRESH_TOKEN,
        },
        timeout=15,
    )
    resp.raise_for_status()
    data = resp.json()
    if "access_token" not in data:
        raise RuntimeError(f"Zoho CRM token error: {data}")
    _token_cache["token"] = data["access_token"]
    _token_cache["expires_at"] = now + data.get("expires_in", 3600)
    log.info("zoho_crm_token_refreshed")
    return _token_cache["token"]


def _headers() -> dict[str, str]:
    return {
        "Authorization": f"Zoho-oauthtoken {_get_access_token()}",
        "Content-Type": "application/json",
    }


def _split_name(full_name: str) -> tuple[str, str]:
    parts = full_name.strip().split(" ", 1)
    return parts[0], parts[1] if len(parts) > 1 else ""


def find_lead_by_email(email: str) -> str | None:
    """Return CRM Lead ID for this email, or None."""
    try:
        resp = httpx.get(
            f"{_BASE}/Leads/search",
            headers=_headers(),
            params={"email": email},
            timeout=15,
        )
        if resp.status_code == 204:
            return None
        resp.raise_for_status()
        data = resp.json()
        records = data.get("data", [])
        return str(records[0]["id"]) if records else None
    except Exception as exc:
        log.warning("zoho_crm_find_failed", email=email, error=str(exc))
        return None


def create_lead(
    email: str,
    contact_name: str,
    company_name: str,
    phone: str = "",
    company_type: str = "",
    onboarding_id: str = "",
    summary: str = "",
    designation: str = "",
    industry: str = "",
    city: str = "",
    state: str = "",
    country: str = "",
    street: str = "",
    annual_revenue: str = "",
) -> str | None:
    """Create a new CRM Lead. Returns Lead ID or None on failure."""
    first, last = _split_name(contact_name)
    desc_parts = []
    if summary:
        desc_parts.append(summary)
    if company_type:
        desc_parts.append(f"Company Type: {company_type}")
    if onboarding_id:
        desc_parts.append(f"Onboarding ID: {onboarding_id}")

    record: dict = {
        "First_Name": first,
        "Last_Name": last or company_name,
        "Email": email,
        "Company": company_name,
        "Lead_Source": "Jane Aerospace Onboarding",
        "Lead_Status": "Leads",
    }
    if phone:
        record["Phone"] = phone
    if desc_parts:
        record["Description"] = "\n".join(desc_parts)
    if designation:
        record["Designation"] = designation
    if industry:
        record["Industry"] = industry
    if city:
        record["City"] = city
    if state:
        record["State"] = state
    if country:
        record["Country"] = country
    if street:
        record["Street"] = street
    if annual_revenue:
        record["Annual_Revenue"] = annual_revenue

    try:
        resp = httpx.post(f"{_BASE}/Leads", headers=_headers(), json={"data": [record]}, timeout=20)
        resp.raise_for_status()
        data = resp.json()
        lead_id = data.get("data", [{}])[0].get("details", {}).get("id")
        log.info("zoho_crm_lead_created", email=email, lead_id=lead_id)
        return str(lead_id) if lead_id else None
    except Exception as exc:
        log.error("zoho_crm_create_failed", email=email, error=str(exc))
        return None


def update_lead(lead_id: str, fields: dict) -> bool:
    """Update fields on an existing CRM Lead."""
    payload = {"data": [{**fields, "id": lead_id}]}
    try:
        resp = httpx.put(f"{_BASE}/Leads", headers=_headers(), json=payload, timeout=20)
        resp.raise_for_status()
        log.info("zoho_crm_lead_updated", lead_id=lead_id, fields=list(fields.keys()))
        return True
    except Exception as exc:
        log.error("zoho_crm_update_failed", lead_id=lead_id, error=str(exc))
        return False


def add_note(lead_id: str, title: str, body: str) -> None:
    """Add a Note to a CRM Lead (visible in Lead Activity timeline)."""
    payload = {
        "data": [
            {
                "Note_Title": title,
                "Note_Content": body,
                "Parent_Id": lead_id,
                "se_module": "Leads",
            }
        ]
    }
    try:
        resp = httpx.post(f"{_BASE}/Notes", headers=_headers(), json=payload, timeout=20)
        resp.raise_for_status()
        log.info("zoho_crm_note_added", lead_id=lead_id, title=title)
    except Exception as exc:
        log.error("zoho_crm_note_failed", lead_id=lead_id, error=str(exc))


def upsert_lead(
    email: str,
    contact_name: str,
    company_name: str,
    phone: str = "",
    company_type: str = "",
    onboarding_id: str = "",
    summary: str = "",
    designation: str = "",
    industry: str = "",
    city: str = "",
    state: str = "",
    country: str = "",
    street: str = "",
    annual_revenue: str = "",
) -> str | None:
    """Find or create a CRM Lead. Returns Lead ID."""
    lead_id = find_lead_by_email(email)
    if lead_id:
        # Update with any enriched data provided
        upd: dict = {"Company": company_name}
        if summary:
            upd["Description"] = summary
        if phone:
            upd["Phone"] = phone
        if designation:
            upd["Designation"] = designation
        if industry:
            upd["Industry"] = industry
        if city:
            upd["City"] = city
        if state:
            upd["State"] = state
        if country:
            upd["Country"] = country
        if street:
            upd["Street"] = street
        if annual_revenue:
            upd["Annual_Revenue"] = annual_revenue
        if onboarding_id:
            upd["Onboarding_ID"] = onboarding_id
        update_lead(lead_id, upd)
        return lead_id
    return create_lead(
        email=email,
        contact_name=contact_name,
        company_name=company_name,
        phone=phone,
        company_type=company_type,
        onboarding_id=onboarding_id,
        summary=summary,
        designation=designation,
        industry=industry,
        city=city,
        state=state,
        country=country,
        street=street,
        annual_revenue=annual_revenue,
    )


def upload_lead_attachment(lead_id: str, filename: str, file_bytes: bytes, mime_type: str = "application/octet-stream") -> str | None:
    """Attach a file to a CRM Lead. Returns the Zoho attachment ID or None."""
    try:
        url = f"{_BASE}/Leads/{lead_id}/Attachments"
        resp = httpx.post(
            url,
            headers={"Authorization": f"Zoho-oauthtoken {_get_access_token()}"},
            files={"file": (filename, file_bytes, mime_type)},
            timeout=30,
        )
        resp.raise_for_status()
        data = resp.json()
        att_id = data.get("data", [{}])[0].get("details", {}).get("id")
        log.info("zoho_crm_attachment_uploaded", lead_id=lead_id, filename=filename, att_id=att_id)
        return str(att_id) if att_id else None
    except Exception as exc:
        log.error("zoho_crm_attachment_failed", lead_id=lead_id, filename=filename, error=str(exc))
        return None


def download_lead_attachment(lead_id: str, attachment_id: str) -> bytes | None:
    """Download a CRM Lead attachment. Returns raw bytes or None."""
    try:
        url = f"{_BASE}/Leads/{lead_id}/Attachments/{attachment_id}"
        resp = httpx.get(
            url,
            headers={"Authorization": f"Zoho-oauthtoken {_get_access_token()}"},
            timeout=30,
            follow_redirects=True,
        )
        resp.raise_for_status()
        return resp.content
    except Exception as exc:
        log.error("zoho_crm_attachment_download_failed", lead_id=lead_id, att_id=attachment_id, error=str(exc))
        return None


def sync_kyc_data(
    email: str,
    company_name: str,
    kyc: dict,
) -> None:
    """Called after KYC submission — updates CRM Lead with all KYC fields + adds Note.

    kyc dict keys: gstin, pan, cin, ifsc, bank_name, registered_address, city, state,
                   nature_of_business, entity_type, date_of_incorporation,
                   annual_turnover, designation, country_of_incorporation,
                   company_reg_number, tax_id_tin, lei_number
    """
    if not settings.ZOHO_CRM_REFRESH_TOKEN:
        return

    lead_id = find_lead_by_email(email)
    if not lead_id:
        log.warning("zoho_crm_sync_kyc_no_lead", email=email)
        return

    upd: dict = {}
    if kyc.get("city"):
        upd["City"] = kyc["city"]
    if kyc.get("state"):
        upd["State"] = kyc["state"]
    if kyc.get("registered_address"):
        upd["Street"] = kyc["registered_address"]
    if kyc.get("country_of_incorporation") or kyc.get("country"):
        upd["Country"] = kyc.get("country_of_incorporation") or kyc.get("country", "")
    if kyc.get("nature_of_business"):
        upd["Industry"] = kyc["nature_of_business"]
    if kyc.get("designation") or kyc.get("signatory1_designation"):
        upd["Designation"] = kyc.get("designation") or kyc.get("signatory1_designation", "")
    if upd:
        update_lead(lead_id, upd)

    # Build a detailed KYC note
    lines = [f"KYC Form Submitted for {company_name}", ""]
    for label, key in [
        ("GSTIN",            "gstin"),
        ("PAN",              "pan"),
        ("CIN",              "cin"),
        ("IFSC",             "ifsc"),
        ("Bank",             "bank_name"),
        ("Address",          "registered_address"),
        ("City",             "city"),
        ("State",            "state"),
        ("Nature of Business", "nature_of_business"),
        ("Entity Type",      "entity_type"),
        ("Date of Incorp.",  "date_of_incorporation"),
        ("Annual Turnover",  "annual_turnover"),
        ("Signatory",        "designation"),
        ("Country of Incorp.", "country_of_incorporation"),
        ("Company Reg No",   "company_reg_number"),
        ("Tax ID / TIN",     "tax_id_tin"),
        ("LEI Number",       "lei_number"),
    ]:
        val = kyc.get(key, "") or ""
        if val:
            lines.append(f"{label}: {val}")

    add_note(lead_id, f"KYC Data — {company_name}", "\n".join(lines))
    log.info("zoho_crm_kyc_synced", email=email, company=company_name)


def sync_onboarding_stage(
    email: str,
    contact_name: str,
    company_name: str,
    stage: str,
    detail: str = "",
    phone: str = "",
    company_type: str = "",
    onboarding_id: str = "",
    nda_contract_id: str = "",
    agreement_contract_id: str = "",
    summary: str = "",
) -> None:
    """Called at every pipeline stage. Upserts the Lead and adds a Note."""
    if not settings.ZOHO_CRM_REFRESH_TOKEN:
        return

    lead_id = upsert_lead(
        email=email,
        contact_name=contact_name,
        company_name=company_name,
        phone=phone,
        company_type=company_type,
        onboarding_id=onboarding_id,
        summary=summary,
    )
    if not lead_id:
        log.warning("zoho_crm_sync_skipped_no_lead", email=email, stage=stage)
        return

    # Build CRM field updates
    extra_fields: dict = {}
    picklist_val = _STAGE_TO_PICKLIST.get(stage)
    if picklist_val:
        extra_fields["Onboarding_Stage"] = picklist_val
    lead_status_val = _STAGE_TO_LEAD_STATUS.get(stage)
    if lead_status_val:
        extra_fields["Lead_Status"] = lead_status_val
    if onboarding_id:
        extra_fields["Onboarding_ID"] = onboarding_id
    if nda_contract_id:
        extra_fields["NDA_Contract_ID"] = nda_contract_id
    if agreement_contract_id:
        extra_fields["Agreement_Contract_ID"] = agreement_contract_id
    if extra_fields:
        update_lead(lead_id, extra_fields)

    note_body = f"Stage: {stage}"
    if detail:
        note_body += f"\n{detail}"
    if onboarding_id:
        note_body += f"\nOnboarding ID: {onboarding_id}"

    add_note(lead_id, f"Onboarding: {stage}", note_body)
    log.info("zoho_crm_stage_synced", email=email, stage=stage, picklist=picklist_val)
