"""NDA / Agreement document flow using the official JAPL PDF templates.

Team flow (HMAC 'edit' token, link arrives in the draft-ready email):
  GET  /documents/edit/{onboarding_id}/{doc_type}/{token}   — edit pre-filled fields, preview
  POST /documents/save/{onboarding_id}/{doc_type}/{token}   — save field values
  POST /documents/send/{onboarding_id}/{doc_type}/{token}   — save + email signing link to lead

Lead flow (HMAC 'sign' token, link arrives in the signing email):
  GET  /documents/sign/{onboarding_id}/{doc_type}/{token}   — view PDF + sign form
  POST /documents/sign/{onboarding_id}/{doc_type}/{token}   — record e-signature, auto-advance

Shared:
  GET  /documents/pdf/{onboarding_id}/{doc_type}/{token}    — filled PDF (inline; edit or sign token)
  GET  /documents/signed/{onboarding_id}/{doc_type}/{token} — signed PDF with signature certificate

doc_type: "nda" | "agreement"
Field values + signature audit data live as JSON in
OnboardingRecord.nda_draft_content / agreement_draft_content.
"""
from __future__ import annotations

import datetime as dt
import json
import uuid
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse, Response
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.logging import get_logger
from app.db.base import DocumentStatus
from app.db.models import KYCSubmission, LeadV2, OnboardingRecord
from app.db.session import get_db
from app.services.onboarding_email import (
    make_doc_sign_url,
    verify_doc_token,
)

logger = get_logger("document_endpoints")

_IST = ZoneInfo("Asia/Kolkata")

router = APIRouter(prefix="/documents", tags=["documents"])

_DOC_LABELS = {"nda": "Non-Disclosure Agreement", "agreement": "Supply / Customer Agreement"}


def _now_ist() -> dt.datetime:
    return dt.datetime.now(_IST)


def _fmt(d: dt.datetime | None) -> str:
    return d.astimezone(_IST).strftime("%d %b %Y %H:%M IST") if d else ""


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _check_doc_type(doc_type: str) -> None:
    if doc_type not in ("nda", "agreement"):
        raise HTTPException(404, "Unknown document type")


def _check_token(onboarding_id: str, doc_type: str, token: str, *purposes: str) -> None:
    if not any(verify_doc_token(onboarding_id, doc_type, p, token) for p in purposes):
        raise HTTPException(403, "Invalid or expired link")


async def _load(db: AsyncSession, onboarding_id: str) -> tuple[OnboardingRecord, LeadV2]:
    rec = await db.get(OnboardingRecord, uuid.UUID(onboarding_id))
    if not rec:
        raise HTTPException(404, "Onboarding record not found")
    lead = await db.get(LeadV2, rec.lead_id)
    if not lead:
        raise HTTPException(404, "Lead not found")
    return rec, lead


def _is_overseas(rec: OnboardingRecord) -> bool:
    return (rec.company_type or "indian").lower() in ("overseas", "foreign", "international")


def _get_doc_data(rec: OnboardingRecord, doc_type: str) -> dict:
    raw = rec.nda_draft_content if doc_type == "nda" else rec.agreement_draft_content
    if raw:
        try:
            data = json.loads(raw)
            if isinstance(data, dict) and "replacements" in data:
                return data
        except (ValueError, TypeError):
            pass  # legacy content (Zoho link) — rebuild defaults
    return {}


def _set_doc_data(rec: OnboardingRecord, doc_type: str, data: dict) -> None:
    raw = json.dumps(data, ensure_ascii=False)
    if doc_type == "nda":
        rec.nda_draft_content = raw
    else:
        rec.agreement_draft_content = raw


async def _build_default_data(db: AsyncSession, rec: OnboardingRecord, lead: LeadV2, doc_type: str) -> dict:
    from app.services.pdf_documents import default_replacements, kyc_fields_from_submission
    kyc = (await db.execute(
        select(KYCSubmission)
        .where(KYCSubmission.onboarding_id == rec.id)
        .order_by(KYCSubmission.attempt_number.desc())
    )).scalars().first()
    extra = (kyc.kyc_verification_result or {}).get("extra_fields", {}) if kyc else {}
    fields = kyc_fields_from_submission(
        company_name=(kyc.company_name if kyc else None) or lead.business_name,
        cin_number=kyc.cin_number if kyc else None,
        extra=extra or {},
        is_overseas=_is_overseas(rec),
    )
    return {
        "replacements": default_replacements(doc_type, fields),
        "signatory_name": (kyc.contact_name if kyc else None) or lead.contact_name or "",
        "signatory_email": lead.email,
    }


async def _ensure_doc_data(db: AsyncSession, rec: OnboardingRecord, lead: LeadV2, doc_type: str) -> dict:
    data = _get_doc_data(rec, doc_type)
    if not data:
        data = await _build_default_data(db, rec, lead, doc_type)
        _set_doc_data(rec, doc_type, data)
        await db.commit()
    return data


def _render_pdf(rec: OnboardingRecord, doc_type: str, data: dict) -> bytes:
    """Always render from the original PDF template — format and content are
    never altered; only placeholder values are written in."""
    from app.services.pdf_documents import fill_document
    return fill_document(doc_type, _is_overseas(rec), data.get("replacements", []))


# ---------------------------------------------------------------------------
# PDF preview (filled, unsigned)
# ---------------------------------------------------------------------------

@router.get("/pdf/{onboarding_id}/{doc_type}/{token}", include_in_schema=False)
async def document_pdf(onboarding_id: str, doc_type: str, token: str, db: AsyncSession = Depends(get_db)):
    _check_doc_type(doc_type)
    _check_token(onboarding_id, doc_type, token, "edit", "sign")
    rec, lead = await _load(db, onboarding_id)
    data = await _ensure_doc_data(db, rec, lead, doc_type)
    pdf = _render_pdf(rec, doc_type, data)
    return Response(content=pdf, media_type="application/pdf",
                    headers={"Content-Disposition": f'inline; filename="{doc_type}_jane_aerospace.pdf"'})


@router.get("/signed/{onboarding_id}/{doc_type}/{token}", include_in_schema=False)
async def document_signed_pdf(onboarding_id: str, doc_type: str, token: str, db: AsyncSession = Depends(get_db)):
    _check_doc_type(doc_type)
    _check_token(onboarding_id, doc_type, token, "edit", "sign")
    rec, lead = await _load(db, onboarding_id)
    data = _get_doc_data(rec, doc_type)
    sig = data.get("signature")
    if not sig:
        raise HTTPException(404, "Document has not been signed yet")
    from app.services.pdf_documents import append_signature_page
    pdf = append_signature_page(_render_pdf(rec, doc_type, data), doc_type, sig)
    return Response(content=pdf, media_type="application/pdf",
                    headers={"Content-Disposition": f'inline; filename="{doc_type}_signed_jane_aerospace.pdf"'})


# ---------------------------------------------------------------------------
# Team: edit page
# ---------------------------------------------------------------------------

@router.get("/edit/{onboarding_id}/{doc_type}/{token}", response_class=HTMLResponse, include_in_schema=False)
async def document_edit_page(onboarding_id: str, doc_type: str, token: str, db: AsyncSession = Depends(get_db)):
    _check_doc_type(doc_type)
    _check_token(onboarding_id, doc_type, token, "edit")
    rec, lead = await _load(db, onboarding_id)
    data = await _ensure_doc_data(db, rec, lead, doc_type)

    label = _DOC_LABELS[doc_type]
    status = rec.nda_status if doc_type == "nda" else rec.agreement_status
    status = getattr(status, "value", status)
    signed = bool(data.get("signature"))
    base = "/api/v1/documents"
    data_json = json.dumps({
        "replacements": data.get("replacements", []),
        "signatory_name": data.get("signatory_name", ""),
        "signatory_email": data.get("signatory_email", ""),
    }, ensure_ascii=False).replace("</", "<\\/")
    signed_badge = ('<span class="chip" style="background:#dcfce7;color:#15803d;">✓ SIGNED</span>' if signed else "")
    signed_btn = (f'<a class="btn b-blue" target="_blank" href="{base}/signed/{onboarding_id}/{doc_type}/{token}">⬇ Signed PDF</a>'
                  if signed else "")

    html = f"""<!DOCTYPE html>
<html lang="en">
<head><meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>{label} — {lead.business_name}</title>
<style>
  *{{box-sizing:border-box;margin:0;padding:0;}}
  body{{font-family:'Segoe UI',Arial,sans-serif;background:#e8edf5;overflow:hidden;}}
  .chip{{display:inline-block;padding:3px 11px;border-radius:99px;font-size:11px;font-weight:700;
        background:#dbeafe;color:#1e40af;margin-left:6px;vertical-align:middle;}}
  .btn{{display:inline-flex;align-items:center;gap:6px;padding:9px 16px;border-radius:8px;border:none;
       color:#fff;font-size:12.5px;font-weight:700;cursor:pointer;text-decoration:none;font-family:inherit;}}
  .b-blue{{background:#1a56db;}} .b-green{{background:#16a34a;}}
  .b-line{{background:#fff;color:#1a3a6b;border:1.5px solid #c7d4ee;}}
  .topbar{{position:fixed;top:0;left:0;right:0;height:56px;background:#0c2344;color:#fff;z-index:20;
          display:flex;align-items:center;gap:12px;padding:0 18px;}}
  .topbar .ttl{{font-weight:800;font-size:13.5px;letter-spacing:.03em;white-space:nowrap;}}
  .topbar .sub{{font-size:11.5px;color:#b9c8e4;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;
               min-width:0;flex-shrink:1;}}
  .topbar .grow{{flex:1;}}
  #saved{{font-size:11.5px;color:#86efac;min-width:70px;text-align:right;}}
  .split{{position:fixed;top:56px;left:0;right:0;bottom:0;display:flex;}}
  .pv{{flex:1;background:#525659;}}
  .pv iframe{{width:100%;height:100%;border:none;}}
  .panel{{width:400px;max-width:46vw;background:#fff;border-left:1px solid #d9e1ef;overflow-y:auto;
         padding:18px 20px 40px;}}
  .sec{{font-size:11px;font-weight:800;color:#1a3a6b;text-transform:uppercase;letter-spacing:.07em;
       border-bottom:2px solid #dbeafe;padding-bottom:6px;margin:18px 0 12px;}}
  .fld{{margin-bottom:11px;}}
  .fld label{{display:block;font-size:11.5px;font-weight:700;color:#374151;margin-bottom:4px;}}
  .fld .ph{{font-family:monospace;font-size:10px;color:#92400e;background:#fff7ed;border-radius:4px;
           padding:1px 6px;margin-left:6px;font-weight:600;}}
  .fld input{{width:100%;padding:9px 11px;border:1.5px solid #cbd5e1;border-radius:7px;font-size:13px;}}
  .fld input:focus{{outline:none;border-color:#1a56db;box-shadow:0 0 0 3px rgba(26,86,219,.12);}}
  .custom{{display:grid;grid-template-columns:1fr 1fr 26px;gap:6px;margin-bottom:8px;align-items:center;}}
  .custom input{{padding:8px 9px;border:1.5px solid #cbd5e1;border-radius:7px;font-size:12px;width:100%;}}
  .custom input.cf{{font-family:monospace;background:#f8fafc;color:#92400e;}}
  .del{{background:none;border:none;color:#dc2626;font-size:16px;cursor:pointer;}}
  .note{{background:#eff6ff;border-left:3px solid #1a56db;padding:9px 12px;font-size:11.5px;color:#1e3a8a;
        border-radius:5px;margin-bottom:14px;line-height:1.5;}}
  .addbtn{{background:#fff;color:#1a3a6b;border:1.5px dashed #94a3b8;border-radius:7px;padding:8px;width:100%;
          font-size:12px;font-weight:700;cursor:pointer;}}
  @media(max-width:760px){{.split{{flex-direction:column;}}.panel{{width:100%;max-width:100%;height:55%;}}
    .pv{{height:45%;}}}}
</style></head>
<body>
<div class="topbar">
  <span class="ttl">✈ JANE AEROSPACE</span>
  <span class="sub">{label} — {lead.business_name} ({'Overseas' if _is_overseas(rec) else 'Indian'} template)</span>
  <span class="chip">{status}</span>{signed_badge}
  <span class="grow"></span>
  <span id="saved">Saved ✓</span>
  <button class="btn b-line" onclick="resetDoc()" title="Rebuild values from KYC data">↺ Reset</button>
  {signed_btn}
  <button class="btn b-green" onclick="sendToLead()">📨 Send for Signature</button>
</div>

<div class="split">
  <div class="pv"><iframe id="pv" src="{base}/pdf/{onboarding_id}/{doc_type}/{token}#toolbar=1"></iframe></div>
  <div class="panel">
    <div class="note">The official PDF template is shown exactly as-is — its format and content never change.
      Only the placeholder values below are written into it. Edit a value and the preview updates.</div>
    <div class="sec">Placeholder Values</div>
    <div id="std-fields"></div>
    <div class="sec">Custom Replacements</div>
    <div id="custom-rows"></div>
    <button class="addbtn" onclick="addCustom('','')">+ Add custom find &amp; replace</button>
    <div class="sec">Signatory (receives the signing link)</div>
    <div class="fld"><label>Full Name</label><input type="text" id="sig-name" oninput="dirty()"></div>
    <div class="fld"><label>Email</label><input type="email" id="sig-email" oninput="dirty()"></div>
  </div>
</div>

<script>
const BASE = '{base}';
const OID = '{onboarding_id}', DT = '{doc_type}', TOK = '{token}';
const initial = {data_json};
let timer = null;

// friendly labels for the standard template placeholders
const STD = {{
  '[●] 2024': 'Agreement Date',
  '[Date]': 'Effective Date',
  '[Company Name]': 'Company Name',
  '[Company Registration Number]': 'Registration Number (CIN / Reg. No)',
  '[Address]': 'Registered Address',
  '[ABC]': 'Company Reference',
  'ABC': 'Company Reference (short)',
}};

function esc(s) {{ const d = document.createElement('div'); d.textContent = s || ''; return d.innerHTML; }}

function render() {{
  const std = document.getElementById('std-fields');
  const cus = document.getElementById('custom-rows');
  std.innerHTML = ''; cus.innerHTML = '';
  (initial.replacements || []).forEach((r, i) => {{
    if (STD[r.find] !== undefined) {{
      std.innerHTML += `<div class="fld"><label>${{esc(STD[r.find])}}<span class="ph">${{esc(r.find)}}</span></label>
        <input type="text" data-find="${{esc(r.find)}}" value="${{esc(r.replace)}}" oninput="dirty()"></div>`;
    }} else {{
      addCustom(r.find, r.replace, true);
    }}
  }});
}}
function addCustom(find, repl, init) {{
  const div = document.createElement('div');
  div.className = 'custom';
  div.innerHTML = `<input class="cf" placeholder="find in document" value="${{esc(find)}}" oninput="dirty()">
    <input placeholder="replace with" value="${{esc(repl)}}" oninput="dirty()">
    <button class="del" onclick="this.parentElement.remove();dirty()">✕</button>`;
  document.getElementById('custom-rows').appendChild(div);
}}

function collect() {{
  const rows = [];
  document.querySelectorAll('#std-fields input').forEach(i =>
    rows.push({{find: i.dataset.find, replace: i.value}}));
  document.querySelectorAll('#custom-rows .custom').forEach(c => {{
    const ins = c.querySelectorAll('input');
    if (ins[0].value.trim()) rows.push({{find: ins[0].value, replace: ins[1].value}});
  }});
  return {{
    replacements: rows,
    signatory_name: document.getElementById('sig-name').value.trim(),
    signatory_email: document.getElementById('sig-email').value.trim(),
  }};
}}

function dirty() {{
  const el = document.getElementById('saved');
  el.textContent = '● editing…'; el.style.color = '#fbbf24';
  clearTimeout(timer);
  timer = setTimeout(saveAndRefresh, 1200);
}}

async function save() {{
  const el = document.getElementById('saved');
  el.textContent = 'Saving…'; el.style.color = '#fbbf24';
  const r = await fetch(`${{BASE}}/save/${{OID}}/${{DT}}/${{TOK}}`, {{
    method: 'POST', headers: {{'Content-Type': 'application/json'}},
    body: JSON.stringify(collect())
  }}).catch(() => null);
  const ok = r && r.ok;
  el.textContent = ok ? 'Saved ✓' : 'Save failed';
  el.style.color = ok ? '#86efac' : '#fca5a5';
  return ok;
}}

async function saveAndRefresh() {{
  if (await save())
    document.getElementById('pv').src = `${{BASE}}/pdf/${{OID}}/${{DT}}/${{TOK}}?t=${{Date.now()}}#toolbar=1`;
}}

async function resetDoc() {{
  if (!confirm('Reset all values back to the approved KYC data?')) return;
  const r = await fetch(`${{BASE}}/reset/${{OID}}/${{DT}}/${{TOK}}`, {{method: 'POST'}});
  if (r.ok) location.reload(); else alert('Reset failed');
}}

async function sendToLead() {{
  clearTimeout(timer);
  await save();
  if (!confirm('Send this document to the lead for e-signature?')) return;
  const r = await fetch(`${{BASE}}/send/${{OID}}/${{DT}}/${{TOK}}`, {{
    method: 'POST', headers: {{'Content-Type': 'application/json'}},
    body: JSON.stringify(collect())
  }});
  const d = await r.json().catch(() => ({{}}));
  alert(r.ok ? (d.message || 'Sent for signature.') : (d.detail || 'Send failed'));
}}

render();
document.getElementById('sig-name').value = initial.signatory_name || '';
document.getElementById('sig-email').value = initial.signatory_email || '';
</script>
</body></html>"""
    return HTMLResponse(html)


class _DocSaveBody(BaseModel):
    replacements: list[dict] = []
    signatory_name: str = ""
    signatory_email: str = ""
    html: str = ""


@router.post("/save/{onboarding_id}/{doc_type}/{token}")
async def document_save(onboarding_id: str, doc_type: str, token: str,
                        body: _DocSaveBody, db: AsyncSession = Depends(get_db)):
    _check_doc_type(doc_type)
    _check_token(onboarding_id, doc_type, token, "edit")
    rec, lead = await _load(db, onboarding_id)
    data = _get_doc_data(rec, doc_type)
    if body.replacements:
        data["replacements"] = [
            {"find": str(r.get("find", ""))[:200], "replace": str(r.get("replace", ""))[:500]}
            for r in body.replacements if str(r.get("find", "")).strip()
        ]
    if body.signatory_name:
        data["signatory_name"] = body.signatory_name[:200]
    if body.signatory_email:
        data["signatory_email"] = body.signatory_email[:320]
    data.pop("html", None)   # PDF template is the only source — never store HTML
    data.setdefault("replacements", [])
    _set_doc_data(rec, doc_type, data)
    await db.commit()
    return {"message": "Saved"}


@router.post("/reset/{onboarding_id}/{doc_type}/{token}")
async def document_reset(onboarding_id: str, doc_type: str, token: str,
                         db: AsyncSession = Depends(get_db)):
    """Rebuild the document from the original template + approved KYC data."""
    _check_doc_type(doc_type)
    _check_token(onboarding_id, doc_type, token, "edit")
    rec, lead = await _load(db, onboarding_id)
    old = _get_doc_data(rec, doc_type)
    if old.get("signature"):
        raise HTTPException(409, "Document already signed — cannot reset")
    data = await _build_default_data(db, rec, lead, doc_type)
    _set_doc_data(rec, doc_type, data)
    await db.commit()
    return {"message": "Document reset from template + KYC data"}


@router.post("/send/{onboarding_id}/{doc_type}/{token}")
async def document_send(onboarding_id: str, doc_type: str, token: str,
                        body: _DocSaveBody, db: AsyncSession = Depends(get_db)):
    _check_doc_type(doc_type)
    _check_token(onboarding_id, doc_type, token, "edit")
    rec, lead = await _load(db, onboarding_id)

    # save the latest edits first
    data = _get_doc_data(rec, doc_type)
    if body.replacements:
        data["replacements"] = [
            {"find": str(r.get("find", ""))[:200], "replace": str(r.get("replace", ""))[:500]}
            for r in body.replacements if str(r.get("find", "")).strip()
        ]
    data.pop("html", None)
    data["signatory_name"] = body.signatory_name[:200] or data.get("signatory_name", "")
    data["signatory_email"] = body.signatory_email[:320] or data.get("signatory_email", "") or lead.email
    data.setdefault("replacements", [])
    _set_doc_data(rec, doc_type, data)

    now = _now_ist()
    if doc_type == "nda":
        rec.nda_status = DocumentStatus.SENT_TO_LEAD
        rec.nda_sent_at = now
        rec.nda_status_display = f"NDA Sent to Lead ({_fmt(now)}) — Awaiting Signature"
    else:
        rec.agreement_status = DocumentStatus.SENT_TO_LEAD
        rec.agreement_sent_at = now
        rec.agreement_status_display = f"Agreement Sent to Lead ({_fmt(now)}) — Awaiting Signature"
    await db.commit()

    from app.services.onboarding_email import send_document_sign_email
    sign_url = make_doc_sign_url(onboarding_id, doc_type)
    to_email = data["signatory_email"] or lead.email
    send_document_sign_email(
        to_email=to_email,
        lead_name=data["signatory_name"] or lead.contact_name or lead.business_name,
        company_name=lead.business_name,
        doc_type=doc_type,
        sign_url=sign_url,
    )

    try:
        from app.workers.onboarding_tasks import export_onboarding_to_sheets
        export_onboarding_to_sheets.delay(onboarding_id)
    except Exception:
        pass

    try:
        from app.services.zoho_crm import sync_onboarding_stage
        sync_onboarding_stage(
            email=lead.email,
            contact_name=lead.contact_name or lead.business_name,
            company_name=lead.business_name,
            stage="NDA Sent for E-Sign" if doc_type == "nda" else "Agreement Sent for E-Sign",
            detail=f"Signing link emailed to {to_email}",
            company_type=rec.company_type or "",
            onboarding_id=onboarding_id,
        )
    except Exception:
        pass

    label = _DOC_LABELS[doc_type]
    logger.info("document_sent_for_signature", onboarding_id=onboarding_id, doc_type=doc_type, to=to_email)
    return {"message": f"{label} sent to {to_email} for signature."}


# ---------------------------------------------------------------------------
# Lead: signing page
# ---------------------------------------------------------------------------

def _result_page(title: str, message: str, ok: bool = True, extra_html: str = "") -> str:
    color = "#16a34a" if ok else "#dc2626"
    icon = "✅" if ok else "❌"
    return f"""<!DOCTYPE html><html><head><meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>{title}</title></head>
<body style="font-family:Arial,sans-serif;background:#f5f7fb;display:flex;align-items:center;
  justify-content:center;min-height:100vh;margin:0;">
<div style="background:#fff;border-radius:12px;padding:40px 48px;max-width:520px;
  text-align:center;box-shadow:0 2px 16px rgba(0,0,0,.1);">
  <div style="font-size:52px;margin-bottom:16px;">{icon}</div>
  <h2 style="color:{color};margin:0 0 12px;">{title}</h2>
  <p style="color:#555;font-size:15px;margin:0;">{message}</p>
  {extra_html}
  <p style="color:#aaa;font-size:12px;margin-top:24px;">Jane Aerospace</p>
</div></body></html>"""


@router.get("/sign/{onboarding_id}/{doc_type}/{token}", response_class=HTMLResponse, include_in_schema=False)
async def document_sign_page(onboarding_id: str, doc_type: str, token: str, db: AsyncSession = Depends(get_db)):
    _check_doc_type(doc_type)
    _check_token(onboarding_id, doc_type, token, "sign")
    rec, lead = await _load(db, onboarding_id)
    data = await _ensure_doc_data(db, rec, lead, doc_type)
    label = _DOC_LABELS[doc_type]

    if data.get("signature"):
        signed_url = f"/api/v1/documents/signed/{onboarding_id}/{doc_type}/{token}"
        return HTMLResponse(_result_page(
            "Already Signed",
            f"This {label} was signed by {data['signature'].get('signed_name', '')} "
            f"on {data['signature'].get('signed_at', '')}.",
            extra_html=f'<p style="margin-top:20px;"><a href="{signed_url}" target="_blank" '
                       f'style="background:#1a56db;color:#fff;padding:11px 24px;border-radius:6px;'
                       f'text-decoration:none;font-weight:700;font-size:14px;">View Signed Document</a></p>'))

    pdf_url = f"/api/v1/documents/pdf/{onboarding_id}/{doc_type}/{token}"
    sig_name = data.get("signatory_name", "") or (lead.contact_name or "")

    html = f"""<!DOCTYPE html>
<html lang="en">
<head><meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Sign {label} — Jane Aerospace</title>
<link href="https://fonts.googleapis.com/css2?family=Dancing+Script:wght@600&family=Great+Vibes&family=Pacifico&display=swap" rel="stylesheet">
<style>
  .fonts{{display:grid;grid-template-columns:repeat(auto-fit,minmax(130px,1fr));gap:10px;margin-top:6px;}}
  .font-opt{{border:2px solid #d1d5db;border-radius:8px;padding:10px 8px;text-align:center;
            font-size:21px;cursor:pointer;color:#1a3a6b;background:#fff;transition:all .15s;}}
  .font-opt.selected{{border-color:#1a56db;background:#eff6ff;box-shadow:0 0 0 3px rgba(26,86,219,.15);}}
  #sig-preview{{border:1px dashed #c7d4ee;border-radius:8px;background:#fbfdff;min-height:64px;
               display:flex;align-items:center;padding:8px 18px;font-size:30px;color:#10245c;
               font-family:Georgia,serif;margin-top:6px;}}
</style>
<style>
  *{{box-sizing:border-box;}}
  body{{font-family:Arial,sans-serif;background:#f4f6fb;margin:0;padding:20px;}}
  .wrap{{max-width:900px;margin:0 auto;}}
  .card{{background:#fff;border-radius:12px;box-shadow:0 2px 16px rgba(0,0,0,.1);padding:28px 32px;margin-bottom:18px;}}
  h1{{color:#1a3a6b;font-size:20px;margin:0 0 4px;}}
  .sub{{color:#666;font-size:13px;margin:0 0 14px;}}
  iframe{{width:100%;height:600px;border:1px solid #d1d5db;border-radius:8px;background:#fff;}}
  label{{display:block;font-size:13px;font-weight:600;color:#222;margin:14px 0 4px;}}
  input[type=text]{{width:100%;padding:11px 13px;border:1px solid #ccc;border-radius:6px;font-size:14px;}}
  .chk{{display:flex;gap:10px;align-items:flex-start;margin:18px 0;font-size:13px;color:#374151;line-height:1.5;}}
  .chk input{{margin-top:3px;width:17px;height:17px;}}
  button{{background:#16a34a;color:#fff;border:none;padding:14px 34px;border-radius:7px;
         font-size:16px;font-weight:700;cursor:pointer;width:100%;}}
  button:disabled{{background:#9ca3af;cursor:not-allowed;}}
  .dl{{font-size:13px;margin-top:8px;}}
  #err{{display:none;background:#fee2e2;color:#991b1b;padding:10px 14px;border-radius:6px;
       font-size:13px;margin:12px 0;}}
  .two-col{{display:grid;grid-template-columns:1fr 1fr;gap:14px;}}
  @media(max-width:640px){{.two-col{{grid-template-columns:1fr;}}.card{{padding:20px 16px;}}iframe{{height:420px;}}}}
</style></head>
<body>
<div class="wrap">
  <div class="card">
    <div style="font-size:15px;font-weight:700;color:#1a3a6b;margin-bottom:12px;">✈ Jane Aerospace</div>
    <h1>{label}</h1>
    <p class="sub">Between <strong>Jane Aerospace Private Limited</strong> and
      <strong>{lead.business_name}</strong> — please review the full document below, then sign at the bottom.</p>
    <iframe src="{pdf_url}#toolbar=1"></iframe>
    <p class="dl"><a href="{pdf_url}" target="_blank" style="color:#1155cc;">Open / download the PDF ↗</a></p>
  </div>

  <div class="card">
    <h1 style="font-size:17px;">Electronic Signature</h1>
    <div class="two-col">
      <div>
        <label>Full Legal Name <span style="color:#e53e3e;">*</span></label>
        <input type="text" id="s-name" value="{sig_name}" placeholder="Your full name" oninput="updateSig()">
      </div>
      <div>
        <label>Designation</label>
        <input type="text" id="s-desig" placeholder="e.g. Director">
      </div>
    </div>
    <label style="margin-top:16px;">Signature Style</label>
    <div class="fonts" id="font-row">
      <div class="font-opt selected" data-font="standard" style="font-family:Georgia,serif;" onclick="pickFont(this)">Signature</div>
      <div class="font-opt" data-font="dancing" style="font-family:'Dancing Script',cursive;" onclick="pickFont(this)">Signature</div>
      <div class="font-opt" data-font="greatvibes" style="font-family:'Great Vibes',cursive;" onclick="pickFont(this)">Signature</div>
      <div class="font-opt" data-font="pacifico" style="font-family:'Pacifico',cursive;" onclick="pickFont(this)">Signature</div>
    </div>
    <label style="margin-top:14px;">Signature Preview</label>
    <div id="sig-preview">Type your name above</div>
    <div class="chk">
      <input type="checkbox" id="s-agree">
      <span>I confirm that I am an authorised signatory of <strong>{lead.business_name}</strong>,
      I have read and understood this {label} in full, and I agree to be legally bound by its terms.
      I understand that typing my name and clicking "I Agree &amp; Sign" constitutes my electronic signature.</span>
    </div>
    <div id="err"></div>
    <button id="sign-btn" onclick="signDoc()">✍ I Agree &amp; Sign</button>
  </div>
</div>

<script>
const FONT_MAP = {{
  standard: "Georgia,serif",
  dancing: "'Dancing Script',cursive",
  greatvibes: "'Great Vibes',cursive",
  pacifico: "'Pacifico',cursive",
}};
let SIG_FONT = 'standard';

function pickFont(el) {{
  document.querySelectorAll('.font-opt').forEach(o => o.classList.remove('selected'));
  el.classList.add('selected');
  SIG_FONT = el.dataset.font;
  updateSig();
}}
function updateSig() {{
  const p = document.getElementById('sig-preview');
  const name = document.getElementById('s-name').value.trim();
  p.style.fontFamily = FONT_MAP[SIG_FONT];
  p.textContent = name || 'Type your name above';
  p.style.color = name ? '#10245c' : '#9ca3af';
  p.style.fontSize = name ? '30px' : '14px';
}}
updateSig();

async function signDoc() {{
  const name = document.getElementById('s-name').value.trim();
  const agree = document.getElementById('s-agree').checked;
  const err = document.getElementById('err');
  err.style.display = 'none';
  if (!name) {{ err.textContent = 'Please enter your full legal name.'; err.style.display = 'block'; return; }}
  if (!agree) {{ err.textContent = 'Please tick the confirmation checkbox to proceed.'; err.style.display = 'block'; return; }}
  const btn = document.getElementById('sign-btn');
  btn.disabled = true; btn.textContent = 'Signing…';
  try {{
    const r = await fetch(window.location.pathname, {{
      method: 'POST', headers: {{'Content-Type': 'application/json'}},
      body: JSON.stringify({{signed_name: name, designation: document.getElementById('s-desig').value.trim(),
                            sig_font: SIG_FONT}})
    }});
    const d = await r.json().catch(() => ({{}}));
    if (r.ok) {{ window.location.href = d.redirect || window.location.pathname; }}
    else {{ err.textContent = d.detail || 'Signing failed. Please try again.'; err.style.display = 'block';
            btn.disabled = false; btn.textContent = '✍ I Agree & Sign'; }}
  }} catch(e) {{
    err.textContent = 'Network error — ' + e.message; err.style.display = 'block';
    btn.disabled = false; btn.textContent = '✍ I Agree & Sign';
  }}
}}
</script>
</body></html>"""
    return HTMLResponse(html)


class _SignBody(BaseModel):
    signed_name: str
    designation: str = ""
    sig_font: str = "standard"


@router.post("/sign/{onboarding_id}/{doc_type}/{token}")
async def document_sign(onboarding_id: str, doc_type: str, token: str,
                        body: _SignBody, request: Request,
                        db: AsyncSession = Depends(get_db)):
    _check_doc_type(doc_type)
    _check_token(onboarding_id, doc_type, token, "sign")
    rec, lead = await _load(db, onboarding_id)
    data = await _ensure_doc_data(db, rec, lead, doc_type)

    if data.get("signature"):
        raise HTTPException(409, "This document has already been signed")
    if not body.signed_name.strip():
        raise HTTPException(400, "Full legal name is required")

    now = _now_ist()
    sig = {
        "signed_name": body.signed_name.strip()[:200],
        "designation": body.designation.strip()[:200],
        "sig_font": body.sig_font if body.sig_font in ("standard", "dancing", "greatvibes", "pacifico") else "standard",
        "email": data.get("signatory_email") or lead.email,
        "company_name": lead.business_name,
        "signed_at": _fmt(now),
        "ip": (request.client.host if request.client else "") or "",
        "user_agent": (request.headers.get("user-agent") or "")[:300],
    }
    data["signature"] = sig
    _set_doc_data(rec, doc_type, data)

    if doc_type == "nda":
        rec.nda_status = DocumentStatus.APPROVED
        rec.nda_signed_received_at = now
        rec.nda_approved_at = now
        rec.nda_status_display = f"NDA Signed by {sig['signed_name']} ✓ ({_fmt(now)}) — Agreement Triggered"
        crm_stage = "NDA Approved"
    else:
        rec.agreement_status = DocumentStatus.PROCEED_NEXT
        rec.agreement_signed_received_at = now
        rec.agreement_approved_at = now
        rec.agreement_status_display = f"Agreement Signed by {sig['signed_name']} ✓ — Onboarding Complete ({_fmt(now)})"
        crm_stage = "Onboarding Complete"
    await db.commit()

    # Build the signed PDF once for the team notification attachment
    signed_pdf: bytes | None = None
    try:
        from app.services.pdf_documents import append_signature_page
        signed_pdf = append_signature_page(_render_pdf(rec, doc_type, data), doc_type, sig)
    except Exception as exc:
        logger.warning("signed_pdf_build_failed", onboarding_id=onboarding_id, error=str(exc))

    try:
        from app.services.onboarding_email import notify_team_document_signed
        notify_team_document_signed(
            company_name=lead.business_name,
            lead_email=lead.email,
            doc_type=doc_type,
            signed_name=sig["signed_name"],
            signed_pdf=signed_pdf,
        )
    except Exception as exc:
        logger.warning("notify_team_document_signed_failed", error=str(exc))

    # Auto-advance pipeline
    try:
        if doc_type == "nda":
            from app.services.onboarding_email import send_nda_approved_email
            send_nda_approved_email(lead.email, sig["signed_name"], lead.business_name)
            from app.workers.onboarding_tasks import generate_agreement_draft_task
            generate_agreement_draft_task.delay(onboarding_id)
        else:
            from app.services.onboarding_email import send_agreement_approved_email
            send_agreement_approved_email(lead.email, sig["signed_name"], lead.business_name)
    except Exception as exc:
        logger.warning("document_sign_next_step_failed", doc_type=doc_type, error=str(exc))

    try:
        from app.workers.onboarding_tasks import export_onboarding_to_sheets
        export_onboarding_to_sheets.delay(onboarding_id)
    except Exception:
        pass

    try:
        from app.services.zoho_crm import sync_onboarding_stage
        sync_onboarding_stage(
            email=lead.email,
            contact_name=lead.contact_name or lead.business_name,
            company_name=lead.business_name,
            stage=crm_stage,
            detail=f"{_DOC_LABELS[doc_type]} e-signed by {sig['signed_name']} at {sig['signed_at']}",
            company_type=rec.company_type or "",
            onboarding_id=onboarding_id,
        )
    except Exception:
        pass

    from app.core.pipeline_logger import log_pipeline
    log_pipeline("NDA_SIGNED" if doc_type == "nda" else "AGREEMENT_SIGNED",
                 company=lead.business_name, email=lead.email,
                 detail=f"E-signed by {sig['signed_name']}")

    return {"message": "Signed successfully", "redirect": f"/api/v1/documents/sign/{onboarding_id}/{doc_type}/{token}"}
