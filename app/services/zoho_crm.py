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
) -> str | None:
    """Create a new CRM Lead. Returns Lead ID or None on failure."""
    first, last = _split_name(contact_name)
    payload = {
        "data": [
            {
                "First_Name": first,
                "Last_Name": last or company_name,
                "Email": email,
                "Phone": phone,
                "Company": company_name,
                "Description": f"Onboarding ID: {onboarding_id} | Type: {company_type}",
                "Lead_Source": "Jane Aerospace Onboarding",
            }
        ]
    }
    try:
        resp = httpx.post(f"{_BASE}/Leads", headers=_headers(), json=payload, timeout=20)
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
) -> str | None:
    """Find or create a Lead. Returns Lead ID."""
    lead_id = find_lead_by_email(email)
    if lead_id:
        update_lead(lead_id, {
            "Company": company_name,
            "Description": f"Onboarding ID: {onboarding_id} | Type: {company_type}",
        })
        return lead_id
    return create_lead(email, contact_name, company_name, phone, company_type, onboarding_id)


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
) -> None:
    """
    Called at every pipeline stage. Upserts the contact and adds a note.

    stage examples:
      "Onboarding Started"
      "KYC Submitted"
      "KYC Auto-Approved"
      "KYC Manually Approved"
      "KYC Rejected"
      "NDA Draft Generated"
      "NDA Sent for E-Sign"
      "NDA Signed — Pending Review"
      "NDA Approved"
      "Agreement Sent for E-Sign"
      "Agreement Signed — Pending Review"
      "Agreement Approved"
      "Onboarding Complete"
    """
    if not settings.ZOHO_CRM_REFRESH_TOKEN:
        return

    lead_id = upsert_lead(email, contact_name, company_name, phone, company_type, onboarding_id)
    if not lead_id:
        log.warning("zoho_crm_sync_skipped_no_lead", email=email, stage=stage)
        return

    # Build CRM field updates
    extra_fields: dict = {}
    picklist_val = _STAGE_TO_PICKLIST.get(stage)
    if picklist_val:
        extra_fields["Onboarding_Stage"] = picklist_val
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
