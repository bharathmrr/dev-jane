"""Quick test for Zoho Contracts integration.
Run: python test_zoho_contracts.py
"""
import sys
sys.path.insert(0, ".")

from app.services.zoho_contracts import (
    get_access_token,
    create_contract,
    send_for_signature,
    list_contract_types,
    get_contract_portal_url,
)
from app.core.config import settings

print("=" * 60)
print("ZOHO CONTRACTS — INTEGRATION TEST")
print("=" * 60)

# 1. Token
print("\n[1] Getting access token...")
try:
    token = get_access_token()
    print(f"    ✓ Token: {token[:30]}...")
except Exception as e:
    print(f"    ✗ FAILED: {e}")
    sys.exit(1)

# 2. List contract types
print("\n[2] Listing contract types (your templates)...")
try:
    types = list_contract_types()
    for t in types:
        mark = "★" if t.get("apiName") in (
            settings.ZOHO_CONTRACTS_NDA_TEMPLATE_ID_INDIAN,
            settings.ZOHO_CONTRACTS_NDA_TEMPLATE_ID_OVERSEAS,
            settings.ZOHO_CONTRACTS_AGREEMENT_TEMPLATE_ID_INDIAN,
        ) else " "
        print(f"    {mark} {t.get('name')} → apiName={t.get('apiName')}")
    print(f"\n    Configured env vars:")
    print(f"      NDA_INDIAN  = {settings.ZOHO_CONTRACTS_NDA_TEMPLATE_ID_INDIAN}")
    print(f"      NDA_OVERSEAS= {settings.ZOHO_CONTRACTS_NDA_TEMPLATE_ID_OVERSEAS}")
    print(f"      AGR_INDIAN  = {settings.ZOHO_CONTRACTS_AGREEMENT_TEMPLATE_ID_INDIAN}")
except Exception as e:
    print(f"    ✗ FAILED: {e}")

# 3. Create test NDA contract
print("\n[3] Creating test NDA contract (Jane-nda-auto)...")
test_merge = {
    "company_name":   "Test Company Pvt Ltd",
    "contact_name":   "Test Contact",
    "effective_date": "11 June 2026",
    "pan_number":     "ABCDE1234F",
    "gstin_number":   "29ABCDE1234F1Z5",
}
try:
    result = create_contract(
        contract_type_api_name=settings.ZOHO_CONTRACTS_NDA_TEMPLATE_ID_INDIAN,
        contract_title="TEST NDA — Test Company Pvt Ltd",
        counterparty_name="Test Contact",
        counterparty_email=settings.ORGANIZER_EMAIL,  # send to yourself for testing
        merge_fields=test_merge,
    )
    print(f"    ✓ Contract created!")
    print(f"      id      = {result['id']}")
    print(f"      apiName = {result['apiName']}")
    portal = get_contract_portal_url(result['apiName'])
    print(f"      portal  = {portal}")

    # 4. Send for signature
    contract_api_name = result["apiName"]
    if contract_api_name:
        print(f"\n[4] Sending contract for signature → {settings.ORGANIZER_EMAIL}...")
        ok = send_for_signature(contract_api_name)
        if ok:
            print(f"    ✓ Sent! Zoho Contracts will email the signing link to {settings.ORGANIZER_EMAIL}")
        else:
            print(f"    ✗ send_for_signature returned False — check logs")
    else:
        print("\n[4] Skipped send_for_signature (no apiName returned)")

except Exception as e:
    print(f"    ✗ FAILED: {e}")
    import traceback
    traceback.print_exc()

print("\n" + "=" * 60)
print("Done.")
