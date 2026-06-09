"""KYC verification service — supports free government APIs and third-party providers.

Provider selection via KYC_PROVIDER env var:
  "free"   — public GST portal + format checks only (current default, no cost)
  "setu"   — Setu API (setu.co) — GSTIN + PAN via government DB (₹1-3/call, free sandbox)
  "karza"  — KARZA API — GSTIN + PAN + CIN via government DB (₹2-5/call, trial available)

Auto-approval:
  - GSTIN valid + API confirmed + name matches → auto_approvable = True
  - Format-only pass → overall_passed but not auto_approvable (team reviews)
  - Any format failure → overall_passed = False (team sees issues)

Sandbox vs Production:
  Setu sandbox : SETU_BASE_URL=https://dg-sandbox.setu.co  (test data, free)
  Setu prod    : SETU_BASE_URL=https://dg.setu.co          (real data, paid)
  KARZA sandbox: KARZA_BASE_URL=https://testapi.karza.in   (test data, free trial)
  KARZA prod   : KARZA_BASE_URL=https://api.karza.in       (real data, paid)
"""
from __future__ import annotations

import logging
import re
import time

import requests

from app.core.config import settings

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Regex patterns
# ---------------------------------------------------------------------------
GSTIN_RE = re.compile(r"^[0-9]{2}[A-Z]{5}[0-9]{4}[A-Z]{1}[1-9A-Z]{1}Z[0-9A-Z]{1}$")
PAN_RE   = re.compile(r"^[A-Z]{5}[0-9]{4}[A-Z]{1}$")
CIN_RE   = re.compile(r"^[LU][0-9]{5}[A-Z]{2}[0-9]{4}[A-Z]{3}[0-9]{6}$")
IFSC_RE  = re.compile(r"^[A-Z]{4}0[A-Z0-9]{6}$")
LEI_RE   = re.compile(r"^[A-Z0-9]{18}[0-9]{2}$")  # ISO 17442: 20 alphanumeric

# Free APIs (fallback)
_IFSC_API_URL   = "https://ifsc.razorpay.com/"
_GST_PORTAL_URL = "https://services.gst.gov.in/services/api/search/taxpayerDetails"
_GLEIF_API_URL  = "https://api.gleif.org/api/v1/lei-records"  # free, no key

# ---------------------------------------------------------------------------
# Setu token cache
# ---------------------------------------------------------------------------
_setu_token_cache: dict = {}


def _setu_get_token() -> str:
    """Get Setu Bearer token — cached until expiry."""
    now = time.time()
    if _setu_token_cache.get("expires_at", 0) - now > 60:
        return _setu_token_cache["token"]

    resp = requests.post(
        f"{settings.SETU_BASE_URL}/auth/token",
        json={
            "clientID": settings.SETU_CLIENT_ID,
            "secret":   settings.SETU_CLIENT_SECRET,
        },
        timeout=10,
    )
    resp.raise_for_status()
    data = resp.json()
    token = data.get("data", {}).get("token") or data.get("token", "")
    expires_in = data.get("data", {}).get("expiresIn", 3600)
    _setu_token_cache["token"] = token
    _setu_token_cache["expires_at"] = now + expires_in
    return token


# ---------------------------------------------------------------------------
# Setu API verification
# ---------------------------------------------------------------------------

def _setu_verify_gstin(gstin: str, company_name: str = "") -> dict:
    """Verify GSTIN via Setu API (real government data)."""
    try:
        token = _setu_get_token()
        resp = requests.post(
            f"{settings.SETU_BASE_URL}/api/verify/gst",
            headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
            json={"gstin": gstin},
            timeout=15,
        )
        data = resp.json()
        if resp.status_code == 200 and data.get("success"):
            detail = data.get("data", {})
            api_name = detail.get("tradeName") or detail.get("legalName") or ""
            return {
                "valid": True,
                "business_name": api_name,
                "gst_status": detail.get("status", "Active"),
                "pan_from_gstin": _pan_from_gstin(gstin),
                "name_match": _name_similarity(company_name, api_name) if company_name and api_name else None,
                "source": "setu_api",
                "error": None,
            }
        return {
            "valid": False,
            "error": data.get("error", {}).get("message") or f"Setu API returned {resp.status_code}",
            "source": "setu_api",
        }
    except Exception as e:
        logger.warning("setu_gstin_failed: %s", e)
        return {"valid": None, "error": str(e), "source": "setu_api_error"}


def _setu_verify_pan(pan: str) -> dict:
    """Verify PAN via Setu API."""
    try:
        token = _setu_get_token()
        resp = requests.post(
            f"{settings.SETU_BASE_URL}/api/verify/pan",
            headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
            json={"pan": pan},
            timeout=15,
        )
        data = resp.json()
        if resp.status_code == 200 and data.get("success"):
            detail = data.get("data", {})
            return {
                "valid": True,
                "pan": pan,
                "name": detail.get("name"),
                "pan_type": detail.get("type"),
                "source": "setu_api",
                "error": None,
            }
        return {
            "valid": False,
            "pan": pan,
            "error": data.get("error", {}).get("message") or f"Setu PAN API {resp.status_code}",
            "source": "setu_api",
        }
    except Exception as e:
        logger.warning("setu_pan_failed: %s", e)
        return {"valid": None, "pan": pan, "error": str(e), "source": "setu_api_error"}


# ---------------------------------------------------------------------------
# KARZA API verification (GSTIN + PAN + CIN)
# ---------------------------------------------------------------------------

def _karza_verify_gstin(gstin: str, company_name: str = "") -> dict:
    """Verify GSTIN via KARZA API."""
    try:
        resp = requests.post(
            f"{settings.KARZA_BASE_URL}/v2/gstin",
            headers={"x-karza-key": settings.KARZA_API_KEY, "Content-Type": "application/json"},
            json={"gstin": gstin, "consent": "Y"},
            timeout=15,
        )
        data = resp.json()
        if resp.status_code == 200 and data.get("statusCode") == 101:
            result = data.get("result", {})
            api_name = result.get("tradeNam") or result.get("lgnm") or ""
            return {
                "valid": True,
                "business_name": api_name,
                "gst_status": result.get("sts", "Active"),
                "pan_from_gstin": _pan_from_gstin(gstin),
                "name_match": _name_similarity(company_name, api_name) if company_name and api_name else None,
                "source": "karza_api",
                "error": None,
            }
        return {
            "valid": False,
            "error": data.get("error") or f"KARZA returned {resp.status_code}",
            "source": "karza_api",
        }
    except Exception as e:
        logger.warning("karza_gstin_failed: %s", e)
        return {"valid": None, "error": str(e), "source": "karza_api_error"}


def _karza_verify_pan(pan: str) -> dict:
    """Verify PAN via KARZA API."""
    try:
        resp = requests.post(
            f"{settings.KARZA_BASE_URL}/v2/pan",
            headers={"x-karza-key": settings.KARZA_API_KEY, "Content-Type": "application/json"},
            json={"pan": pan, "consent": "Y"},
            timeout=15,
        )
        data = resp.json()
        if resp.status_code == 200 and data.get("statusCode") == 101:
            result = data.get("result", {})
            return {
                "valid": True,
                "pan": pan,
                "name": result.get("panHolderTitle", "") + " " + result.get("firstName", ""),
                "pan_type": result.get("panType"),
                "source": "karza_api",
                "error": None,
            }
        return {
            "valid": False,
            "pan": pan,
            "error": data.get("error") or f"KARZA PAN API {resp.status_code}",
            "source": "karza_api",
        }
    except Exception as e:
        logger.warning("karza_pan_failed: %s", e)
        return {"valid": None, "pan": pan, "error": str(e), "source": "karza_api_error"}


def _karza_verify_cin(cin: str) -> dict:
    """Verify CIN via KARZA API (MCA21 data)."""
    try:
        resp = requests.post(
            f"{settings.KARZA_BASE_URL}/v2/cin",
            headers={"x-karza-key": settings.KARZA_API_KEY, "Content-Type": "application/json"},
            json={"cin": cin, "consent": "Y"},
            timeout=15,
        )
        data = resp.json()
        if resp.status_code == 200 and data.get("statusCode") == 101:
            result = data.get("result", {})
            return {
                "valid": True,
                "cin": cin,
                "company_name": result.get("company_name"),
                "company_status": result.get("company_status"),
                "source": "karza_api",
                "error": None,
            }
        return {
            "valid": False,
            "cin": cin,
            "error": data.get("error") or f"KARZA CIN API {resp.status_code}",
            "source": "karza_api",
        }
    except Exception as e:
        logger.warning("karza_cin_failed: %s", e)
        return {"valid": None, "cin": cin, "error": str(e), "source": "karza_api_error"}


# ---------------------------------------------------------------------------
# GLEIF LEI verification (free — no API key required)
# ---------------------------------------------------------------------------

def _gleif_verify_lei(lei: str) -> dict:
    """Verify LEI via Global LEI Foundation public API (https://api.gleif.org).

    Completely free, no registration required. Returns company legal name
    and registration status from the global LEI database.
    """
    try:
        resp = requests.get(
            f"{_GLEIF_API_URL}/{lei}",
            timeout=10,
            headers={"Accept": "application/vnd.api+json"},
        )
        if resp.status_code == 200:
            data = resp.json()
            attrs = data.get("data", {}).get("attributes", {})
            entity = attrs.get("entity", {})
            legal_name = entity.get("legalName", {}).get("name", "")
            status = entity.get("status", "ACTIVE")
            registration = attrs.get("registration", {})
            reg_status = registration.get("status", "")
            return {
                "valid": True,
                "lei": lei,
                "legal_name": legal_name,
                "entity_status": status,
                "registration_status": reg_status,
                "source": "gleif_api",
                "error": None,
            }
        if resp.status_code == 404:
            return {
                "valid": False,
                "lei": lei,
                "error": f"LEI {lei} not found in GLEIF global database",
                "source": "gleif_api",
            }
        return {
            "valid": True,  # format passed; treat as soft pass
            "lei": lei,
            "error": f"GLEIF returned HTTP {resp.status_code}. Format OK — manual review.",
            "source": "gleif_api",
        }
    except Exception as e:
        logger.warning("gleif_lei_failed: %s", e)
        return {"valid": True, "lei": lei, "error": str(e), "source": "gleif_api_error"}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _pan_from_gstin(gstin: str) -> str:
    """PAN is embedded in GSTIN at positions 3-12 (0-indexed 2:12)."""
    return gstin[2:12].upper()


def _name_similarity(submitted: str, api_name: str) -> bool:
    """Return True if at least one significant word matches between the two names."""
    stopwords = {"pvt", "ltd", "private", "limited", "llp", "inc", "corp", "and", "the", "of"}
    s_words = {w.lower() for w in submitted.split()} - stopwords
    a_words = {w.lower() for w in api_name.split()} - stopwords
    return bool(s_words & a_words)


def verify_gstin(gstin: str, company_name: str = "") -> dict:
    """
    Verify GSTIN via format check + public GST portal API.
    Returns dict with: valid, business_name, status, pan_from_gstin, source, error
    """
    result: dict = {
        "valid": False,
        "gstin": "",
        "business_name": None,
        "gst_status": None,
        "pan_from_gstin": None,
        "name_match": None,
        "source": "format_only",
        "error": None,
    }

    if not gstin or not gstin.strip():
        result["error"] = "GSTIN not provided"
        return result

    gstin = gstin.upper().strip().replace(" ", "")
    result["gstin"] = gstin

    if not GSTIN_RE.match(gstin):
        result["error"] = f"Invalid GSTIN format ({len(gstin)} chars). Expected 15-char format like 22AAAAA0000A1Z5"
        return result

    result["pan_from_gstin"] = _pan_from_gstin(gstin)

    # Try public GST portal API
    try:
        resp = requests.get(
            _GST_PORTAL_URL,
            params={"gstin": gstin},
            timeout=10,
            headers={
                "Accept": "application/json",
                "User-Agent": "Mozilla/5.0 (compatible; KYCVerify/1.0)",
                "Referer": "https://www.gst.gov.in/",
            },
        )
        if resp.status_code == 200:
            data = resp.json()
            taxpayer = (
                data.get("taxpayerInfo")
                or data.get("data")
                or (data if isinstance(data, dict) and "tradeNam" in data else None)
                or (data if isinstance(data, dict) and "lgnm" in data else None)
            )
            if taxpayer and isinstance(taxpayer, dict):
                api_name = (
                    taxpayer.get("tradeNam")
                    or taxpayer.get("lgnm")
                    or taxpayer.get("tradeName")
                    or taxpayer.get("legalName")
                    or ""
                )
                result["valid"] = True
                result["business_name"] = api_name
                result["gst_status"] = taxpayer.get("sts") or taxpayer.get("status") or "Active"
                result["source"] = "gst_portal_api"
                if company_name and api_name:
                    result["name_match"] = _name_similarity(company_name, api_name)
            else:
                # API responded but no taxpayer block — GSTIN might be invalid
                result["valid"] = True  # format is valid; treat as passed with note
                result["error"] = "GSTIN format valid. GST portal did not return taxpayer details — manual review."
        else:
            result["valid"] = True  # format check passed
            result["error"] = f"GST portal returned HTTP {resp.status_code}. Format check passed — manual review."
    except requests.RequestException as exc:
        logger.warning("GST portal API unreachable: %s", exc)
        result["valid"] = True  # format check passed
        result["error"] = "GST portal unreachable. Format check passed — manual review."

    return result


def verify_pan(pan: str | None, gstin_pan: str | None = None) -> dict:
    """
    Verify PAN. If not provided, uses PAN extracted from GSTIN.
    Cross-checks submitted PAN against GSTIN-embedded PAN.
    """
    result: dict = {
        "valid": False,
        "pan": "",
        "matches_gstin": None,
        "source": "format_only",
        "error": None,
    }

    if not pan or not pan.strip():
        if gstin_pan:
            result["valid"] = True
            result["pan"] = gstin_pan
            result["source"] = "extracted_from_gstin"
            result["error"] = None
        else:
            result["error"] = "PAN not provided and not extractable from GSTIN"
        return result

    pan = pan.upper().strip().replace(" ", "")
    result["pan"] = pan

    if not PAN_RE.match(pan):
        result["error"] = f"Invalid PAN format ({pan}). Expected format: AAAAA1234A"
        return result

    result["valid"] = True

    if gstin_pan:
        result["matches_gstin"] = pan == gstin_pan.upper()
        if not result["matches_gstin"]:
            result["valid"] = False
            result["error"] = f"PAN {pan} does not match PAN in GSTIN ({gstin_pan}). Please verify."

    return result


def verify_cin(cin: str | None) -> dict:
    """Verify CIN format (optional field)."""
    result: dict = {
        "valid": True,
        "cin": "",
        "skipped": True,
        "error": None,
    }

    if not cin or not cin.strip():
        result["note"] = "CIN not provided (optional)"
        return result

    cin = cin.upper().strip().replace(" ", "")
    result["cin"] = cin
    result["skipped"] = False

    if not CIN_RE.match(cin):
        result["valid"] = False
        result["error"] = f"Invalid CIN format ({cin}). Expected: L12345AB1234ABC123456"
        return result

    return result


def verify_ifsc(ifsc: str | None) -> dict:
    """Verify IFSC code via format check + Razorpay free IFSC API."""
    result: dict = {"valid": True, "ifsc": "", "skipped": True, "bank": None, "branch": None, "error": None}
    if not ifsc or not ifsc.strip():
        result["note"] = "IFSC not provided (optional)"
        return result

    ifsc = ifsc.upper().strip().replace(" ", "")
    result["ifsc"] = ifsc
    result["skipped"] = False

    if not IFSC_RE.match(ifsc):
        result["valid"] = False
        result["error"] = f"Invalid IFSC format ({ifsc}). Expected 11-char format like HDFC0001234"
        return result

    try:
        resp = requests.get(f"{_IFSC_API_URL}{ifsc}", timeout=8)
        if resp.status_code == 200:
            data = resp.json()
            result["bank"] = data.get("BANK")
            result["branch"] = data.get("BRANCH")
            result["city"] = data.get("CITY")
            result["source"] = "razorpay_api"
        elif resp.status_code == 404:
            result["valid"] = False
            result["error"] = f"IFSC {ifsc} not found in RBI database"
    except Exception as e:
        result["note"] = f"IFSC API check skipped: {e}"

    return result


def run_kyc_verification(
    company_type: str,
    company_name: str,
    gstin: str | None,
    pan: str | None,
    cin: str | None,
    ifsc: str | None = None,
    # Overseas-specific fields
    lei_number: str | None = None,
    country_of_incorporation: str | None = None,
    company_reg_number: str | None = None,
    tax_id_tin: str | None = None,
) -> dict:
    """
    Run full KYC verification. Returns combined result dict suitable for JSONB storage.

    Indian  : GSTIN (KARZA/Setu/free) + PAN cross-check + CIN (KARZA) + IFSC
    Overseas: LEI (GLEIF free API) + presence checks for required overseas fields
    overall_passed: all required checks passed (at minimum format-valid)
    auto_approvable: all checks passed AND API confirmed at least one identifier
    """
    results: dict = {
        "company_type": company_type,
        "company_name_submitted": company_name,
        "overall_passed": False,
        "auto_approvable": False,
        "gstin_check": None,
        "pan_check": None,
        "cin_check": None,
        "ifsc_check": None,
        "lei_check": None,
        "issues": [],
        "notes": [],
    }

    gstin_pan: str | None = None
    gstin_api_confirmed = False

    provider = settings.KYC_PROVIDER.lower()

    if company_type == "indian":
        if not gstin:
            results["issues"].append("GSTIN is required for Indian companies")
        else:
            # Route to correct provider
            if provider == "setu" and settings.SETU_CLIENT_ID:
                g = _setu_verify_gstin(gstin, company_name)
                if g.get("valid") is None:  # API error — fall back to free
                    g = verify_gstin(gstin, company_name)
            elif provider == "karza" and settings.KARZA_API_KEY:
                g = _karza_verify_gstin(gstin, company_name)
                if g.get("valid") is None:
                    g = verify_gstin(gstin, company_name)
            else:
                g = verify_gstin(gstin, company_name)  # free fallback

            results["gstin_check"] = g

            if not g["valid"]:
                results["issues"].append(f"GSTIN: {g['error']}")
            else:
                gstin_pan = g.get("pan_from_gstin")
                gstin_api_confirmed = g.get("source") in (
                    "gst_portal_api", "setu_api", "karza_api"
                )
                if g.get("error"):
                    results["notes"].append(f"GSTIN note: {g['error']}")
                if g.get("name_match") is False:
                    results["notes"].append(
                        f"Name mismatch: submitted '{company_name}', "
                        f"API shows '{g.get('business_name')}'"
                    )
                elif g.get("business_name"):
                    results["notes"].append(
                        f"Verified via {g['source']}: {g['business_name']}"
                    )

        # PAN check
        if provider == "setu" and settings.SETU_CLIENT_ID and pan:
            p = _setu_verify_pan(pan.upper().strip())
            if p.get("valid") is None:
                p = verify_pan(pan, gstin_pan)
        elif provider == "karza" and settings.KARZA_API_KEY and pan:
            p = _karza_verify_pan(pan.upper().strip())
            if p.get("valid") is None:
                p = verify_pan(pan, gstin_pan)
        else:
            p = verify_pan(pan, gstin_pan)

        results["pan_check"] = p
        if not p["valid"]:
            results["issues"].append(f"PAN: {p['error']}")
        elif p.get("source") == "extracted_from_gstin":
            results["notes"].append("PAN extracted from GSTIN")

    else:
        # Overseas company — GST/PAN not applicable
        results["gstin_check"] = {"skipped": True, "valid": True, "note": "Overseas company"}
        results["pan_check"] = {"skipped": True, "valid": True, "note": "Overseas company"}
        results["notes"].append("Overseas company — GST/PAN verification skipped")

        # Required presence checks for overseas
        if not country_of_incorporation:
            results["issues"].append("Country of Incorporation is required for overseas companies")
        if not company_reg_number:
            results["issues"].append("Company Registration Number is required for overseas companies")
        if not tax_id_tin:
            results["issues"].append("Tax ID / TIN is required for overseas companies")

        # LEI verification via GLEIF (free API — no key needed)
        lei_clean = (lei_number or "").upper().strip().replace(" ", "")
        if lei_clean:
            if LEI_RE.match(lei_clean):
                lei_result = _gleif_verify_lei(lei_clean)
                results["lei_check"] = lei_result
                if lei_result["valid"]:
                    gstin_api_confirmed = True
                    results["notes"].append(
                        f"LEI verified via GLEIF: {lei_result.get('legal_name', '')} "
                        f"({lei_result.get('entity_status', '')})"
                    )
                    if lei_result.get("error"):
                        results["notes"].append(f"LEI note: {lei_result['error']}")
                else:
                    results["issues"].append(f"LEI: {lei_result['error']}")
                    gstin_api_confirmed = False
            else:
                results["lei_check"] = {
                    "valid": False,
                    "lei": lei_clean,
                    "error": "Invalid LEI format — must be exactly 20 alphanumeric characters (ISO 17442)",
                }
                results["issues"].append(f"LEI format invalid: {lei_clean}")
                gstin_api_confirmed = False
        else:
            results["lei_check"] = {"skipped": True, "valid": True, "note": "LEI not provided"}
            results["notes"].append("No LEI provided — overseas KYC requires manual document review by team")
            gstin_api_confirmed = False  # No API confirmation; team must review docs

    # CIN (optional — KARZA gives real MCA21 data)
    if cin and provider == "karza" and settings.KARZA_API_KEY:
        c = _karza_verify_cin(cin.upper().strip())
        if c.get("valid") is None:
            c = verify_cin(cin)
    else:
        c = verify_cin(cin)
    results["cin_check"] = c
    if not c["valid"]:
        results["issues"].append(f"CIN: {c['error']}")

    # IFSC (optional but validated when provided)
    i = verify_ifsc(ifsc)
    results["ifsc_check"] = i
    if not i["valid"]:
        results["issues"].append(f"IFSC: {i['error']}")
    elif not i["skipped"] and i.get("bank"):
        results["notes"].append(f"IFSC verified: {i['bank']} — {i.get('branch', '')}")

    # Final verdict
    results["overall_passed"] = len(results["issues"]) == 0
    results["auto_approvable"] = results["overall_passed"] and gstin_api_confirmed

    return results
