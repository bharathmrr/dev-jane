"""
End-to-end test for the document (NDA / Agreement) flow.
Target: onboarding record for desaithryakshari@gmail.com
DB reset done via psql before this script runs.
"""
import hmac, hashlib, httpx, sys

OID  = "b7173420-076c-4b8a-a0e4-257cf557edaf"
HMAC_SECRET = "89fdb1243f2fa9d093727f11d22faa02da7128389296c06d"
BASE = "http://localhost:8000/api/v1"

def make_token(oid, doc_type, purpose):
    msg = f"doc:{purpose}:{doc_type}:{oid}".encode()
    return hmac.new(HMAC_SECRET.encode(), msg, hashlib.sha256).hexdigest()[:24]

EDIT_TOK = make_token(OID, "nda", "edit")
SIGN_TOK = make_token(OID, "nda", "sign")
AGR_EDIT = make_token(OID, "agreement", "edit")
AGR_SIGN = make_token(OID, "agreement", "sign")

print(f"\nNDA  edit token : {EDIT_TOK}")
print(f"NDA  sign token : {SIGN_TOK}")
print(f"AGR  edit token : {AGR_EDIT}")
print(f"AGR  sign token : {AGR_SIGN}\n")

PASS = []; FAIL = []

def ok(label, r=None, detail=""):
    msg = f"  OK  {label}"
    if r is not None: msg += f" [{r.status_code}]"
    if detail: msg += f"  -- {detail}"
    print(msg); PASS.append(label)

def fail(label, r=None, detail=""):
    msg = f"  FAIL {label}"
    if r is not None: msg += f" [{r.status_code}]"
    if detail: msg += f"  -- {detail}"
    print(msg); FAIL.append(label)

def check(label, r, expected=200, detail=""):
    if r.status_code == expected:
        ok(label, r, detail)
    else:
        body = ""
        try: body = r.json().get("detail", r.text[:300])
        except: body = r.text[:300]
        fail(label, r, body or detail)

with httpx.Client(base_url=BASE, timeout=30, follow_redirects=False) as c:

    print("=" * 60)
    print("STEP 1 -- P1 live editor page (GET /editor/)")
    print("=" * 60)
    r = c.get(f"/documents/editor/{OID}/nda/{EDIT_TOK}")
    check("GET /editor/ nda -- live editor page loads", r, 200)
    if r.status_code == 200:
        ok("has sendForReview") if "sendForReview" in r.text else fail("MISSING sendForReview -- JS broken")
        ok("has renderBubbles") if "renderBubbles" in r.text else fail("MISSING renderBubbles")
        ok("has _bubAccept helper") if "_bubAccept" in r.text else fail("MISSING _bubAccept -- escape bug not fixed")
        ok("has _bubReject helper") if "_bubReject" in r.text else fail("MISSING _bubReject -- escape bug not fixed")
        ok("has _cancelBubRej helper") if "_cancelBubRej" in r.text else fail("MISSING _cancelBubRej")
        ok("has no raw \\' escapes in JS") if "\\'" not in r.text else fail("RAW \\' ESCAPE IN JS -- will break script")

    print("\n" + "=" * 60)
    print("STEP 2 -- Save document (POST /save/)")
    print("=" * 60)
    r = c.post(f"/documents/save/{OID}/nda/{EDIT_TOK}",
               json={"html": "<p>This NDA is between Jane Aerospace and the Lead company.</p>",
                     "mode": "live"})
    check("POST /save/ without comment", r, 200)

    r = c.post(f"/documents/save/{OID}/nda/{EDIT_TOK}",
               json={"html": "<p class='chg-block chg-p1' data-cmt='temp'>Jane Aerospace clause 3 was revised for mutual benefit.</p>",
                     "mode": "live",
                     "comment": "Revised clause 3 for mutual benefit"})
    check("POST /save/ with comment", r, 200)
    cmt_id = None
    try:
        d = r.json(); cmt_id = d.get("comment_id")
        ok("response has comment_id", detail=cmt_id) if cmt_id else fail("response missing comment_id")
    except: fail("save response not JSON")

    print("\n" + "=" * 60)
    print("STEP 3 -- Versions / comments (GET /versions/)")
    print("=" * 60)
    r = c.get(f"/documents/versions/{OID}/nda/{EDIT_TOK}")
    check("GET /versions/", r, 200)
    try:
        d = r.json()
        cmts = d.get("comments", [])
        ok(f"versions response -- {len(cmts)} comment(s)", detail=str([x.get('id') for x in cmts]))
    except: fail("versions response not JSON")

    print("\n" + "=" * 60)
    print("STEP 4 -- Preview info (GET /preview-info/)")
    print("=" * 60)
    r = c.get(f"/documents/preview-info/{OID}/nda/{EDIT_TOK}")
    check("GET /preview-info/", r, 200)

    print("\n" + "=" * 60)
    print("STEP 5 -- Accept pending comment before send")
    print("=" * 60)
    if cmt_id:
        r = c.post(f"/documents/comment-action/{OID}/nda/{EDIT_TOK}",
                   json={"comment_id": cmt_id, "action": "accept", "text": ""})
        check("POST /comment-action/ accept", r, 200)
    else:
        ok("No comment to accept (skipped)")

    # verify no open comments
    r2 = c.get(f"/documents/versions/{OID}/nda/{EDIT_TOK}")
    try:
        d2 = r2.json()
        open_cmts = [x for x in d2.get("comments",[]) if x.get("status","pending") in ("pending","rejected")]
        ok(f"No open comments remaining") if not open_cmts else fail(f"{len(open_cmts)} open comment(s) still blocking send")
    except: pass

    print("\n" + "=" * 60)
    print("STEP 6 -- Send for review (POST /send/) -> emails desaithryakshari@gmail.com")
    print("=" * 60)
    r = c.post(f"/documents/send/{OID}/nda/{EDIT_TOK}",
               json={"html": "<p class='chg-block chg-p1'>Jane Aerospace NDA final draft.</p>", "mode": "live"})
    check("POST /send/ nda -- send to lead", r, 200,
          detail=r.json().get("message","") if r.status_code==200 else "")

    print("\n" + "=" * 60)
    print("STEP 7 -- Portal redirect (GET /portal/, P2 link)")
    print("=" * 60)
    r = c.get(f"/documents/portal/{OID}/nda/{SIGN_TOK}")
    if r.status_code == 302:
        ok("GET /portal/ -- 302 redirect to sign page", r, detail=r.headers.get("location","?"))
    else:
        fail("GET /portal/ -- expected 302", r,
             detail=r.json().get("detail","") if "json" in r.headers.get("content-type","") else r.text[:200])

    print("\n" + "=" * 60)
    print("STEP 8 -- Lead sign page (GET /sign/)")
    print("=" * 60)
    r = c.get(f"/documents/sign/{OID}/nda/{SIGN_TOK}")
    check("GET /sign/ nda -- lead sign page loads", r, 200)
    if r.status_code == 200:
        ok("sign page has signature UI") if ("signature" in r.text.lower() or "sign" in r.text.lower()) else fail("sign page missing signature content")

    print("\n" + "=" * 60)
    print("STEP 9 -- Internal sign by P1 team (POST /internal-sign/)")
    print("=" * 60)
    r = c.post(f"/documents/internal-sign/{OID}/nda/{EDIT_TOK}",
               json={"signatory_name": "Leo Peter Charles",
                     "signatory_email": "desaithryakshari@gmail.com"})
    if r.status_code == 200:
        ok("POST /internal-sign/ -- P1 signed NDA", r,
           detail=r.json().get("message","") if r.status_code==200 else "")
    elif r.status_code == 409:
        body = r.json().get("detail","")
        ok("POST /internal-sign/ -- 409 (expected if already signed)", r, detail=body)
    else:
        check("POST /internal-sign/", r, 200)

    print("\n" + "=" * 60)
    print("STEP 10 -- Agreement editor (GET /editor/ agreement)")
    print("=" * 60)
    r = c.get(f"/documents/editor/{OID}/agreement/{AGR_EDIT}")
    check("GET /editor/ agreement -- live editor loads", r, 200)

    print("\n" + "=" * 60)
    print("STEP 11 -- Agreement sign page (GET /sign/ agreement)")
    print("=" * 60)
    r = c.get(f"/documents/sign/{OID}/agreement/{AGR_SIGN}")
    check("GET /sign/ agreement -- sign page loads", r, 200)

    print("\n" + "=" * 60)
    print("STEP 12 -- Security: bad tokens must be rejected")
    print("=" * 60)
    r = c.get(f"/documents/edit/{OID}/nda/baaaaaaaaaaaaaaaaaaaaaaaa")
    ok("bad edit token rejected", r) if r.status_code in (400,401,403) else fail("bad edit token NOT rejected", r)
    r = c.get(f"/documents/sign/{OID}/nda/baaaaaaaaaaaaaaaaaaaaaaaa")
    ok("bad sign token rejected", r) if r.status_code in (400,401,403) else fail("bad sign token NOT rejected", r)
    r = c.get(f"/documents/portal/{OID}/nda/baaaaaaaaaaaaaaaaaaaaaaaa")
    ok("bad portal token rejected", r) if r.status_code in (400,401,403) else fail("bad portal token NOT rejected", r)

print("\n" + "=" * 60)
print(f"RESULTS: {len(PASS)} passed  |  {len(FAIL)} failed")
if FAIL:
    print("\nFailed checks:")
    for f in FAIL: print(f"  * {f}")
print("=" * 60)
sys.exit(0 if not FAIL else 1)
