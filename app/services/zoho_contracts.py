"""Zoho Contracts integration — create NDA/Agreement contracts from templates.

Real API flow (corrected from official docs):
  1. POST /api/v1/contracts          — create contract using inputfields structure
                                       contract type identified by apiName (e.g. "jane-nda-auto")
  2. GET  /api/v1/contracts          — list to find the new contract's apiName
  3. POST /api/v1/contracts/{apiName}/actions/send-for-signature
                                     — Zoho sends signing email to counterparty directly

Env vars (store the contract type apiName from Admin → Contract Types):
  ZOHO_CONTRACTS_CLIENT_ID
  ZOHO_CONTRACTS_CLIENT_SECRET
  ZOHO_CONTRACTS_REFRESH_TOKEN
  ZOHO_CONTRACTS_NDA_TEMPLATE_ID_INDIAN       ← apiName e.g. "jane-nda-auto"
  ZOHO_CONTRACTS_NDA_TEMPLATE_ID_OVERSEAS     ← apiName e.g. "jane-nda-overseas"
  ZOHO_CONTRACTS_AGREEMENT_TEMPLATE_ID_INDIAN ← apiName e.g. "jane-customer-agreement-auto"
  ZOHO_CONTRACTS_AGREEMENT_TEMPLATE_ID_OVERSEAS
  ZOHO_DC                                     ← default "in"
"""
from __future__ import annotations

import time
from typing import Any

import requests

from app.core.config import settings
from app.core.logging import get_logger

logger = get_logger("zoho_contracts")

_token_cache: dict[str, Any] = {}


def _dc() -> str:
    return (settings.ZOHO_DC or "in").lower()


def _base() -> str:
    return f"https://contracts.zoho.{_dc()}/api/v1"


def _accounts_url() -> str:
    return f"https://accounts.zoho.{_dc()}/oauth/v2/token"


# ---------------------------------------------------------------------------
# OAuth
# ---------------------------------------------------------------------------

def get_access_token() -> str:
    now = time.time()
    if _token_cache.get("access_token") and _token_cache.get("expires_at", 0) > now + 60:
        return _token_cache["access_token"]

    resp = requests.post(
        _accounts_url(),
        data={
            "grant_type":    "refresh_token",
            "client_id":     settings.ZOHO_CONTRACTS_CLIENT_ID,
            "client_secret": settings.ZOHO_CONTRACTS_CLIENT_SECRET,
            "refresh_token": settings.ZOHO_CONTRACTS_REFRESH_TOKEN,
        },
        timeout=15,
    )
    resp.raise_for_status()
    data = resp.json()
    token = data.get("access_token")
    if not token:
        raise RuntimeError(f"Zoho Contracts token refresh failed: {data}")

    _token_cache["access_token"] = token
    _token_cache["expires_at"] = now + int(data.get("expires_in", 3600))
    logger.info("zoho_contracts_token_refreshed")
    return token


def _headers() -> dict:
    return {
        "Authorization": f"Zoho-oauthtoken {get_access_token()}",
        "Content-Type": "application/json",
    }


# ---------------------------------------------------------------------------
# List contract types (helper — use to discover apiName values for .env)
# ---------------------------------------------------------------------------

def list_contract_types() -> list[dict]:
    """Return all contract types with id, name, apiName.

    Run this once to discover the apiName values you need for .env.
    """
    resp = requests.get(f"{_base()}/contracttypes", headers=_headers(), timeout=15)
    resp.raise_for_status()
    data = resp.json()
    return data.get("contracttypes", [])


# ---------------------------------------------------------------------------
# Build inputfields payload (Zoho Contracts API format)
# ---------------------------------------------------------------------------

# Placeholder format in Zoho Contracts Writer templates: ##field_name##
# The inputApiName must match exactly the name between ## in the document.
# Our internal key  →  Zoho Contracts placeholder name (between ##...##)
_FIELD_MAP = {
    "company_name":             "company_name",
    "contact_name":             "contact_name",
    "effective_date":           "effective_date",
    "registered_address":       "registered_address",
    "pan_number":               "pan_number",
    "gstin_number":             "gstin_number",
    "cin_number":               "cin_number",
    "ifsc_code":                "ifsc_code",
    "bank_name":                "bank_name",
    "nature_of_business":       "nature_of_business",
    "entity_type":              "entity_type",
    "date_of_incorporation":    "date_of_incorporation",
    "annual_turnover":          "annual_turnover",
    "signatory_email":          "signatory_email",
    "signatory1_designation":   "signatory1_designation",
    "country_of_incorporation": "country_of_incorporation",
    "company_reg_number":       "company_reg_number",
    "lei_number":               "lei_number",
    "tax_id_tin":               "tax_id_tin",
    "contact_number":           "contact_number",
    "city":                     "city",
    "state":                    "state",
}


def _build_inputfields(
    contract_type_api_name: str,
    contract_title: str,
    counterparty_name: str,
    counterparty_email: str,
    merge_fields: dict,
) -> list[dict]:
    """Convert flat merge_fields dict into Zoho Contracts inputfields array."""

    def _field(api_name: str, value: Any) -> dict:
        return {
            "metaApiName": api_name,
            "inputs": [{"inputApiName": api_name, "inputValue": value if isinstance(value, str) else str(value)}],
        }

    fields = [
        _field("contract-type",  contract_type_api_name),
        _field("title",          contract_title),
        _field("description",    f"Contract for {contract_title}"),
        _field("requester-name", "Leo Peter Charles"),
        _field("requester-department", "legal"),
        _field("party-b-name",   counterparty_name),
        {
            "metaApiName": "counterparty-primary-contact",
            "inputs": [{"inputApiName": "party-b-primary-contact-name", "inputValue": counterparty_email}],
        },
        _field("contract-term", True),
        {
            "metaApiName": "contract-effective-date",
            "inputs": [
                {"inputApiName": "contract-effective-date", "inputValue": 0},
                {"inputApiName": "effective-specific-date",
                 "inputValue": __import__("datetime").date.today().strftime("%d/%m/%Y")},
            ],
        },
    ]

    # Add custom document merge fields using confirmed Zoho Contracts apiNames
    for key, value in merge_fields.items():
        if not value:
            continue
        zoho_key = _FIELD_MAP.get(key, key)  # map to confirmed apiName
        fields.append(_field(zoho_key, str(value)))

    return fields


# ---------------------------------------------------------------------------
# Create contract
# ---------------------------------------------------------------------------

def create_contract(
    contract_type_api_name: str,
    contract_title: str,
    counterparty_name: str,
    counterparty_email: str,
    merge_fields: dict,
) -> dict:
    """Create a contract in Zoho Contracts.

    Returns the full response dict (contains contract id, apiName, etc.).
    Raises RuntimeError on failure.
    """
    inputfields = _build_inputfields(
        contract_type_api_name=contract_type_api_name,
        contract_title=contract_title,
        counterparty_name=counterparty_name,
        counterparty_email=counterparty_email,
        merge_fields=merge_fields,
    )
    # templateType=1 (Zoho Writer doc) needs externalSource:True
    # templateType=0 (form-based) needs source:1
    payload = {
        "externalSource": True,
        "source": 1,
        "inputfields": inputfields,
    }

    resp = requests.post(f"{_base()}/contracts", json=payload, headers=_headers(), timeout=20)

    if resp.status_code not in (200, 201):
        logger.error("zoho_contracts_create_failed",
                     status=resp.status_code, body=resp.text[:400])
        raise RuntimeError(
            f"Zoho Contracts create failed ({resp.status_code}): {resp.text[:300]}"
        )

    data = resp.json()
    # Response structure: {"contracts": [{...}]} or {"contract": {...}}
    contracts = data.get("contracts") or []
    contract = contracts[0] if contracts else data.get("contract") or data

    contract_id = (
        str(contract.get("id", ""))
        or str(contract.get("contractId", ""))
        or ""
    )
    contract_api_name = (
        contract.get("apiName")
        or contract.get("api_name")
        or contract_id
    )

    logger.info("zoho_contracts_created",
                contract_id=contract_id, api_name=contract_api_name, title=contract_title)
    return {
        "id":      contract_id,
        "apiName": contract_api_name,
        "raw":     contract,
    }


# ---------------------------------------------------------------------------
# Send for signature
# ---------------------------------------------------------------------------

def send_for_signature(contract_api_name: str) -> bool:
    """Submit contract for e-signature.

    Zoho Contracts sends its own signing email to the counterparty.
    Returns True on success.
    """
    url = f"{_base()}/contracts/{contract_api_name}/actions/send-for-signature"
    resp = requests.post(url, headers=_headers(), timeout=15)

    if resp.status_code in (200, 201, 204):
        logger.info("zoho_contracts_sent_for_signature", contract_api_name=contract_api_name)
        return True

    logger.error("zoho_contracts_send_sig_failed",
                 contract_api_name=contract_api_name,
                 status=resp.status_code, body=resp.text[:200])
    return False


# ---------------------------------------------------------------------------
# Build a portal view URL (no share-link API — use portal direct URL)
# ---------------------------------------------------------------------------

def get_contract_portal_url(contract_api_name: str, org_name: str = "janeaerospace") -> str:
    dc = _dc()
    return f"https://contracts.zoho.{dc}/{org_name}#/contracts/{contract_api_name}"


# Aliases used by onboarding_tasks.py
def submit_contract(contract_api_name: str) -> bool:
    """Alias for send_for_signature — submits contract for e-signature."""
    return send_for_signature(contract_api_name)


def get_contract_view_link(contract_api_name: str) -> str:
    """Return portal view URL for the contract (requires Zoho login).

    Zoho sends its own signing email to the counterparty when send_for_signature()
    is called; this link is for our team/admin notification email.
    """
    return get_contract_portal_url(contract_api_name)


# ---------------------------------------------------------------------------
# Two-step helpers: prepare (team review) then submit (send to lead)
# ---------------------------------------------------------------------------

def prepare_nda_contract(
    merge_fields: dict,
    is_overseas: bool,
    signatory_name: str,
    signatory_email: str,
    company_name: str,
) -> tuple[str, str]:
    """Create NDA contract (no signing email yet) for team review.

    Returns (contract_api_name, portal_url).
    Raises RuntimeError if apiName is not configured.
    """
    api_name = (
        settings.ZOHO_CONTRACTS_NDA_TEMPLATE_ID_OVERSEAS
        if is_overseas
        else settings.ZOHO_CONTRACTS_NDA_TEMPLATE_ID_INDIAN
    )
    if not api_name:
        raise RuntimeError(
            "Zoho Contracts NDA apiName not configured. "
            "Run list_contract_types() to find apiName, then set "
            "ZOHO_CONTRACTS_NDA_TEMPLATE_ID_INDIAN / _OVERSEAS in .env"
        )

    result = create_contract(
        contract_type_api_name=api_name,
        contract_title=f"NDA — {company_name}",
        counterparty_name=signatory_name,
        counterparty_email=signatory_email,
        merge_fields=merge_fields,
    )
    contract_api_name = result["apiName"] or result["id"]
    portal_url = get_contract_portal_url(contract_api_name)
    return contract_api_name, portal_url


def prepare_agreement_contract(
    merge_fields: dict,
    is_overseas: bool,
    signatory_name: str,
    signatory_email: str,
    company_name: str,
) -> tuple[str, str]:
    """Create Agreement contract (no signing email yet) for team review.

    Returns (contract_api_name, portal_url).
    """
    api_name = (
        settings.ZOHO_CONTRACTS_AGREEMENT_TEMPLATE_ID_OVERSEAS
        if is_overseas
        else settings.ZOHO_CONTRACTS_AGREEMENT_TEMPLATE_ID_INDIAN
    )
    if not api_name:
        raise RuntimeError(
            "Zoho Contracts Agreement apiName not configured. "
            "Set ZOHO_CONTRACTS_AGREEMENT_TEMPLATE_ID_INDIAN / _OVERSEAS in .env"
        )

    result = create_contract(
        contract_type_api_name=api_name,
        contract_title=f"Customer Agreement — {company_name}",
        counterparty_name=signatory_name,
        counterparty_email=signatory_email,
        merge_fields=merge_fields,
    )
    contract_api_name = result["apiName"] or result["id"]
    portal_url = get_contract_portal_url(contract_api_name)
    return contract_api_name, portal_url
