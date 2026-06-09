"""Zoho Contracts e-signature integration.

Flow per document (NDA or Customer Agreement):
  1. get_access_token()        — exchange refresh token → access token
  2. create_contract()         — POST /createcontract with merge fields
                                 returns (contract_id, contract_api_name)
  3. send_for_signature()      — POST /contracts/{api_name}/actions/send-for-signature
  4. Webhook OR polling        — webhook: POST /api/v1/onboarding/zoho-webhook
                                 polling: poll_contract_status() every 2h
  5. DB updated                — status → SIGN_UNDER_REVIEW

Base URL  : https://contracts.zoho.in/api/v1/
Org header: X-com-zoho-contracts-orgid: {ZOHO_CONTRACTS_ORG_ID}
Auth      : Zoho-oauthtoken {access_token}

Merge fields in templates must use {{field_name}} syntax. The field API names
must match exactly: company_name, contact_name, contact_number, effective_date,
signatory_email — as configured in each Zoho Contracts template.
"""
from __future__ import annotations

import re
import time
from typing import Any

import httpx

from app.core.config import settings
from app.core.logging import get_logger

log = get_logger("zoho_contracts")

_BASE = "https://contracts.zoho.in/api/v1"
_TOKEN_URL = "https://accounts.zoho.in/oauth/v2/token"

_token_cache: dict[str, Any] = {}


# ---------------------------------------------------------------------------
# Auth
# ---------------------------------------------------------------------------

def get_access_token() -> str:
    """Return a valid access token, refreshing when < 60s remain."""
    now = time.time()
    if _token_cache.get("expires_at", 0) - now > 60:
        return _token_cache["token"]

    resp = httpx.post(
        _TOKEN_URL,
        data={
            "grant_type": "refresh_token",
            "client_id": settings.ZOHO_CONTRACTS_CLIENT_ID or settings.ZOHO_CLIENT_ID,
            "client_secret": settings.ZOHO_CONTRACTS_CLIENT_SECRET or settings.ZOHO_CLIENT_SECRET,
            "refresh_token": settings.ZOHO_CONTRACTS_REFRESH_TOKEN,
        },
        timeout=15,
    )
    resp.raise_for_status()
    data = resp.json()
    if "access_token" not in data:
        raise RuntimeError(f"Zoho Contracts token error: {data}")

    _token_cache["token"] = data["access_token"]
    _token_cache["expires_at"] = now + data.get("expires_in", 3600)
    log.info("zoho_contracts_token_refreshed")
    return _token_cache["token"]


def _headers(token: str) -> dict[str, str]:
    return {
        "Authorization": f"Zoho-oauthtoken {token}",
        "X-com-zoho-contracts-orgid": settings.ZOHO_CONTRACTS_ORG_ID,
        "Content-Type": "application/json",
    }


def _slug(name: str) -> str:
    """Convert display name to Zoho API name slug (e.g. 'Jane Corp' → 'jane-corp')."""
    return re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")[:60]


# ---------------------------------------------------------------------------
# Create contract
# ---------------------------------------------------------------------------

def create_contract(
    contract_type_id: str,
    contract_name: str,
    lead_name: str,
    lead_email: str,
    merge_fields: dict | None = None,
) -> tuple[str, str]:
    """Create a contract from a Zoho Contracts template.

    Uses POST /createcontract (the documented endpoint for pre-filling
    document/template merge fields).

    Returns:
        (contract_id, contract_api_name) — store the api_name; it is used
        for send-for-signature, status polling, and webhook matching.

    Template merge fields must be configured in Zoho Contracts under the
    contract type. Field API names must match exactly what you pass in
    merge_fields (e.g. company_name, contact_name, effective_date).
    """
    token = get_access_token()

    counterparty_slug = _slug(lead_name)

    # Build inputfields array using the documented format
    inputfields: list[dict] = [
        {
            "metaApiName": "contract-type",
            "inputs": [{"inputApiName": "contract-type", "inputValue": contract_type_id}],
        },
        {
            "metaApiName": "title",
            "inputs": [{"inputApiName": "title", "inputValue": contract_name}],
        },
        {
            "metaApiName": "party-b-name",
            "inputs": [{"inputApiName": "party-b-name", "inputValue": counterparty_slug}],
        },
        {
            "metaApiName": "counterparty-primary-contact",
            "inputs": [{"inputApiName": "counterparty-primary-contact", "inputValue": lead_email}],
        },
    ]

    # Add template merge/document fields
    if merge_fields:
        for field_name, field_value in merge_fields.items():
            if field_value:
                inputfields.append({
                    "metaApiName": field_name,
                    "inputs": [{"inputApiName": field_name, "inputValue": str(field_value)}],
                })

    payload = {
        "source": 1,
        "inputfields": inputfields,
    }

    log.info("zoho_contracts_create", contract_type_id=contract_type_id,
             name=contract_name, email=lead_email)

    resp = httpx.post(
        f"{_BASE}/createcontract",
        headers=_headers(token),
        json=payload,
        timeout=30,
    )

    if not resp.is_success:
        log.error("zoho_contracts_create_failed", status=resp.status_code,
                  body=resp.text[:500], payload=payload)
        resp.raise_for_status()

    data = resp.json()
    log.info("zoho_contracts_created", response_keys=list(data.keys()))

    # Extract from response — Zoho returns a "contracts" array
    contracts_list = (
        data.get("contracts")
        or (data.get("data", {}).get("contracts") if isinstance(data.get("data"), dict) else None)
        or []
    )
    if not contracts_list:
        # Fallback: maybe response is the contract object directly
        contracts_list = [data]

    contract = contracts_list[0] if contracts_list else {}

    contract_id = (
        contract.get("id")
        or contract.get("contractId")
        or contract.get("contract_id")
        or data.get("contractId")
        or data.get("id")
    )
    contract_api_name = (
        contract.get("contractApiName")
        or contract.get("contract_api_name")
        or contract.get("contractapiname")
        or data.get("contractApiName")
    )

    if not contract_id:
        raise RuntimeError(f"No contract ID in Zoho Contracts response: {data}")

    # If api_name not returned, fall back to the numeric ID
    if not contract_api_name:
        contract_api_name = str(contract_id)
        log.warning("zoho_contracts_no_api_name", contract_id=contract_id)

    log.info("zoho_contracts_created_ok", contract_id=contract_id,
             contract_api_name=contract_api_name)
    return str(contract_id), str(contract_api_name)


# ---------------------------------------------------------------------------
# Send for signature
# ---------------------------------------------------------------------------

def send_for_signature(contract_api_name: str) -> None:
    """Send a Zoho Contracts contract for e-signature.

    Uses the documented endpoint:
      POST /contracts/{contractApiName}/actions/send-for-signature

    The lead receives an email with a Zoho Sign link.
    """
    token = get_access_token()
    log.info("zoho_contracts_send_sign", contract_api_name=contract_api_name)

    resp = httpx.post(
        f"{_BASE}/contracts/{contract_api_name}/actions/send-for-signature",
        headers=_headers(token),
        json={},
        timeout=30,
    )

    if not resp.is_success:
        log.error("zoho_contracts_send_sign_failed", contract_api_name=contract_api_name,
                  status=resp.status_code, body=resp.text[:500])
        resp.raise_for_status()

    data = resp.json()
    stage = (data.get("action") or [{}])[0].get("stage") if isinstance(data.get("action"), list) else data.get("stage")
    log.info("zoho_contracts_sign_sent", contract_api_name=contract_api_name, stage=stage)


# ---------------------------------------------------------------------------
# Create + send in one call
# ---------------------------------------------------------------------------

def create_and_send(
    contract_type_id: str,
    contract_name: str,
    lead_name: str,
    lead_email: str,
    merge_fields: dict | None = None,
) -> str:
    """Create contract and immediately send for e-signature.

    Returns the contract_api_name — store this in nda_zoho_contract_id or
    agreement_zoho_contract_id. It is used for status polling and webhook
    matching (the webhook payload contains this same value as contract.id).
    """
    _contract_id, contract_api_name = create_contract(
        contract_type_id=contract_type_id,
        contract_name=contract_name,
        lead_name=lead_name,
        lead_email=lead_email,
        merge_fields=merge_fields,
    )
    send_for_signature(contract_api_name)
    return contract_api_name


# ---------------------------------------------------------------------------
# Poll contract status  (webhook fallback)
# ---------------------------------------------------------------------------

_SIGNED_STAGES = {"signed", "completed", "executed", "sign-completed"}


def get_contract_status(contract_api_name: str) -> dict:
    """Fetch current contract status from Zoho Contracts API.

    GET /contracts/{contractApiName}

    Returns a dict with at minimum:
      - "api_name": str
      - "stage": str   (e.g. "sign-pending", "signed", "active", "withdrawn")
      - "is_signed": bool
    """
    token = get_access_token()
    resp = httpx.get(
        f"{_BASE}/contracts/{contract_api_name}",
        headers=_headers(token),
        timeout=20,
    )

    if resp.status_code == 404:
        log.warning("zoho_contracts_not_found", contract_api_name=contract_api_name)
        return {"api_name": contract_api_name, "stage": "not_found", "is_signed": False}

    if not resp.is_success:
        log.warning("zoho_contracts_status_failed", contract_api_name=contract_api_name,
                    status=resp.status_code, body=resp.text[:300])
        return {"api_name": contract_api_name, "stage": "error", "is_signed": False}

    data = resp.json()
    contracts_list = data.get("contracts") or [data]
    contract = contracts_list[0] if contracts_list else data

    stage = (
        contract.get("stage")
        or contract.get("contractStage")
        or contract.get("status")
        or contract.get("contractstatus")
        or ""
    ).lower()

    is_signed = stage in _SIGNED_STAGES

    log.info("zoho_contracts_status_polled", contract_api_name=contract_api_name,
             stage=stage, is_signed=is_signed)

    return {
        "api_name": contract_api_name,
        "stage": stage,
        "is_signed": is_signed,
        "raw": contract,
    }


# ---------------------------------------------------------------------------
# Template helpers
# ---------------------------------------------------------------------------

def get_contract_type_id(doc_type: str, company_type: str) -> str:
    """Return the Zoho Contracts contract type ID for a given doc + company type."""
    is_overseas = company_type.upper() in ("OVERSEAS", "FOREIGN", "INTERNATIONAL")
    if doc_type == "nda":
        return (
            settings.ZOHO_CONTRACTS_NDA_TEMPLATE_OVERSEAS
            if is_overseas
            else settings.ZOHO_CONTRACTS_NDA_TEMPLATE_INDIAN
        )
    return (
        settings.ZOHO_CONTRACTS_AGREEMENT_TEMPLATE_OVERSEAS
        if is_overseas
        else settings.ZOHO_CONTRACTS_AGREEMENT_TEMPLATE_INDIAN
    )


def get_contract_type_details(contract_type_id: str) -> dict:
    """Fetch contract type metadata from Zoho Contracts API."""
    token = get_access_token()
    resp = httpx.get(
        f"{_BASE}/contracttypes/{contract_type_id}",
        headers=_headers(token),
        timeout=20,
    )
    if not resp.is_success:
        log.warning("zoho_contracts_type_fetch_failed", contract_type_id=contract_type_id,
                    status=resp.status_code)
        return {}
    return resp.json()


def get_template_preview_text(contract_type_id: str, contract_type_name: str = "") -> str:
    """Return a human-readable summary of the Zoho Contracts template for team review."""
    details = get_contract_type_details(contract_type_id)

    name = (
        details.get("contracttype", {}).get("contracttypename")
        or details.get("contracttypename")
        or details.get("name")
        or contract_type_name
        or contract_type_id
    )
    description = (
        details.get("contracttype", {}).get("description")
        or details.get("description")
        or ""
    )
    body = (
        details.get("contracttype", {}).get("contractbody")
        or details.get("contractbody")
        or details.get("body")
        or ""
    )

    portal_link = f"https://contracts.zoho.in/janeaerospace/contracttypes/{contract_type_id}"

    lines = [f"Template: {name}", f"Contract Type ID: {contract_type_id}"]
    if description:
        lines.append(f"Description: {description}")
    lines.append(f"Preview in Zoho Contracts: {portal_link}")
    if body:
        lines.append("\n--- Template Content ---")
        lines.append(body[:5000])

    return "\n".join(lines)
