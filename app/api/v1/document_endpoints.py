"""NDA / Agreement document flow using the official JAPL PDF templates.

Team flow (HMAC 'edit' token, link arrives in the draft-ready email):
  GET  /documents/edit/{onboarding_id}/{doc_type}/{token}    — placeholder editor + preview
  GET  /documents/editor/{onboarding_id}/{doc_type}/{token}  — Live Document Editor (full content)
  POST /documents/save/{onboarding_id}/{doc_type}/{token}    — save fields / live HTML
  GET  /documents/versions/{onboarding_id}/{doc_type}/{token}        — version history
  POST /documents/versions/restore/{onboarding_id}/{doc_type}/{token}— restore a version
  POST /documents/revert-template/{onboarding_id}/{doc_type}/{token} — drop live edits
  POST /documents/send/{onboarding_id}/{doc_type}/{token}    — send T&C to lead for review
  POST /documents/internal-sign/{onboarding_id}/{doc_type}/{token}   — rep signs, doc goes to lead

Lead flow (HMAC 'sign' token):
  GET  /documents/sign/{onboarding_id}/{doc_type}/{token}    — review page or sign form (by stage)
  POST /documents/review/{onboarding_id}/{doc_type}/{token}  — accept terms / request changes
  POST /documents/sign/{onboarding_id}/{doc_type}/{token}    — record e-signature, auto-advance

Shared:
  GET  /documents/pdf/{onboarding_id}/{doc_type}/{token}     — working PDF (inline)
  GET  /documents/signed/{onboarding_id}/{doc_type}/{token}  — PDF + signature certificate

doc_type: "nda" | "agreement"
Document JSON (OnboardingRecord.nda_draft_content / agreement_draft_content):
  replacements, signatory_name, signatory_email          — placeholder mode
  mode ("template"|"live"), html                          — Live Editor mode
  stage: "" legacy | review | changes_requested | accepted | awaiting_lead_sign
  comments: [{by, email, text, at}], versions: [...]
  internal_signature, signature                           — e-sign audit blocks
"""
from __future__ import annotations

import datetime as dt
import json
import re
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


_DATA_IMG_RE = re.compile(r"^data:image/(?:png|jpeg);base64,[A-Za-z0-9+/=]{40,800000}$")


def _clean_sig_image(s: str) -> str:
    """Validated signature-image data URL, or '' when absent/invalid."""
    s = (s or "").strip()
    return s if _DATA_IMG_RE.match(s) else ""


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
    """Render the working document.

    Live-edited documents render from their edited HTML (Live Editor mode);
    everything else fills the original PDF template placeholder-by-placeholder,
    leaving format and content untouched."""
    if data.get("mode") == "live" and data.get("html"):
        from app.services.pdf_documents import live_html_to_pdf
        return live_html_to_pdf(data["html"])
    from app.services.pdf_documents import fill_document
    return fill_document(doc_type, _is_overseas(rec), data.get("replacements", []))


_MAX_VERSIONS = 15


def _snapshot_version(data: dict, by: str, note: str) -> None:
    """Store a full snapshot of the current document state in the version log."""
    versions = data.setdefault("versions", [])
    n = (versions[-1]["n"] + 1) if versions else 1
    versions.append({
        "n": n, "at": _fmt(_now_ist()), "by": by[:120], "note": (note or "")[:300],
        "mode": data.get("mode", "template"),
        "html": data.get("html", ""),
        "replacements": data.get("replacements", []),
        "editor_v": data.get("editor_v"),
    })
    if len(versions) > _MAX_VERSIONS:
        del versions[:-_MAX_VERSIONS]


def _apply_status(rec: OnboardingRecord, doc_type: str,
                  status: DocumentStatus | None = None, display: str | None = None) -> None:
    if doc_type == "nda":
        if status is not None:
            rec.nda_status = status
        if display is not None:
            rec.nda_status_display = display
    else:
        if status is not None:
            rec.agreement_status = status
        if display is not None:
            rec.agreement_status_display = display


def _crm_stage_safe(rec: OnboardingRecord, lead: LeadV2, onboarding_id: str,
                    stage: str, detail: str) -> None:
    try:
        from app.services.zoho_crm import sync_onboarding_stage
        sync_onboarding_stage(
            email=lead.email,
            contact_name=lead.contact_name or lead.business_name,
            company_name=lead.business_name,
            stage=stage, detail=detail,
            company_type=rec.company_type or "",
            onboarding_id=onboarding_id,
        )
    except Exception:
        pass


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


# Page-image preview for the Live Editor — refreshing images in place keeps
# the preview pane's scroll position (an <iframe> PDF resets to page 1 on
# every reload). One PDF render is cached per (doc, version) and the page
# PNGs are cut from it.
_PREVIEW_CACHE: dict[str, tuple[str, bytes]] = {}


async def _preview_pdf(db: AsyncSession, onboarding_id: str, doc_type: str, v: str) -> bytes:
    key = f"{onboarding_id}:{doc_type}"
    hit = _PREVIEW_CACHE.get(key)
    if hit and v and hit[0] == v:
        return hit[1]
    rec, lead = await _load(db, onboarding_id)
    data = await _ensure_doc_data(db, rec, lead, doc_type)
    pdf = _render_pdf(rec, doc_type, data)
    _PREVIEW_CACHE[key] = (v, pdf)
    while len(_PREVIEW_CACHE) > 10:
        _PREVIEW_CACHE.pop(next(iter(_PREVIEW_CACHE)))
    return pdf


@router.get("/preview-info/{onboarding_id}/{doc_type}/{token}", include_in_schema=False)
async def document_preview_info(onboarding_id: str, doc_type: str, token: str,
                                v: str = "", db: AsyncSession = Depends(get_db)):
    _check_doc_type(doc_type)
    _check_token(onboarding_id, doc_type, token, "edit")
    import fitz
    pdf = await _preview_pdf(db, onboarding_id, doc_type, v)
    doc = fitz.open(stream=pdf, filetype="pdf")
    pages = doc.page_count
    doc.close()
    return {"pages": pages}


@router.get("/preview-page/{onboarding_id}/{doc_type}/{token}", include_in_schema=False)
async def document_preview_page(onboarding_id: str, doc_type: str, token: str,
                                p: int = 0, v: str = "", db: AsyncSession = Depends(get_db)):
    _check_doc_type(doc_type)
    _check_token(onboarding_id, doc_type, token, "edit")
    import fitz
    pdf = await _preview_pdf(db, onboarding_id, doc_type, v)
    doc = fitz.open(stream=pdf, filetype="pdf")
    if not 0 <= p < doc.page_count:
        doc.close()
        raise HTTPException(404, "Page out of range")
    png = doc[p].get_pixmap(dpi=110).tobytes("png")
    doc.close()
    return Response(content=png, media_type="image/png",
                    headers={"Cache-Control": "no-store"})


@router.get("/signed/{onboarding_id}/{doc_type}/{token}", include_in_schema=False)
async def document_signed_pdf(onboarding_id: str, doc_type: str, token: str, db: AsyncSession = Depends(get_db)):
    _check_doc_type(doc_type)
    _check_token(onboarding_id, doc_type, token, "edit", "sign")
    rec, lead = await _load(db, onboarding_id)
    data = _get_doc_data(rec, doc_type)
    sig = data.get("signature")
    internal_sig = data.get("internal_signature")
    if not sig and not internal_sig:
        raise HTTPException(404, "Document has not been signed yet")
    from app.services.pdf_documents import append_signature_page
    pdf = append_signature_page(_render_pdf(rec, doc_type, data), doc_type,
                                sig=sig, internal_sig=internal_sig)
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
    internal_sig = data.get("internal_signature")
    stage = data.get("stage") or ""
    comments = data.get("comments") or []
    live_mode = data.get("mode") == "live"
    base = "/api/v1/documents"
    data_json = json.dumps({
        "replacements": data.get("replacements", []),
        "signatory_name": data.get("signatory_name", ""),
        "signatory_email": data.get("signatory_email", ""),
    }, ensure_ascii=False).replace("</", "<\\/")
    signed_badge = ('<span class="chip" style="background:#dcfce7;color:#15803d;">✓ SIGNED</span>' if signed else "")
    if not signed and internal_sig:
        signed_badge = '<span class="chip" style="background:#dbeafe;color:#1e40af;">✓ INTERNALLY SIGNED</span>'
    if live_mode:
        signed_badge += '<span class="chip" style="background:#fef3c7;color:#92400e;">LIVE-EDITED</span>'
    _stage_chips = {
        "review": ("#e0e7ff", "#3730a3", "OUT FOR T&amp;C REVIEW"),
        "changes_requested": ("#fee2e2", "#991b1b", f"CHANGES REQUESTED ({len(comments)})"),
        "accepted": ("#dcfce7", "#15803d", "TERMS ACCEPTED BY LEAD"),
        "awaiting_lead_sign": ("#dbeafe", "#1e40af", "AWAITING LEAD SIGNATURE"),
    }
    if stage in _stage_chips:
        bg, fg, txt = _stage_chips[stage]
        signed_badge += f'<span class="chip" style="background:{bg};color:{fg};">{txt}</span>'
    signed_btn = (f'<a class="btn b-blue" target="_blank" href="{base}/signed/{onboarding_id}/{doc_type}/{token}">⬇ Signed PDF</a>'
                  if (signed or internal_sig) else "")

    # Lead comments panel
    comments_html = ""
    if comments:
        items = "".join(
            f'<div style="background:#fff7ed;border-left:3px solid #f59e0b;border-radius:5px;'
            f'padding:8px 11px;margin-bottom:8px;">'
            f'<div style="font-size:10.5px;color:#92400e;font-weight:700;">'
            f'{(c.get("by") or "Lead")} — {c.get("at", "")}</div>'
            f'<div style="font-size:12.5px;color:#451a03;margin-top:3px;white-space:pre-wrap;">{c.get("text", "")}</div></div>'
            for c in reversed(comments[-10:]))
        comments_html = f'<div class="sec">Lead Comments ({len(comments)})</div>{items}'

    # Internal signature panel — appears once the lead accepts the T&C
    internal_html = ""
    if not signed:
        if internal_sig:
            internal_html = (f'<div class="sec">Internal Signature</div>'
                             f'<div class="note" style="background:#ecfdf5;border-left-color:#16a34a;color:#14532d;">'
                             f'Signed by <b>{internal_sig.get("signed_name", "")}</b> '
                             f'({internal_sig.get("designation") or "Authorised Signatory"}) '
                             f'on {internal_sig.get("signed_at", "")}. The document was emailed to the lead for counter-signature.</div>')
        elif stage == "accepted":
            internal_html = """
    <div class="sec">Internal Signature — Required</div>
    <div class="note" style="background:#ecfdf5;border-left-color:#16a34a;color:#14532d;">
      The lead accepted the Terms &amp; Conditions. Sign on behalf of Jane Aerospace —
      the internally-signed document is then emailed to the lead for their signature.</div>
    <div class="fld"><label>Authorised Representative Name</label><input type="text" id="int-name" placeholder="Full name"></div>
    <div class="fld"><label>Designation</label><input type="text" id="int-desig" placeholder="e.g. Director"></div>
    <div class="fld"><label>Signature Style</label>
      <select id="int-font" style="width:100%;padding:9px 11px;border:1.5px solid #cbd5e1;border-radius:7px;font-size:13px;">
        <option value="standard">Standard</option><option value="dancing">Cursive</option>
      </select></div>
    <div class="fld"><label>Or Upload Signature Image (PNG/JPG)</label>
      <input type="file" id="int-sigimg" accept=".png,.jpg,.jpeg" onchange="loadIntSig(this)" style="font-size:12px;">
      <div id="int-sig-preview" style="margin-top:6px;"></div></div>
    <button class="btn b-green" style="width:100%;justify-content:center;" onclick="internalSign()">✍ Sign &amp; Send to Lead</button>"""

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
  <a class="btn b-line" href="{base}/editor/{onboarding_id}/{doc_type}/{token}" title="Edit the full document content">📝 Live Editor</a>
  <button class="btn b-line" onclick="resetDoc()" title="Rebuild values from KYC data">↺ Reset</button>
  {signed_btn}
  <button class="btn b-green" onclick="sendToLead()">📨 Send to Lead for Review</button>
</div>

<div class="split">
  <div class="pv"><iframe id="pv" src="{base}/pdf/{onboarding_id}/{doc_type}/{token}#toolbar=1"></iframe></div>
  <div class="panel">
    {internal_html}
    {comments_html}
    <div class="note">The official PDF template is shown exactly as-is — its format and content never change.
      Only the placeholder values below are written into it. Edit a value and the preview updates.
      Need to change the actual content? Use the <b>Live Editor</b>.</div>
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
  if (!confirm('Send this document to the lead to review the Terms & Conditions?\\n' +
               'They can accept or request changes. Signatures happen after acceptance.')) return;
  const r = await fetch(`${{BASE}}/send/${{OID}}/${{DT}}/${{TOK}}`, {{
    method: 'POST', headers: {{'Content-Type': 'application/json'}},
    body: JSON.stringify(collect())
  }});
  const d = await r.json().catch(() => ({{}}));
  alert(r.ok ? (d.message || 'Sent for review.') : (d.detail || 'Send failed'));
  if (r.ok) location.reload();
}}

let INT_SIG_IMAGE = '';
function loadIntSig(input) {{
  const f = input.files && input.files[0];
  const pv = document.getElementById('int-sig-preview');
  if (!f) {{ INT_SIG_IMAGE = ''; pv.innerHTML = ''; return; }}
  if (f.size > 5 * 1024 * 1024) {{ alert('Signature image too large — max 5 MB.'); input.value = ''; return; }}
  const img = new Image();
  img.onload = function() {{
    const scale = Math.min(1, 480 / img.width);
    const c = document.createElement('canvas');
    c.width = Math.round(img.width * scale);
    c.height = Math.round(img.height * scale);
    c.getContext('2d').drawImage(img, 0, 0, c.width, c.height);
    INT_SIG_IMAGE = c.toDataURL('image/png');
    pv.innerHTML = '<img src="' + INT_SIG_IMAGE + '" style="max-height:46px;border:1px dashed #c7d4ee;border-radius:5px;padding:3px;">';
  }};
  img.onerror = function() {{ alert('Could not read that image file.'); input.value = ''; }};
  img.src = URL.createObjectURL(f);
}}

async function internalSign() {{
  const name = (document.getElementById('int-name') || {{}}).value || '';
  if (!name.trim()) {{ alert('Enter the authorised representative name.'); return; }}
  if (!confirm(`Sign this document as ${{name}} on behalf of Jane Aerospace and email it to the lead for counter-signature?`)) return;
  const r = await fetch(`${{BASE}}/internal-sign/${{OID}}/${{DT}}/${{TOK}}`, {{
    method: 'POST', headers: {{'Content-Type': 'application/json'}},
    body: JSON.stringify({{
      signed_name: name.trim(),
      designation: (document.getElementById('int-desig') || {{}}).value || '',
      sig_font: (document.getElementById('int-font') || {{}}).value || 'standard',
      sig_image: INT_SIG_IMAGE,
    }})
  }});
  const d = await r.json().catch(() => ({{}}));
  alert(r.ok ? (d.message || 'Signed and sent.') : (d.detail || 'Signing failed'));
  if (r.ok) location.reload();
}}

render();
document.getElementById('sig-name').value = initial.signatory_name || '';
document.getElementById('sig-email').value = initial.signatory_email || '';
</script>
</body></html>"""
    return HTMLResponse(html)


# ---------------------------------------------------------------------------
# Team: Live Document Editor (full content editing)
# ---------------------------------------------------------------------------

@router.get("/editor/{onboarding_id}/{doc_type}/{token}", response_class=HTMLResponse, include_in_schema=False)
async def document_live_editor(onboarding_id: str, doc_type: str, token: str,
                               db: AsyncSession = Depends(get_db)):
    _check_doc_type(doc_type)
    _check_token(onboarding_id, doc_type, token, "edit")
    rec, lead = await _load(db, onboarding_id)
    data = await _ensure_doc_data(db, rec, lead, doc_type)

    label = _DOC_LABELS[doc_type]
    signed = bool(data.get("signature"))
    stage = data.get("stage") or ""
    comments = data.get("comments") or []
    base = "/api/v1/documents"

    if data.get("mode") == "live" and data.get("html"):
        doc_html = data["html"]
        live_now = True
    else:
        from app.services.pdf_documents import extract_editable_html
        doc_html = extract_editable_html(doc_type, _is_overseas(rec), data.get("replacements", []))
        live_now = False

    html_json = json.dumps(doc_html, ensure_ascii=False).replace("</", "<\\/")
    comments_json = json.dumps(comments, ensure_ascii=False).replace("</", "<\\/")
    locked_banner = ""
    if signed:
        locked_banner = ('<div style="background:#fee2e2;color:#991b1b;padding:9px 16px;font-size:13px;'
                         'font-weight:700;text-align:center;">This document is signed — editing is locked. '
                         'Changes will not be saved.</div>')
    elif live_now and data.get("editor_v") != 2:
        # html stored by the old extractor — its formatting is known to be off
        locked_banner = ('<div style="background:#fef3c7;color:#92400e;padding:9px 16px;font-size:13px;'
                         'font-weight:700;text-align:center;">⚠ This document was saved with the old editor and '
                         'its formatting may look wrong. Click <b>↺ Original</b> above to rebuild it from the '
                         'official template (your placeholder values are kept; the current state is snapshotted '
                         'into version history).</div>')
    elif stage == "accepted":
        locked_banner = (f'<div style="background:#dcfce7;color:#14532d;padding:9px 16px;font-size:13px;'
                         f'font-weight:700;text-align:center;">Lead accepted the Terms &amp; Conditions — '
                         f'<a href="{base}/edit/{onboarding_id}/{doc_type}/{token}" style="color:#15803d;">'
                         f'open the Send &amp; Sign page</a> to countersign and send for signature.</div>')

    html = f"""<!DOCTYPE html>
<html lang="en">
<head><meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Live Editor — {label} — {lead.business_name}</title>
<style>
  *{{box-sizing:border-box;margin:0;padding:0;}}
  body{{font-family:'Segoe UI',Arial,sans-serif;background:#e8edf5;overflow:hidden;}}
  .topbar{{height:52px;background:#0c2344;color:#fff;display:flex;align-items:center;gap:10px;padding:0 16px;}}
  .topbar .ttl{{font-weight:800;font-size:13px;white-space:nowrap;}}
  .topbar .sub{{font-size:11px;color:#b9c8e4;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;min-width:0;flex-shrink:1;}}
  .grow{{flex:1;}}
  #saved{{font-size:11px;color:#86efac;min-width:64px;text-align:right;}}
  .btn{{display:inline-flex;align-items:center;gap:5px;padding:7px 13px;border-radius:7px;border:none;
       color:#fff;font-size:12px;font-weight:700;cursor:pointer;text-decoration:none;font-family:inherit;white-space:nowrap;}}
  .b-blue{{background:#1a56db;}} .b-green{{background:#16a34a;}}
  .b-line{{background:transparent;color:#cdd9f0;border:1.4px solid #3b5379;}}
  .chip{{display:inline-block;padding:2px 10px;border-radius:99px;font-size:10.5px;font-weight:700;
        background:#fef3c7;color:#92400e;}}
  .toolbar{{background:#fff;border-bottom:1px solid #d9e1ef;padding:6px 14px;display:flex;gap:4px;
           align-items:center;flex-wrap:wrap;}}
  .tb{{border:1px solid transparent;background:none;border-radius:6px;min-width:30px;height:30px;
      font-size:13.5px;cursor:pointer;color:#1f2c44;display:inline-flex;align-items:center;
      justify-content:center;padding:0 7px;font-family:inherit;}}
  .tb:hover{{background:#eef3fc;border-color:#c9d8f3;}}
  .tsep{{width:1px;height:22px;background:#dde5f1;margin:0 5px;}}
  select.tb{{height:30px;font-size:12px;}}
  .work{{position:fixed;top:0;left:0;right:0;bottom:0;display:flex;flex-direction:column;}}
  .main{{flex:1;display:flex;min-height:0;}}
  .edwrap{{flex:1.25;overflow-y:auto;padding:26px 18px 80px;display:flex;justify-content:center;}}
  .sheet{{background:#fff;width:794px;max-width:100%;min-height:1000px;box-shadow:0 3px 18px rgba(12,35,68,.18);
         padding:62px 58px;outline:none;font-family:Calibri,Carlito,'Segoe UI',sans-serif;font-size:10.5pt;
         line-height:1.24;color:#111;}}
  .sheet p{{margin:3pt 0;}}
  .sheet table{{border-collapse:collapse;width:100%;margin:8pt 0;table-layout:fixed;}}
  .sheet td{{border:1px solid #999;padding:4pt 6pt;vertical-align:top;word-wrap:break-word;}}
  .sheet:focus{{outline:none;}}
  .pv{{flex:1;background:#525659;display:flex;flex-direction:column;min-width:0;}}
  .pv .pvhead{{background:#3c4043;color:#e8eaed;font-size:11px;font-weight:700;padding:6px 12px;
              display:flex;justify-content:space-between;align-items:center;}}
  #pv-pages{{flex:1;overflow-y:auto;padding:14px 10px;display:flex;flex-direction:column;
            gap:12px;align-items:center;}}
  #pv-pages img.pg{{width:94%;max-width:740px;aspect-ratio:595/842;background:#fff;
                   box-shadow:0 2px 12px rgba(0,0,0,.4);display:block;}}
  .modal-bg{{display:none;position:fixed;inset:0;background:rgba(8,20,40,.55);z-index:50;}}
  .modal{{position:absolute;top:70px;right:40px;width:420px;max-width:92vw;max-height:75vh;overflow-y:auto;
         background:#fff;border-radius:12px;box-shadow:0 12px 40px rgba(0,0,0,.3);padding:20px 22px;}}
  .modal h3{{font-size:14px;color:#0c2344;margin-bottom:12px;}}
  .vrow{{display:flex;justify-content:space-between;align-items:center;gap:8px;border-bottom:1px solid #eef1f7;
        padding:9px 2px;font-size:12.5px;}}
  .vrow .meta{{color:#64748b;font-size:11px;}}
  .vrow button{{background:#1a56db;color:#fff;border:none;border-radius:6px;padding:5px 12px;
               font-size:11px;font-weight:700;cursor:pointer;}}
  .crow{{background:#fff7ed;border-left:3px solid #f59e0b;border-radius:6px;padding:8px 11px;margin-bottom:8px;}}
  .crow .by{{font-size:10.5px;color:#92400e;font-weight:700;}}
  .crow .tx{{font-size:12.5px;color:#451a03;margin-top:3px;white-space:pre-wrap;}}
  /* Comments & activity sidebar */
  #sidebar{{position:fixed;top:52px;right:0;bottom:0;width:332px;background:#fff;
    border-left:1px solid #d9e1ef;box-shadow:-4px 0 18px rgba(12,35,68,.14);z-index:40;
    display:none;flex-direction:column;}}
  #sidebar.open{{display:flex;}}
  .sb-head{{padding:11px 16px;border-bottom:1px solid #e5eaf3;display:flex;
    justify-content:space-between;align-items:center;font-weight:800;font-size:13px;color:#0c2344;}}
  .sb-head button{{background:none;border:none;font-size:17px;color:#94a3b8;cursor:pointer;}}
  .sb-body{{flex:1;overflow-y:auto;padding:10px 14px 30px;}}
  .sb-sec{{font-size:10.5px;font-weight:800;color:#64748b;text-transform:uppercase;
    letter-spacing:.06em;margin:14px 0 8px;}}
  .sb-card{{background:#fff7ed;border-left:3px solid #f59e0b;border-radius:7px;
    padding:9px 11px;margin-bottom:9px;cursor:pointer;}}
  .sb-card.done{{background:#f0fdf4;border-left-color:#16a34a;opacity:.88;}}
  .sb-card .by{{font-size:10.5px;font-weight:700;color:#92400e;}}
  .sb-card.done .by{{color:#15803d;}}
  .sb-card .tx{{font-size:12.5px;color:#451a03;margin-top:3px;white-space:pre-wrap;
    max-height:54px;overflow:hidden;}}
  .sb-card.expanded .tx{{max-height:none;}}
  .sb-ai{{margin-top:6px;display:flex;flex-direction:column;gap:3px;}}
  .sb-ai span{{font-size:11px;background:#eef3fc;color:#1a3a6b;border-radius:5px;
    padding:3px 8px;display:block;}}
  .sb-act{{font-size:11.5px;color:#475569;padding:5px 2px;border-bottom:1px solid #f1f5f9;
    display:flex;gap:7px;align-items:baseline;}}
  .sb-act .when{{color:#94a3b8;font-size:10px;margin-left:auto;white-space:nowrap;}}
  .sb-empty{{font-size:12px;color:#94a3b8;padding:4px 2px;}}
  @media(max-width:980px){{.main{{flex-direction:column;}}.pv{{min-height:38vh;}}
    #sidebar{{width:100%;}}}}
</style></head>
<body>
<div class="work">
{locked_banner}
<div class="topbar">
  <span class="ttl">✈ LIVE EDITOR</span>
  <span class="sub">{label} — {lead.business_name}</span>
  <span class="chip" id="mode-chip">{'LIVE-EDITED' if live_now else 'FROM TEMPLATE'}</span>
  <span class="grow"></span>
  <span id="saved">Saved ✓</span>
  <button class="btn b-line" onclick="toggleSidebar()">🔔 Comments (<span id="cmt-n">{len([c for c in comments if not c.get("done")])}</span>)</button>
  <button class="btn b-line" onclick="openVersions()">🕘 Versions</button>
  <a class="btn b-line" target="_blank" id="dl-btn" href="{base}/pdf/{onboarding_id}/{doc_type}/{token}">⬇ PDF</a>
  <a class="btn b-line" href="{base}/edit/{onboarding_id}/{doc_type}/{token}">⚙ Placeholders &amp; Send</a>
  <button class="btn b-line" onclick="revertTemplate()" title="Drop all live edits">↺ Original</button>
  <button class="btn b-green" onclick="sendForReview()">📨 Send for Review</button>
</div>
<div class="toolbar">
  <button class="tb" onmousedown="return false" onclick="cmd('undo')" title="Undo">↶</button>
  <button class="tb" onmousedown="return false" onclick="cmd('redo')" title="Redo">↷</button>
  <span class="tsep"></span>
  <select class="tb" onmousedown="saveRange()" onchange="cmd('fontSize',this.value);this.selectedIndex=2;" title="Font size">
    <option value="1">Small</option><option value="2">Normal-</option>
    <option value="3" selected>Normal</option><option value="4">Large</option>
    <option value="5">XL</option><option value="6">Heading</option>
  </select>
  <span class="tsep"></span>
  <button class="tb" style="font-weight:800;" onmousedown="return false" onclick="cmd('bold')" title="Bold">B</button>
  <button class="tb" style="font-style:italic;" onmousedown="return false" onclick="cmd('italic')" title="Italic">I</button>
  <button class="tb" style="text-decoration:underline;" onmousedown="return false" onclick="cmd('underline')" title="Underline">U</button>
  <button class="tb" style="text-decoration:line-through;" onmousedown="return false" onclick="cmd('strikeThrough')" title="Strike">S</button>
  <span class="tsep"></span>
  <button class="tb" onmousedown="return false" onclick="cmd('justifyLeft')" title="Align left">⫷</button>
  <button class="tb" onmousedown="return false" onclick="cmd('justifyCenter')" title="Center">≡</button>
  <button class="tb" onmousedown="return false" onclick="cmd('justifyFull')" title="Justify">☰</button>
  <span class="tsep"></span>
  <button class="tb" onmousedown="return false" onclick="cmd('insertUnorderedList')" title="Bullet list">• ─</button>
  <button class="tb" onmousedown="return false" onclick="cmd('insertOrderedList')" title="Numbered list">1. ─</button>
  <span class="tsep"></span>
  <button class="tb" style="background:#fde047;" onmousedown="return false" onclick="cmd('hiliteColor','#fde047')" title="Highlight">🖊</button>
  <button class="tb" onmousedown="return false" onclick="cmd('hiliteColor','transparent')" title="Remove highlight">⊘</button>
  <button class="tb" style="color:#1d4ed8;" onmousedown="return false" onclick="insertNote()" title="Insert note">📝 Note</button>
  <button class="tb" onmousedown="return false" onclick="insertClause()" title="Add a new clause/paragraph">¶ Clause</button>
  <button class="tb" onmousedown="return false" onclick="document.getElementById('sig-file').click()"
          title="Insert your signature image at the cursor — drag it to reposition">🖊 Sign</button>
  <input type="file" id="sig-file" accept=".png,.jpg,.jpeg" style="display:none" onchange="insertSigImage(this)">
  <span class="tsep"></span>
  <button class="tb" onmousedown="return false" onclick="findReplace()" title="Find and replace">🔍 Replace</button>
  <button class="tb" onmousedown="return false" onclick="cmd('removeFormat')" title="Clear formatting">Tx</button>
  <span class="grow"></span>
  <button class="tb" style="color:#1a56db;font-weight:700;" onmousedown="return false" onclick="saveVersion()" title="Snapshot this state">💾 Save Version</button>
</div>
<div class="main">
  <div class="edwrap"><div class="sheet" id="sheet" contenteditable="{'false' if signed else 'true'}" spellcheck="false" onblur="saveRange()"></div></div>
  <div class="pv">
    <div class="pvhead"><span>LIVE PDF PREVIEW</span><button class="tb" style="color:#e8eaed;height:22px;" onclick="refreshPv()" title="Refresh">⟳</button></div>
    <div id="pv-pages"></div>
  </div>
</div>
</div>

<div class="modal-bg" id="m-versions" onclick="if(event.target===this)this.style.display='none'">
  <div class="modal"><h3>🕘 Version History</h3><div id="v-list">Loading…</div></div>
</div>
<div id="sidebar">
  <div class="sb-head"><span>🔔 Comments &amp; Activity</span>
    <button onclick="toggleSidebar()" title="Close">✕</button></div>
  <div class="sb-body" id="sb-body"><p class="sb-empty">Loading…</p></div>
</div>

<script>
const BASE = '{base}', OID = '{onboarding_id}', DT = '{doc_type}', TOK = '{token}';
const SIGNED = {str(signed).lower()};
const INIT_HTML = {html_json};
const COMMENTS = {comments_json};
let timer = null;

const sheet = document.getElementById('sheet');
try {{
  sheet.innerHTML = INIT_HTML;
}} catch(e) {{
  sheet.textContent = 'Error loading document: ' + e.message + ' — try clicking ↺ Original above.';
}}
if (!sheet.innerHTML.trim()) {{
  sheet.innerHTML = '<p style="color:#dc2626;">Document content could not be loaded — click ↺ Original to reload from the template.</p>';
}}
try {{ document.execCommand('styleWithCSS', false, true); }} catch(e) {{}}
// Enter creates a styled <p> (not a bare <div>) so new paragraphs keep the document look
try {{ document.execCommand('defaultParagraphSeparator', false, 'p'); }} catch(e) {{}}

// Save/restore selection so toolbar buttons do not lose the cursor position.
let _savedRange = null;
function saveRange() {{
  const s = window.getSelection();
  if (s && s.rangeCount > 0 && sheet.contains(s.anchorNode))
    _savedRange = s.getRangeAt(0).cloneRange();
}}
function restoreRange() {{
  if (!_savedRange) return;
  sheet.focus();
  const s = window.getSelection();
  s.removeAllRanges();
  s.addRange(_savedRange);
}}

function cmd(c, v) {{
  restoreRange();
  document.execCommand(c, false, v || null);
  dirty();
}}

function dirty() {{
  if (SIGNED) return;
  const el = document.getElementById('saved');
  el.textContent = '● editing…'; el.style.color = '#fbbf24';
  clearTimeout(timer); timer = setTimeout(saveDoc, 2200);
}}
sheet.addEventListener('input', dirty);

async function saveDoc(extra) {{
  if (SIGNED) return false;
  const el = document.getElementById('saved');
  el.textContent = 'Saving…'; el.style.color = '#fbbf24';
  const payload = Object.assign({{html: sheet.innerHTML, mode: 'live'}}, extra || {{}});
  const r = await fetch(`${{BASE}}/save/${{OID}}/${{DT}}/${{TOK}}`, {{
    method: 'POST', headers: {{'Content-Type': 'application/json'}},
    body: JSON.stringify(payload)
  }}).catch(() => null);
  const ok = r && r.ok;
  el.textContent = ok ? 'Saved ✓' : 'Save failed';
  el.style.color = ok ? '#86efac' : '#fca5a5';
  if (ok) {{
    document.getElementById('mode-chip').textContent = 'LIVE-EDITED';
    refreshPv();
    if (document.getElementById('sidebar').classList.contains('open')) loadSidebar();
  }}
  return ok;
}}

// Image-based preview: refreshing <img> tags in place keeps the scroll
// position, so saving never throws the preview back to page 1.
let _pvBusy = false, _pvAgain = false;
async function refreshPv() {{
  if (_pvBusy) {{ _pvAgain = true; return; }}
  _pvBusy = true;
  try {{
    const v = Date.now().toString(36);
    const r = await fetch(`${{BASE}}/preview-info/${{OID}}/${{DT}}/${{TOK}}?v=${{v}}`);
    const d = await r.json();
    const wrap = document.getElementById('pv-pages');
    const n = Math.max(d.pages || 1, 1);
    while (wrap.children.length > n) wrap.removeChild(wrap.lastChild);
    while (wrap.children.length < n) {{
      const img = document.createElement('img');
      img.className = 'pg';
      img.loading = 'lazy';
      wrap.appendChild(img);
    }}
    for (let i = 0; i < n; i++) {{
      wrap.children[i].src = `${{BASE}}/preview-page/${{OID}}/${{DT}}/${{TOK}}?p=${{i}}&v=${{v}}`;
    }}
  }} catch(e) {{}}
  _pvBusy = false;
  if (_pvAgain) {{ _pvAgain = false; refreshPv(); }}
}}
refreshPv();

async function saveVersion() {{
  const note = prompt('Note for this version (what changed?):', '');
  if (note === null) return;
  await saveDoc({{snapshot: true, note: note || 'Manual save'}});
  alert('Version saved.');
}}

function insertNote() {{
  const t = prompt('Note text (visible in the document, styled blue italic):');
  if (!t) return;
  cmd('insertHTML', `<span style="color:#1d4ed8;font-style:italic;">[Note: ${{t.replace(/</g,'&lt;')}}]</span>&nbsp;`);
}}

function insertClause() {{
  cmd('insertHTML', `<p style="font-size:10.5pt;text-align:justify;"><b>New Clause.</b>&nbsp;Type the clause text here…</p>`);
}}

function insertSigImage(input) {{
  const f = input.files && input.files[0];
  if (!f) return;
  if (f.size > 5 * 1024 * 1024) {{ alert('Signature image too large — max 5 MB.'); input.value = ''; return; }}
  const img = new Image();
  img.onload = function() {{
    const scale = Math.min(1, 480 / img.width);
    const c = document.createElement('canvas');
    c.width = Math.round(img.width * scale);
    c.height = Math.round(img.height * scale);
    c.getContext('2d').drawImage(img, 0, 0, c.width, c.height);
    cmd('insertHTML',
        '<img class="sig-img" draggable="true" src="' + c.toDataURL('image/png') +
        '" style="width:150pt;cursor:move;vertical-align:middle;">&nbsp;');
    input.value = '';
  }};
  img.onerror = function() {{ alert('Could not read that image file.'); input.value = ''; }};
  img.src = URL.createObjectURL(f);
}}

// ── Signature image resize / move toolbar ────────────────────────────────────
(function() {{
  // inject CSS for selected sig image outline
  const st = document.createElement('style');
  st.textContent = '.sig-img{{cursor:move;vertical-align:middle;transition:outline .1s}}' +
    '.sig-img:hover{{outline:2px dashed #4f8ef7;outline-offset:2px}}' +
    '.sig-img.sig-sel{{outline:2px solid #1a56db;outline-offset:2px}}';
  document.head.appendChild(st);

  // build the toolbar
  const tb = document.createElement('div');
  tb.id = 'sig-tb';
  tb.style.cssText = 'position:fixed;display:none;z-index:9999;background:#1e3a5f;' +
    'border-radius:8px;padding:5px 10px;gap:6px;align-items:center;' +
    'box-shadow:0 4px 16px rgba(0,0,0,.35);font-family:sans-serif;';
  tb.innerHTML =
    '<span style="color:#93c5fd;font-size:11px;font-weight:600;">✏ Signature</span>' +
    '<button id="sig-tb-sm" title="Smaller" style="background:#2d5a8e;color:#fff;border:none;' +
      'border-radius:5px;padding:3px 9px;cursor:pointer;font-size:14px;font-weight:bold;">−</button>' +
    '<span id="sig-tb-sz" style="color:#fff;font-size:11px;min-width:44px;text-align:center;"></span>' +
    '<button id="sig-tb-lg" title="Larger" style="background:#2d5a8e;color:#fff;border:none;' +
      'border-radius:5px;padding:3px 9px;cursor:pointer;font-size:14px;font-weight:bold;">+</button>' +
    '<span style="color:#4b7ab5;margin:0 2px;">|</span>' +
    '<button id="sig-tb-del" title="Remove signature" style="background:#7f1d1d;color:#fca5a5;' +
      'border:none;border-radius:5px;padding:3px 9px;cursor:pointer;font-size:12px;">✕ Remove</button>';
  document.body.appendChild(tb);

  let sel = null;

  function _ptWidth(img) {{
    // read width style in pt (may be px or pt)
    const s = img.style.width || '';
    const v = parseFloat(s) || 150;
    return s.endsWith('px') ? Math.round(v * 0.75) : v;  // px→pt rough
  }}

  function _showTb(img) {{
    if (sel && sel !== img) sel.classList.remove('sig-sel');
    sel = img;
    sel.classList.add('sig-sel');
    _reposTb();
    tb.style.display = 'flex';
  }}

  function _reposTb() {{
    if (!sel) return;
    const r = sel.getBoundingClientRect();
    const tbW = 300;
    let left = r.left;
    if (left + tbW > window.innerWidth - 10) left = window.innerWidth - tbW - 10;
    tb.style.left = Math.max(4, left) + 'px';
    tb.style.top  = (r.top > 46 ? r.top - 42 : r.bottom + 6) + 'px';
    document.getElementById('sig-tb-sz').textContent = _ptWidth(sel) + ' pt';
  }}

  function _resize(delta) {{
    if (!sel) return;
    const nw = Math.max(30, Math.min(500, _ptWidth(sel) + delta));
    sel.style.width = nw + 'pt';
    sel.style.height = '';   // let aspect ratio breathe
    document.getElementById('sig-tb-sz').textContent = nw + ' pt';
    _reposTb();
  }}

  document.getElementById('sig-tb-sm').onclick  = function(e) {{ e.stopPropagation(); _resize(-15); }};
  document.getElementById('sig-tb-lg').onclick  = function(e) {{ e.stopPropagation(); _resize(+15); }};
  document.getElementById('sig-tb-del').onclick = function(e) {{
    e.stopPropagation();
    if (sel) {{ sel.remove(); sel = null; tb.style.display = 'none'; }}
  }};

  // click on a sig-img → select; click elsewhere → deselect
  document.addEventListener('click', function(e) {{
    if (e.target.classList && e.target.classList.contains('sig-img')) {{
      _showTb(e.target);
    }} else if (!tb.contains(e.target)) {{
      if (sel) sel.classList.remove('sig-sel');
      sel = null;
      tb.style.display = 'none';
    }}
  }}, true);

  // reposition toolbar on scroll / resize
  window.addEventListener('scroll', _reposTb, true);
  window.addEventListener('resize', _reposTb);
}})();

function findReplace() {{
  const f = prompt('Find text:'); if (!f) return;
  const rep = prompt(`Replace "${{f}}" with:`); if (rep === null) return;
  let count = 0;
  const walker = document.createTreeWalker(sheet, NodeFilter.SHOW_TEXT);
  const nodes = [];
  while (walker.nextNode()) nodes.push(walker.currentNode);
  nodes.forEach(n => {{
    if (n.nodeValue.includes(f)) {{
      count += n.nodeValue.split(f).length - 1;
      n.nodeValue = n.nodeValue.split(f).join(rep);
    }}
  }});
  if (count) dirty();
  alert(count ? `Replaced ${{count}} occurrence(s).` : 'Not found.');
}}

async function revertTemplate() {{
  if (!confirm('Drop ALL live edits and go back to the original PDF template with the current placeholder values?\\n(The current state is kept in version history.)')) return;
  const r = await fetch(`${{BASE}}/revert-template/${{OID}}/${{DT}}/${{TOK}}`, {{method: 'POST'}});
  if (r.ok) location.reload(); else alert('Revert failed');
}}

async function openVersions() {{
  document.getElementById('m-versions').style.display = 'block';
  const r = await fetch(`${{BASE}}/versions/${{OID}}/${{DT}}/${{TOK}}`).catch(() => null);
  const d = r && r.ok ? await r.json() : {{versions: []}};
  const list = document.getElementById('v-list');
  if (!d.versions.length) {{ list.innerHTML = '<p style="font-size:12.5px;color:#64748b;">No versions yet — use 💾 Save Version or send the document.</p>'; return; }}
  list.innerHTML = d.versions.slice().reverse().map(v =>
    `<div class="vrow"><div><b>v${{v.n}}</b> — ${{v.note || ''}}<div class="meta">${{v.at}} · ${{v.by}} · ${{v.mode}}</div></div>
     <button onclick="restoreV(${{v.n}})">Restore</button></div>`).join('');
}}

async function restoreV(n) {{
  if (!confirm(`Restore version ${{n}}? The current state is snapshotted first.`)) return;
  const r = await fetch(`${{BASE}}/versions/restore/${{OID}}/${{DT}}/${{TOK}}`, {{
    method: 'POST', headers: {{'Content-Type': 'application/json'}}, body: JSON.stringify({{n: n}})
  }});
  if (r.ok) location.reload(); else alert('Restore failed');
}}

function toggleSidebar() {{
  const sb = document.getElementById('sidebar');
  if (sb.classList.toggle('open')) loadSidebar();
}}

function _sbCard(c, isDone) {{
  const card = document.createElement('div');
  card.className = 'sb-card' + (isDone ? ' done' : '');
  card.onclick = () => card.classList.toggle('expanded');
  const by = document.createElement('div');
  by.className = 'by';
  by.textContent = (c.by || 'Lead') + ' — ' + (c.at || '');
  const tx = document.createElement('div');
  tx.className = 'tx';
  tx.textContent = c.text || '';
  card.appendChild(by); card.appendChild(tx);
  if (c.ai_actions && c.ai_actions.length) {{
    const ai = document.createElement('div');
    ai.className = 'sb-ai';
    c.ai_actions.forEach(a => {{
      const s = document.createElement('span');
      s.textContent = '🤖 ' + a;
      ai.appendChild(s);
    }});
    card.appendChild(ai);
  }}
  return card;
}}

function _sbSection(body, title) {{
  const h = document.createElement('div');
  h.className = 'sb-sec';
  h.textContent = title;
  body.appendChild(h);
}}

async function loadSidebar() {{
  const body = document.getElementById('sb-body');
  let d = {{comments: COMMENTS, versions: []}};
  try {{
    const r = await fetch(`${{BASE}}/versions/${{OID}}/${{DT}}/${{TOK}}`);
    if (r.ok) d = await r.json();
  }} catch(e) {{}}
  const open = (d.comments || []).filter(c => !c.done);
  const done = (d.comments || []).filter(c => c.done);
  document.getElementById('cmt-n').textContent = open.length;
  body.innerHTML = '';

  _sbSection(body, '📌 To-do — changes requested (' + open.length + ')');
  if (!open.length) {{
    const p = document.createElement('p'); p.className = 'sb-empty';
    p.textContent = 'Nothing pending — all comments handled.'; body.appendChild(p);
  }}
  open.slice().reverse().forEach(c => body.appendChild(_sbCard(c, false)));

  _sbSection(body, '✅ Done — handled & re-sent (' + done.length + ')');
  if (!done.length) {{
    const p = document.createElement('p'); p.className = 'sb-empty';
    p.textContent = 'Comments move here automatically when you send the revised document.';
    body.appendChild(p);
  }}
  done.slice().reverse().forEach(c => body.appendChild(_sbCard(c, true)));

  _sbSection(body, '📜 Activity — saves & sends');
  const vs = (d.versions || []).slice().reverse();
  if (!vs.length) {{
    const p = document.createElement('p'); p.className = 'sb-empty';
    p.textContent = 'No activity yet.'; body.appendChild(p);
  }}
  vs.forEach(v => {{
    const note = v.note || '';
    const icon = note.indexOf('Sent to lead') >= 0 ? '📨'
               : (note.toLowerCase().indexOf('restor') >= 0 || note.toLowerCase().indexOf('revert') >= 0) ? '↺'
               : '💾';
    const row = document.createElement('div');
    row.className = 'sb-act';
    const ic = document.createElement('span'); ic.textContent = icon;
    const tx = document.createElement('span'); tx.textContent = 'v' + v.n + ' · ' + (note || 'Save');
    const when = document.createElement('span'); when.className = 'when';
    when.textContent = v.at || '';
    row.appendChild(ic); row.appendChild(tx); row.appendChild(when);
    body.appendChild(row);
  }});
}}

async function sendForReview() {{
  clearTimeout(timer);
  if (!SIGNED && !(await saveDoc())) {{ alert('Could not save the document — not sent.'); return; }}
  if (!confirm('Send this edited document to the lead to review the Terms & Conditions?')) return;
  const r = await fetch(`${{BASE}}/send/${{OID}}/${{DT}}/${{TOK}}`, {{
    method: 'POST', headers: {{'Content-Type': 'application/json'}},
    body: JSON.stringify({{html: sheet.innerHTML, mode: 'live'}})
  }});
  const d = await r.json().catch(() => ({{}}));
  alert(r.ok ? (d.message || 'Sent for review.') : (d.detail || 'Send failed'));
}}
</script>
</body></html>"""
    return HTMLResponse(html)


class _DocSaveBody(BaseModel):
    replacements: list[dict] = []
    signatory_name: str = ""
    signatory_email: str = ""
    html: str = ""
    mode: str = ""        # "" = unchanged | "live" (store html) | "template" (drop html)
    snapshot: bool = False
    note: str = ""


@router.post("/save/{onboarding_id}/{doc_type}/{token}")
async def document_save(onboarding_id: str, doc_type: str, token: str,
                        body: _DocSaveBody, db: AsyncSession = Depends(get_db)):
    _check_doc_type(doc_type)
    _check_token(onboarding_id, doc_type, token, "edit")
    rec, lead = await _load(db, onboarding_id)
    data = _get_doc_data(rec, doc_type)
    if data.get("signature"):
        raise HTTPException(409, "Document already signed — editing is locked")
    if body.replacements:
        data["replacements"] = [
            {"find": str(r.get("find", ""))[:200], "replace": str(r.get("replace", ""))[:500]}
            for r in body.replacements if str(r.get("find", "")).strip()
        ]
    if body.signatory_name:
        data["signatory_name"] = body.signatory_name[:200]
    if body.signatory_email:
        data["signatory_email"] = body.signatory_email[:320]
    if body.mode == "live" and body.html.strip():
        from app.services.pdf_documents import sanitize_live_html
        if len(body.html) > 800_000:
            raise HTTPException(413, "Document content too large")
        data["html"] = sanitize_live_html(body.html)
        data["mode"] = "live"
        data["editor_v"] = 2   # saved with the layout-faithful extractor
    elif body.mode == "template":
        data.pop("html", None)
        data["mode"] = "template"
    if body.snapshot:
        _snapshot_version(data, "team", body.note or "Manual save")
    data.setdefault("replacements", [])
    _set_doc_data(rec, doc_type, data)
    await db.commit()
    return {"message": "Saved", "versions": len(data.get("versions", []))}


@router.get("/versions/{onboarding_id}/{doc_type}/{token}")
async def document_versions(onboarding_id: str, doc_type: str, token: str,
                            db: AsyncSession = Depends(get_db)):
    _check_doc_type(doc_type)
    _check_token(onboarding_id, doc_type, token, "edit")
    rec, _lead = await _load(db, onboarding_id)
    data = _get_doc_data(rec, doc_type)
    return {
        "mode": data.get("mode", "template"),
        "stage": data.get("stage", ""),
        "comments": data.get("comments", []),
        "versions": [{k: v for k, v in ver.items() if k not in ("html", "replacements")}
                     for ver in data.get("versions", [])],
    }


class _RestoreBody(BaseModel):
    n: int


@router.post("/versions/restore/{onboarding_id}/{doc_type}/{token}")
async def document_version_restore(onboarding_id: str, doc_type: str, token: str,
                                   body: _RestoreBody, db: AsyncSession = Depends(get_db)):
    _check_doc_type(doc_type)
    _check_token(onboarding_id, doc_type, token, "edit")
    rec, _lead = await _load(db, onboarding_id)
    data = _get_doc_data(rec, doc_type)
    if data.get("signature"):
        raise HTTPException(409, "Document already signed — editing is locked")
    ver = next((v for v in data.get("versions", []) if v.get("n") == body.n), None)
    if not ver:
        raise HTTPException(404, "Version not found")
    _snapshot_version(data, "team", f"Before restoring version {body.n}")
    data["mode"] = ver.get("mode", "template")
    data["replacements"] = ver.get("replacements", [])
    if ver.get("html"):
        data["html"] = ver["html"]
        data["editor_v"] = ver.get("editor_v")
    else:
        data.pop("html", None)
        data.pop("editor_v", None)
    _set_doc_data(rec, doc_type, data)
    await db.commit()
    return {"message": f"Restored version {body.n}"}


@router.post("/revert-template/{onboarding_id}/{doc_type}/{token}")
async def document_revert_template(onboarding_id: str, doc_type: str, token: str,
                                   db: AsyncSession = Depends(get_db)):
    """Drop all live edits — the document goes back to the original PDF template
    filled with the current placeholder values."""
    _check_doc_type(doc_type)
    _check_token(onboarding_id, doc_type, token, "edit")
    rec, _lead = await _load(db, onboarding_id)
    data = _get_doc_data(rec, doc_type)
    if data.get("signature"):
        raise HTTPException(409, "Document already signed — editing is locked")
    if data.get("html"):
        _snapshot_version(data, "team", "Before reverting to the original template")
    data.pop("html", None)
    data.pop("editor_v", None)
    data["mode"] = "template"
    _set_doc_data(rec, doc_type, data)
    await db.commit()
    return {"message": "Reverted to the original template"}


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
    if data.get("signature"):
        raise HTTPException(409, "Document already signed")
    if body.replacements:
        data["replacements"] = [
            {"find": str(r.get("find", ""))[:200], "replace": str(r.get("replace", ""))[:500]}
            for r in body.replacements if str(r.get("find", "")).strip()
        ]
    if body.mode == "live" and body.html.strip():
        from app.services.pdf_documents import sanitize_live_html
        data["html"] = sanitize_live_html(body.html)
        data["mode"] = "live"
        data["editor_v"] = 2
    data["signatory_name"] = body.signatory_name[:200] or data.get("signatory_name", "")
    data["signatory_email"] = body.signatory_email[:320] or data.get("signatory_email", "") or lead.email
    data.setdefault("replacements", [])
    data["stage"] = "review"
    # re-sending means the requested changes were handled — move open comments to Done
    for c in data.get("comments", []):
        c["done"] = True
    rev = len([v for v in data.get("versions", []) if "Sent to lead" in (v.get("note") or "")]) + 1
    _snapshot_version(data, "team", f"Sent to lead for T&C review (revision {rev})")
    _set_doc_data(rec, doc_type, data)

    now = _now_ist()
    label = _DOC_LABELS[doc_type]
    short = "NDA" if doc_type == "nda" else "Agreement"
    _apply_status(rec, doc_type, DocumentStatus.SENT_TO_LEAD,
                  f"{short} Sent to Lead ({_fmt(now)}) — Awaiting T&C Review")
    if doc_type == "nda":
        rec.nda_sent_at = now
    else:
        rec.agreement_sent_at = now
    await db.commit()

    from app.services.onboarding_email import send_document_review_email
    review_url = make_doc_sign_url(onboarding_id, doc_type)
    to_email = data["signatory_email"] or lead.email
    send_document_review_email(
        to_email=to_email,
        lead_name=data["signatory_name"] or lead.contact_name or lead.business_name,
        company_name=lead.business_name,
        doc_type=doc_type,
        review_url=review_url,
    )

    try:
        from app.workers.onboarding_tasks import export_onboarding_to_sheets
        export_onboarding_to_sheets.delay(onboarding_id)
    except Exception:
        pass

    _crm_stage_safe(rec, lead, onboarding_id,
                    stage=f"{short} Sent for Review",
                    detail=f"T&C review link emailed to {to_email}")

    logger.info("document_sent_for_review", onboarding_id=onboarding_id, doc_type=doc_type, to=to_email)
    return {"message": f"{label} sent to {to_email} for Terms & Conditions review."}


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


def _review_page_html(onboarding_id: str, doc_type: str, token: str, label: str,
                      lead: LeadV2, data: dict) -> str:
    """Lead-facing Terms & Conditions review page — accept or request changes."""
    pdf_url = f"/api/v1/documents/pdf/{onboarding_id}/{doc_type}/{token}"
    sig_name = data.get("signatory_name", "") or (lead.contact_name or "")
    comments = data.get("comments") or []
    prev_comments = ""
    if comments:
        items = "".join(
            f'<div style="background:#fff7ed;border-left:3px solid #f59e0b;border-radius:6px;'
            f'padding:10px 14px;margin-bottom:8px;">'
            f'<div style="font-size:11px;color:#92400e;font-weight:700;">{(c.get("by") or "You")} — {c.get("at", "")}</div>'
            f'<div style="font-size:13px;color:#451a03;margin-top:4px;white-space:pre-wrap;">{c.get("text", "")}</div></div>'
            for c in comments[-6:])
        prev_comments = (f'<div class="card"><h1 style="font-size:16px;">Previous Comments</h1>'
                         f'<p class="sub">Our team reviews every comment and sends you an updated version.</p>{items}</div>')
    updated_note = ""
    if data.get("stage") == "review" and comments:
        updated_note = ('<div style="background:#ecfdf5;border-left:3px solid #16a34a;border-radius:6px;'
                        'padding:10px 14px;margin-bottom:14px;font-size:13px;color:#14532d;">'
                        'This document was <b>updated</b> based on your previous comments — please review the latest version below.</div>')
    return f"""<!DOCTYPE html>
<html lang="en">
<head><meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Review {label} — Jane Aerospace</title>
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
  textarea{{width:100%;min-height:110px;padding:11px 13px;border:1px solid #ccc;border-radius:6px;
           font-size:14px;font-family:inherit;resize:vertical;}}
  .btn{{border:none;padding:14px 30px;border-radius:7px;font-size:15px;font-weight:700;cursor:pointer;width:100%;color:#fff;}}
  .btn:disabled{{background:#9ca3af!important;cursor:not-allowed;}}
  .accept{{background:#16a34a;}}
  .changes{{background:#fff;color:#b45309;border:2px solid #f59e0b;}}
  .grid{{display:grid;grid-template-columns:1fr 1fr;gap:18px;}}
  #err{{display:none;background:#fee2e2;color:#991b1b;padding:10px 14px;border-radius:6px;font-size:13px;margin:12px 0;}}
  .chk{{display:flex;gap:10px;align-items:flex-start;margin:14px 0;font-size:13px;color:#374151;line-height:1.5;}}
  .chk input{{margin-top:3px;width:17px;height:17px;}}
  @media(max-width:700px){{.grid{{grid-template-columns:1fr;}}.card{{padding:20px 16px;}}iframe{{height:420px;}}}}
</style></head>
<body>
<div class="wrap">
  <div class="card">
    <div style="font-size:15px;font-weight:700;color:#1a3a6b;margin-bottom:12px;">✈ Jane Aerospace</div>
    <h1>{label} — Terms &amp; Conditions Review</h1>
    <p class="sub">Between <strong>Jane Aerospace Private Limited</strong> and
      <strong>{lead.business_name}</strong>. Please review the document below. You can
      <b>accept the terms</b> or <b>request changes</b> — no signature is needed at this stage.</p>
    {updated_note}
    <iframe src="{pdf_url}#toolbar=1"></iframe>
    <p style="font-size:13px;margin-top:8px;"><a href="{pdf_url}" target="_blank" style="color:#1155cc;">Open / download the PDF ↗</a></p>
  </div>
  {prev_comments}
  <div class="grid">
    <div class="card">
      <h1 style="font-size:17px;color:#15803d;">✓ Accept Terms &amp; Conditions</h1>
      <p class="sub">Accepting notifies our team. Jane Aerospace countersigns first, then you
        receive the final document for your e-signature.</p>
      <label>Your Name</label>
      <input type="text" id="acc-name" value="{sig_name}" placeholder="Full name">
      <div class="chk"><input type="checkbox" id="acc-agree">
        <span>I have reviewed this {label} and accept its Terms &amp; Conditions on behalf of
        <strong>{lead.business_name}</strong>.</span></div>
      <button class="btn accept" id="acc-btn" onclick="reviewAct('accept')">✓ Accept Terms</button>
    </div>
    <div class="card">
      <h1 style="font-size:17px;color:#b45309;">💬 Request Changes</h1>
      <p class="sub">Add your comments or suggested modifications — our team will update the
        document and send you a revised version.</p>
      <label>Your Name</label>
      <input type="text" id="cmt-name" value="{sig_name}" placeholder="Full name">
      <label>Comments / Suggested Changes</label>
      <textarea id="cmt-text" placeholder="e.g. Please change the notice period in clause 7 to 30 days…"></textarea>
      <button class="btn changes" id="cmt-btn" onclick="reviewAct('comment')">Send Comments to Jane Aerospace</button>
    </div>
  </div>
  <div class="card">
    <h1 style="font-size:17px;color:#1a56db;">👤 Not the right person for this review?</h1>
    <p class="sub">If someone else in your company (e.g. your legal / review team) should review these
      Terms &amp; Conditions, enter their details — we will send the review link directly to them.</p>
    <div class="grid">
      <div>
        <label>Reviewer's Name</label>
        <input type="text" id="fwd-name" placeholder="Full name of the right person">
      </div>
      <div>
        <label>Reviewer's Email</label>
        <input type="text" id="fwd-email" placeholder="name@company.com">
      </div>
    </div>
    <button class="btn" id="fwd-btn" style="background:#1a56db;margin-top:14px;"
            onclick="reviewAct('forward')">→ Send Review Link to This Person</button>
  </div>
  <div id="err"></div>
</div>
<script>
async function reviewAct(action) {{
  const err = document.getElementById('err'); err.style.display = 'none';
  let payload = {{action: action}};
  if (action === 'accept') {{
    payload.name = document.getElementById('acc-name').value.trim();
    if (!payload.name) {{ err.textContent = 'Please enter your name.'; err.style.display = 'block'; return; }}
    if (!document.getElementById('acc-agree').checked) {{
      err.textContent = 'Please tick the confirmation checkbox to accept.'; err.style.display = 'block'; return; }}
  }} else if (action === 'forward') {{
    payload.name = document.getElementById('cmt-name').value.trim()
                   || document.getElementById('acc-name').value.trim();
    payload.forward_name = document.getElementById('fwd-name').value.trim();
    payload.forward_email = document.getElementById('fwd-email').value.trim();
    if (!payload.forward_name) {{ err.textContent = "Please enter the reviewer's name."; err.style.display = 'block'; return; }}
    if (!/^[^@\\s]+@[^@\\s]+\\.[^@\\s]+$/.test(payload.forward_email)) {{
      err.textContent = "Please enter a valid reviewer email address."; err.style.display = 'block'; return; }}
  }} else {{
    payload.name = document.getElementById('cmt-name').value.trim();
    payload.comments = document.getElementById('cmt-text').value.trim();
    if (!payload.comments) {{ err.textContent = 'Please write your comments first.'; err.style.display = 'block'; return; }}
  }}
  const btn = document.getElementById(
    action === 'accept' ? 'acc-btn' : action === 'forward' ? 'fwd-btn' : 'cmt-btn');
  btn.disabled = true;
  try {{
    const r = await fetch('/api/v1/documents/review/{onboarding_id}/{doc_type}/{token}', {{
      method: 'POST', headers: {{'Content-Type': 'application/json'}}, body: JSON.stringify(payload)
    }});
    const d = await r.json().catch(() => ({{}}));
    if (r.ok) {{
      if (action === 'forward') {{ alert(d.message || 'Review link sent.'); }}
      window.location.reload();
    }}
    else {{ err.textContent = d.detail || 'Something went wrong.'; err.style.display = 'block'; btn.disabled = false; }}
  }} catch(e) {{
    err.textContent = 'Network error — ' + e.message; err.style.display = 'block'; btn.disabled = false;
  }}
}}
</script>
</body></html>"""


class _ReviewBody(BaseModel):
    action: str            # accept | comment | forward
    name: str = ""
    comments: str = ""
    forward_name: str = ""    # action=forward — the right person on the lead's side
    forward_email: str = ""


@router.post("/review/{onboarding_id}/{doc_type}/{token}")
async def document_review(onboarding_id: str, doc_type: str, token: str,
                          body: _ReviewBody, db: AsyncSession = Depends(get_db)):
    """Lead reviews the T&C: accept, or request changes with comments."""
    _check_doc_type(doc_type)
    _check_token(onboarding_id, doc_type, token, "sign")
    rec, lead = await _load(db, onboarding_id)
    data = await _ensure_doc_data(db, rec, lead, doc_type)
    if data.get("signature"):
        raise HTTPException(409, "Document already signed")
    if data.get("stage") not in ("review", "changes_requested"):
        raise HTTPException(409, "This document is not in the review stage")

    now = _now_ist()
    short = "NDA" if doc_type == "nda" else "Agreement"
    name = (body.name or lead.contact_name or lead.business_name).strip()[:200]

    if body.action == "accept":
        data["stage"] = "accepted"
        data["accepted"] = {"by": name, "email": lead.email, "at": _fmt(now)}
        _apply_status(rec, doc_type, None,
                      f"{short} T&C Accepted by {name} ({_fmt(now)}) — Internal Signature Required")
        _set_doc_data(rec, doc_type, data)
        await db.commit()
        try:
            from app.services.onboarding_email import notify_team_terms_accepted
            notify_team_terms_accepted(
                company_name=lead.business_name, lead_email=lead.email,
                doc_type=doc_type, accepted_by=name,
                edit_url=_make_edit_url(onboarding_id, doc_type))
        except Exception as exc:
            logger.warning("notify_terms_accepted_failed", error=str(exc))
        _crm_stage_safe(rec, lead, onboarding_id,
                        stage=f"{short} Terms Accepted",
                        detail=f"T&C accepted by {name}; internal signature pending")
        logger.info("document_terms_accepted", onboarding_id=onboarding_id, doc_type=doc_type, by=name)
        return {"message": "Terms accepted"}

    if body.action == "comment":
        text = body.comments.strip()[:4000]
        if not text:
            raise HTTPException(400, "Comments are required")
        comments = data.setdefault("comments", [])
        entry: dict = {"by": name, "email": lead.email, "text": text,
                       "at": _fmt(now), "done": False}
        try:  # AI filters the comment into short action items for the team
            import asyncio
            from app.services.onboarding_ai import summarize_doc_comment
            entry["ai_actions"] = await asyncio.wait_for(
                asyncio.to_thread(summarize_doc_comment, text), timeout=15)
        except Exception:
            entry["ai_actions"] = []
        comments.append(entry)
        data["stage"] = "changes_requested"
        # A revision request is NOT a rejection — the draft goes back to the
        # team for revision and is re-sent to the lead after the changes.
        _apply_status(rec, doc_type, DocumentStatus.TEAM_REVIEW,
                      f"{short} Revision Requested by {name} ({_fmt(now)}) — "
                      f"{len(comments)} comment(s), under revision")
        _set_doc_data(rec, doc_type, data)
        await db.commit()
        try:
            from app.services.onboarding_email import notify_team_document_comments
            notify_team_document_comments(
                company_name=lead.business_name, lead_email=lead.email,
                doc_type=doc_type, commenter=name, comments_text=text,
                edit_url=_make_edit_url(onboarding_id, doc_type))
        except Exception as exc:
            logger.warning("notify_document_comments_failed", error=str(exc))
        _crm_stage_safe(rec, lead, onboarding_id,
                        stage=f"{short} Changes Requested",
                        detail=f"Lead comment: {text[:180]}")
        logger.info("document_changes_requested", onboarding_id=onboarding_id, doc_type=doc_type, by=name)
        return {"message": "Comments sent"}

    if body.action == "forward":
        # "I'm not the right person for this review" — the lead hands the T&C
        # review to their legal / review team. We re-send the review link to
        # that person and they continue the same flow.
        import re as _re
        fwd_name = body.forward_name.strip()[:200]
        fwd_email = body.forward_email.strip().lower()[:320]
        if not fwd_name:
            raise HTTPException(400, "Please enter the reviewer's name")
        if not _re.match(r"^[^@\s]+@[^@\s]+\.[^@\s]+$", fwd_email):
            raise HTTPException(400, "Please enter a valid reviewer email address")

        data["forwarded_to"] = {"name": fwd_name, "email": fwd_email,
                                "by": name, "at": _fmt(now)}
        # the new person is now the document contact / signatory
        data["signatory_name"] = fwd_name
        data["signatory_email"] = fwd_email
        comments = data.setdefault("comments", [])
        comments.append({"by": name, "email": lead.email,
                         "text": f"[Forwarded] {name} is not the right person for this review — "
                                 f"forwarded to {fwd_name} <{fwd_email}>.",
                         "at": _fmt(now)})
        data["stage"] = "review"
        _apply_status(rec, doc_type, None,
                      f"{short} Review Forwarded to {fwd_name} ({_fmt(now)}) — Awaiting T&C Review")
        _set_doc_data(rec, doc_type, data)
        await db.commit()

        from app.services.onboarding_email import send_document_review_email
        review_url = make_doc_sign_url(onboarding_id, doc_type)
        send_document_review_email(
            to_email=fwd_email, lead_name=fwd_name,
            company_name=lead.business_name, doc_type=doc_type, review_url=review_url)
        try:
            from app.services.onboarding_email import notify_team_document_comments
            notify_team_document_comments(
                company_name=lead.business_name, lead_email=lead.email,
                doc_type=doc_type, commenter=name,
                comments_text=f"Review forwarded: {name} indicated they are not the right person. "
                              f"The {short} T&C review link was sent to {fwd_name} <{fwd_email}>.",
                edit_url=_make_edit_url(onboarding_id, doc_type))
        except Exception as exc:
            logger.warning("notify_forward_failed", error=str(exc))
        _crm_stage_safe(rec, lead, onboarding_id,
                        stage=f"{short} Review Forwarded",
                        detail=f"T&C review forwarded by {name} to {fwd_name} <{fwd_email}>")
        logger.info("document_review_forwarded", onboarding_id=onboarding_id,
                    doc_type=doc_type, by=name, to=fwd_email)
        return {"message": f"Review link sent to {fwd_name}"}

    raise HTTPException(400, "Unknown action")


def _make_edit_url(onboarding_id: str, doc_type: str) -> str:
    from app.services.onboarding_email import make_doc_edit_url
    return make_doc_edit_url(onboarding_id, doc_type)


class _InternalSignBody(BaseModel):
    signed_name: str
    designation: str = ""
    sig_font: str = "standard"
    sig_image: str = ""      # optional uploaded signature picture (data URL)


@router.post("/internal-sign/{onboarding_id}/{doc_type}/{token}")
async def document_internal_sign(onboarding_id: str, doc_type: str, token: str,
                                 body: _InternalSignBody, request: Request,
                                 db: AsyncSession = Depends(get_db)):
    """Jane Aerospace authorised representative signs first, then the
    countersigned document is emailed to the lead for their signature."""
    _check_doc_type(doc_type)
    _check_token(onboarding_id, doc_type, token, "edit")
    rec, lead = await _load(db, onboarding_id)
    data = await _ensure_doc_data(db, rec, lead, doc_type)
    if data.get("signature"):
        raise HTTPException(409, "Document already signed by the lead")
    if data.get("internal_signature"):
        raise HTTPException(409, "Document already internally signed")
    if not body.signed_name.strip():
        raise HTTPException(400, "Representative name is required")

    now = _now_ist()
    data["internal_signature"] = {
        "signed_name": body.signed_name.strip()[:200],
        "designation": body.designation.strip()[:200] or "Authorised Signatory",
        "sig_font": body.sig_font if body.sig_font in ("standard", "dancing", "greatvibes", "pacifico") else "standard",
        "sig_image": _clean_sig_image(body.sig_image),
        "email": "",
        "company_name": "Jane Aerospace Private Limited",
        "signed_at": _fmt(now),
        "ip": (request.client.host if request.client else "") or "",
    }
    data["stage"] = "awaiting_lead_sign"
    short = "NDA" if doc_type == "nda" else "Agreement"
    _apply_status(rec, doc_type, DocumentStatus.SENT_TO_LEAD,
                  f"{short} Internally Signed by {body.signed_name.strip()} ({_fmt(now)}) — Sent to Lead for Signature")
    _set_doc_data(rec, doc_type, data)
    await db.commit()

    from app.services.onboarding_email import send_document_sign_email
    sign_url = make_doc_sign_url(onboarding_id, doc_type)
    to_email = data.get("signatory_email") or lead.email
    send_document_sign_email(
        to_email=to_email,
        lead_name=data.get("signatory_name") or lead.contact_name or lead.business_name,
        company_name=lead.business_name,
        doc_type=doc_type,
        sign_url=sign_url,
    )
    _crm_stage_safe(rec, lead, onboarding_id,
                    stage=f"{short} Sent for E-Sign",
                    detail=f"Internally signed by {body.signed_name.strip()}; signing link emailed to {to_email}")
    logger.info("document_internal_signed", onboarding_id=onboarding_id, doc_type=doc_type,
                by=body.signed_name.strip(), to=to_email)
    return {"message": f"Signed on behalf of Jane Aerospace and sent to {to_email} for counter-signature."}


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

    stage = data.get("stage") or ""
    internal_sig = data.get("internal_signature")

    if stage == "accepted" and not internal_sig:
        return HTMLResponse(_result_page(
            "Terms Accepted — Thank You",
            f"You have accepted the Terms & Conditions of this {label}. "
            f"Jane Aerospace will countersign the document and email you the final "
            f"version for your signature shortly."))

    if stage in ("review", "changes_requested") and not internal_sig:
        return HTMLResponse(_review_page_html(onboarding_id, doc_type, token, label, lead, data))

    pdf_url = f"/api/v1/documents/pdf/{onboarding_id}/{doc_type}/{token}"
    if internal_sig:
        # show the internally countersigned document for the lead's signature
        pdf_url = f"/api/v1/documents/signed/{onboarding_id}/{doc_type}/{token}"
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
    <label style="margin-top:14px;">Or Upload Your Signature Image
      <span style="color:#9ca3af;font-weight:400;">(PNG / JPG of your real signature — used instead of the style above)</span></label>
    <input type="file" id="s-sigimg" accept=".png,.jpg,.jpeg" onchange="loadSigImage(this)"
           style="font-size:13px;margin-top:2px;">
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
let SIG_IMAGE = '';
function updateSig() {{
  const p = document.getElementById('sig-preview');
  const name = document.getElementById('s-name').value.trim();
  if (SIG_IMAGE) {{
    p.innerHTML = '<img src="' + SIG_IMAGE + '" style="max-height:56px;max-width:90%;">';
    return;
  }}
  p.style.fontFamily = FONT_MAP[SIG_FONT];
  p.textContent = name || 'Type your name above';
  p.style.color = name ? '#10245c' : '#9ca3af';
  p.style.fontSize = name ? '30px' : '14px';
}}
function loadSigImage(input) {{
  const f = input.files && input.files[0];
  if (!f) {{ SIG_IMAGE = ''; updateSig(); return; }}
  if (f.size > 5 * 1024 * 1024) {{
    alert('Signature image is too large — maximum 5 MB.'); input.value = ''; return;
  }}
  const img = new Image();
  img.onload = function() {{
    const scale = Math.min(1, 480 / img.width);
    const c = document.createElement('canvas');
    c.width = Math.round(img.width * scale);
    c.height = Math.round(img.height * scale);
    c.getContext('2d').drawImage(img, 0, 0, c.width, c.height);
    SIG_IMAGE = c.toDataURL('image/png');
    updateSig();
  }};
  img.onerror = function() {{ alert('Could not read that image file.'); input.value = ''; }};
  img.src = URL.createObjectURL(f);
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
                            sig_font: SIG_FONT, sig_image: SIG_IMAGE}})
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
    sig_image: str = ""      # optional uploaded signature picture (data URL)


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
    if data.get("stage") in ("review", "changes_requested", "accepted") and not data.get("internal_signature"):
        raise HTTPException(409, "This document is still in the review stage — signing opens "
                                 "after the terms are accepted and Jane Aerospace countersigns")
    if not body.signed_name.strip():
        raise HTTPException(400, "Full legal name is required")

    now = _now_ist()
    sig = {
        "signed_name": body.signed_name.strip()[:200],
        "designation": body.designation.strip()[:200],
        "sig_font": body.sig_font if body.sig_font in ("standard", "dancing", "greatvibes", "pacifico") else "standard",
        "sig_image": _clean_sig_image(body.sig_image),
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
        signed_pdf = append_signature_page(_render_pdf(rec, doc_type, data), doc_type,
                                           sig=sig, internal_sig=data.get("internal_signature"))
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
