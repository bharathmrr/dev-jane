"""Standalone KYC integration test — runs real API calls against Setu, KARZA, free GST portal.

Usage:
    python scripts/test_kyc_live.py                    # test all providers
    python scripts/test_kyc_live.py --provider free    # free GST portal only
    python scripts/test_kyc_live.py --provider setu    # Setu only
    python scripts/test_kyc_live.py --provider karza   # KARZA only
    python scripts/test_kyc_live.py --provider sheets  # Google Sheets export only

Results saved to: scripts/kyc_test_results.json
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime
from pathlib import Path

# Add project root to path
sys.path.insert(0, str(Path(__file__).parent.parent))

from dotenv import load_dotenv
load_dotenv()

from app.core.config import settings

# ── Test data ────────────────────────────────────────────────────────────────
# Real Indian company test cases
INDIAN_TESTS = [
    {
        "label": "Valid Indian Company — should AUTO-APPROVE",
        "company_type": "indian",
        "company_name": "Infosys Limited",
        "gstin": "29AABCI1682H1ZN",   # Infosys Karnataka GSTIN
        "pan": "AABCI1682H",
        "cin": "L85110KA1981PLC013115",
    },
    {
        "label": "Invalid GSTIN format — should FAIL",
        "company_type": "indian",
        "company_name": "Test Company",
        "gstin": "INVALID123",
        "pan": "AAAAA1234A",
        "cin": None,
    },
    {
        "label": "PAN mismatch with GSTIN — should FAIL",
        "company_type": "indian",
        "company_name": "Test Company",
        "gstin": "29AABCI1682H1ZN",
        "pan": "ZZZZZ9999Z",   # wrong PAN
        "cin": None,
    },
    {
        "label": "Valid GSTIN, IFSC verify",
        "company_type": "indian",
        "company_name": "Test Corp",
        "gstin": "27AAACT1234A1Z5",
        "pan": "AAACT1234A",
        "cin": None,
        "ifsc": "HDFC0001234",
    },
]

OVERSEAS_TESTS = [
    {
        "label": "Overseas with LEI — should AUTO-APPROVE",
        "company_type": "overseas",
        "company_name": "Boeing Company",
        "gstin": None,
        "pan": None,
        "cin": None,
        "lei_number": "SPTQXBS3R6JWK6BVMC98",   # Boeing LEI
        "country_of_incorporation": "United States",
        "company_reg_number": "1-800-424-9737",
        "tax_id_tin": "91-0425694",
    },
    {
        "label": "Overseas without LEI — should go MANUAL REVIEW",
        "company_type": "overseas",
        "company_name": "Sample Corp",
        "gstin": None,
        "pan": None,
        "cin": None,
        "lei_number": None,
        "country_of_incorporation": "United Kingdom",
        "company_reg_number": "12345678",
        "tax_id_tin": "GB123456789",
    },
]


def _print_result(label: str, result: dict, provider: str) -> None:
    status = "AUTO-APPROVE [OK]" if result["auto_approvable"] else (
        "MANUAL REVIEW [!]" if result["overall_passed"] else "FAILED [X]"
    )
    print("\n" + "-"*60)
    print(f"  {label}")
    print(f"  Provider : {provider}")
    print(f"  Status   : {status}")
    if result.get("gstin_check"):
        g = result["gstin_check"]
        print(f"  GSTIN    : valid={g.get('valid')} source={g.get('source')} name={g.get('business_name','—')}")
    if result.get("pan_check"):
        p = result["pan_check"]
        print(f"  PAN      : valid={p.get('valid')} matches_gstin={p.get('matches_gstin','—')}")
    if result.get("lei_check"):
        l = result["lei_check"]
        print(f"  LEI      : valid={l.get('valid')} name={l.get('legal_name','—')}")
    if result.get("ifsc_check") and not result["ifsc_check"].get("skipped"):
        i = result["ifsc_check"]
        print(f"  IFSC     : valid={i.get('valid')} bank={i.get('bank','—')} branch={i.get('branch','—')}")
    if result.get("issues"):
        print(f"  Issues   : {'; '.join(result['issues'])}")
    if result.get("notes"):
        print(f"  Notes    : {'; '.join(result['notes'][:3])}")


def test_provider(provider: str) -> list[dict]:
    from app.services.kyc_verify import run_kyc_verification

    # Override provider in settings
    os.environ["KYC_PROVIDER"] = provider

    # Re-import to pick up new env (settings is cached — patch directly)
    import app.core.config as _cfg
    _cfg.settings.KYC_PROVIDER = provider

    results = []
    all_tests = INDIAN_TESTS + OVERSEAS_TESTS

    print("\n" + "="*60)
    print(f"  PROVIDER: {provider.upper()}")
    print("="*60)

    for t in all_tests:
        result = run_kyc_verification(
            company_type=t["company_type"],
            company_name=t["company_name"],
            gstin=t.get("gstin"),
            pan=t.get("pan"),
            cin=t.get("cin"),
            ifsc=t.get("ifsc"),
            lei_number=t.get("lei_number"),
            country_of_incorporation=t.get("country_of_incorporation"),
            company_reg_number=t.get("company_reg_number"),
            tax_id_tin=t.get("tax_id_tin"),
        )
        _print_result(t["label"], result, provider)
        results.append({
            "provider": provider,
            "label": t["label"],
            "company_type": t["company_type"],
            "overall_passed": result["overall_passed"],
            "auto_approvable": result["auto_approvable"],
            "issues": result.get("issues", []),
            "notes": result.get("notes", []),
            "gstin_source": (result.get("gstin_check") or {}).get("source"),
        })

    return results


def test_sheets() -> None:
    """Test Google Sheets export with a dummy KYC row."""
    print("\n" + "="*60)
    print("  GOOGLE SHEETS — KYC export test")
    print("="*60)

    if not settings.GOOGLE_SHEETS_SPREADSHEET_ID or not settings.GOOGLE_SHEETS_CREDENTIALS_JSON:
        print("  SKIP — GOOGLE_SHEETS_SPREADSHEET_ID or credentials not set")
        return

    try:
        import json as _json
        import gspread
        from google.oauth2.service_account import Credentials

        creds_dict = _json.loads(settings.GOOGLE_SHEETS_CREDENTIALS_JSON)
        scopes = ["https://spreadsheets.google.com/feeds", "https://www.googleapis.com/auth/drive"]
        creds = Credentials.from_service_account_info(creds_dict, scopes=scopes)
        gc = gspread.authorize(creds)
        sh = gc.open_by_key(settings.GOOGLE_SHEETS_SPREADSHEET_ID)

        ws_name = settings.ONBOARDING_WORKSHEET_NAME or "Onboarding"
        try:
            ws = sh.worksheet(ws_name)
            print(f"  Worksheet '{ws_name}' found OK")
        except gspread.WorksheetNotFound:
            ws = sh.add_worksheet(title=ws_name, rows=1000, cols=35)
            print(f"  Worksheet '{ws_name}' created OK")

        # Write a test row
        test_row = [
            "TEST-ROW", "SCRIPT-TEST", "test@example.com", "Test Company Ltd",
            "Test Contact", "indian", "UNDER_REVIEW", "KYC Test Row",
            "0", "Test Company", "Test Contact", "+91 9000000000",
            "", "", "0", "", "", "0",
            datetime.now().strftime("%d %b %Y %H:%M IST"), "", "0",
            "True", "gst_portal_api", "Test Company Ltd",
            "True", "True", "True", "True",
            "Test Company Ltd", "gst_certificate", "",
        ]
        ws.append_row(test_row)
        print(f"  Test row written to '{ws_name}' OK")
        print(f"  Sheet URL: https://docs.google.com/spreadsheets/d/{settings.GOOGLE_SHEETS_SPREADSHEET_ID}")

    except Exception as e:
        print(f"  FAILED: {e}")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--provider", choices=["free", "setu", "karza", "sheets", "all"], default="all")
    args = parser.parse_args()

    all_results = []

    if args.provider == "sheets":
        test_sheets()
        return

    providers = ["free"]

    if args.provider == "all":
        if settings.SETU_CLIENT_ID:
            providers.append("setu")
        else:
            print("\n!  Setu: SETU_CLIENT_ID not set — skipping")
        if settings.KARZA_API_KEY:
            providers.append("karza")
        else:
            print("!  KARZA: KARZA_API_KEY not set — skipping")
    elif args.provider in ("setu", "karza"):
        providers = [args.provider]
    elif args.provider == "free":
        providers = ["free"]

    for p in providers:
        all_results.extend(test_provider(p))

    # Also test Sheets
    test_sheets()

    # Save results
    out_path = Path(__file__).parent / "kyc_test_results.json"
    out_path.write_text(json.dumps({
        "run_at": datetime.now().isoformat(),
        "results": all_results,
    }, indent=2))
    print(f"\n\nResults saved to {out_path}")

    # Summary
    print("\n" + "="*60)
    print("  SUMMARY")
    print("="*60)
    for r in all_results:
        status = "AUTO OK" if r["auto_approvable"] else ("MANUAL [!]" if r["overall_passed"] else "FAIL [X]")
        print(f"  [{r['provider']:6}] {status}  {r['label'][:45]}")


if __name__ == "__main__":
    main()
