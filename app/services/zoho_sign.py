"""Zoho Sign e-signature integration.

Flow for NDA (or any document):
  1. get_access_token()          — refresh → access token
  2. send_nda_for_signature()    — upload HTML → add sig fields → submit
     OR send_via_template()      — use existing JAPL_NDA_Final.docx template

Base URL : https://sign.zoho.in/api/v1
Auth     : Zoho-oauthtoken {access_token}
Scopes   : ZohoSign.documents.ALL  ZohoSign.templates.ALL

NOTE: Sending documents via API requires a PAID Zoho Sign plan.
      Use Leo's (leopeter@janeaerospace.co.in) credentials in .env.
"""
from __future__ import annotations

import json
import time
from typing import Any

import httpx

from app.core.config import settings
from app.core.logging import get_logger
from app.services.nda_template import render_nda_html

log = get_logger("zoho_sign")

_BASE = "https://sign.zoho.in/api/v1"
_TOKEN_URL = "https://accounts.zoho.in/oauth/v2/token"

_token_cache: dict[str, Any] = {}


# ---------------------------------------------------------------------------
# Auth
# ---------------------------------------------------------------------------

def get_access_token() -> str:
    now = time.time()
    if _token_cache.get("expires_at", 0) - now > 60:
        return _token_cache["token"]

    resp = httpx.post(
        _TOKEN_URL,
        data={
            "grant_type": "refresh_token",
            "client_id": settings.ZOHO_SIGN_CLIENT_ID,
            "client_secret": settings.ZOHO_SIGN_CLIENT_SECRET,
            "refresh_token": settings.ZOHO_SIGN_REFRESH_TOKEN,
        },
        timeout=15,
    )
    resp.raise_for_status()
    data = resp.json()
    if "access_token" not in data:
        raise RuntimeError(f"Zoho Sign token error: {data}")

    _token_cache["token"] = data["access_token"]
    _token_cache["expires_at"] = now + data.get("expires_in", 3600)
    log.info("zoho_sign_token_refreshed")
    return _token_cache["token"]


def _headers(token: str) -> dict[str, str]:
    return {"Authorization": f"Zoho-oauthtoken {token}"}


# ---------------------------------------------------------------------------
# Send NDA by uploading HTML document directly
# ---------------------------------------------------------------------------

def send_nda_for_signature(
    lead_name: str,
    lead_email: str,
    merge_fields: dict,
    is_overseas: bool = False,
    jane_signer_name: str = "Leo Peter Charles",
    jane_signer_email: str = "leopeter@janeaerospace.co.in",
) -> str:
    """Generate NDA HTML, upload to Zoho Sign, add signature fields, and send.

    Returns the Zoho Sign request_id — store this for status polling.
    """
    token = get_access_token()
    headers = _headers(token)

    html = render_nda_html(merge_fields, is_overseas=is_overseas)
    company_name = merge_fields.get("company_name") or lead_name
    request_name = f"Jane Aerospace NDA - {company_name}"

    request_data = json.dumps({
        "requests": {
            "request_name": request_name,
            "expiration_days": 15,
            "is_sequential": True,
            "email_reminders": True,
            "reminder_period": 3,
            "actions": [
                {
                    "recipient_name": jane_signer_name,
                    "recipient_email": jane_signer_email,
                    "action_type": "SIGN",
                    "signing_order": 1,
                    "verify_recipient": False,
                },
                {
                    "recipient_name": lead_name,
                    "recipient_email": lead_email,
                    "action_type": "SIGN",
                    "signing_order": 2,
                    "verify_recipient": False,
                },
            ],
        }
    })

    upload_resp = httpx.post(
        f"{_BASE}/requests",
        headers=headers,
        data={"data": request_data},
        files={"file": (f"NDA_{company_name}.html", html.encode("utf-8"), "text/html")},
        timeout=30,
    )
    if not upload_resp.is_success:
        log.error("zoho_sign_upload_failed", status=upload_resp.status_code, body=upload_resp.text[:500])
        upload_resp.raise_for_status()

    req = upload_resp.json()["requests"]
    request_id = req["request_id"]
    doc_id = req["document_ids"][0]["document_id"]
    total_pages = req["document_ids"][0]["total_pages"]
    actions = {a["signing_order"]: a["action_id"] for a in req["actions"]}
    action_jane = actions[1]
    action_lead = actions[2]

    log.info("zoho_sign_uploaded", request_id=request_id, doc_id=doc_id, pages=total_pages)

    # Place signature fields on the last page
    last_page = total_pages - 1
    sig_fields = _make_sig_field(doc_id, last_page)

    add_fields_payload = {
        "requests": {
            "actions": [
                {"action_id": action_jane, "action_type": "SIGN", "recipient_name": jane_signer_name, "recipient_email": jane_signer_email, "signing_order": 1, "fields": [sig_fields("Signature1", "Sign - Jane Aerospace", 50)]},
                {"action_id": action_lead, "action_type": "SIGN", "recipient_name": lead_name, "recipient_email": lead_email, "signing_order": 2, "fields": [sig_fields("Signature2", f"Sign - {company_name}", 330)]},
            ]
        }
    }

    put_resp = httpx.put(
        f"{_BASE}/requests/{request_id}",
        headers={**headers, "Content-Type": "application/json"},
        json=add_fields_payload,
        timeout=30,
    )
    if not put_resp.is_success:
        log.error("zoho_sign_add_fields_failed", status=put_resp.status_code, body=put_resp.text[:500])
        put_resp.raise_for_status()

    submit_payload = {"requests": {"actions": add_fields_payload["requests"]["actions"]}}
    sub_resp = httpx.post(
        f"{_BASE}/requests/{request_id}/submit",
        headers={**headers, "Content-Type": "application/json"},
        json=submit_payload,
        timeout=30,
    )
    if not sub_resp.is_success:
        log.error("zoho_sign_submit_failed", status=sub_resp.status_code, body=sub_resp.text[:500])
        sub_resp.raise_for_status()

    log.info("zoho_sign_sent", request_id=request_id, lead_email=lead_email)
    return request_id


def _make_sig_field(doc_id: str, page_no: int):
    def build(field_name: str, label: str, x: int) -> dict:
        return {
            "document_id": doc_id,
            "field_name": field_name,
            "field_type_name": "Signature",
            "field_label": label,
            "field_category": "image",
            "page_no": page_no,
            "x_coord": x,
            "y_coord": 660,
            "abs_width": 200,
            "abs_height": 50,
            "is_mandatory": True,
        }
    return build


# ---------------------------------------------------------------------------
# Send via existing JAPL_NDA_Final.docx template (Leo's template)
# ---------------------------------------------------------------------------

def send_via_template(
    lead_name: str,
    lead_email: str,
    company_name: str,
) -> str:
    """Create a sign request from the existing Zoho Sign NDA template.

    Replaces the counterparty slot with the lead's details.
    Returns the Zoho Sign request_id.
    """
    token = get_access_token()
    headers = {**_headers(token), "Content-Type": "application/json"}

    template_id = settings.ZOHO_SIGN_NDA_TEMPLATE_ID
    action_jane = settings.ZOHO_SIGN_NDA_TEMPLATE_ACTION_JANE
    action_counterparty = settings.ZOHO_SIGN_NDA_TEMPLATE_ACTION_COUNTERPARTY

    payload = {
        "templates": {
            "request_name": f"Jane Aerospace NDA - {company_name}",
            "actions": [
                {
                    "action_id": action_jane,
                    "action_type": "SIGN",
                    "role": "Authorized Signatory",
                    "recipient_name": "Leo Peter Charles",
                    "recipient_email": "leopeter@janeaerospace.co.in",
                    "signing_order": 1,
                    "verify_recipient": False,
                },
                {
                    "action_id": action_counterparty,
                    "action_type": "SIGN",
                    "role": "Authorized Signatory",
                    "recipient_name": lead_name,
                    "recipient_email": lead_email,
                    "signing_order": 2,
                    "verify_recipient": False,
                },
            ],
            "notes": "",
        },
    }

    resp = httpx.post(
        f"{_BASE}/templates/{template_id}/createdocument",
        headers=headers,
        json=payload,
        timeout=30,
    )
    if not resp.is_success:
        log.error("zoho_sign_template_failed", status=resp.status_code, body=resp.text[:500])
        resp.raise_for_status()

    req = resp.json().get("requests", {})
    request_id = req.get("request_id", "")
    log.info("zoho_sign_template_sent", request_id=request_id, lead_email=lead_email)
    return request_id


# ---------------------------------------------------------------------------
# Status polling
# ---------------------------------------------------------------------------

_SIGNED_STATUSES = {"signed", "completed"}


def get_sign_status(request_id: str) -> dict:
    """Poll Zoho Sign for the status of a sign request."""
    token = get_access_token()
    resp = httpx.get(
        f"{_BASE}/requests/{request_id}",
        headers=_headers(token),
        timeout=20,
    )
    if resp.status_code == 404:
        return {"request_id": request_id, "status": "not_found", "is_signed": False}
    if not resp.is_success:
        return {"request_id": request_id, "status": "error", "is_signed": False}

    req = resp.json().get("requests", {})
    status = req.get("request_status", "").lower()
    return {
        "request_id": request_id,
        "status": status,
        "is_signed": status in _SIGNED_STATUSES,
        "raw": req,
    }
