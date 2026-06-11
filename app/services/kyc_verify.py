"""KYC format validation — regex checks only, no external API calls.

Indian  : GSTIN (required) + PAN (required or extracted from GSTIN) + CIN (optional) + IFSC (optional)
Overseas: Country of Incorporation + Company Reg Number + Tax ID/TIN (all required) + LEI (optional)
"""
from __future__ import annotations

import re

GSTIN_RE = re.compile(r"^[0-9]{2}[A-Z]{5}[0-9]{4}[A-Z]{1}[1-9A-Z]{1}Z[0-9A-Z]{1}$")
PAN_RE   = re.compile(r"^[A-Z]{5}[0-9]{4}[A-Z]{1}$")
CIN_RE   = re.compile(r"^[LU][0-9]{5}[A-Z]{2}[0-9]{4}[A-Z]{3}[0-9]{6}$")
IFSC_RE  = re.compile(r"^[A-Z]{4}0[A-Z0-9]{6}$")
LEI_RE   = re.compile(r"^[A-Z0-9]{18}[0-9]{2}$")


def _pan_from_gstin(gstin: str) -> str:
    return gstin[2:12].upper()


def run_kyc_verification(
    company_type: str,
    company_name: str,
    gstin: str | None,
    pan: str | None,
    cin: str | None,
    ifsc: str | None = None,
    lei_number: str | None = None,
    country_of_incorporation: str | None = None,
    company_reg_number: str | None = None,
    tax_id_tin: str | None = None,
) -> dict:
    """Format-only KYC validation. Returns issues list and overall_passed flag."""
    issues: list[str] = []
    notes: list[str] = []
    gstin_check = None
    pan_check = None

    if company_type == "indian":
        gstin_pan: str | None = None

        # GSTIN
        if not gstin:
            issues.append("GSTIN is required for Indian companies")
        else:
            g = gstin.upper().strip().replace(" ", "")
            if not GSTIN_RE.match(g):
                issues.append(f"Invalid GSTIN format: {g} (expected 15-char, e.g. 22AAAAA0000A1Z5)")
                gstin_check = {"valid": False, "gstin": g}
            else:
                gstin_pan = _pan_from_gstin(g)
                gstin_check = {"valid": True, "gstin": g, "pan_from_gstin": gstin_pan}

        # PAN
        if not pan:
            if gstin_pan:
                pan_check = {"valid": True, "pan": gstin_pan, "source": "extracted_from_gstin"}
                notes.append("PAN extracted from GSTIN")
            else:
                issues.append("PAN is required for Indian companies")
                pan_check = {"valid": False}
        else:
            p = pan.upper().strip().replace(" ", "")
            if not PAN_RE.match(p):
                issues.append(f"Invalid PAN format: {p} (expected AAAAA1234A)")
                pan_check = {"valid": False, "pan": p}
            elif gstin_pan and p != gstin_pan:
                issues.append(f"PAN {p} does not match PAN embedded in GSTIN ({gstin_pan})")
                pan_check = {"valid": False, "pan": p, "matches_gstin": False}
            else:
                pan_check = {"valid": True, "pan": p, "matches_gstin": (p == gstin_pan) if gstin_pan else None}

        # CIN (optional)
        if cin:
            c = cin.upper().strip().replace(" ", "")
            if not CIN_RE.match(c):
                issues.append(f"Invalid CIN format: {c} (expected L12345AB1234ABC123456)")

        # IFSC (optional)
        if ifsc:
            i = ifsc.upper().strip().replace(" ", "")
            if not IFSC_RE.match(i):
                issues.append(f"Invalid IFSC format: {i} (expected 11-char, e.g. HDFC0001234)")

    else:
        # Overseas — presence checks
        if not country_of_incorporation:
            issues.append("Country of Incorporation is required for overseas companies")
        if not company_reg_number:
            issues.append("Company Registration Number is required for overseas companies")
        if not tax_id_tin:
            issues.append("Tax ID / TIN is required for overseas companies")

        # LEI (optional)
        if lei_number:
            lei = lei_number.upper().strip().replace(" ", "")
            if not LEI_RE.match(lei):
                issues.append(f"Invalid LEI format: {lei} (must be exactly 20 alphanumeric characters)")
            else:
                notes.append(f"LEI format valid: {lei}")

    return {
        "company_type": company_type,
        "company_name_submitted": company_name,
        "overall_passed": len(issues) == 0,
        "auto_approvable": False,
        "gstin_check": gstin_check,
        "pan_check": pan_check,
        "issues": issues,
        "notes": notes,
    }
