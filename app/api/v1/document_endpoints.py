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
from html import unescape as _html_unescape
from zoneinfo import ZoneInfo

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.logging import get_logger
from app.db.base import DocumentStatus
from app.db.models import KYCSubmission, LeadV2, OnboardingRecord
from app.db.session import get_db
from app.services.onboarding_email import (
    make_doc_portal_url,
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


def _render_pdf(rec: OnboardingRecord, doc_type: str, data: dict, *, stamp: bool = True) -> bytes:
    """Render the working document.

    Live-edited documents render from their edited HTML (Live Editor mode);
    everything else fills the original PDF template placeholder-by-placeholder,
    leaving format and content untouched.

    Placed signature overlays are flattened on top when ``stamp`` is True. The
    editor preview passes ``stamp=False`` so its draggable overlay layer is the
    only representation; downloads / the signing copy stamp them into the PDF."""
    from app.services.pdf_documents import stamp_overlays
    if data.get("mode") == "live" and data.get("html"):
        from app.services.pdf_documents import live_html_to_pdf
        pdf = live_html_to_pdf(data["html"])
    else:
        from app.services.pdf_documents import fill_document
        pdf = fill_document(doc_type, _is_overseas(rec), data.get("replacements", []))
    return stamp_overlays(pdf, data.get("signatures_overlay")) if stamp else pdf


def _plain_text_from_html(raw: str) -> str:
    """Best-effort plain text from editor HTML, used as the Track-Changes baseline."""
    s = raw or ""
    s = re.sub(r"(?is)<(?:script|style)\b.*?</(?:script|style)>", "", s)
    s = re.sub(r"(?i)<(?:br|/p|/div|/h[1-6]|/li|/tr|/table)\s*/?>", "\n", s)
    s = re.sub(r"(?i)<(?:p|div|h[1-6]|li|tr)\b[^>]*>", "\n", s)
    s = re.sub(r"<[^>]+>", "", s)
    s = _html_unescape(s)
    s = re.sub(r"[ \t]+", " ", s)
    s = re.sub(r" *\n *", "\n", s)
    s = re.sub(r"\n{3,}", "\n\n", s)
    return s.strip()


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
    # stamp=False: the editor shows placed signatures as a live, draggable overlay
    # layer on top of these page images — they are flattened only in the real PDF.
    pdf = _render_pdf(rec, doc_type, data, stamp=False)
    _PREVIEW_CACHE[key] = (v, pdf)
    while len(_PREVIEW_CACHE) > 10:
        _PREVIEW_CACHE.pop(next(iter(_PREVIEW_CACHE)))
    return pdf


@router.get("/preview-info/{onboarding_id}/{doc_type}/{token}", include_in_schema=False)
async def document_preview_info(onboarding_id: str, doc_type: str, token: str,
                                v: str = "", db: AsyncSession = Depends(get_db)):
    _check_doc_type(doc_type)
    _check_token(onboarding_id, doc_type, token, "edit", "sign")   # lead may render pages to place their signature
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
    _check_token(onboarding_id, doc_type, token, "edit", "sign")   # lead may render pages to place their signature
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
    organizer_name_json = json.dumps(settings.ORGANIZER_NAME)
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
    needs_internal_sign = (stage == "accepted" and not internal_sig and not signed)
    sign_topbar_btn = (
        f'<button class="btn b-green" onclick="openSignModal()">✍ Sign &amp; Send to Lead</button>'
        if needs_internal_sign else ""
    )

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

    # Internal signature — only show status note in panel; form lives in the modal
    internal_html = ""
    if not signed and internal_sig:
        internal_html = (f'<div class="sec">Internal Signature</div>'
                         f'<div class="note" style="background:#ecfdf5;border-left-color:#16a34a;color:#14532d;">'
                         f'✓ Signed by <b>{internal_sig.get("signed_name", "")}</b> '
                         f'({internal_sig.get("designation") or "Authorised Signatory"}) '
                         f'on {internal_sig.get("signed_at", "")}. '
                         f'Document emailed to lead for counter-signature.</div>')

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
  /* Internal sign modal */
  .modal-overlay{{position:fixed;inset:0;background:rgba(7,16,32,.55);z-index:100;
    display:none;align-items:center;justify-content:center;backdrop-filter:blur(2px);}}
  .modal-overlay.open{{display:flex;}}
  .modal-box{{background:#fff;border-radius:14px;width:420px;max-width:94vw;
    box-shadow:0 20px 60px rgba(7,16,32,.3);overflow:hidden;}}
  .modal-head{{background:#0c2344;color:#fff;padding:16px 20px;display:flex;align-items:center;gap:10px;}}
  .modal-head h3{{font-size:14px;font-weight:800;letter-spacing:.02em;flex:1;}}
  .modal-head button{{background:rgba(255,255,255,.15);border:none;color:#fff;border-radius:6px;
    width:28px;height:28px;font-size:16px;cursor:pointer;line-height:1;}}
  .modal-body{{padding:20px;}}
  .modal-note{{background:#ecfdf5;border-left:3px solid #16a34a;color:#14532d;
    padding:9px 12px;border-radius:5px;font-size:12px;line-height:1.5;margin-bottom:16px;}}
  .modal-fld{{margin-bottom:12px;}}
  .modal-fld label{{display:block;font-size:11.5px;font-weight:700;color:#374151;margin-bottom:4px;}}
  .modal-fld input,.modal-fld select{{width:100%;padding:9px 11px;border:1.5px solid #cbd5e1;
    border-radius:7px;font-size:13px;font-family:inherit;}}
  .modal-fld input:focus,.modal-fld select:focus{{outline:none;border-color:#1a56db;
    box-shadow:0 0 0 3px rgba(26,86,219,.12);}}
  .modal-upload-zone{{border:2px dashed #c7d4ee;border-radius:8px;background:#f8fafc;
    padding:18px;text-align:center;cursor:pointer;transition:border-color .15s;margin-bottom:8px;}}
  .modal-upload-zone:hover{{border-color:#1a56db;background:#eff6ff;}}
  .modal-footer{{display:flex;gap:8px;justify-content:flex-end;padding:14px 20px;
    border-top:1px solid #e5eaf2;}}
  @media(max-width:760px){{.split{{flex-direction:column;}}.panel{{width:100%;max-width:100%;height:55%;}}
    .pv{{height:45%;}}}}
</style></head>
<body>

<!-- Internal signing modal (only relevant when stage==accepted and not yet signed) -->
<div class="modal-overlay" id="sign-modal">
  <div class="modal-box">
    <div class="modal-head">
      <h3>✍ Sign on Behalf of Jane Aerospace</h3>
      <button onclick="closeSignModal()" title="Cancel">✕</button>
    </div>
    <div class="modal-body">
      <div class="modal-note">
        The lead has accepted the Terms &amp; Conditions. Sign below and the counter-signed document
        will be emailed to the lead automatically for their signature.
      </div>
      <div class="modal-fld">
        <label>Designation</label>
        <input type="text" id="int-desig" placeholder="e.g. Director, Authorised Signatory">
      </div>
      <div class="modal-fld">
        <label>Signature Style</label>
        <select id="int-font">
          <option value="standard">Standard</option>
          <option value="dancing">Cursive</option>
        </select>
      </div>
      <div class="modal-fld">
        <label>Upload Signature Image (PNG / JPG)</label>
        <div class="modal-upload-zone" onclick="document.getElementById('int-sigimg').click()">
          🖊 Click to upload signature image
          <div style="font-size:11px;color:#64748b;margin-top:4px;">PNG or JPG recommended</div>
          <input type="file" id="int-sigimg" accept=".png,.jpg,.jpeg" onchange="loadIntSig(this)" style="display:none;">
        </div>
        <div id="int-sig-preview" style="margin-top:6px;"></div>
      </div>
    </div>
    <div class="modal-footer">
      <button class="btn b-line" onclick="closeSignModal()">Cancel</button>
      <button class="btn b-green" onclick="internalSign()">✍ Sign &amp; Send to Lead</button>
    </div>
  </div>
</div>

<div class="topbar">
  <span class="ttl">✈ JANE AEROSPACE</span>
  <span class="sub">{label} — {lead.business_name} ({'Overseas' if _is_overseas(rec) else 'Indian'} template)</span>
  <span class="chip">{status}</span>{signed_badge}
  <span class="grow"></span>
  <span id="saved">Saved ✓</span>
  <button class="btn b-line" id="edit-toggle" onclick="toggleEditMode()" title="Toggle edit mode">🔒 View Only</button>
  <a class="btn b-line" href="{base}/editor/{onboarding_id}/{doc_type}/{token}" title="Edit the full document content">📝 Live Editor</a>
  <button class="btn b-line" id="btn-reset" onclick="resetDoc()" title="Rebuild values from KYC data" style="display:none">↺ Reset</button>
  {signed_btn}
  {sign_topbar_btn}
  <button class="btn b-green" id="btn-send" onclick="sendToLead()" style="display:none">📨 Send to Lead</button>
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
const ORGANIZER_NAME = {organizer_name_json};
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

function openSignModal() {{
  document.getElementById('sign-modal').classList.add('open');
}}
function closeSignModal() {{
  document.getElementById('sign-modal').classList.remove('open');
}}
document.getElementById('sign-modal').addEventListener('click', function(e) {{
  if (e.target === this) closeSignModal();
}});

async function internalSign() {{
  const desig = (document.getElementById('int-desig') || {{}}).value || '';
  const font  = (document.getElementById('int-font') || {{}}).value || 'standard';
  const r = await fetch(`${{BASE}}/internal-sign/${{OID}}/${{DT}}/${{TOK}}`, {{
    method: 'POST', headers: {{'Content-Type': 'application/json'}},
    body: JSON.stringify({{
      signed_name: ORGANIZER_NAME,
      designation: desig,
      sig_font: font,
      sig_image: INT_SIG_IMAGE,
    }})
  }});
  const d = await r.json().catch(() => ({{}}));
  if (!r.ok) {{ alert(d.detail || 'Signing failed'); return; }}
  closeSignModal();
  const pv = document.getElementById('pv');
  if (pv) pv.src = `${{BASE}}/signed/${{OID}}/${{DT}}/${{TOK}}`;
  location.reload();
}}

render();
document.getElementById('sig-name').value = initial.signatory_name || '';
document.getElementById('sig-email').value = initial.signatory_email || '';

// ── Edit mode toggle ──────────────────────────────────────────────────────
var _editMode = false;
function toggleEditMode() {{
  _editMode = !_editMode;
  var btn = document.getElementById('edit-toggle');
  var allInputs = document.querySelectorAll('.panel input, .panel select, .panel textarea, .panel button:not(#edit-toggle)');
  var btnReset = document.getElementById('btn-reset');
  var btnSend = document.getElementById('btn-send');
  if (_editMode) {{
    btn.textContent = '✏ Edit Mode'; btn.style.background = '#fef3c7'; btn.style.color = '#92400e'; btn.style.borderColor = '#fcd34d';
    allInputs.forEach(function(el) {{ el.removeAttribute('disabled'); }});
    if (btnReset) btnReset.style.display = '';
    if (btnSend) btnSend.style.display = '';
  }} else {{
    btn.textContent = '🔒 View Only'; btn.style.background = ''; btn.style.color = ''; btn.style.borderColor = '';
    allInputs.forEach(function(el) {{ el.setAttribute('disabled', 'disabled'); }});
    if (btnReset) btnReset.style.display = 'none';
    if (btnSend) btnSend.style.display = 'none';
  }}
}}
// Start in view-only mode — disable all inputs by default
(function() {{
  var allInputs = document.querySelectorAll('.panel input, .panel select, .panel textarea, .panel button:not(#edit-toggle)');
  allInputs.forEach(function(el) {{ el.setAttribute('disabled', 'disabled'); }});
}})();
</script>
</body></html>"""
    return HTMLResponse(html)


# ---------------------------------------------------------------------------
# Team: Live Document Editor (full content editing)
# ---------------------------------------------------------------------------

# ── Word-style ribbon for the Live Editor ────────────────────────────────────
# Plain (non-f-string) constants injected via single {placeholders} so the many
# CSS/JS braces need no {{ }} escaping. The JS shares the editor's global scope
# (cmd / restoreRange / dirty / sheet are defined in the main editor script).
_EDITOR_RIBBON_CSS = """
  /* Word-style ribbon */
  .toolbar{background:linear-gradient(#fbfcfe,#eceff5);border-bottom:1px solid #d4dae6;
           padding:5px 12px;gap:0;align-items:stretch;flex-wrap:wrap;}
  .rb-lbl-grp{display:inline-flex;flex-direction:column;align-items:center;gap:3px;
              padding:3px 10px;border-right:1px solid #dde3ee;}
  .rb-lbl-grp:last-of-type{border-right:none;}
  .rb-end{margin-left:auto;border-right:none;}
  .rb-row{display:inline-flex;align-items:center;gap:3px;}
  .rb-cap{font-size:9.5px;color:#8a93a6;font-weight:600;letter-spacing:.03em;}
  .tb{border:1px solid transparent;background:none;border-radius:6px;min-width:30px;height:30px;
      font-size:13.5px;cursor:pointer;color:#26324a;display:inline-flex;align-items:center;
      justify-content:center;padding:0 8px;transition:background .12s,border-color .12s,box-shadow .12s;}
  .tb:hover{background:#e8effb;border-color:#c4d6f5;}
  .tb:active{background:#d8e6fb;}
  .tb.active{background:#dcebff;border-color:#9cc0f5;color:#1a56db;box-shadow:inset 0 0 0 1px #c3dbfb;}
  .tb-accent{width:auto;color:#1a56db;font-weight:700;border-color:#cfe0fb;background:#f2f7ff;}
  .tb-accent:hover{background:#e4eeff;}
  .tb-color{position:relative;flex-direction:column;gap:0;font-weight:800;font-size:13px;padding-top:2px;}
  .tb-color input[type=color]{position:absolute;left:5px;right:5px;bottom:4px;width:auto;height:4px;
      border:none;padding:0;background:none;cursor:pointer;}
  .rb-sel{height:30px;border:1px solid #cbd4e3;border-radius:6px;background:#fff;font-size:12.5px;
          color:#26324a;padding:0 6px;cursor:pointer;font-family:inherit;transition:border-color .12s;}
  .rb-sel:hover{border-color:#9cb6e6;}
  .rb-sel:focus{outline:none;border-color:#1a56db;box-shadow:0 0 0 3px rgba(26,86,219,.13);}
  #rb-style{min-width:104px;} .rb-size{min-width:60px;}
  .tsep{display:none;}
  /* Word-style page canvas */
  .edwrap{background:#e4e6ea;padding:30px 18px 90px;}
  .sheet{box-shadow:0 1px 4px rgba(0,0,0,.12),0 10px 30px rgba(12,35,68,.16);
         border:1px solid #d6dae2;border-radius:2px;}
  .sheet:focus{border-color:#bcd0f2;
         box-shadow:0 1px 4px rgba(0,0,0,.12),0 12px 34px rgba(26,86,219,.18);}
  /* diff stat chips shown in preview header */
  .chip-add{display:inline-flex;align-items:center;padding:2px 8px;border-radius:99px;
    font-size:11px;font-weight:700;background:#dcfce7;color:#15803d;}
  .chip-del{display:inline-flex;align-items:center;padding:2px 8px;border-radius:99px;
    font-size:11px;font-weight:700;background:#fee2e2;color:#b91c1c;}
  /* track-changes redline, shown inline in the document (read-only review) */
  /* track-changes redline — Word style: thin colored underline / strikethrough
     plus a change-bar in the left margin. No heavy block fills. */
  ins.rl-ins{background:none;color:#1a56db;text-decoration:underline;
    text-decoration-thickness:1px;text-underline-offset:2px;padding:0;}
  del.rl-del{background:none;color:#c0182f;text-decoration:line-through;
    text-decoration-thickness:1px;padding:0;}
  p.rl-blank{margin:0;height:8px;}
  /* change-bar removed — the inline strikethrough/underline shows the change */
  /* review mode: very subtle tint so it reads as read-only */
  .sheet.review{background:#fcfcfd;cursor:default;box-shadow:0 1px 4px rgba(0,0,0,.12),
    0 10px 30px rgba(12,35,68,.16);}
  /* real SVG icons in toolbar + topbar */
  .tb svg{width:17px;height:17px;display:block;}
  .tb-accent{display:inline-flex;align-items:center;}
  .tb-accent svg{width:16px;height:16px;margin-right:5px;}
  .topbar .btn{display:inline-flex;align-items:center;gap:6px;}
  .topbar .btn svg{width:15px;height:15px;flex:0 0 auto;}
  #rb-font{min-width:118px;}
"""

_EDITOR_RIBBON_JS = """
/* Word-style ribbon behaviour: paragraph styles, point font sizes, and live
   active-state highlighting. Relies on cmd()/restoreRange()/dirty()/sheet from
   the main editor script (classic scripts share the global lexical scope). */
(function(){
  'use strict';
  window.rbStyle = function(v){ cmd('formatBlock', '<' + v + '>'); };
  window.rbSize = function(pt){
    if(!pt) return;
    restoreRange();
    // execCommand fontSize only accepts 1-7, so tag the run as size 7 then
    // rewrite those <font> nodes to a real point size (survives the sanitizer).
    document.execCommand('fontSize', false, '7');
    var marks = sheet.querySelectorAll('font[size="7"]');
    for(var i=0;i<marks.length;i++){ marks[i].removeAttribute('size'); marks[i].style.fontSize = pt + 'pt'; }
    dirty();
  };
  window.rbFont = function(v){ if(!v) return; cmd('fontName', v); };
  var STATE = [['b-bold','bold'],['b-italic','italic'],['b-underline','underline'],
               ['b-strike','strikeThrough'],['a-left','justifyLeft'],['a-center','justifyCenter'],
               ['a-right','justifyRight'],['a-just','justifyFull']];
  function sync(){
    var sel = document.getSelection();
    if(!sel || !sel.anchorNode || !sheet.contains(sel.anchorNode)) return;
    for(var i=0;i<STATE.length;i++){
      var el = document.getElementById(STATE[i][0]); if(!el) continue;
      var on = false; try{ on = document.queryCommandState(STATE[i][1]); }catch(e){}
      el.classList.toggle('active', on);
    }
  }
  document.addEventListener('selectionchange', sync);

  /* ── Track Changes: word-level redline of the current doc vs the original
        template. Deterministic (LCS), bounded, no network/AI. ── */
  function esc(s){ return (s + '').replace(/[&<>"]/g, function(c){
    return {'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]; }); }
  function wc(s){ var m = (s || '').match(/[^\\s]+/g); return m ? m.length : 0; }
  function tok(s){ return (s || '').match(/\\s+|[^\\s]+/g) || []; }
  function merge(ops){
    var r = [];
    for(var i=0;i<ops.length;i++){
      if(r.length && r[r.length-1][0] === ops[i][0]) r[r.length-1][1] += ops[i][1];
      else r.push([ops[i][0], ops[i][1]]);
    }
    return r;
  }
  function lcs(a, b, ops){
    var n = a.length, m = b.length, i, j;
    if(n === 0){ if(m) ops.push(['+', b.join('')]); return; }
    if(m === 0){ ops.push(['-', a.join('')]); return; }
    var dp = [];
    for(i=0;i<=n;i++) dp.push(new Int32Array(m + 1));
    for(i=n-1;i>=0;i--) for(j=m-1;j>=0;j--)
      dp[i][j] = (a[i] === b[j]) ? dp[i+1][j+1] + 1 : Math.max(dp[i+1][j], dp[i][j+1]);
    var x = 0, y = 0;
    while(x < n && y < m){
      if(a[x] === b[y]){ ops.push(['=', a[x]]); x++; y++; }
      else if(dp[x+1][y] >= dp[x][y+1]){ ops.push(['-', a[x]]); x++; }
      else { ops.push(['+', b[y]]); y++; }
    }
    while(x < n) ops.push(['-', a[x++]]);
    while(y < m) ops.push(['+', b[y++]]);
  }
  function diffWords(aStr, bStr){
    var a = tok(aStr), b = tok(bStr), start = 0;
    while(start < a.length && start < b.length && a[start] === b[start]) start++;
    var ea = a.length, eb = b.length;
    while(ea > start && eb > start && a[ea-1] === b[eb-1]){ ea--; eb--; }
    var am = a.slice(start, ea), bm = b.slice(start, eb), ops = [];
    if(start) ops.push(['=', a.slice(0, start).join('')]);
    if(am.length * bm.length > 1000000){   // guard: coarse replace on huge rewrites
      if(am.length) ops.push(['-', am.join('')]);
      if(bm.length) ops.push(['+', bm.join('')]);
    } else { lcs(am, bm, ops); }
    if(ea < a.length) ops.push(['=', a.slice(ea).join('')]);
    return merge(ops);
  }
  // Normalise HTML -> plain text the SAME way on BOTH sides of the diff (mirrors
  // server _plain_text_from_html). Previously base used server text but cur used
  // sheet.innerText: the two extractors tokenise whitespace/tables differently,
  // so the diff flagged nearly the whole document. One normaliser fixes that.
  function htmlUnescape(s){
    var t = document.createElement('textarea'); t.innerHTML = (s || ''); return t.value;
  }
  function plainText(raw){
    var s = raw || '';
    s = s.replace(/<(?:script|style)\\b[\\s\\S]*?<\\/(?:script|style)>/gi, '');
    s = s.replace(/<(?:br|\\/p|\\/div|\\/h[1-6]|\\/li|\\/tr|\\/table)\\s*\\/?>/gi, '\\n');
    s = s.replace(/<(?:p|div|h[1-6]|li|tr)\\b[^>]*>/gi, '\\n');
    s = s.replace(/<[^>]+>/g, '');
    s = htmlUnescape(s);
    s = s.replace(/[ \\t]+/g, ' ');
    s = s.replace(/ *\\n */g, '\\n');
    s = s.replace(/\\n{3,}/g, '\\n\\n');
    return s.replace(/^\\s+|\\s+$/g, '');
  }
  // Single source of truth for the diff: original template vs current document.
  // While reviewing, the sheet holds redline markup — diff against the stashed
  // real document instead, so counts never balloon to the whole doc mid-review.
  function docOps(){
    var base = (typeof ORIG_HTML === 'string') ? plainText(ORIG_HTML)
             : ((typeof ORIG_TEXT === 'string') ? ORIG_TEXT : '');
    var curHtml = (window._reviewing && window._reviewBackup != null)
                ? window._reviewBackup
                : ((typeof sheet !== 'undefined' && sheet) ? sheet.innerHTML : '');
    return diffWords(base, plainText(curHtml));
  }

  // exposed so the page-level togglePreview() can call it without re-implementing LCS
  window._diffStats = function(){
    var ops = docOps(), ins = 0, del = 0;
    for(var i = 0; i < ops.length; i++){
      if(ops[i][0] === '+') ins += wc(ops[i][1]);
      else if(ops[i][0] === '-') del += wc(ops[i][1]);
    }
    return {ins: ins, del: del};
  };

  // ── Inline redline: render the +/-/= ops as Word-style tracked changes ──
  // Block-level redline that PRESERVES the document's formatting: unchanged
  // blocks (paragraphs, headings, tables, signature images) are emitted with
  // their exact current HTML; only blocks that actually changed are shown as
  // tracked text. Fonts / bold / tables / signatures stay intact in review.
  function ntext(s){ return (s || '').replace(/\\s+/g, ' ').replace(/^\\s+|\\s+$/g, ''); }
  function blocksOf(html){
    var d = document.createElement('div'); d.innerHTML = html || '';
    var out = [], kids = d.children, i;
    for(i = 0; i < kids.length; i++){
      var t = ntext(kids[i].textContent);
      out.push({ html: kids[i].outerHTML, text: t, key: t || kids[i].outerHTML });
    }
    if(out.length === 0 && ntext(d.textContent) !== ''){
      var tt = ntext(d.textContent);
      out.push({ html: '<p>' + esc(d.textContent) + '</p>', text: tt, key: tt });
    }
    return out;
  }
  function blockLCS(a, b){
    var n = a.length, m = b.length, dp = [], i, j;
    for(i = 0; i <= n; i++) dp.push(new Int32Array(m + 1));
    for(i = n - 1; i >= 0; i--) for(j = m - 1; j >= 0; j--)
      dp[i][j] = (a[i].key === b[j].key) ? dp[i+1][j+1] + 1 : Math.max(dp[i+1][j], dp[i][j+1]);
    var ops = [], x = 0, y = 0;
    while(x < n && y < m){
      if(a[x].key === b[y].key){ ops.push(['=', y]); x++; y++; }
      else if(dp[x+1][y] >= dp[x][y+1]){ ops.push(['-', x]); x++; }
      else { ops.push(['+', y]); y++; }
    }
    while(x < n) ops.push(['-', x++]);
    while(y < m) ops.push(['+', y++]);
    return ops;
  }
  function injectClass(html, cls){
    return (html || '').replace(/^(\\s*<[a-zA-Z][^>]*?)(\\s*\\/?>)/, function(m, p1, p2){
      return /\\sclass\\s*=/.test(p1)
        ? p1.replace(/class\\s*=\\s*"([^"]*)"/, 'class="$1 ' + cls + '"') + p2
        : p1 + ' class="' + cls + '"' + p2;
    });
  }
  // Re-wrap inner content in the block's ORIGINAL tag + attributes so the
  // paragraph keeps its font-size / alignment / indent in review mode — only the
  // words inside change. Prevents "the format changes when tracking is on".
  function _reblock(html, inner, cls){
    var m = (html || '').match(/^(\\s*<([a-zA-Z][a-z0-9]*)\\b[^>]*>)([\\s\\S]*)(<\\/\\2>\\s*)$/);
    if(!m) return '<p class="' + cls + '">' + inner + '</p>';
    return injectClass(m[1], cls) + inner + m[4];
  }
  function wordRedline(aText, bText){
    var ops = diffWords(aText, bText), s = '';
    for(var i = 0; i < ops.length; i++){
      var t = ops[i][0], raw = ops[i][1], safe = esc(raw), solid = raw.replace(/\\s+/g, '') !== '';
      if(t === '+' && solid) s += '<ins class="rl-ins">' + safe + '</ins>';
      else if(t === '-' && solid) s += '<del class="rl-del">' + safe + '</del>';
      else s += safe;
    }
    return s;
  }
  function insBlock(b){
    // empty-text block = signature image / media: keep its HTML so it survives review
    return b.text === '' ? injectClass(b.html, 'rl-chg rl-newblk')
                         : _reblock(b.html, wordRedline('', b.text), 'rl-chg');
  }
  function delBlock(b){ return _reblock(b.html, '<del class="rl-del">' + esc(b.text) + '</del>', 'rl-chg'); }
  function renderRedline(){
    var orig = blocksOf(typeof ORIG_HTML === 'string' ? ORIG_HTML : ''),
        cur  = blocksOf((typeof sheet !== 'undefined' && sheet) ? sheet.innerHTML : '');
    var ops = blockLCS(orig, cur), html = '', i = 0, k;
    while(i < ops.length){
      if(ops[i][0] === '='){ html += cur[ops[i][1]].html; i++; continue; }   // unchanged -> exact formatting
      var dels = [], inss = [];
      while(i < ops.length && ops[i][0] === '-'){ dels.push(orig[ops[i][1]]); i++; }
      while(i < ops.length && ops[i][0] === '+'){ inss.push(cur[ops[i][1]]); i++; }
      // Pair each edited paragraph with its replacement and diff at WORD level, so
      // only the words that changed are marked — never the whole paragraph. Any
      // leftover blocks are genuine paragraph adds / removes.
      var pairN = Math.min(dels.length, inss.length);
      for(k = 0; k < pairN; k++){
        if(dels[k].text !== '' && inss[k].text !== '')
          html += _reblock(inss[k].html, wordRedline(dels[k].text, inss[k].text), 'rl-chg');
        else { html += delBlock(dels[k]); html += insBlock(inss[k]); }
      }
      for(k = pairN; k < dels.length; k++) html += delBlock(dels[k]);
      for(k = pairN; k < inss.length; k++) html += insBlock(inss[k]);
    }
    return html || '<p style="color:#64748b;">No changes vs the original template.</p>';
  }

  // Track Changes is always active — shows inline redline of current doc vs
  // original template. Editing temporarily exits review mode; blur/save re-enters.
  window._reviewing = false;
  window._reviewBackup = null;

  function _enterReview(){
    if(window._reviewing) return;
    window._reviewBackup = sheet.innerHTML;
    sheet.classList.add('review');
    sheet.setAttribute('contenteditable', 'false');
    sheet.innerHTML = renderRedline();
    window._reviewing = true;
  }
  function _exitReview(){
    if(!window._reviewing) return;
    if(window._reviewBackup != null) sheet.innerHTML = window._reviewBackup;
    window._reviewBackup = null;
    window._reviewing = false;
    sheet.classList.remove('review');
    if(typeof SIGNED === 'undefined' || !SIGNED) sheet.setAttribute('contenteditable', 'true');
  }

  // Clicking the document exits review mode so the user can edit
  sheet.addEventListener('click', function(){
    if(window._reviewing && (typeof SIGNED === 'undefined' || !SIGNED)) _exitReview();
  });
  // Losing focus re-enters review mode automatically
  sheet.addEventListener('blur', function(){
    if(!window._reviewing) setTimeout(_enterReview, 400);
  }, true);

  // Auto-activate track changes on page load (after document is populated)
  window.addEventListener('load', function(){
    if(typeof SIGNED !== 'undefined' && SIGNED) return;
    setTimeout(_enterReview, 300);
  });

  // Allow saveDoc to temporarily exit/re-enter review around the save
  window._tcEnterReview = _enterReview;
  window._tcExitReview = _exitReview;
})();
"""


_SIG_OVERLAY_CSS = """
  /* signature overlays placed on the PDF preview page (Adobe-style) */
  #pv-pages .pg-wrap{position:relative;width:94%;max-width:740px;margin:0 auto;
    background:#fff;aspect-ratio:595/842;box-shadow:0 2px 12px rgba(0,0,0,.4);}
  #pv-pages .pg-wrap img.pg{width:100%;display:block;box-shadow:none;}
  .sig-ov{position:absolute;cursor:move;touch-action:none;outline:1.5px dashed transparent;
    outline-offset:2px;transition:outline-color .1s;}
  .sig-ov:hover,.sig-ov.drag{outline-color:#1a56db;}
  .sig-ov img{display:block;width:100%;height:auto;pointer-events:none;user-select:none;}
  .sig-ov-del{position:absolute;top:-10px;right:-10px;width:20px;height:20px;line-height:16px;
    border-radius:50%;border:2px solid #fff;background:#dc2626;color:#fff;font-size:11px;
    cursor:pointer;padding:0;display:none;}
  .sig-ov-grip{position:absolute;right:-7px;bottom:-7px;width:14px;height:14px;border-radius:3px;
    background:#1a56db;border:2px solid #fff;cursor:nwse-resize;touch-action:none;display:none;}
  .sig-ov:hover .sig-ov-del,.sig-ov:hover .sig-ov-grip,
  .sig-ov.drag .sig-ov-del,.sig-ov.drag .sig-ov-grip{display:block;}

  /* full-screen placement stage */
  .pv.sigmode{position:fixed;inset:0;z-index:9000;display:flex;flex-direction:column;background:#3a3d40;}
  .pv.sigmode .pvhead{display:none;}
  .pv.sigmode #pv-pages{flex:1;overflow:auto;padding:18px 0 60px;}
  .pv.sigmode .pg-wrap{max-width:840px;margin-bottom:16px;}
  .sig-bar{display:none;background:#1f2937;color:#fff;padding:9px 16px;align-items:center;gap:14px;
    flex-shrink:0;box-shadow:0 2px 10px rgba(0,0,0,.35);flex-wrap:wrap;}
  .pv.sigmode .sig-bar{display:flex;}
  .sig-bar-t{font-weight:700;font-size:13.5px;}
  .sig-pal{display:flex;align-items:center;min-height:40px;}
  .sig-chip{height:40px;max-width:180px;background:#fff;border-radius:6px;padding:3px 6px;cursor:pointer;
    border:2px solid #60a5fa;}
  .sig-pal-empty{font-size:12px;color:#9ca3af;}
  .sig-bar-btn{background:#374151;color:#fff;border:1px solid #4b5563;border-radius:7px;padding:7px 12px;
    font-size:12.5px;cursor:pointer;}
  .sig-bar-btn:hover{background:#4b5563;}
  .sig-bar-hint{font-size:11.5px;color:#9ca3af;margin-left:auto;}
  .sig-bar-done{background:#16a34a;color:#fff;border:none;border-radius:7px;padding:8px 18px;
    font-size:12.5px;font-weight:700;cursor:pointer;}
  .sig-bar-done:hover{background:#15803d;}
  /* create-signature modal */
  .sig-modal{display:flex;position:fixed;inset:0;z-index:9500;background:rgba(15,23,42,.6);
    align-items:center;justify-content:center;}
  .sig-modal-box{background:#fff;border-radius:14px;width:480px;max-width:94vw;padding:20px 22px;
    box-shadow:0 24px 60px rgba(0,0,0,.4);}
  .sig-modal-box h3{margin:0 0 12px;font-size:16px;color:#111;}
  .sig-tabs{display:flex;gap:6px;margin-bottom:12px;}
  .sig-tab{flex:1;padding:8px;border:1px solid #d4dae6;background:#f6f8fc;border-radius:8px;cursor:pointer;
    font-size:12.5px;font-weight:600;color:#3a4661;text-align:center;}
  .sig-tab.on{background:#1a56db;color:#fff;border-color:#1a56db;}
  .sig-panel{display:none;} .sig-panel.on{display:block;}
  #sig-canvas{width:100%;height:150px;border:1px dashed #c2ccdc;border-radius:8px;background:#fcfdff;
    touch-action:none;cursor:crosshair;}
  #sig-type{width:100%;padding:10px 12px;border:1px solid #c2ccdc;border-radius:8px;font-size:15px;}
  #sig-type-prev{height:90px;display:flex;align-items:center;justify-content:center;font-size:38px;
    font-family:'Brush Script MT','Segoe Script',cursive;color:#111;border:1px solid #eef1f6;
    border-radius:8px;margin-top:10px;}
  .sig-modal-actions{display:flex;gap:10px;justify-content:flex-end;margin-top:16px;}
  .sig-mbtn{padding:9px 18px;border-radius:8px;font-size:13px;font-weight:700;cursor:pointer;border:none;}
  .sig-mbtn.ok{background:#16a34a;color:#fff;} .sig-mbtn.x{background:#eef1f6;color:#374151;}
"""

_SIG_OVERLAY_JS = """
/* Adobe-style signature overlays: signature images placed ON the PDF preview
   page (never in the editable text). Persisted as fractional page coordinates
   and flattened into the final PDF server-side (see stamp_overlays). Shares the
   editor's global scope: BASE/OID/DT/TOK/SIG_OVERLAYS/togglePreview. */
(function(){
  'use strict';
  var overlays = (typeof SIG_OVERLAYS !== 'undefined' && Array.isArray(SIG_OVERLAYS)) ? SIG_OVERLAYS.slice() : [];
  var saveT = null;
  var _sigData = overlays.length ? overlays[overlays.length - 1].image : '';   // reuse the last-placed signature
  var bar = null, modal = null, drawing = false, dctx = null, dlast = null;

  function _save(){
    clearTimeout(saveT);
    saveT = setTimeout(function(){
      var el = document.getElementById('saved');
      if(el){ el.textContent = 'Saving\\u2026'; el.style.color = '#fbbf24'; }
      fetch(BASE + '/save/' + OID + '/' + DT + '/' + TOK, {
        method: 'POST', headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({ signatures_overlay: overlays })
      }).then(function(r){
        if(el){ el.textContent = (r && r.ok) ? 'Saved \\u2713' : 'Save failed';
                el.style.color = (r && r.ok) ? '#86efac' : '#fca5a5'; }
      }).catch(function(){ if(el){ el.textContent = 'Save failed'; el.style.color = '#fca5a5'; } });
    }, 350);
  }

  function _pageImg(wrap){ return wrap.querySelector('img.pg'); }

  // Rebuild overlays[] from the live DOM (fractions of each page), then persist.
  function _commit(){
    var out = [], wraps = document.querySelectorAll('#pv-pages .pg-wrap');
    for(var i=0;i<wraps.length;i++){
      var pr = wraps[i].getBoundingClientRect();   // measure the WRAP (always sized via aspect-ratio) so a not-yet-loaded image never drops the overlay
      if(!pr.width || !pr.height) continue;
      var ovs = wraps[i].querySelectorAll('.sig-ov');
      for(var j=0;j<ovs.length;j++){
        var er = ovs[j].getBoundingClientRect();
        out.push({
          page: i,
          x: (er.left - pr.left) / pr.width,
          y: (er.top  - pr.top ) / pr.height,
          w: er.width / pr.width,
          h: er.height / pr.height,
          image: ovs[j].getAttribute('data-img')
        });
      }
    }
    overlays = out;
    _save();
  }

  // which page is under the pointer (so a signature can be dragged page-to-page)
  function _pageUnder(x, y){
    var wraps = document.querySelectorAll('#pv-pages .pg-wrap');
    for(var i=0;i<wraps.length;i++){
      var r = wraps[i].getBoundingClientRect();
      if(x>=r.left && x<=r.right && y>=r.top && y<=r.bottom) return wraps[i];
    }
    return null;
  }

  function _bindDrag(el){
    var d = null;
    el.addEventListener('pointerdown', function(e){
      if(e.target.classList.contains('sig-ov-grip') || e.target.classList.contains('sig-ov-del')) return;
      e.preventDefault();
      try{ el.setPointerCapture(e.pointerId); }catch(x){}
      var er = el.getBoundingClientRect();
      d = { dx: e.clientX - er.left, dy: e.clientY - er.top };
      el.classList.add('drag');
    });
    el.addEventListener('pointermove', function(e){
      if(!d) return;
      var page = _pageUnder(e.clientX, e.clientY) || el.parentNode;
      if(page !== el.parentNode) page.appendChild(el);          // move across pages
      var pr = page.getBoundingClientRect();
      var left = (e.clientX - d.dx - pr.left) / pr.width;
      var top  = (e.clientY - d.dy - pr.top ) / pr.height;
      left = Math.max(0, Math.min(1 - el.offsetWidth  / pr.width,  left));
      top  = Math.max(0, Math.min(1 - el.offsetHeight / pr.height, top));
      el.style.left = (left*100) + '%';
      el.style.top  = (top*100) + '%';
    });
    function up(e){ if(!d) return; d = null; el.classList.remove('drag');
      try{ el.releasePointerCapture(e.pointerId); }catch(x){} _commit(); }
    el.addEventListener('pointerup', up);
    el.addEventListener('pointercancel', up);
  }

  function _bindResize(el, grip){
    var r = null;
    grip.addEventListener('pointerdown', function(e){
      e.preventDefault(); e.stopPropagation();
      try{ grip.setPointerCapture(e.pointerId); }catch(x){}
      r = { x: e.clientX, w: el.offsetWidth, pr: el.parentNode.getBoundingClientRect() };
    });
    grip.addEventListener('pointermove', function(e){
      if(!r) return;
      var nw = Math.max(24, Math.min(r.pr.width, r.w + (e.clientX - r.x)));
      el.style.width = (nw / r.pr.width * 100) + '%';   // height follows the image aspect ratio
    });
    function up(e){ if(!r) return; r = null;
      try{ grip.releasePointerCapture(e.pointerId); }catch(x){} _commit(); }
    grip.addEventListener('pointerup', up);
    grip.addEventListener('pointercancel', up);
  }

  function _makeOverlay(wrap, ov){
    var el = document.createElement('div');
    el.className = 'sig-ov';
    el.setAttribute('data-img', ov.image);
    el.style.left  = (ov.x*100) + '%';
    el.style.top   = (ov.y*100) + '%';
    el.style.width = (ov.w*100) + '%';
    var im = document.createElement('img'); im.src = ov.image; im.alt = 'signature';
    el.appendChild(im);
    var del = document.createElement('button');
    del.className = 'sig-ov-del'; del.type = 'button'; del.textContent = '\\u2715';
    del.title = 'Remove signature';
    del.addEventListener('click', function(e){ e.stopPropagation(); el.remove(); _commit(); });
    el.appendChild(del);
    var grip = document.createElement('div'); grip.className = 'sig-ov-grip';
    el.appendChild(grip);
    _bindDrag(el);
    _bindResize(el, grip);
    wrap.appendChild(el);
    return el;
  }

  // Re-paint overlay DOM from overlays[] (called after each preview refresh).
  window.renderSigOverlays = function(){
    var wraps = document.querySelectorAll('#pv-pages .pg-wrap');
    if(!wraps.length) return;
    for(var i=0;i<wraps.length;i++){
      var old = wraps[i].querySelectorAll('.sig-ov');
      for(var k=0;k<old.length;k++) old[k].remove();
    }
    for(var j=0;j<overlays.length;j++){
      var o = overlays[j];
      var w = (o.page >= 0 && o.page < wraps.length) ? wraps[o.page] : wraps[0];
      if(w) _makeOverlay(w, o);
    }
  };

  // ── full-screen placement stage ──────────────────────────────────────────
  window.placeSignature = function(){
    var pv = document.querySelector('.pv');
    if(pv && !pv.classList.contains('open') && typeof togglePreview === 'function') togglePreview();
    if(!pv) return;
    pv.classList.add('sigmode');
    _ensureBar();
    _refreshPalette();
    if(!_sigData) _openCreate();          // no signature yet -> ask for one
  };
  function _exitPlacement(){
    var pv = document.querySelector('.pv'); if(pv) pv.classList.remove('sigmode');
    if(bar) bar.style.display = 'none';
    _commit();
  }
  function _ensureBar(){
    var pv = document.querySelector('.pv'); if(!pv) return;
    if(bar){ bar.style.display = 'flex'; return; }
    bar = document.createElement('div'); bar.className = 'sig-bar';
    bar.innerHTML =
      '<span class="sig-bar-t">Place your signature</span>' +
      '<div id="sig-palette" class="sig-pal"></div>' +
      '<button type="button" class="sig-bar-btn" id="sig-create-btn">＋ Create / change</button>' +
      '<span class="sig-bar-hint">Click your signature to drop it, then drag it onto any page</span>' +
      '<button type="button" class="sig-bar-done" id="sig-done-btn">Done</button>';
    pv.insertBefore(bar, pv.firstChild);
    document.getElementById('sig-create-btn').addEventListener('click', _openCreate);
    document.getElementById('sig-done-btn').addEventListener('click', _exitPlacement);
    bar.style.display = 'flex';
  }
  function _refreshPalette(){
    var p = document.getElementById('sig-palette'); if(!p) return;
    if(_sigData){
      p.innerHTML = '<img class="sig-chip" id="sig-chip" src="' + _sigData + '" alt="signature" title="Click to drop on the page">';
      document.getElementById('sig-chip').addEventListener('click', _placeFromPalette);
    } else {
      p.innerHTML = '<span class="sig-pal-empty">No signature yet — click Create</span>';
    }
  }
  function _visiblePage(){
    var pv = document.querySelector('.pv'), wraps = document.querySelectorAll('#pv-pages .pg-wrap');
    if(!wraps.length) return null;
    var mid = pv.getBoundingClientRect().top + pv.clientHeight/2, best = wraps[0], bd = 1e9;
    for(var i=0;i<wraps.length;i++){ var r = wraps[i].getBoundingClientRect();
      var dd = Math.abs((r.top + r.bottom)/2 - mid); if(dd < bd){ bd = dd; best = wraps[i]; } }
    return best;
  }
  function _placeFromPalette(){
    if(!_sigData) return;
    var wrap = _visiblePage(); if(!wrap) return;
    var im = new Image();
    im.onload = function(){
      var pr = wrap.getBoundingClientRect();
      var ar = (im.width/im.height) || 3, pw = pr.width||600, ph = pr.height||850;
      var wFrac = Math.min(0.28, 180/pw), hFrac = (wFrac*pw/ar)/ph;
      if(hFrac > 0.11){ hFrac = 0.11; wFrac = (hFrac*ph*ar)/pw; }   // cap height so it never covers the cell
      _makeOverlay(wrap, { x: 0.5 - wFrac/2, y: 0.42, w: wFrac, h: hFrac, image: _sigData });
      _commit();
    };
    im.src = _sigData;
  }

  // ── create-signature modal: upload only ─────────────────────────────────
  function _openCreate(){
    if(modal){ modal.style.display = 'flex'; return; }
    modal = document.createElement('div'); modal.className = 'sig-modal';
    modal.innerHTML =
      '<div class="sig-modal-box">' +
        '<h3>Upload your signature</h3>' +
        '<div class="sig-panel on" data-panel="upload">' +
          '<p style="font-size:13px;color:#374151;margin:0 0 12px;">Upload a clear PNG or JPG image of your signature.</p>' +
          '<input type="file" id="sig-upload" accept=".png,.jpg,.jpeg" style="display:block;margin-bottom:8px;">' +
          '<div id="sig-upload-prev" style="display:none;border:1px solid #e5e9f2;border-radius:8px;padding:8px;background:#f8fafc;margin-bottom:8px;">' +
            '<img id="sig-upload-img" style="max-height:80px;max-width:100%;border-radius:4px;" alt="preview">' +
          '</div>' +
          '<p style="font-size:11.5px;color:#64748b;margin:0;">PNG or JPG · Max 5 MB</p>' +
        '</div>' +
        '<div class="sig-modal-actions">' +
          '<button type="button" class="sig-mbtn x" id="sig-cancel">Cancel</button>' +
          '<button type="button" class="sig-mbtn ok" id="sig-use">Use signature</button>' +
        '</div>' +
      '</div>';
    document.body.appendChild(modal);
    document.getElementById('sig-upload').addEventListener('change', function(){
      var f = this.files && this.files[0];
      var pv = document.getElementById('sig-upload-prev');
      var pi = document.getElementById('sig-upload-img');
      if(f){ pv.style.display = 'block'; pi.src = URL.createObjectURL(f); } else { pv.style.display = 'none'; }
    });
    document.getElementById('sig-cancel').addEventListener('click', function(){ modal.style.display = 'none'; });
    document.getElementById('sig-use').addEventListener('click', _useSignature);
    modal.addEventListener('click', function(e){ if(e.target === modal) modal.style.display = 'none'; });
  }
  function _compress(img){
    var maxSide = 380, sc = Math.min(1, maxSide / Math.max(img.width || 1, img.height || 1));
    var c = document.createElement('canvas');
    c.width = Math.max(1, Math.round((img.width||1)*sc));
    c.height = Math.max(1, Math.round((img.height||1)*sc));
    c.getContext('2d').drawImage(img, 0, 0, c.width, c.height);
    var data = c.toDataURL('image/png');
    if(data.length > 520000){
      var c2 = document.createElement('canvas'); c2.width = c.width; c2.height = c.height;
      var x = c2.getContext('2d'); x.fillStyle = '#fff'; x.fillRect(0,0,c2.width,c2.height);
      x.drawImage(c, 0, 0); data = c2.toDataURL('image/jpeg', 0.82);
    }
    return data;
  }
  function _useSignature(){
    var f = document.getElementById('sig-upload').files && document.getElementById('sig-upload').files[0];
    if(!f){ alert('Please choose a signature image file (PNG or JPG).'); return; }
    if(f.size > 5*1024*1024){ alert('Image too large — max 5 MB.'); return; }
    var img = new Image();
    img.onload = function(){ _setSig(_compress(img)); };
    img.onerror = function(){ alert('Could not read that image.'); };
    img.src = URL.createObjectURL(f);
  }
  function _setSig(data){ _sigData = data; if(modal) modal.style.display = 'none'; _refreshPalette(); _placeFromPalette(); }

  // kept for the topbar file input (legacy entry point)
  window._sigOverlayFromFile = function(input){
    var f = input.files && input.files[0]; if(!f) return;
    if(f.size > 5*1024*1024){ alert('Signature image too large — max 5 MB.'); input.value = ''; return; }
    var img = new Image();
    img.onload = function(){
      var pv = document.querySelector('.pv');
      if(pv && !pv.classList.contains('sigmode')) window.placeSignature();
      _setSig(_compress(img)); input.value = ''; };
    img.onerror = function(){ alert('Could not read that image file.'); input.value = ''; };
    img.src = URL.createObjectURL(f);
  };
})();
"""


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

    from app.services.pdf_documents import extract_editable_html as _extract_orig
    _orig_html = _extract_orig(doc_type, _is_overseas(rec), data.get("replacements", []))
    orig_text_js = json.dumps(_plain_text_from_html(_orig_html), ensure_ascii=False).replace("</", "<\\/")
    orig_html_js = json.dumps(_orig_html, ensure_ascii=False).replace("</", "<\\/")
    html_json = json.dumps(doc_html, ensure_ascii=False).replace("</", "<\\/")
    comments_json = json.dumps(comments, ensure_ascii=False).replace("</", "<\\/")
    overlays_json = json.dumps(data.get("signatures_overlay") or [], ensure_ascii=False).replace("</", "<\\/")
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
<script>
window.onerror = function(msg, src, line, col, err) {{
  var d = document.getElementById('_js_err_banner');
  if (!d) {{
    d = document.createElement('div');
    d.id = '_js_err_banner';
    d.style.cssText = 'position:fixed;bottom:0;left:0;right:0;background:#dc2626;color:#fff;padding:10px 14px;font-size:12px;z-index:9999;word-break:break-all;';
    document.body && document.body.appendChild(d);
  }}
  d.textContent = 'JS Error: ' + msg + ' (line ' + line + ')';
}};
</script>
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
  .pv{{display:none;flex-direction:column;min-width:0;flex:1;background:#525659;}}
  .pv.open{{display:flex;}}
  .pv .pvhead{{background:#2d3033;padding:5px 10px;display:flex;justify-content:space-between;align-items:center;flex-shrink:0;gap:8px;}}
  #pv-diff-stats{{display:flex;gap:5px;align-items:center;}}
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
  /* ── Change-tracking mark highlights ── */
  [data-cmt]{{border-radius:3px;transition:background .3s,outline .2s;}}
  .chg-p1{{background:rgba(37,99,235,0.11)!important;border-left:3px solid #2563eb!important;padding-left:6px!important;}}
  .chg-p2{{background:rgba(245,158,11,0.14)!important;border-left:3px solid #f59e0b!important;padding-left:6px!important;}}
  .chg-accepted{{background:rgba(22,163,74,0.09)!important;border-left:3px solid #16a34a!important;padding-left:6px!important;}}
  .chg-rejected{{background:rgba(239,68,68,0.09)!important;border-left:3px solid #dc2626!important;padding-left:6px!important;}}
  .jump-flash{{animation:cmtFlash 1.7s ease;}}
  @keyframes cmtFlash{{0%,100%{{outline:none;}}25%,65%{{outline:3px solid #f59e0b;outline-offset:3px;}}}}
  /* ── Floating comment bubbles ── */
  .chg-bubble{{position:fixed;width:228px;background:#fff;border:1px solid #e2e8f0;
    border-radius:10px;padding:10px 12px;
    box-shadow:0 4px 18px rgba(15,23,42,.13);font-size:12px;font-family:'Segoe UI',Arial,sans-serif;
    z-index:45;cursor:pointer;display:none;transition:box-shadow .15s,border-color .15s;}}
  .chg-bubble:hover,.chg-bubble.bub-focus{{box-shadow:0 7px 26px rgba(37,99,235,.22);border-color:#93c5fd;}}
  .bub-rej-box textarea{{width:100%;min-height:58px;padding:6px 8px;border:1px solid #fca5a5;
    border-radius:6px;font-size:11.5px;font-family:inherit;resize:vertical;margin-top:6px;}}
</style>
<style>{_EDITOR_RIBBON_CSS}</style>
<style>{_SIG_OVERLAY_CSS}</style></head>
<body>
<div class="work">
{locked_banner}
<div class="topbar">
  <span class="sub">{label} — {lead.business_name}</span>
  <span class="grow"></span>
  <span id="saved">Saved ✓</span>
  <button class="btn b-line" onclick="toggleSidebar()" title="Comments"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M21 15a2 2 0 0 1-2 2H7l-4 4V5a2 2 0 0 1 2-2h14a2 2 0 0 1 2 2z"/></svg><span id="cmt-n">{len([c for c in comments if not c.get("done")])}</span></button>
  <button class="btn b-line" onclick="openVersions()" title="Version history"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="9"/><polyline points="12 7 12 12 15 14"/></svg></button>
  <button class="btn b-line" id="btn-pv" onclick="togglePreview()" title="Toggle PDF preview with change summary"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M2 12s3.5-7 10-7 10 7 10 7-3.5 7-10 7-10-7-10-7z"/><circle cx="12" cy="12" r="3"/></svg>Preview</button>
  <button class="btn b-line" onclick="placeSignature()" title="Place a signature on the page — floats on top, never changes the text"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M3 19c3-1 4-7 6-7s2 4 4 4 2-9 4-9"/><line x1="3" y1="21" x2="21" y2="21"/></svg>Place Signature</button>
  <input type="file" id="sig-ov-file" accept=".png,.jpg,.jpeg" style="display:none" onchange="_sigOverlayFromFile(this)">
  <a class="btn b-line" target="_blank" id="dl-btn" href="{base}/pdf/{onboarding_id}/{doc_type}/{token}" title="Download PDF"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"/><polyline points="7 10 12 15 17 10"/><line x1="12" y1="15" x2="12" y2="3"/></svg>PDF</a>
  <button class="btn b-line" onclick="revertTemplate()" title="Restore original template"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M3 12a9 9 0 1 0 3-6.7L3 8"/><polyline points="3 3 3 8 8 8"/></svg>Original</button>
  <span id="p1-cmt-count" style="display:none;font-size:11px;background:#dbeafe;color:#1e40af;border-radius:6px;padding:3px 10px;font-weight:700;"></span>
  <button class="btn b-green" onclick="sendForReview()"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><line x1="22" y1="2" x2="11" y2="13"/><polygon points="22 2 15 22 11 13 2 9 22 2"/></svg>Send for Review</button>
</div>
<div class="toolbar" id="ribbon" onmousedown="saveRange()">
  <div class="rb-lbl-grp">
    <div class="rb-row">
      <button class="tb" onmousedown="return false" onclick="cmd('undo')" title="Undo (Ctrl+Z)"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M9 14 4 9l5-5"/><path d="M4 9h11a5 5 0 0 1 0 10h-3"/></svg></button>
      <button class="tb" onmousedown="return false" onclick="cmd('redo')" title="Redo (Ctrl+Y)"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="m15 14 5-5-5-5"/><path d="M20 9H9a5 5 0 0 0 0 10h3"/></svg></button>
    </div><span class="rb-cap">Undo</span>
  </div>
  <div class="rb-lbl-grp">
    <div class="rb-row">
      <select class="rb-sel" id="rb-font" title="Font" onmousedown="saveRange()" onchange="rbFont(this.value)">
        <option value="Calibri, sans-serif" selected>Calibri</option>
        <option value="Arial, sans-serif">Arial</option>
        <option value="Georgia, serif">Georgia</option>
        <option value="'Times New Roman', serif">Times New Roman</option>
        <option value="'Courier New', monospace">Courier New</option>
        <option value="Verdana, sans-serif">Verdana</option>
        <option value="'Segoe UI', sans-serif">Segoe UI</option>
      </select>
    </div><span class="rb-cap">Font</span>
  </div>
  <div class="rb-lbl-grp">
    <div class="rb-row">
      <select class="rb-sel" id="rb-style" title="Paragraph style" onmousedown="saveRange()" onchange="rbStyle(this.value)">
        <option value="p" selected>Normal</option>
        <option value="h3">Heading 3</option>
        <option value="h2">Heading 2</option>
        <option value="h1">Heading 1</option>
      </select>
    </div><span class="rb-cap">Styles</span>
  </div>
  <div class="rb-lbl-grp">
    <div class="rb-row">
      <select class="rb-sel rb-size" id="rb-size" title="Font size (pt)" onmousedown="saveRange()" onchange="rbSize(this.value)">
        <option>8</option><option>9</option><option>10</option><option selected>10.5</option>
        <option>11</option><option>12</option><option>14</option><option>16</option>
        <option>18</option><option>20</option><option>24</option><option>28</option><option>36</option>
      </select>
      <button class="tb" id="b-bold" style="font-weight:800;" onmousedown="return false" onclick="cmd('bold')" title="Bold (Ctrl+B)">B</button>
      <button class="tb" id="b-italic" style="font-style:italic;" onmousedown="return false" onclick="cmd('italic')" title="Italic (Ctrl+I)">I</button>
      <button class="tb" id="b-underline" style="text-decoration:underline;" onmousedown="return false" onclick="cmd('underline')" title="Underline (Ctrl+U)">U</button>
      <button class="tb" id="b-strike" style="text-decoration:line-through;" onmousedown="return false" onclick="cmd('strikeThrough')" title="Strikethrough">S</button>
      <label class="tb tb-color" title="Font colour">A<input type="color" value="#111111" onmousedown="saveRange()" oninput="cmd('foreColor',this.value)"></label>
      <label class="tb tb-color" title="Highlight colour"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="m9 11-6 6v3h9l3-3"/><path d="m22 12-4.6 4.6a1.9 1.9 0 0 1-2.7 0l-5.3-5.3a1.9 1.9 0 0 1 0-2.7L14 4"/></svg><input type="color" value="#ffe066" onmousedown="saveRange()" oninput="cmd('hiliteColor',this.value)"></label>
      <button class="tb" onmousedown="return false" onclick="cmd('hiliteColor','transparent')" title="No highlight"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="9"/><line x1="6" y1="6" x2="18" y2="18"/></svg></button>
    </div><span class="rb-cap">Format</span>
  </div>
  <div class="rb-lbl-grp">
    <div class="rb-row">
      <button class="tb" id="a-left" onmousedown="return false" onclick="cmd('justifyLeft')" title="Align left"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><line x1="3" y1="6" x2="21" y2="6"/><line x1="3" y1="12" x2="14" y2="12"/><line x1="3" y1="18" x2="18" y2="18"/></svg></button>
      <button class="tb" id="a-center" onmousedown="return false" onclick="cmd('justifyCenter')" title="Center"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><line x1="3" y1="6" x2="21" y2="6"/><line x1="7" y1="12" x2="17" y2="12"/><line x1="5" y1="18" x2="19" y2="18"/></svg></button>
      <button class="tb" id="a-right" onmousedown="return false" onclick="cmd('justifyRight')" title="Align right"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><line x1="3" y1="6" x2="21" y2="6"/><line x1="10" y1="12" x2="21" y2="12"/><line x1="6" y1="18" x2="21" y2="18"/></svg></button>
      <button class="tb" id="a-just" onmousedown="return false" onclick="cmd('justifyFull')" title="Justify"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><line x1="3" y1="6" x2="21" y2="6"/><line x1="3" y1="12" x2="21" y2="12"/><line x1="3" y1="18" x2="21" y2="18"/></svg></button>
      <button class="tb" onmousedown="return false" onclick="cmd('insertUnorderedList')" title="Bulleted list"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><line x1="9" y1="6" x2="21" y2="6"/><line x1="9" y1="12" x2="21" y2="12"/><line x1="9" y1="18" x2="21" y2="18"/><circle cx="4" cy="6" r="1.4" fill="currentColor" stroke="none"/><circle cx="4" cy="12" r="1.4" fill="currentColor" stroke="none"/><circle cx="4" cy="18" r="1.4" fill="currentColor" stroke="none"/></svg></button>
      <button class="tb" onmousedown="return false" onclick="cmd('insertOrderedList')" title="Numbered list"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><line x1="10" y1="6" x2="21" y2="6"/><line x1="10" y1="12" x2="21" y2="12"/><line x1="10" y1="18" x2="21" y2="18"/><text x="2" y="8" font-size="7" stroke="none" fill="currentColor">1</text><text x="2" y="14" font-size="7" stroke="none" fill="currentColor">2</text><text x="2" y="20" font-size="7" stroke="none" fill="currentColor">3</text></svg></button>
      <button class="tb" onmousedown="return false" onclick="cmd('outdent')" title="Decrease indent"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><line x1="3" y1="6" x2="21" y2="6"/><line x1="11" y1="12" x2="21" y2="12"/><line x1="3" y1="18" x2="21" y2="18"/><polyline points="7 9 3 12 7 15"/></svg></button>
      <button class="tb" onmousedown="return false" onclick="cmd('indent')" title="Increase indent"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><line x1="3" y1="6" x2="21" y2="6"/><line x1="11" y1="12" x2="21" y2="12"/><line x1="3" y1="18" x2="21" y2="18"/><polyline points="3 9 7 12 3 15"/></svg></button>
    </div><span class="rb-cap">Paragraph</span>
  </div>
  <div class="rb-lbl-grp">
    <div class="rb-row">
      <button class="tb" onmousedown="return false" onclick="insertNote()" title="Insert note"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M14 3v5h5"/><path d="M17 21H7a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h7l5 5v11a2 2 0 0 1-2 2z"/><line x1="9" y1="13" x2="15" y2="13"/><line x1="9" y1="17" x2="13" y2="17"/></svg></button>
      <button class="tb" onmousedown="return false" onclick="insertClause()" title="Add a clause / paragraph"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><line x1="4" y1="6" x2="20" y2="6"/><line x1="4" y1="12" x2="14" y2="12"/><line x1="4" y1="18" x2="14" y2="18"/><line x1="19" y1="13" x2="19" y2="21"/><line x1="15" y1="17" x2="23" y2="17"/></svg></button>
      <button class="tb" onmousedown="return false" onclick="findReplace()" title="Find &amp; replace"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><circle cx="11" cy="11" r="7"/><line x1="21" y1="21" x2="16.65" y2="16.65"/></svg></button>
      <button class="tb" onmousedown="return false" onclick="cmd('removeFormat')" title="Clear formatting"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M4 7V4h15"/><path d="M9 4 6 20"/><line x1="5" y1="20" x2="11" y2="20"/><line x1="14" y1="14" x2="21" y2="21"/><line x1="21" y1="14" x2="14" y2="21"/></svg></button>
    </div><span class="rb-cap">Insert</span>
  </div>
  <div class="rb-lbl-grp">
    <div class="rb-row">
      <span class="tb tb-accent" style="cursor:default;opacity:.7;" title="Track changes is always active"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M12 20h9"/><path d="M16.5 3.5a2.1 2.1 0 0 1 3 3L7 19l-4 1 1-4 12.5-12.5z"/></svg>Changes: ON</span>
    </div><span class="rb-cap">Review</span>
  </div>
  <div class="rb-lbl-grp rb-end">
    <div class="rb-row">
      <button class="tb tb-accent" onmousedown="return false" onclick="saveVersion()" title="Snapshot this version"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M19 21H5a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h11l5 5v11a2 2 0 0 1-2 2z"/><polyline points="17 21 17 13 7 13 7 21"/><polyline points="7 3 7 8 14 8"/></svg>Save Version</button>
    </div><span class="rb-cap">Version</span>
  </div>
</div>
<div class="main">
  <div class="edwrap"><div class="sheet" id="sheet" contenteditable="{'false' if signed else 'true'}" spellcheck="false" onblur="saveRange()"></div></div>
  <div class="pv">
    <div class="pvhead"><div id="pv-diff-stats"></div><button class="tb" style="color:#bdc1c6;height:20px;background:none;" onclick="refreshPv()" title="Refresh preview">⟳</button></div>
    <div id="pv-pages"></div>
  </div>
</div>
</div>

<!-- Mandatory comment bar — slides up when P1 makes any edit -->
<div id="edit-cmt-bar" style="display:none;position:fixed;bottom:0;left:0;right:0;z-index:8000;
  background:#1e3a5f;border-top:2px solid #f59e0b;padding:12px 20px;box-shadow:0 -4px 18px rgba(0,0,0,.35);
  display:none;align-items:flex-start;gap:14px;flex-wrap:wrap;">
  <div style="flex:1;min-width:260px;">
    <div style="color:#fbbf24;font-size:12px;font-weight:700;margin-bottom:5px;">
      ✏ You made changes — a comment is required before saving
    </div>
    <textarea id="edit-cmt-text" placeholder="Describe what you changed and why (mandatory)…"
      style="width:100%;padding:8px 10px;border:1.5px solid #f59e0b;border-radius:6px;
             background:#fff;font-size:13px;font-family:inherit;resize:none;height:56px;line-height:1.4;"
      oninput="_onCmtInput()"></textarea>
  </div>
  <div style="display:flex;flex-direction:column;gap:6px;padding-top:20px;">
    <button onclick="_saveCmt()" style="background:#16a34a;color:#fff;border:none;border-radius:7px;
      padding:8px 18px;font-size:13px;font-weight:700;cursor:pointer;white-space:nowrap;">💾 Save with Comment</button>
    <button onclick="_dismissCmt()" style="background:#374151;color:#9ca3af;border:none;border-radius:7px;
      padding:6px 18px;font-size:12px;cursor:pointer;">Discard my changes</button>
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

<!-- P1 per-change comment modal (shown when stage=changes_requested and P1 edits) -->
<div id="p1-cmt-modal" style="display:none;position:fixed;inset:0;z-index:600;background:rgba(12,35,68,.6);
     align-items:center;justify-content:center;">
  <div style="background:#fff;border-radius:14px;width:500px;max-width:93vw;padding:24px 26px;
       box-shadow:0 24px 60px rgba(0,0,0,.38);">
    <div style="font-size:15px;font-weight:800;color:#0c2344;margin-bottom:4px;">&#x270F; Explain your change — visible to lead</div>
    <div style="font-size:12.5px;color:#64748b;margin-bottom:14px;">
      You are responding to the lead&rsquo;s revision request. Explain what you changed and why — the lead will see this comment.</div>
    <textarea id="p1-cmt-ta" rows="4"
      placeholder="e.g. &#x27;We kept the 30-day notice period as per our standard terms. The 60-day request is not feasible.&#x27;"
      style="width:100%;padding:10px 12px;border:1.5px solid #d1d5db;border-radius:8px;font-size:13px;
             font-family:inherit;resize:vertical;outline:none;box-sizing:border-box;"></textarea>
    <div id="p1-cmt-err" style="display:none;color:#dc2626;font-size:12px;margin-top:5px;"></div>
    <div style="display:flex;gap:10px;margin-top:16px;justify-content:flex-end;">
      <button onclick="document.getElementById('p1-cmt-modal').style.display='none';document.getElementById('p1-cmt-err').style.display='none';"
        style="background:#f1f5f9;color:#374151;border:1px solid #d4dae6;border-radius:7px;
               padding:9px 18px;font-size:13px;cursor:pointer;font-family:inherit;">
        &#x2190; Continue Editing</button>
      <button onclick="_p1SaveComment()"
        style="background:#1a56db;color:#fff;border:none;border-radius:7px;
               padding:9px 22px;font-size:13px;font-weight:700;cursor:pointer;font-family:inherit;">
        &#x2713; Save Comment</button>
    </div>
  </div>
</div>

<script>
const BASE = '{base}', OID = '{onboarding_id}', DT = '{doc_type}', TOK = '{token}';
const SIGNED = {str(signed).lower()};
const STAGE = '{stage}';
const INIT_HTML = {html_json};
const COMMENTS = {comments_json};
const ORIG_TEXT = {orig_text_js};
const ORIG_HTML = {orig_html_js};   // raw original HTML — normalized client-side for an apples-to-apples diff
const SIG_OVERLAYS = {overlays_json};   // placed signature overlays {{page,x,y,w,h,image}}
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

let _hasUnsavedEdit = false;
let _pendingCmt = '';

function dirty() {{
  if (SIGNED) return;
  const el = document.getElementById('saved');
  el.textContent = '● editing…'; el.style.color = '#fbbf24';
  if (!_hasUnsavedEdit) {{
    _hasUnsavedEdit = true;
    // Exit track-changes review so the user can see what they're editing
    if (window._reviewing && typeof window._tcExitReview === 'function') window._tcExitReview();
    // In changes_requested mode the blur-triggered modal handles the comment — don't double-show the bar
    if (STAGE !== 'changes_requested') {{
      const bar = document.getElementById('edit-cmt-bar');
      if (bar) bar.style.display = 'flex';
    }}
  }}
  clearTimeout(timer);
  // Do NOT auto-save while there's a pending comment — user must fill it first
}}
function _onCmtInput() {{
  _pendingCmt = (document.getElementById('edit-cmt-text').value || '').trim();
}}
async function _saveCmt() {{
  _pendingCmt = (document.getElementById('edit-cmt-text').value || '').trim();
  if (!_pendingCmt) {{
    document.getElementById('edit-cmt-text').style.borderColor = '#dc2626';
    document.getElementById('edit-cmt-text').focus();
    return;
  }}
  document.getElementById('edit-cmt-text').style.borderColor = '#f59e0b';
  // Inject anchor marks on every block that was edited so the HTML stores the change location
  _editedEls.forEach(function(el) {{
    if (el.isConnected && !el.getAttribute('data-cmt')) {{
      el.classList.add('chg-block', 'chg-p1');
      el.setAttribute('data-cmt', 'temp');
    }}
  }});
  const ok = await saveDoc({{comment: _pendingCmt}});
  if (ok) {{
    _hasUnsavedEdit = false;
    _pendingCmt = '';
    document.getElementById('edit-cmt-text').value = '';
    const bar = document.getElementById('edit-cmt-bar');
    if (bar) bar.style.display = 'none';
  }}
}}
function _dismissCmt() {{
  if (!confirm('Discard all unsaved changes and revert to the last saved version?')) return;
  _hasUnsavedEdit = false;
  _pendingCmt = '';
  document.getElementById('edit-cmt-text').value = '';
  const bar = document.getElementById('edit-cmt-bar');
  if (bar) bar.style.display = 'none';
  // Reload the page to restore last saved state
  location.reload();
}}
var _editedEls = new Set();
sheet.addEventListener('input', function() {{
  dirty();
  // Track which block-level element was edited so we can anchor the change mark
  var sel = window.getSelection && window.getSelection();
  var node = sel && sel.anchorNode;
  if (!node) return;
  var el = node.nodeType === 3 ? node.parentElement : node;
  while (el && el.parentElement && el.parentElement !== sheet) el = el.parentElement;
  if (el && el !== sheet && el.isConnected) _editedEls.add(el);
}});

// ── Floating comment bubble system ───────────────────────────────────────────
window._lastComments = [];

function _applyCommentId(cmtId) {{
  var idx = 0;
  sheet.querySelectorAll('[data-cmt="temp"]').forEach(function(el) {{
    el.setAttribute('data-cmt', cmtId);
    if (!el.id) el.id = 'cm-' + cmtId + (idx > 0 ? '-' + idx : '');
    idx++;
  }});
  _editedEls.clear();
}}

function _chgClass(party, status) {{
  if (status === 'accepted') return 'chg-accepted';
  if (status === 'rejected') return 'chg-rejected';
  return party === 'p1' ? 'chg-p1' : 'chg-p2';
}}

function _applyMarkColors(comments) {{
  window._lastComments = comments || [];
  var map = {{}};
  (comments || []).forEach(function(c) {{ map[c.id] = c; }});
  sheet.querySelectorAll('[data-cmt]').forEach(function(el) {{
    var cid = el.getAttribute('data-cmt');
    if (!cid || cid === 'temp') return;
    var c = map[cid]; if (!c) return;
    // Remove old state classes, apply new one
    el.classList.remove('chg-p1','chg-p2','chg-accepted','chg-rejected');
    el.classList.add(_chgClass(c.party, c.status));
    el.style.cursor = 'pointer';
    el.title = 'Click to view / jump to comment';
    el.onclick = (function(id) {{ return function() {{ jumpToChange(id); }}; }})(cid);
  }});
}}

function renderBubbles(comments) {{
  document.querySelectorAll('.chg-bubble').forEach(function(b) {{ b.remove(); }});
  window._lastComments = comments || [];
  if (!comments || !comments.length) return;
  var map = {{}};
  comments.forEach(function(c) {{ map[c.id] = c; }});
  var seen = {{}};
  sheet.querySelectorAll('[data-cmt]').forEach(function(mark) {{
    var cid = mark.getAttribute('data-cmt');
    if (!cid || cid === 'temp' || seen[cid]) return;
    seen[cid] = true;
    var c = map[cid]; if (!c) return;
    var status = c.status || 'pending';
    var pCol  = c.party === 'p1' ? '#1a56db' : '#059669';
    var sCol  = {{pending:'#f59e0b',accepted:'#16a34a',rejected:'#dc2626'}}[status] || '#94a3b8';
    var pLbl  = c.party === 'p1' ? 'Jane' : 'Lead';
    var txt   = ((c.thread || [])[0] || {{}}).text || '';
    var accRej = status === 'pending' ? (
      '<div style="display:flex;gap:5px;margin-top:7px;">' +
      '<button data-bub-cid="' + cid + '" onclick="event.stopPropagation();_bubAccept(this)" ' +
        'style="flex:1;background:#16a34a;color:#fff;border:none;border-radius:5px;padding:4px 0;font-size:11px;font-weight:700;cursor:pointer;">✓ Accept</button>' +
      '<button data-bub-cid="' + cid + '" onclick="event.stopPropagation();_bubReject(this)" ' +
        'style="flex:1;background:#fee2e2;color:#dc2626;border:1px solid #fca5a5;border-radius:5px;padding:4px 0;font-size:11px;font-weight:700;cursor:pointer;">✗ Reject</button>' +
      '</div>') : '';
    var bub = document.createElement('div');
    bub.className = 'chg-bubble';
    bub.id = 'bub-' + cid;
    bub.setAttribute('data-for', cid);
    bub.innerHTML =
      '<div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:5px;">' +
        '<span style="font-size:11px;font-weight:700;color:' + pCol + '">' + (c.by||'') +
          ' <span style="color:#94a3b8;font-weight:400;">(' + pLbl + ')</span></span>' +
        '<span style="font-size:9.5px;font-weight:700;background:' + sCol + ';color:#fff;padding:1px 6px;border-radius:99px;">' + status + '</span>' +
      '</div>' +
      '<div style="font-size:11.5px;color:#374151;line-height:1.45;margin-bottom:4px;word-break:break-word;">' +
        txt.slice(0,130) + (txt.length > 130 ? '…' : '') +
      '</div>' +
      '<div style="font-size:10px;color:#94a3b8;">' + (c.at||'') + '</div>' +
      accRej;
    bub.onclick = (function(id) {{ return function() {{ jumpToChange(id); }}; }})(cid);
    document.body.appendChild(bub);
  }});
  positionBubbles();
}}

function positionBubbles() {{
  var sheetRect = sheet.getBoundingClientRect();
  var bubLeft = Math.min(sheetRect.right + 14, window.innerWidth - 244);
  var seen = {{}};
  sheet.querySelectorAll('[data-cmt]').forEach(function(mark) {{
    var cid = mark.getAttribute('data-cmt');
    if (!cid || cid === 'temp' || seen[cid]) return;
    seen[cid] = true;
    var bub = document.getElementById('bub-' + cid); if (!bub) return;
    var rect = mark.getBoundingClientRect();
    var top  = Math.max(60, rect.top - 8);
    bub.style.top  = top + 'px';
    bub.style.left = bubLeft + 'px';
    bub.style.display = (rect.top < -300 || rect.top > window.innerHeight + 120) ? 'none' : 'block';
  }});
}}

function jumpToChange(cmtId) {{
  var el = sheet.querySelector('[data-cmt="' + cmtId + '"]');
  if (el) {{
    el.scrollIntoView({{behavior:'smooth',block:'center'}});
    el.classList.add('jump-flash');
    setTimeout(function() {{ el.classList.remove('jump-flash'); }}, 1800);
  }}
  var bub = document.getElementById('bub-' + cmtId);
  if (bub) {{
    bub.classList.add('bub-focus');
    setTimeout(function() {{ bub.classList.remove('bub-focus'); }}, 1800);
  }}
  // Scroll the sidebar card into view
  setTimeout(function() {{
    var card = document.getElementById('card-' + cmtId);
    if (card) card.scrollIntoView({{behavior:'smooth',block:'nearest'}});
  }}, 300);
}}

function _showBubRej(cmtId, btn) {{
  var bub = document.getElementById('bub-' + cmtId); if (!bub) return;
  if (bub.querySelector('.bub-rej-box')) return;
  var box = document.createElement('div');
  box.className = 'bub-rej-box';
  box.innerHTML =
    '<textarea placeholder="Explain why this change is not acceptable (required)…"></textarea>' +
    '<div style="display:flex;gap:5px;margin-top:5px;">' +
      '<button data-bub-cid="' + cmtId + '" onclick="event.stopPropagation();_sendBubRejClick(this)" ' +
        'style="background:#dc2626;color:#fff;border:none;border-radius:5px;padding:4px 10px;font-size:11px;font-weight:700;cursor:pointer;">Send</button>' +
      '<button onclick="event.stopPropagation();_cancelBubRej(this)" ' +
        'style="background:#f1f5f9;color:#374151;border:1px solid #d4dae6;border-radius:5px;padding:4px 10px;font-size:11px;cursor:pointer;">Cancel</button>' +
    '</div>';
  bub.appendChild(box);
}}

function _sendBubRej(cmtId, btn) {{
  var txt = btn.closest('.bub-rej-box').querySelector('textarea').value.trim();
  if (!txt) {{ alert('Please explain why this change is not acceptable.'); return; }}
  _commentAction(cmtId, 'reject', txt);
}}
function _bubAccept(btn) {{
  _commentAction(btn.getAttribute('data-bub-cid'), 'accept', '');
}}
function _bubReject(btn) {{
  _showBubRej(btn.getAttribute('data-bub-cid'), btn);
}}
function _sendBubRejClick(btn) {{
  _sendBubRej(btn.getAttribute('data-bub-cid'), btn);
}}
function _cancelBubRej(btn) {{
  var box = btn.parentElement;
  while (box && !box.classList.contains('bub-rej-box')) box = box.parentElement;
  if (box) box.remove();
}}

window.addEventListener('scroll', positionBubbles, {{passive:true}});
window.addEventListener('resize', positionBubbles, {{passive:true}});

function togglePreview() {{
  var pv = document.querySelector('.pv');
  var btn = document.getElementById('btn-pv');
  if (pv.classList.contains('open')) {{
    pv.classList.remove('open');
    if (btn) {{ btn.classList.remove('active'); btn.textContent = '👁 Preview'; }}
    return;
  }}
  pv.classList.add('open');
  if (btn) {{ btn.classList.add('active'); btn.textContent = '👁 Preview ✕'; }}
  // show word diff stats in the preview header
  var statsEl = document.getElementById('pv-diff-stats');
  if (statsEl && typeof window._diffStats === 'function') {{
    var s = window._diffStats();
    if (s.ins === 0 && s.del === 0) {{
      statsEl.innerHTML = '<span style="color:#9ca3af;font-size:11px;">No changes vs template</span>';
    }} else {{
      statsEl.innerHTML =
        (s.ins > 0 ? '<span class="chip-add">+' + s.ins + ' added</span>' : '') +
        (s.del > 0 ? '<span class="chip-del">−' + s.del + ' removed</span>' : '');
    }}
  }}
  refreshPv();
}}

async function saveDoc(extra) {{
  if (SIGNED) return false;
  // Block auto-save if user has unsaved edits without a comment.
  // In changes_requested mode the modal system handles comment capture — skip the bar entirely.
  if (_hasUnsavedEdit && !_pendingCmt && !(extra && extra.comment)) {{
    if (STAGE !== 'changes_requested') {{
      const bar = document.getElementById('edit-cmt-bar');
      if (bar) {{ bar.style.display = 'flex'; document.getElementById('edit-cmt-text').focus(); }}
      return false;
    }}
  }}
  const el = document.getElementById('saved');
  el.textContent = 'Saving…'; el.style.color = '#fbbf24';
  // While reviewing, the sheet shows redline markup — persist the stashed real doc instead.
  const liveHtml = window._reviewing ? (window._reviewBackup || '') : sheet.innerHTML;
  const payload = Object.assign({{html: liveHtml, mode: 'live'}}, extra || {{}});
  const r = await fetch(`${{BASE}}/save/${{OID}}/${{DT}}/${{TOK}}`, {{
    method: 'POST', headers: {{'Content-Type': 'application/json'}},
    body: JSON.stringify(payload)
  }}).catch(() => null);
  const ok = r && r.ok;
  el.textContent = ok ? 'Saved ✓' : 'Save failed';
  el.style.color = ok ? '#86efac' : '#fca5a5';
  if (ok) {{
    var respData = {{}}; try {{ respData = await r.json(); }} catch(e) {{}}
    // Bind the real comment ID to the temp anchor marks injected before saving
    if (respData.comment_id) _applyCommentId(respData.comment_id);
    var mc = document.getElementById('mode-chip'); if (mc) mc.textContent = 'LIVE-EDITED';
    if (document.querySelector('.pv.open')) refreshPv();
    if (document.getElementById('sidebar').classList.contains('open')) loadSidebar();
    // Re-enter track changes view after save
    if (typeof window._tcEnterReview === 'function' && !window._reviewing) {{
      setTimeout(window._tcEnterReview, 300);
    }}
    // Refresh bubbles immediately after any save
    setTimeout(function() {{
      var d = (window._lastComments || []); renderBubbles(d); _applyMarkColors(d);
    }}, 400);
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
    // each page is an <img.pg> inside a position:relative .pg-wrap so signature
    // overlays can be absolutely positioned on top of it.
    while (wrap.children.length > n) wrap.removeChild(wrap.lastChild);
    while (wrap.children.length < n) {{
      const pw = document.createElement('div');
      pw.className = 'pg-wrap';
      const img = document.createElement('img');
      img.className = 'pg';
      img.loading = 'lazy';
      pw.appendChild(img);
      wrap.appendChild(pw);
    }}
    for (let i = 0; i < n; i++) {{
      wrap.children[i].querySelector('img.pg').src = `${{BASE}}/preview-page/${{OID}}/${{DT}}/${{TOK}}?p=${{i}}&v=${{v}}`;
    }}
    if (typeof renderSigOverlays === 'function') renderSigOverlays();
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

// ── Signature image resize / move toolbar ────────────────────────────────────
(function() {{
  // inject CSS for selected sig image outline
  const st = document.createElement('style');
  st.textContent = '.sig-img{{cursor:move;vertical-align:middle;max-width:100%;transition:outline .1s}}' +
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
    '<button id="sig-tb-fit" title="Fit to signature height" style="background:#2d5a8e;color:#fff;border:none;' +
      'border-radius:5px;padding:3px 9px;cursor:pointer;font-size:12px;">↕ Fit</button>' +
    '<span style="color:#4b7ab5;margin:0 2px;">|</span>' +
    '<button id="sig-tb-del" title="Remove signature" style="background:#7f1d1d;color:#fca5a5;' +
      'border:none;border-radius:5px;padding:3px 9px;cursor:pointer;font-size:12px;">✕ Remove</button>';
  document.body.appendChild(tb);

  // Word-style drag-to-resize grip on the selected signature image
  const grip = document.createElement('div');
  grip.id = 'sig-grip';
  grip.style.cssText = 'position:fixed;display:none;z-index:9999;width:14px;height:14px;' +
    'background:#1a56db;border:2px solid #fff;border-radius:3px;cursor:nwse-resize;' +
    'box-shadow:0 1px 4px rgba(0,0,0,.4);touch-action:none;';
  document.body.appendChild(grip);
  let _drag = null;
  grip.addEventListener('pointerdown', function(e) {{
    if (!sel) return;
    e.preventDefault(); e.stopPropagation();
    try {{ grip.setPointerCapture(e.pointerId); }} catch(x) {{}}
    _drag = {{ x: e.clientX, w: _ptWidth(sel) }};
  }});
  grip.addEventListener('pointermove', function(e) {{
    if (!_drag || !sel) return;
    const nw = Math.max(30, Math.min(500, Math.round(_drag.w + (e.clientX - _drag.x) * 0.75)));
    sel.style.width = nw + 'pt'; sel.style.height = '';
    document.getElementById('sig-tb-sz').textContent = nw + ' pt';
    _reposTb();
  }});
  grip.addEventListener('pointerup', function(e) {{
    if (!_drag) return;
    _drag = null;
    try {{ grip.releasePointerCapture(e.pointerId); }} catch(x) {{}}
    if (typeof dirty === 'function') dirty();   // persist the new size
  }});

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
    grip.style.display = 'block';
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
    grip.style.left = (r.right - 7) + 'px';
    grip.style.top  = (r.bottom - 7) + 'px';
  }}

  function _resize(delta) {{
    if (!sel) return;
    const nw = Math.max(30, Math.min(500, _ptWidth(sel) + delta));
    sel.style.width = nw + 'pt';
    sel.style.height = '';   // let aspect ratio breathe
    document.getElementById('sig-tb-sz').textContent = nw + ' pt';
    _reposTb();
    if (typeof dirty === 'function') dirty();   // persist the new size
  }}

  // Normalise the selected signature to a ~48pt height using its real aspect
  // ratio — one click to tame an oversized / portrait image (e.g. a screenshot).
  function _fitHeight() {{
    if (!sel) return;
    const ar = (sel.naturalWidth && sel.naturalHeight) ? (sel.naturalWidth / sel.naturalHeight) : 1;
    const nw = Math.max(30, Math.min(500, Math.round(48 * ar)));
    sel.style.width = nw + 'pt';
    sel.style.height = '';
    document.getElementById('sig-tb-sz').textContent = nw + ' pt';
    _reposTb();
    if (typeof dirty === 'function') dirty();
  }}

  document.getElementById('sig-tb-sm').onclick  = function(e) {{ e.stopPropagation(); _resize(-15); }};
  document.getElementById('sig-tb-lg').onclick  = function(e) {{ e.stopPropagation(); _resize(+15); }};
  document.getElementById('sig-tb-fit').onclick = function(e) {{ e.stopPropagation(); _fitHeight(); }};
  document.getElementById('sig-tb-del').onclick = function(e) {{
    e.stopPropagation();
    if (sel) {{ sel.remove(); sel = null; tb.style.display = 'none'; }}
  }};

  // click on a sig-img → select; click elsewhere → deselect
  document.addEventListener('click', function(e) {{
    if (e.target.classList && e.target.classList.contains('sig-img')) {{
      _showTb(e.target);
    }} else if (!tb.contains(e.target) && !grip.contains(e.target)) {{
      if (sel) sel.classList.remove('sig-sel');
      sel = null;
      tb.style.display = 'none';
      grip.style.display = 'none';
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

function _sbSection(body, title) {{
  const h = document.createElement('div');
  h.className = 'sb-sec';
  h.textContent = title;
  body.appendChild(h);
}}

function _statusBadge(status) {{
  var colours = {{pending:'#f59e0b', accepted:'#16a34a', rejected:'#dc2626'}};
  var labels  = {{pending:'Pending', accepted:'Accepted', rejected:'Rejected'}};
  var bg  = colours[status] || '#64748b';
  var lbl = labels[status]  || status;
  return '<span style="display:inline-block;padding:1px 7px;border-radius:99px;font-size:10px;font-weight:700;background:' + bg + ';color:#fff;">' + lbl + '</span>';
}}

function _sbCommentCard(c) {{
  var status = c.status || (c.done ? 'accepted' : 'pending');
  var thread = c.thread || [];
  var card = document.createElement('div');
  card.id = 'card-' + c.id;
  var borderCol = c.party === 'p1' ? '#2563eb' : '#f59e0b';
  card.style.cssText = 'background:#fff;border:1px solid #e5e9f2;border-left:3px solid ' + borderCol + ';border-radius:8px;padding:12px 14px;margin-bottom:10px;';

  // Header row: name + status badge
  var hdr = document.createElement('div');
  hdr.style.cssText = 'display:flex;justify-content:space-between;align-items:center;margin-bottom:6px;';
  var partyLabel = c.party === 'p1' ? 'Jane' : 'Lead';
  var nameSpan = document.createElement('span');
  nameSpan.style.cssText = 'font-size:11.5px;font-weight:700;color:#374151;';
  nameSpan.textContent = (c.by || 'Lead') + ' (' + partyLabel + ')';
  hdr.appendChild(nameSpan);
  hdr.innerHTML = hdr.innerHTML + _statusBadge(status);
  card.appendChild(hdr);

  // Timestamp
  var ts = document.createElement('div');
  ts.style.cssText = 'font-size:11px;color:#94a3b8;margin-bottom:6px;';
  ts.textContent = c.at || '';
  card.appendChild(ts);

  // Jump-to-change button (only if document has an anchor for this comment)
  var hasAnchor = !!sheet.querySelector('[data-cmt="' + c.id + '"]');
  if (hasAnchor) {{
    var jw = document.createElement('div');
    jw.style.marginBottom = '8px';
    var jb = document.createElement('button');
    jb.textContent = 'Jump to change';
    jb.style.cssText = 'background:#eff6ff;color:#1a56db;border:1px solid #bfdbfe;border-radius:5px;padding:3px 9px;font-size:10.5px;font-weight:700;cursor:pointer;margin-right:4px;';
    jb.onclick = function(){{ jumpToChange(c.id); }};
    jw.appendChild(jb);
    card.appendChild(jw);
  }}

  // Thread entries
  var threadDiv = document.createElement('div');
  threadDiv.id = 'thread-' + c.id;
  threadDiv.style.cssText = 'border-left:3px solid #e5e9f2;padding-left:10px;margin-bottom:10px;';
  thread.forEach(function(t) {{
    var te = document.createElement('div');
    te.style.marginBottom = '8px';
    var tParty = t.party === 'p1' ? 'Jane' : 'Lead';
    var tCol   = t.party === 'p1' ? '#1a56db' : '#059669';
    var tMeta = document.createElement('div');
    tMeta.style.cssText = 'font-size:11px;font-weight:700;color:' + tCol + ';';
    tMeta.textContent = (t.by || '') + ' (' + tParty + ') - ' + (t.action || 'comment') + ' - ' + (t.at || '');
    var tBody = document.createElement('div');
    tBody.style.cssText = 'font-size:12.5px;color:#374151;white-space:pre-wrap;margin-top:2px;';
    tBody.textContent = t.text || '';
    te.appendChild(tMeta);
    te.appendChild(tBody);
    threadDiv.appendChild(te);
  }});
  card.appendChild(threadDiv);

  // Accept / Reject buttons
  if (status === 'pending' || status === 'rejected') {{
    var acts = document.createElement('div');
    acts.style.cssText = 'display:flex;gap:8px;flex-wrap:wrap;';
    if (status === 'pending') {{
      var acc = document.createElement('button');
      acc.textContent = 'Accept';
      acc.style.cssText = 'background:#16a34a;color:#fff;border:none;border-radius:6px;padding:5px 12px;font-size:12px;font-weight:700;cursor:pointer;';
      acc.onclick = function(){{ _commentAction(c.id, 'accept', ''); }};
      acts.appendChild(acc);
    }}
    var rejBtn = document.createElement('button');
    rejBtn.textContent = status === 'rejected' ? 'Reply' : 'Reject';
    rejBtn.style.cssText = 'background:#fee2e2;color:#dc2626;border:1px solid #fca5a5;border-radius:6px;padding:5px 12px;font-size:12px;font-weight:700;cursor:pointer;';
    rejBtn.onclick = function(){{ _showRejectBox(c.id, acts); }};
    acts.appendChild(rejBtn);
    card.appendChild(acts);
  }}
  return card;
}}

function _showRejectBox(commentId, container) {{
  if (container.querySelector('.rej-box')) return;
  var box = document.createElement('div');
  box.className = 'rej-box';
  box.style.cssText = 'margin-top:8px;width:100%;';
  var ta = document.createElement('textarea');
  ta.placeholder = 'Explain why this change is not acceptable (required)';
  ta.style.cssText = 'width:100%;min-height:70px;padding:8px;border:1px solid #fca5a5;border-radius:6px;font-size:13px;font-family:inherit;resize:vertical;';
  var row = document.createElement('div');
  row.style.cssText = 'display:flex;gap:6px;margin-top:6px;';
  var sendBtn = document.createElement('button');
  sendBtn.textContent = 'Send Rejection';
  sendBtn.setAttribute('data-cid', commentId);
  sendBtn.style.cssText = 'background:#dc2626;color:#fff;border:none;border-radius:6px;padding:5px 12px;font-size:12px;font-weight:700;cursor:pointer;';
  sendBtn.onclick = function(){{ _commentReject(this); }};
  var cancelBtn = document.createElement('button');
  cancelBtn.textContent = 'Cancel';
  cancelBtn.style.cssText = 'background:#eef1f6;color:#374151;border:1px solid #d4dae6;border-radius:6px;padding:5px 12px;font-size:12px;cursor:pointer;';
  cancelBtn.onclick = function(){{ box.parentNode && box.parentNode.removeChild(box); }};
  row.appendChild(sendBtn);
  row.appendChild(cancelBtn);
  box.appendChild(ta);
  box.appendChild(row);
  container.appendChild(box);
}}

// Cached versions list kept in sync by loadSidebar
var _sbVersions = [];

// Pure render - reads COMMENTS and _sbVersions, no network call.
// Always use exclusive categorisation so a comment can never appear in two sections.
function _renderSidebar() {{
  var body = document.getElementById('sb-body');
  if (!body) return;
  var pending = [], rejected = [], accepted = [];
  COMMENTS.forEach(function(c) {{
    var s = c.status || 'pending';
    if (s === 'accepted') accepted.push(c);
    else if (s === 'rejected') rejected.push(c);
    else pending.push(c);
  }});
  var openCount = pending.length;
  var cntEl = document.getElementById('cmt-n');
  if (cntEl) cntEl.textContent = openCount;
  body.innerHTML = '';

  if (openCount > 0) {{
    var b1 = document.createElement('div');
    b1.style.cssText = 'background:#fff7ed;border:1px solid #fed7aa;color:#92400e;padding:10px 14px;border-radius:8px;font-size:12.5px;margin-bottom:12px;';
    b1.textContent = 'Warning: ' + openCount + ' comment(s) not yet reviewed - respond to each before sending.';
    body.appendChild(b1);
  }} else if (rejected.length > 0) {{
    var b2 = document.createElement('div');
    b2.style.cssText = 'background:#fef9c3;border:1px solid #fde047;color:#713f12;padding:10px 14px;border-radius:8px;font-size:12.5px;margin-bottom:12px;';
    b2.textContent = rejected.length + ' comment(s) rejected - you can send the document back to the lead.';
    body.appendChild(b2);
  }} else if (!pending.length && !rejected.length && accepted.length > 0) {{
    var b3 = document.createElement('div');
    b3.style.cssText = 'background:#dcfce7;border:1px solid #86efac;color:#14532d;padding:10px 14px;border-radius:8px;font-size:12.5px;margin-bottom:12px;font-weight:700;';
    b3.textContent = 'All comments accepted - ready to send for signing.';
    body.appendChild(b3);
  }}

  _sbSection(body, '🔴 Pending (' + pending.length + ')');
  if (!pending.length) {{
    var ep = document.createElement('p'); ep.className = 'sb-empty';
    ep.textContent = 'No pending comments.'; body.appendChild(ep);
  }}
  pending.slice().reverse().forEach(function(c) {{ body.appendChild(_sbCommentCard(c)); }});

  if (rejected.length) {{
    _sbSection(body, '🟠 Rejected by Jane (' + rejected.length + ')');
    rejected.slice().reverse().forEach(function(c) {{ body.appendChild(_sbCommentCard(c)); }});
  }}

  _sbSection(body, '✅ Accepted (' + accepted.length + ')');
  if (!accepted.length) {{
    var ea = document.createElement('p'); ea.className = 'sb-empty';
    ea.textContent = 'No accepted comments yet.'; body.appendChild(ea);
  }}
  accepted.slice().reverse().forEach(function(c) {{ body.appendChild(_sbCommentCard(c)); }});

  // Jane own draft comments (in modal, not yet sent to server)
  if (typeof _p1Comments !== 'undefined' && _p1Comments.length > 0) {{
    _sbSection(body, 'Jane changes (' + _p1Comments.length + ' draft, not sent)');
    _p1Comments.forEach(function(txt) {{
      var card = document.createElement('div');
      card.style.cssText = 'background:#eff6ff;border:1px solid #bfdbfe;border-left:3px solid #1a56db;border-radius:8px;padding:10px 13px;margin-bottom:8px;font-size:12px;';
      card.innerHTML = '<div style="font-weight:700;color:#1a56db;margin-bottom:4px;">Jane Aerospace'
        + ' <span style="font-size:10px;background:#dbeafe;color:#1e40af;padding:1px 6px;border-radius:99px;font-weight:700;margin-left:5px;">DRAFT</span></div>'
        + '<div style="color:#374151;white-space:pre-wrap;">' + txt.replace(/</g, '&lt;') + '</div>';
      body.appendChild(card);
    }});
  }}

  _sbSection(body, 'Activity - saves and sends');
  var vs = _sbVersions.slice().reverse();
  if (!vs.length) {{
    var ev = document.createElement('p'); ev.className = 'sb-empty';
    ev.textContent = 'No activity yet.'; body.appendChild(ev);
  }}
  vs.forEach(function(v) {{
    var note = v.note || '';
    var icon = note.indexOf('Sent to lead') >= 0 ? '📨'
             : (note.toLowerCase().indexOf('restor') >= 0 || note.toLowerCase().indexOf('revert') >= 0) ? '↺'
             : '💾';
    var row = document.createElement('div'); row.className = 'sb-act';
    var ic = document.createElement('span'); ic.textContent = icon;
    var tx = document.createElement('span'); tx.textContent = 'v' + v.n + ' · ' + (note || 'Save');
    var wh = document.createElement('span'); wh.className = 'when'; wh.textContent = v.at || '';
    row.appendChild(ic); row.appendChild(tx); row.appendChild(wh);
    body.appendChild(row);
  }});
  renderBubbles(COMMENTS);
  _applyMarkColors(COMMENTS);
}}

async function _commentAction(commentId, action, text) {{
  var r = await fetch(BASE + '/comment-action/' + OID + '/' + DT + '/' + TOK, {{
    method: 'POST', headers: {{'Content-Type': 'application/json'}},
    body: JSON.stringify({{comment_id: commentId, action: action, text: text}})
  }}).catch(function() {{ return null; }});
  if (r && r.ok) {{
    // Optimistically update the in-memory COMMENTS array immediately so the
    // card moves to the correct section before the loadSidebar round-trip.
    var target = COMMENTS.find(function(c) {{ return c.id === commentId; }});
    if (target) {{
      if (action === 'accept')  target.status = 'accepted';
      if (action === 'reject')  target.status = 'rejected';
      _renderSidebar();
    }}
    await loadSidebar();   // reconcile with server truth (updates status + thread)
  }} else {{
    var errDetail = 'Action failed';
    if (r) {{
      try {{ var errJson = await r.json(); errDetail = errJson.detail || errDetail; }} catch(e) {{}}
    }}
    alert(errDetail);
  }}
}}

window._commentReject = function(btn) {{
  var cid = btn.getAttribute('data-cid');
  var box = btn.closest ? btn.closest('.rej-box') : (btn.parentNode && btn.parentNode.parentNode);
  var ta = box ? box.querySelector('textarea') : null;
  var txt = ta ? ta.value.trim() : '';
  if (!txt) {{ alert('Please explain why this change is not acceptable.'); return; }}
  _commentAction(cid, 'reject', txt);
}};

async function loadSidebar() {{
  try {{
    var r = await fetch(BASE + '/versions/' + OID + '/' + DT + '/' + TOK);
    if (r.ok) {{
      var d = await r.json();
      COMMENTS.splice(0, COMMENTS.length, ...(d.comments || []));
      _sbVersions = d.versions || [];
    }}
  }} catch(e) {{}}
  _renderSidebar();
}}

// Auto-render bubbles and colour marks on page load; also prime _sbVersions
(async function() {{
  try {{
    var r = await fetch(BASE + '/versions/' + OID + '/' + DT + '/' + TOK);
    if (r.ok) {{
      var d = await r.json();
      COMMENTS.splice(0, COMMENTS.length, ...(d.comments || []));
      _sbVersions = d.versions || [];
      renderBubbles(COMMENTS);
      _applyMarkColors(COMMENTS);
    }}
  }} catch(e) {{}}
}})();

// ── P1 per-change comment system (only active when responding to lead's revision request) ──
var _p1HasEdits = false;
var _p1Comments = [];
var _p1PopupPending = false;

if (STAGE === 'changes_requested' && !SIGNED) {{
  // Detect edits via input event
  sheet.addEventListener('input', function() {{ _p1HasEdits = true; }});
  // When sheet loses focus after an edit, show comment popup
  sheet.addEventListener('blur', function() {{
    if (!_p1HasEdits || _p1PopupPending) return;
    _p1PopupPending = true;
    setTimeout(function() {{
      _p1PopupPending = false;
      if (_p1HasEdits) _p1ShowModal();
    }}, 500);
  }}, true);
  // Allow Ctrl+Enter to save comment
  document.getElementById('p1-cmt-ta').addEventListener('keydown', function(e) {{
    if ((e.ctrlKey || e.metaKey) && e.key === 'Enter') {{ e.preventDefault(); _p1SaveComment(); }}
  }});
}}

function _p1ShowModal() {{
  var modal = document.getElementById('p1-cmt-modal');
  if (modal) {{
    modal.style.display = 'flex';
    var ta = document.getElementById('p1-cmt-ta');
    if (ta) setTimeout(function() {{ ta.focus(); }}, 60);
  }}
}}

function _p1SaveComment() {{
  var ta = document.getElementById('p1-cmt-ta');
  var err = document.getElementById('p1-cmt-err');
  var txt = ta ? ta.value.trim() : '';
  if (!txt) {{
    if (err) {{ err.textContent = 'Please describe your change before saving.'; err.style.display = 'block'; }}
    if (ta) ta.focus();
    return;
  }}
  _p1Comments.push(txt);
  _p1HasEdits = false;
  document.getElementById('p1-cmt-modal').style.display = 'none';
  if (err) err.style.display = 'none';
  if (ta) ta.value = '';
  // Update the saved count indicator
  var el = document.getElementById('p1-cmt-count');
  if (el) {{ el.textContent = _p1Comments.length + ' comment(s) saved'; el.style.display = 'inline'; }}
  // Immediately re-render the sidebar so the "Jane changes (draft)" section
  // appears without waiting for the next loadSidebar() round-trip.
  if (document.getElementById('sidebar') && document.getElementById('sidebar').classList.contains('open')) {{
    _renderSidebar();
  }}
}}

async function sendForReview() {{
  clearTimeout(timer);
  // If responding to lead and there are uncommitted edits → require comment first
  if (STAGE === 'changes_requested' && !SIGNED && _p1HasEdits) {{
    _p1ShowModal(); return;
  }}
  if (!SIGNED && !(await saveDoc())) {{ alert('Could not save the document — not sent.'); return; }}
  // Build confirmation message
  var confirmMsg = STAGE === 'changes_requested'
    ? 'Send your revised document back to the lead' + (_p1Comments.length > 0 ? ' with ' + _p1Comments.length + ' comment(s)' : '') + '?'
    : 'Send this document to the lead for Terms & Conditions review?';
  if (!confirm(confirmMsg)) return;
  var p1Comment = _p1Comments.length > 0
    ? (_p1Comments.length === 1 ? _p1Comments[0] : _p1Comments.map(function(c,i) {{ return 'Change ' + (i+1) + ': ' + c; }}).join('\\n\\n'))
    : '';
  const r = await fetch(`${{BASE}}/send/${{OID}}/${{DT}}/${{TOK}}`, {{
    method: 'POST', headers: {{'Content-Type': 'application/json'}},
    body: JSON.stringify({{
      html: window._reviewing ? (window._reviewBackup || '') : sheet.innerHTML,
      mode: 'live',
      p1_comment: p1Comment
    }})
  }});
  const d = await r.json().catch(() => ({{}}));
  if (!r.ok && d.detail && d.detail.indexOf('not yet reviewed') >= 0) {{
    alert('Cannot send: some lead comments have not been reviewed yet. Please open the Comments sidebar and accept or reject each pending comment before sending.');
  }} else if (r.ok) {{
    _p1Comments = []; _p1HasEdits = false;
    // ── Lock the editor immediately ─────────────────────────────────────────
    // The document is now with the lead. Jane cannot edit until P2 responds.
    sheet.setAttribute('contenteditable', 'false');
    sheet.style.cursor = 'default';
    if (typeof window._tcEnterReview === 'function') window._tcEnterReview();
    // Disable the send button and toolbar so it's clear the ball is in P2's court
    document.querySelectorAll('.tb, .btn.b-green').forEach(function(el) {{
      el.setAttribute('disabled', 'disabled');
      el.style.opacity = '0.45';
      el.style.pointerEvents = 'none';
    }});
    // Show a locked banner below the topbar
    var lockBanner = document.createElement('div');
    lockBanner.style.cssText = 'background:#ecfdf5;color:#14532d;padding:9px 16px;font-size:13px;'
      + 'font-weight:700;text-align:center;border-bottom:1px solid #86efac;';
    lockBanner.textContent = '\u2705 Sent for review \u2014 editing is locked until the lead responds.';
    var topbar = document.querySelector('.topbar');
    if (topbar && topbar.nextSibling) {{
      topbar.parentNode.insertBefore(lockBanner, topbar.nextSibling);
    }}
    // Hide the mandatory comment bar if it's showing
    var cbar = document.getElementById('edit-cmt-bar');
    if (cbar) cbar.style.display = 'none';
    alert(d.message || 'Sent for review.');
  }} else {{
    alert(d.detail || 'Send failed');
  }}
}}
</script>
<script>{_EDITOR_RIBBON_JS}</script>
<script>{_SIG_OVERLAY_JS}</script>
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
    comment: str = ""     # mandatory edit comment from P1 — saved to comments[]
    p1_comment: str = ""  # per-change comment from P1 when responding to P2's revision request
    # Adobe-style signature overlays placed on the PDF preview. None = leave as-is
    # (so a text autosave never clobbers them); [] = clear all.
    signatures_overlay: list[dict] | None = None


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
    if body.signatures_overlay is not None:
        clean = []
        for ov in body.signatures_overlay[:30]:        # cap the number of overlays
            img = _clean_sig_image(str((ov or {}).get("image", "")))
            if not img:
                continue
            try:
                clean.append({
                    "page": max(0, int(ov.get("page", 0))),
                    "x": max(0.0, min(1.0, float(ov.get("x", 0)))),
                    "y": max(0.0, min(1.0, float(ov.get("y", 0)))),
                    "w": max(0.0, min(1.0, float(ov.get("w", 0.2)))),
                    "h": max(0.0, min(1.0, float(ov.get("h", 0.08)))),
                    "image": img,
                })
            except (TypeError, ValueError):
                continue
        data["signatures_overlay"] = clean
    _new_cmt_id = None
    if body.comment and body.comment.strip():
        import secrets as _sec
        now = _now_ist()
        from app.core.config import settings as _cfg
        p1_name = _cfg.ORGANIZER_NAME or "Jane Aerospace"
        comments = data.setdefault("comments", [])
        cmt_id = f"c{int(now.timestamp())}_{_sec.token_hex(4)}"
        _new_cmt_id = cmt_id
        comments.append({
            "id": cmt_id,
            "by": p1_name,
            "party": "p1",
            "email": "",
            "at": _fmt(now),
            "status": "accepted",   # P1's own edits are self-confirmed on save
            "thread": [{"by": p1_name, "party": "p1", "action": "edit",
                        "text": body.comment.strip()[:2000], "at": _fmt(now)}],
        })
    if body.snapshot:
        _snapshot_version(data, "team", body.note or "Manual save")
    data.setdefault("replacements", [])
    _set_doc_data(rec, doc_type, data)
    await db.commit()
    result: dict = {"message": "Saved", "versions": len(data.get("versions", []))}
    if _new_cmt_id:
        result["comment_id"] = _new_cmt_id
    return result


@router.get("/versions/{onboarding_id}/{doc_type}/{token}")
async def document_versions(onboarding_id: str, doc_type: str, token: str,
                            db: AsyncSession = Depends(get_db)):
    _check_doc_type(doc_type)
    _check_token(onboarding_id, doc_type, token, "edit", "sign")   # P1 or P2 may load comments
    rec, _lead = await _load(db, onboarding_id)
    data = _get_doc_data(rec, doc_type)
    # Deduplicate comments by id — if the same id appears more than once
    # (caused by P2 submitting the document twice), keep only the last entry
    # which carries the most-recently-written status (accepted / rejected).
    raw_comments = data.get("comments", [])
    seen_ids: dict = {}
    for c in raw_comments:
        cid = c.get("id")
        if cid:
            seen_ids[cid] = c   # last one wins
        else:
            seen_ids[id(c)] = c  # no id — keep as-is
    deduped_comments = list(seen_ids.values())
    return {
        "mode": data.get("mode", "template"),
        "stage": data.get("stage", ""),
        "comments": deduped_comments,
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
    # Strip P1-side editing artefacts before P2 sees the document:
    # removes temp data-cmt anchors and chg-* CSS classes (those highlight sections blue/amber
    # in P2's editor even before any negotiation has happened).
    # Real data-cmt="<id>" anchors for intentional P1 comments are preserved so
    # the bubble system still works during negotiation rounds.
    if data.get("html"):
        data["html"] = _strip_editor_marks(data["html"])
    data["signatory_name"] = body.signatory_name[:200] or data.get("signatory_name", "")
    data["signatory_email"] = body.signatory_email[:320] or data.get("signatory_email", "") or lead.email
    data.setdefault("replacements", [])
    # Block send only if P2 has comments P1 hasn't reviewed yet (pending).
    # Rejected = P1 has responded; that's fine — send goes back to P2 for another round.
    open_comments = [c for c in data.get("comments", [])
                     if c.get("party") == "p2"
                     and (c.get("status") or "pending") == "pending"]
    if open_comments:
        raise HTTPException(409, f"Cannot send: {len(open_comments)} lead comment(s) not yet reviewed. "
                                 "Please open the Comments sidebar and accept or reject each comment before sending.")
    # If P1 added per-change comments while responding to P2's revision request, store them
    # as a party="p1" comment entry so P2 can see P1's explanation in their sidebar.
    if body.p1_comment and body.p1_comment.strip() and data.get("stage") == "changes_requested":
        import secrets as _sec
        now_ts = _now_ist()
        p1_cmt_id = f"c{int(now_ts.timestamp())}_{_sec.token_hex(4)}"
        p1_entry = {
            "id": p1_cmt_id,
            "by": "Jane Aerospace",
            "party": "p1",
            "email": "team@janeaerospace.co.in",
            "at": _fmt(now_ts),
            "status": "pending",   # P2 must accept before signing can proceed
            "thread": [{"by": "Jane Aerospace", "party": "p1", "action": "comment",
                        "text": body.p1_comment.strip()[:4000], "at": _fmt(now_ts)}],
        }
        data.setdefault("comments", []).append(p1_entry)
    data["stage"] = "review"
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
    review_url = make_doc_portal_url(onboarding_id, doc_type)
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
    """Lead-facing review page — PATH A (accept) or PATH B (open proper live editor to propose changes)."""
    import html as _html_lib
    pdf_url = f"/api/v1/documents/pdf/{onboarding_id}/{doc_type}/{token}"
    p2editor_url = f"/api/v1/documents/p2editor/{onboarding_id}/{doc_type}/{token}"
    sig_name = _html_lib.escape(data.get("signatory_name", "") or (lead.contact_name or ""))
    comments = data.get("comments") or []
    stage = data.get("stage") or ""

    updated_note = ""
    if stage == "review" and any(c.get("party") == "p1" for c in comments):
        updated_note = ('<div style="background:#ecfdf5;border-left:3px solid #16a34a;border-radius:6px;'
                        'padding:10px 14px;margin-bottom:14px;font-size:13px;color:#14532d;">'
                        '&#x2705; Jane Aerospace has <b>updated the document</b> based on your previous comments. '
                        'Please review the latest version below.</div>')

    # Only show the "Jane is reviewing" banner if there are P2 comments that
    # P1 has NOT yet responded to (i.e. the thread has only P2 entries — no
    # accept/reject from P1 yet). This avoids showing the wait-banner on a new
    # review round after Jane has already responded to the previous round.
    p2_pending = [
        c for c in comments
        if c.get("party") == "p2"
        and (c.get("status") or "pending") in ("pending", "rejected")
        and not any(t.get("party") == "p1" for t in (c.get("thread") or []))
    ]
    changes_note = ""
    if p2_pending:
        changes_note = ('<div style="background:#fff7ed;border-left:3px solid #f59e0b;border-radius:6px;'
                        'padding:10px 14px;margin-bottom:14px;font-size:13px;color:#92400e;">'
                        '&#x231B; Your proposed changes have been submitted. Jane Aerospace is currently reviewing them. '
                        'You will receive an email once they respond.</div>')

    _esc_biz = _html_lib.escape(lead.business_name)
    if p2_pending:
        _action_html = ""
    else:
        _action_html = f"""
  <p style="text-align:center;font-size:14px;font-weight:700;color:#374151;margin:0 0 16px;">
    How would you like to respond to this {label}?</p>

  <div class="grid">
    <div class="card">
      <span class="path-badge badge-a">PATH A &mdash; Accept</span>
      <h2 style="font-size:18px;color:#15803d;margin:0 0 8px;">&#x2713; Accept the Terms</h2>
      <p class="sub" style="margin-bottom:12px;">The document is acceptable as-is. After you accept:
        <br>&#x2794; Jane Aerospace countersigns internally
        <br>&#x2794; You receive the final document for your e-signature</p>
      <label>Your Full Name <span style="color:#e11d48;">*</span></label>
      <input type="text" id="acc-name" value="{sig_name}" placeholder="Your full name">
      <div class="chk"><input type="checkbox" id="acc-agree">
        <span>I have read this {label} and agree to its terms on behalf of
        <strong>{_esc_biz}</strong>.</span></div>
      <button class="btn btn-green" id="acc-btn" onclick="doAccept()">&#x2713; Accept Terms &amp; Conditions</button>
    </div>

    <div class="card">
      <span class="path-badge badge-b">PATH B &mdash; Propose Changes</span>
      <h2 style="font-size:18px;color:#d97706;margin:0 0 8px;">&#x270F; Request Edits</h2>
      <p class="sub" style="margin-bottom:10px;">You want changes made before agreeing. Click the button below
        to open the <strong>live document editor</strong> where you can edit text and add a comment.</p>
      <ul class="how-list">
        <li>Open the editor (link below)</li>
        <li>Edit the document directly</li>
        <li>Add a comment explaining your changes</li>
        <li>Submit &mdash; Jane Aerospace reviews &amp; responds</li>
        <li>Signing only begins once <em>both parties agree</em></li>
      </ul>
      <a class="btn btn-amber" href="{p2editor_url}" id="edit-btn">
        &#x270F; Open Document Editor &rarr;</a>
    </div>
  </div>

  <div class="card">
    <h2 style="font-size:16px;color:#1a56db;margin:0 0 6px;">&#x1F464; Not the right person for this review?</h2>
    <p class="sub">Forward this review link to your legal or review team.</p>
    <div class="grid">
      <div><label>Reviewer's Name</label>
        <input type="text" id="fwd-name" placeholder="Full name"></div>
      <div><label>Reviewer's Email</label>
        <input type="text" id="fwd-email" placeholder="name@company.com"></div>
    </div>
    <button class="btn btn-blue" id="fwd-btn" onclick="doForward()" style="margin-top:10px;">
      &#x2192; Forward Review Link</button>
  </div>
"""

    return f"""<!DOCTYPE html>
<html lang="en">
<head><meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Review {label} — Jane Aerospace</title>
<style>
  *{{box-sizing:border-box;}}
  body{{font-family:Arial,sans-serif;background:#f4f6fb;margin:0;padding:20px;}}
  .wrap{{max-width:880px;margin:0 auto;}}
  .card{{background:#fff;border-radius:12px;box-shadow:0 2px 16px rgba(0,0,0,.1);padding:24px 28px;margin-bottom:18px;}}
  h1{{color:#1a3a6b;font-size:20px;margin:0 0 4px;}}
  .sub{{color:#666;font-size:13px;margin:0 0 14px;}}
  iframe{{width:100%;height:560px;border:1px solid #d1d5db;border-radius:8px;background:#fff;}}
  label{{display:block;font-size:13px;font-weight:600;color:#222;margin:14px 0 4px;}}
  input[type=text]{{width:100%;padding:11px 13px;border:1px solid #ccc;border-radius:6px;font-size:14px;}}
  .btn{{display:block;border:none;padding:14px 22px;border-radius:8px;font-size:15px;font-weight:700;
       cursor:pointer;width:100%;color:#fff;margin-top:10px;text-align:center;text-decoration:none;line-height:1.3;}}
  .btn:disabled{{background:#9ca3af!important;cursor:not-allowed;}}
  .btn-green{{background:#16a34a;}}
  .btn-blue{{background:#1a56db;}}
  .btn-amber{{background:#d97706;}}
  .grid{{display:grid;grid-template-columns:1fr 1fr;gap:18px;}}
  #err{{display:none;background:#fee2e2;color:#991b1b;padding:10px 14px;border-radius:6px;font-size:13px;margin:12px 0;}}
  .chk{{display:flex;gap:10px;align-items:flex-start;margin:14px 0;font-size:13px;color:#374151;line-height:1.5;}}
  .chk input{{margin-top:3px;width:17px;height:17px;flex-shrink:0;}}
  .path-badge{{display:inline-block;padding:3px 11px;border-radius:99px;font-size:11px;font-weight:800;
              letter-spacing:.04em;margin-bottom:10px;}}
  .badge-a{{background:#dcfce7;color:#15803d;}}
  .badge-b{{background:#fef3c7;color:#92400e;}}
  .how-list{{margin:0 0 14px;padding-left:0;list-style:none;font-size:12.5px;color:#64748b;}}
  .how-list li{{padding:3px 0 3px 18px;position:relative;}}
  .how-list li:before{{content:"";position:absolute;left:4px;top:9px;width:6px;height:6px;
    border-radius:50%;background:#d97706;}}
  @media(max-width:700px){{.grid{{grid-template-columns:1fr;}}.card{{padding:18px 14px;}}iframe{{height:380px;}}}}
</style></head>
<body>
<div class="wrap">

  <div class="card">
    <div style="font-size:15px;font-weight:700;color:#1a3a6b;margin-bottom:10px;">&#x2708; Jane Aerospace</div>
    <h1>{label} — Review &amp; Response</h1>
    <p class="sub">Sent by <strong>Jane Aerospace Private Limited</strong> to
      <strong>{_html_lib.escape(lead.business_name)}</strong> &mdash; please read the document and choose how to respond below.</p>
    {updated_note}
    {changes_note}
    <iframe src="{pdf_url}#toolbar=1" title="Document Preview"></iframe>
    <p style="font-size:12.5px;margin-top:6px;"><a href="{pdf_url}" target="_blank" style="color:#1155cc;">&#x2B07; Download PDF</a></p>
  </div>

  {_action_html}

  <div id="err"></div>
</div>

<script>
function showErr(msg) {{
  var e = document.getElementById('err');
  e.textContent = msg; e.style.display = 'block';
  setTimeout(function() {{ e.scrollIntoView({{behavior:'smooth',block:'nearest'}}); }}, 50);
}}
function hideErr() {{ document.getElementById('err').style.display = 'none'; }}

async function doAccept() {{
  hideErr();
  var name = document.getElementById('acc-name').value.trim();
  if (!name) {{ showErr('Please enter your full name.'); return; }}
  if (!document.getElementById('acc-agree').checked) {{
    showErr('Please tick the checkbox to confirm you have read and accept the terms.'); return; }}
  var btn = document.getElementById('acc-btn'); btn.disabled = true; btn.textContent = 'Submitting…';
  try {{
    var r = await fetch('/api/v1/documents/review/{onboarding_id}/{doc_type}/{token}', {{
      method: 'POST', headers: {{'Content-Type': 'application/json'}},
      body: JSON.stringify({{action: 'accept', name: name}})
    }});
    var d = await r.json().catch(function() {{ return {{}}; }});
    if (r.ok) {{ window.location.reload(); }}
    else {{ showErr(d.detail || 'Something went wrong. Please try again.'); btn.disabled = false; btn.textContent = '✓ Accept Terms & Conditions'; }}
  }} catch(e) {{ showErr('Network error: ' + e.message); btn.disabled = false; btn.textContent = '✓ Accept Terms & Conditions'; }}
}}

async function doForward() {{
  hideErr();
  var fwdName = document.getElementById('fwd-name').value.trim();
  var fwdEmail = document.getElementById('fwd-email').value.trim();
  if (!fwdName) {{ showErr("Please enter the reviewer's name."); return; }}
  if (!/^[^@\s]+@[^@\s]+\.[^@\s]+$/.test(fwdEmail)) {{
    showErr("Please enter a valid email address."); return; }}
  var btn = document.getElementById('fwd-btn'); btn.disabled = true;
  try {{
    var r = await fetch('/api/v1/documents/review/{onboarding_id}/{doc_type}/{token}', {{
      method: 'POST', headers: {{'Content-Type': 'application/json'}},
      body: JSON.stringify({{action: 'forward', forward_name: fwdName, forward_email: fwdEmail}})
    }});
    var d = await r.json().catch(function() {{ return {{}}; }});
    if (r.ok) {{ alert(d.message || 'Review link sent successfully.'); btn.disabled = false; }}
    else {{ showErr(d.detail || 'Something went wrong.'); btn.disabled = false; }}
  }} catch(e) {{ showErr('Network error: ' + e.message); btn.disabled = false; }}
}}
</script>
</body></html>"""


class _ReviewBody(BaseModel):
    action: str            # accept | comment | forward
    name: str = ""
    comments: str = ""
    edited_html: str = ""  # inline HTML changes made by P2 in the review editor
    forward_name: str = ""    # action=forward — the right person on the lead's side
    forward_email: str = ""
    comment_ids: list[str] = []   # client-assigned IDs matching data-cmt in edited_html


@router.post("/review/{onboarding_id}/{doc_type}/{token}")
async def document_review(onboarding_id: str, doc_type: str, token: str,
                          body: _ReviewBody, background_tasks: BackgroundTasks,
                          db: AsyncSession = Depends(get_db)):
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
        background_tasks.add_task(_crm_stage_safe, rec, lead, onboarding_id,
                                   stage=f"{short} Terms Accepted",
                                   detail=f"T&C accepted by {name}; internal signature pending")
        logger.info("document_terms_accepted", onboarding_id=onboarding_id, doc_type=doc_type, by=name)
        return {"message": "Terms accepted"}

    if body.action == "comment":
        text = body.comments.strip()[:4000]
        if not text:
            raise HTTPException(400, "Comments are required")
        comments = data.setdefault("comments", [])
        import secrets as _sec

        # Build individual (id, text) pairs.
        # If client sent comment_ids (one per edit round), parse the combined text back into
        # individual texts so each changed element gets its own comment entry.
        client_ids = [cid.strip() for cid in body.comment_ids if cid.strip()]
        if client_ids and len(client_ids) > 1:
            # Combined text is "Change 1: ...\n\nChange 2: ..." — split it back
            import re as _re2
            parts = _re2.split(r'\nChange \d+: ', text)
            # First part may start without the prefix if len==1
            if parts and parts[0].startswith('Change 1: '):
                parts[0] = parts[0][len('Change 1: '):]
            # Pad/trim to match ids
            while len(parts) < len(client_ids):
                parts.append(parts[-1] if parts else text)
            pairs = list(zip(client_ids, parts))
        elif client_ids:
            pairs = [(client_ids[0], text)]
        else:
            pairs = [(f"c{int(now.timestamp())}_{_sec.token_hex(4)}", text)]

        for cmt_id, cmt_text in pairs:
            cmt_text = cmt_text.strip()[:4000] or text[:4000]
            entry: dict = {
                "id": cmt_id,
                "by": name,
                "party": "p2",
                "email": lead.email,
                "at": _fmt(now),
                "status": "pending",
                "thread": [{"by": name, "party": "p2", "action": "comment", "text": cmt_text, "at": _fmt(now)}],
            }
            try:
                import asyncio
                from app.services.onboarding_ai import summarize_doc_comment
                ai_actions = await asyncio.wait_for(
                    asyncio.to_thread(summarize_doc_comment, cmt_text), timeout=15)
                entry["ai_actions"] = ai_actions
            except Exception:
                entry["ai_actions"] = []
            comments.append(entry)

        # Save the edited HTML (elements already carry their data-cmt IDs from the client)
        if body.edited_html and body.edited_html.strip():
            from app.services.pdf_documents import sanitize_live_html
            if len(body.edited_html) <= 800_000:
                data["html"] = sanitize_live_html(body.edited_html)
                data["mode"] = "live"
                data["editor_v"] = 2
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
        background_tasks.add_task(_crm_stage_safe, rec, lead, onboarding_id,
                                   f"{short} Changes Requested",
                                   f"Lead comment: {text[:180]}")
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
        review_url = make_doc_portal_url(onboarding_id, doc_type)
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
        background_tasks.add_task(_crm_stage_safe, rec, lead, onboarding_id,
                                   f"{short} Review Forwarded",
                                   f"T&C review forwarded by {name} to {fwd_name} <{fwd_email}>")
        logger.info("document_review_forwarded", onboarding_id=onboarding_id,
                    doc_type=doc_type, by=name, to=fwd_email)
        return {"message": f"Review link sent to {fwd_name}"}

    raise HTTPException(400, "Unknown action")


@router.get("/p2editor/{onboarding_id}/{doc_type}/{token}", response_class=HTMLResponse, include_in_schema=False)
async def p2_document_editor(onboarding_id: str, doc_type: str, token: str,
                             db: AsyncSession = Depends(get_db)):
    """Full live editor for the lead (P2) — opened via PATH B on the review page.
    Shows real inline track changes (strikethrough old, new text) vs the original template.
    P2 adds a mandatory comment and submits — backend saves data-cmt anchors for P1 bubbles."""
    import html as _html_lib
    _check_doc_type(doc_type)
    _check_token(onboarding_id, doc_type, token, "sign")
    rec, lead = await _load(db, onboarding_id)
    data = await _ensure_doc_data(db, rec, lead, doc_type)

    stage = data.get("stage") or ""
    if stage not in ("review", "changes_requested"):
        raise HTTPException(409, "This document is not currently open for review")

    label = _DOC_LABELS[doc_type]

    # INIT_HTML = what P2 edits (current stored version — what P1 sent)
    if data.get("mode") == "live" and data.get("html"):
        doc_html = data["html"]
    else:
        from app.services.pdf_documents import extract_editable_html
        doc_html = extract_editable_html(doc_type, _is_overseas(rec), data.get("replacements", []))

    # ORIG_HTML baseline for track-changes diff:
    #   First send (stage="review")       → baseline is P1's sent version, so only P2's new edits show as redlines
    #   Subsequent rounds (changes_requested) → baseline is original template, so ALL accumulated changes from both parties show
    if stage == "review":
        orig_html = doc_html
    else:
        from app.services.pdf_documents import extract_editable_html as _ext_orig
        orig_html = _ext_orig(doc_type, _is_overseas(rec), data.get("replacements", []))

    html_json = json.dumps(doc_html, ensure_ascii=False).replace("</", "<\\/")
    orig_html_js = json.dumps(orig_html, ensure_ascii=False).replace("</", "<\\/")
    review_url = f"/api/v1/documents/sign/{onboarding_id}/{doc_type}/{token}"
    biz = _html_lib.escape(lead.business_name)

    return HTMLResponse(f"""<!DOCTYPE html>
<html lang="en">
<head><meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Propose Changes — {label} — Jane Aerospace</title>
<script>
window.onerror = function(msg, src, line, col, err) {{
  var d = document.getElementById('_js_err_banner');
  if (!d) {{
    d = document.createElement('div');
    d.id = '_js_err_banner';
    d.style.cssText = 'position:fixed;bottom:0;left:0;right:0;background:#dc2626;color:#fff;padding:10px 14px;font-size:12px;z-index:9999;word-break:break-all;';
    document.body && document.body.appendChild(d);
  }}
  d.textContent = 'JS Error: ' + msg + ' (line ' + line + ')';
}};
</script>
<style>
  *{{box-sizing:border-box;margin:0;padding:0;}}
  body{{font-family:'Segoe UI',Arial,sans-serif;background:#e4e6ea;overflow:hidden;}}
  .work{{position:fixed;top:0;left:0;right:0;bottom:0;display:flex;flex-direction:column;}}
  .topbar{{height:52px;background:#0c2344;color:#fff;display:flex;align-items:center;
          gap:10px;padding:0 16px;flex-shrink:0;}}
  .ttl{{font-weight:800;font-size:13px;}}
  .tsub{{font-size:11px;color:#b9c8e4;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;}}
  .grow{{flex:1;}}
  .btn{{display:inline-flex;align-items:center;gap:5px;padding:8px 14px;border-radius:7px;
       border:none;color:#fff;font-size:12.5px;font-weight:700;cursor:pointer;
       font-family:inherit;text-decoration:none;white-space:nowrap;}}
  .btn:disabled{{opacity:.5;cursor:not-allowed;}}
  .b-amber{{background:#d97706;}} .b-line{{background:transparent;color:#cdd9f0;border:1.4px solid #3b5379;}}
  .b-green{{background:#16a34a;}}
  /* Editor body row */
  .body-row{{flex:1;display:flex;overflow:hidden;}}
  .edwrap{{flex:1;overflow-y:auto;padding:26px 18px 20px;display:flex;justify-content:center;}}
  /* Sidebar */
  .sb2{{width:0;overflow:hidden;transition:width .25s;background:#f8fafc;border-left:1px solid #e2e8f0;
       display:flex;flex-direction:column;flex-shrink:0;}}
  .sb2.open{{width:280px;}}
  .sb2-head{{padding:12px 14px;font-weight:800;font-size:13px;color:#0c2344;border-bottom:1px solid #e2e8f0;
            display:flex;justify-content:space-between;align-items:center;}}
  .sb2-body{{flex:1;overflow-y:auto;padding:12px;}}
  .sb2-card{{background:#fff;border:1px solid #e5e9f2;border-radius:8px;padding:12px 13px;
            margin-bottom:10px;font-size:12px;}}
  .sb2-card .thread-entry{{border-left:3px solid #e5e9f2;padding-left:9px;margin:6px 0;}}
  .sb2-empty{{color:#94a3b8;font-size:12px;text-align:center;padding:16px 0;}}
  .all-ok-banner{{background:#dcfce7;border:1px solid #86efac;color:#14532d;
                 padding:14px;border-radius:8px;margin-bottom:12px;font-size:13px;font-weight:700;}}
  /* Document sheet */
  .sheet{{background:#fff;width:794px;max-width:100%;min-height:900px;
         box-shadow:0 1px 4px rgba(0,0,0,.12),0 10px 30px rgba(12,35,68,.16);
         border:1px solid #d6dae2;border-radius:2px;
         padding:62px 58px;outline:none;
         font-family:Calibri,Carlito,'Segoe UI',sans-serif;font-size:10.5pt;line-height:1.24;color:#111;}}
  .sheet p{{margin:3pt 0;}}
  .sheet table{{border-collapse:collapse;width:100%;margin:8pt 0;table-layout:fixed;}}
  .sheet td{{border:1px solid #999;padding:4pt 6pt;vertical-align:top;word-wrap:break-word;}}
  .sheet:focus{{border-color:#bcd0f2;
    box-shadow:0 1px 4px rgba(0,0,0,.12),0 12px 34px rgba(26,86,219,.18);}}
  .sheet.review{{background:#fcfcfd;cursor:default;}}
  .hint-bar{{flex-shrink:0;background:#fffbeb;border-top:2px solid #fde68a;
             padding:10px 18px;font-size:12px;color:#92400e;line-height:1.5;}}
  #chg-count{{font-size:11.5px;color:#92400e;background:#fef3c7;border-radius:6px;
             padding:4px 10px;font-weight:700;display:none;white-space:nowrap;}}
  #review-tip{{display:none;font-size:11px;color:#1a56db;background:#eff6ff;border-radius:6px;
              padding:4px 10px;white-space:nowrap;}}
  /* Change-tracking mark highlights */
  [data-cmt]{{border-radius:3px;transition:background .3s;}}
  .chg-p1{{background:rgba(37,99,235,0.11)!important;border-left:3px solid #2563eb!important;padding-left:6px!important;}}
  .chg-p2{{background:rgba(245,158,11,0.14)!important;border-left:3px solid #f59e0b!important;padding-left:6px!important;}}
  .chg-accepted{{background:rgba(22,163,74,0.09)!important;border-left:3px solid #16a34a!important;padding-left:6px!important;}}
  .chg-rejected{{background:rgba(239,68,68,0.09)!important;border-left:3px solid #dc2626!important;padding-left:6px!important;}}
  .jump-flash{{animation:cmtFlash 1.7s ease;}}
  @keyframes cmtFlash{{0%,100%{{outline:none;}}25%,65%{{outline:3px solid #f59e0b;outline-offset:3px;}}}}
  /* Floating comment bubbles */
  .chg-bubble{{position:fixed;width:228px;background:#fff;border:1px solid #e2e8f0;
    border-radius:10px;padding:10px 12px;
    box-shadow:0 4px 18px rgba(15,23,42,.13);font-size:12px;font-family:'Segoe UI',Arial,sans-serif;
    z-index:45;cursor:pointer;display:none;transition:box-shadow .15s,border-color .15s;}}
  .chg-bubble:hover,.chg-bubble.bub-focus{{box-shadow:0 7px 26px rgba(37,99,235,.22);border-color:#93c5fd;}}
  .bub-rej-box textarea{{width:100%;min-height:58px;padding:6px 8px;border:1px solid #fca5a5;
    border-radius:6px;font-size:11.5px;font-family:inherit;resize:vertical;margin-top:6px;}}
  /* Redline styles */
  ins.rl-ins{{background:none;color:#1a56db;text-decoration:underline;
    text-decoration-thickness:1px;text-underline-offset:2px;padding:0;}}
  del.rl-del{{background:none;color:#c0182f;text-decoration:line-through;
    text-decoration-thickness:1px;padding:0;}}
  p.rl-blank{{margin:0;height:8px;}}
  [data-cmt="temp"]{{border-left:3px solid #f59e0b!important;padding-left:6px!important;border-radius:3px;}}
  #cmt-badge{{background:#ef4444;color:#fff;border-radius:99px;padding:1px 6px;
             font-size:10px;font-weight:700;margin-left:3px;display:none;}}
  @media(max-width:860px){{.sheet{{padding:28px 16px;width:100%;}}}}
</style></head>
<body>
<div class="work">
  <div class="topbar">
    <div>
      <div class="ttl">&#x270F; Propose Changes — {label}</div>
      <div class="tsub">{biz} &middot; Click to edit &bull; click away to see redlines</div>
    </div>
    <span class="grow"></span>
    <span id="review-tip">&#x1F441; Viewing track changes — click document to edit</span>
    <span id="chg-count">0 sections changed</span>
    <button class="btn b-line" id="sb2-btn" onclick="toggleSb2()" title="View comments from Jane">
      &#x1F4AC; Comments<span id="cmt-badge"></span></button>
    <a class="btn b-line" href="{review_url}">&#x2190; Back to Review</a>
    <button class="btn b-amber" id="sub-btn" onclick="submitChanges()">
      &#x2709; Submit Changes</button>
  </div>
  <div class="body-row">
    <div class="edwrap">
      <div class="sheet" id="sheet" contenteditable="true" spellcheck="true"></div>
    </div>
    <div class="sb2" id="sb2">
      <div class="sb2-head">
        <span>&#x1F4AC; Comments &amp; Changes</span>
        <button onclick="toggleSb2()" style="background:none;border:none;color:#64748b;cursor:pointer;font-size:16px;">&#x2715;</button>
      </div>
      <div class="sb2-body" id="sb2-body"><p class="sb2-empty">Loading…</p></div>
    </div>
  </div>
  <!-- Comment bar: appears inline below the document when P2 makes an edit -->
  <div id="cmt-bar" style="display:none;flex-direction:column;gap:8px;background:#fffbeb;
       border-top:3px solid #f59e0b;padding:14px 18px;">
    <div style="font-size:14px;font-weight:800;color:#92400e;">
      &#x270F; You made a change — explain it before continuing</div>
    <div style="font-size:12px;color:#78350f;">
      Jane Aerospace will review this. Describe what you changed and why it is needed.</div>
    <textarea id="cmt-bar-ta" rows="3"
      placeholder="e.g. Changed notice period from 30 to 60 days to align with our standard contract terms."
      style="width:100%;padding:10px 12px;border:1.5px solid #f59e0b;border-radius:8px;font-size:13px;
             font-family:inherit;resize:vertical;outline:none;box-sizing:border-box;background:#fff;"></textarea>
    <div id="cmt-bar-err" style="display:none;color:#dc2626;font-size:12px;"></div>
    <div style="display:flex;gap:10px;flex-wrap:wrap;">
      <button onclick="_saveComment()"
        style="background:#d97706;color:#fff;border:none;border-radius:7px;
               padding:10px 22px;font-size:13px;font-weight:700;cursor:pointer;font-family:inherit;flex:1;">
        &#x2713; Save Comment &amp; Continue Editing</button>
    </div>
  </div>
  <!-- Saved comments list + submit -->
  <div id="session-cmts" style="display:none;background:#f8fafc;border-top:1px solid #e2e8f0;
       padding:10px 18px;max-height:220px;overflow-y:auto;"></div>
  <div class="hint-bar">
    &#x270F; <b>Tap</b> the document to edit &rarr; a yellow comment box appears below &rarr; explain your change &rarr; save &rarr; repeat for each change &rarr; tap <b>Submit Changes</b> when done.
    &nbsp;&nbsp;<span id="cmt-saved-info" style="display:none;background:#dcfce7;color:#14532d;border-radius:6px;padding:2px 10px;font-weight:700;"></span>
  </div>
</div>
<script>
var BASE = '/api/v1/documents';
var OID  = '{onboarding_id}';
var DT   = '{doc_type}';
var TOK  = '{token}';
// Globals required by _EDITOR_RIBBON_JS track-changes system
var ORIG_HTML = {orig_html_js};
var ORIG_TEXT = '';
var SIGNED = false;
var sheet = document.getElementById('sheet');
// Populate sheet with current document content before ribbon JS fires
sheet.innerHTML = {html_json};
// Stubs for ribbon functions not used in p2editor
function cmd(){{}} function dirty(){{}} function restoreRange(){{}} function saveRange(){{}}
</script>
<script>{_EDITOR_RIBBON_JS}</script>
<script>
// ── Comment bubble system (P2 can accept/reject P1's comments) ──

function _chgClass(party, status) {{
  if (status === 'accepted') return 'chg-accepted';
  if (status === 'rejected') return 'chg-rejected';
  return party === 'p1' ? 'chg-p1' : 'chg-p2';
}}

function _applyMarkColors(comments) {{
  var map = {{}};
  (comments || []).forEach(function(c) {{ map[c.id] = c; }});
  sheet.querySelectorAll('[data-cmt]').forEach(function(el) {{
    var cid = el.getAttribute('data-cmt');
    if (!cid || cid === 'temp') return;
    var c = map[cid]; if (!c) return;
    el.classList.remove('chg-p1','chg-p2','chg-accepted','chg-rejected');
    el.classList.add(_chgClass(c.party, c.status));
    el.style.cursor = 'pointer';
    el.title = 'Click to jump to comment';
    el.onclick = (function(id) {{ return function() {{ jumpToChange(id); }}; }})(cid);
  }});
}}

function renderBubbles(comments) {{
  document.querySelectorAll('.chg-bubble').forEach(function(b) {{ b.remove(); }});
  if (!comments || !comments.length) return;
  var map = {{}};
  comments.forEach(function(c) {{ map[c.id] = c; }});
  var seen = {{}};
  sheet.querySelectorAll('[data-cmt]').forEach(function(mark) {{
    var cid = mark.getAttribute('data-cmt');
    if (!cid || cid === 'temp' || seen[cid]) return;
    seen[cid] = true;
    var c = map[cid]; if (!c) return;
    var status = c.status || 'pending';
    var pCol  = c.party === 'p1' ? '#1a56db' : '#059669';
    var sCol  = {{pending:'#f59e0b',accepted:'#16a34a',rejected:'#dc2626'}}[status] || '#94a3b8';
    var pLbl  = c.party === 'p1' ? 'Jane' : 'You';
    // Full thread display
    var threadHtml = '';
    (c.thread || []).forEach(function(t) {{
      var tCol = t.party === 'p1' ? '#1a56db' : '#059669';
      threadHtml += '<div style="border-left:2px solid #e5e9f2;padding-left:7px;margin-top:6px;">' +
        '<span style="font-size:10px;font-weight:700;color:' + tCol + ';">' +
          (t.by || '') + ' — ' + (t.action || 'comment') + '</span>' +
        '<div style="font-size:11px;color:#374151;margin-top:2px;white-space:pre-wrap;">' +
          (t.text || '').slice(0, 200) + '</div></div>';
    }});
    // Only show Accept/Reject for P1's comments that are pending AND P2 has not yet commented
    var accRej = (status === 'pending' && c.party === 'p1') ? (
      '<div style="display:flex;gap:5px;margin-top:7px;">' +
      '<button data-bub-cid="' + cid + '" onclick="event.stopPropagation();_bubAccept(this)" ' +
        'style="flex:1;background:#16a34a;color:#fff;border:none;border-radius:5px;padding:4px 0;font-size:11px;font-weight:700;cursor:pointer;">&#x2713; Accept</button>' +
      '<button data-bub-cid="' + cid + '" onclick="event.stopPropagation();_bubReject(this)" ' +
        'style="flex:1;background:#fee2e2;color:#dc2626;border:1px solid #fca5a5;border-radius:5px;padding:4px 0;font-size:11px;font-weight:700;cursor:pointer;">&#x2717; Reject</button>' +
      '</div>') : (status === 'rejected' && c.party === 'p1' ? (
      '<div style="display:flex;gap:5px;margin-top:7px;">' +
      '<button data-bub-cid="' + cid + '" onclick="event.stopPropagation();_bubReply(this)" ' +
        'style="flex:1;background:#f1f5f9;color:#374151;border:1px solid #d4dae6;border-radius:5px;padding:4px 0;font-size:11px;font-weight:700;cursor:pointer;">&#x21A9; Reply</button>' +
      '</div>') : '');
    var bub = document.createElement('div');
    bub.className = 'chg-bubble';
    bub.id = 'bub-' + cid;
    bub.setAttribute('data-for', cid);
    bub.innerHTML =
      '<div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:5px;">' +
        '<span style="font-size:11px;font-weight:700;color:' + pCol + '">' + (c.by||'') +
          ' <span style="color:#94a3b8;font-weight:400;">(' + pLbl + ')</span></span>' +
        '<span style="font-size:9.5px;font-weight:700;background:' + sCol + ';color:#fff;padding:1px 6px;border-radius:99px;">' + status + '</span>' +
      '</div>' + threadHtml +
      '<div style="font-size:10px;color:#94a3b8;margin-top:5px;">' + (c.at||'') + '</div>' +
      accRej;
    bub.onclick = (function(id) {{ return function() {{ jumpToChange(id); }}; }})(cid);
    document.body.appendChild(bub);
  }});
  positionBubbles();
}}

function positionBubbles() {{
  var sheetRect = sheet.getBoundingClientRect();
  var sb2 = document.getElementById('sb2');
  var sbWidth = sb2 && sb2.classList.contains('open') ? sb2.offsetWidth : 0;
  var bubLeft = Math.min(sheetRect.right + 14, window.innerWidth - sbWidth - 244);
  var seen = {{}};
  sheet.querySelectorAll('[data-cmt]').forEach(function(mark) {{
    var cid = mark.getAttribute('data-cmt');
    if (!cid || cid === 'temp' || seen[cid]) return;
    seen[cid] = true;
    var bub = document.getElementById('bub-' + cid); if (!bub) return;
    var rect = mark.getBoundingClientRect();
    var top  = Math.max(60, rect.top - 8);
    bub.style.top  = top + 'px';
    bub.style.left = bubLeft + 'px';
    bub.style.display = (rect.top < -300 || rect.top > window.innerHeight + 120) ? 'none' : 'block';
  }});
}}

function jumpToChange(cmtId) {{
  var el = sheet.querySelector('[data-cmt="' + cmtId + '"]');
  if (el) {{
    el.scrollIntoView({{behavior:'smooth',block:'center'}});
    el.classList.add('jump-flash');
    setTimeout(function() {{ el.classList.remove('jump-flash'); }}, 1800);
  }}
  var bub = document.getElementById('bub-' + cmtId);
  if (bub) {{ bub.classList.add('bub-focus'); setTimeout(function() {{ bub.classList.remove('bub-focus'); }}, 1800); }}
}}

function _bubAccept(btn) {{
  _commentAction(btn.getAttribute('data-bub-cid'), 'accept', '');
}}
function _bubReject(btn) {{
  _showBubRejBox(btn.getAttribute('data-bub-cid'));
}}
function _bubReply(btn) {{
  _showBubRejBox(btn.getAttribute('data-bub-cid'));
}}
function _showBubRejBox(cmtId) {{
  var bub = document.getElementById('bub-' + cmtId); if (!bub) return;
  if (bub.querySelector('.bub-rej-box')) return;
  var box = document.createElement('div');
  box.className = 'bub-rej-box';
  box.innerHTML =
    '<textarea placeholder="Explain your concern (required)…"></textarea>' +
    '<div style="display:flex;gap:5px;margin-top:5px;">' +
      '<button data-bub-cid="' + cmtId + '" onclick="event.stopPropagation();_sendBubRejClick(this)" ' +
        'style="background:#dc2626;color:#fff;border:none;border-radius:5px;padding:4px 10px;font-size:11px;font-weight:700;cursor:pointer;">Send</button>' +
      '<button onclick="event.stopPropagation();this.closest(\\'.bub-rej-box\\').remove()" ' +
        'style="background:#f1f5f9;color:#374151;border:1px solid #d4dae6;border-radius:5px;padding:4px 10px;font-size:11px;cursor:pointer;">Cancel</button>' +
    '</div>';
  bub.appendChild(box);
}}
function _sendBubRejClick(btn) {{
  var txt = btn.closest('.bub-rej-box').querySelector('textarea').value.trim();
  if (!txt) {{ alert('Please explain your concern.'); return; }}
  _commentAction(btn.getAttribute('data-bub-cid'), 'reject', txt);
}}

async function _commentAction(commentId, action, text) {{
  var r = await fetch(BASE + '/comment-action/' + OID + '/' + DT + '/' + TOK, {{
    method: 'POST', headers: {{'Content-Type': 'application/json'}},
    body: JSON.stringify({{comment_id: commentId, action: action, text: text}})
  }}).catch(function() {{ return null; }});
  if (r && r.ok) {{
    var d = await r.json().catch(function() {{ return {{}}; }});
    loadComments();
    if (d.all_accepted) {{
      document.getElementById('err-bar').style.display = 'none';
      var banner = document.getElementById('all-ok');
      if (!banner) {{
        banner = document.createElement('div');
        banner.id = 'all-ok';
        banner.style.cssText = 'position:fixed;top:60px;left:50%;transform:translateX(-50%);' +
          'background:#16a34a;color:#fff;padding:14px 28px;border-radius:10px;font-size:14px;font-weight:700;' +
          'z-index:999;box-shadow:0 4px 18px rgba(0,0,0,.2);text-align:center;';
        banner.innerHTML = '&#x2705; All changes agreed by both parties!<br>' +
          '<span style="font-size:12px;font-weight:400;">Jane Aerospace will now proceed to sign — you will receive an email to complete your signature.</span>';
        document.body.appendChild(banner);
      }}
    }}
  }} else {{
    var errD = r ? await r.json().catch(function() {{ return {{}}; }}) : {{}};
    alert(errD.detail || 'Action failed. Please try again.');
  }}
}}

window.addEventListener('scroll', positionBubbles, {{passive:true}});
window.addEventListener('resize', positionBubbles, {{passive:true}});

// ── Sidebar ──
function toggleSb2() {{
  var sb = document.getElementById('sb2');
  sb.classList.toggle('open');
  if (sb.classList.contains('open')) loadComments();
  positionBubbles();
}}

function _statusBadge(status) {{
  var map = {{pending:'#f59e0b',accepted:'#16a34a',rejected:'#dc2626'}};
  var label = {{pending:'Pending',accepted:'Accepted',rejected:'Rejected'}};
  return '<span style="display:inline-block;padding:1px 7px;border-radius:99px;font-size:10px;font-weight:700;background:' +
    (map[status]||'#64748b') + ';color:#fff;">' + (label[status]||status) + '</span>';
}}

async function loadComments() {{
  var body = document.getElementById('sb2-body');
  var cmts = [];
  try {{
    var r = await fetch(BASE + '/versions/' + OID + '/' + DT + '/' + TOK);
    if (r.ok) {{ var d = await r.json(); cmts = d.comments || []; }}
  }} catch(e) {{}}

  var open = cmts.filter(function(c) {{ return !c.status || c.status === 'pending' || c.status === 'rejected'; }});
  var accepted = cmts.filter(function(c) {{ return c.status === 'accepted'; }});

  var badge = document.getElementById('cmt-badge');
  if (open.length > 0) {{ badge.textContent = open.length; badge.style.display='inline'; }}
  else {{ badge.style.display='none'; }}

  body.innerHTML = '';
  if (cmts.length === 0) {{
    body.innerHTML = '<p class="sb2-empty">No comments yet — Jane Aerospace has not reviewed your changes yet.</p>';
  }} else {{
    var allOk = cmts.length > 0 && cmts.every(function(c) {{ return c.status === 'accepted'; }});
    if (allOk) {{
      var okDiv = document.createElement('div'); okDiv.className = 'all-ok-banner';
      okDiv.innerHTML = '&#x2705; All changes agreed! Signing process will begin shortly.';
      body.appendChild(okDiv);
    }} else if (open.length > 0) {{
      var warn = document.createElement('div');
      warn.style.cssText = 'background:#fff7ed;border:1px solid #fed7aa;color:#92400e;padding:10px;border-radius:7px;font-size:12px;margin-bottom:10px;';
      warn.textContent = open.length + ' comment(s) still need resolution.';
      body.appendChild(warn);
    }}
    var sec1 = document.createElement('div');
    sec1.style.cssText = 'font-size:11px;font-weight:700;color:#64748b;margin:8px 0 6px;text-transform:uppercase;letter-spacing:.06em;';
    sec1.textContent = 'Open (' + open.length + ')';
    body.appendChild(sec1);
    open.forEach(function(c) {{ body.appendChild(_buildSb2Card(c)); }});
    var sec2 = document.createElement('div');
    sec2.style.cssText = 'font-size:11px;font-weight:700;color:#64748b;margin:12px 0 6px;text-transform:uppercase;letter-spacing:.06em;';
    sec2.textContent = 'Accepted (' + accepted.length + ')';
    body.appendChild(sec2);
    if (accepted.length === 0) {{
      var emp = document.createElement('p'); emp.className = 'sb2-empty'; emp.textContent = 'None yet.';
      body.appendChild(emp);
    }} else {{ accepted.forEach(function(c) {{ body.appendChild(_buildSb2Card(c)); }}); }}
  }}
  renderBubbles(cmts);
  _applyMarkColors(cmts);
}}

function _buildSb2Card(c) {{
  var status = c.status || 'pending';
  var borderCol = c.party === 'p1' ? '#2563eb' : '#f59e0b';
  var card = document.createElement('div');
  card.className = 'sb2-card';
  card.style.borderLeft = '3px solid ' + borderCol;
  var thread = c.thread || [];
  var threadHtml = thread.map(function(t) {{
    var tCol = t.party === 'p1' ? '#1a56db' : '#059669';
    return '<div class="thread-entry">' +
      '<div style="font-size:10.5px;font-weight:700;color:' + tCol + ';">' +
        (t.by||'') + ' — ' + (t.action||'comment') + ' — ' + (t.at||'') + '</div>' +
      '<div style="font-size:12px;color:#374151;margin-top:2px;white-space:pre-wrap;">' + (t.text||'') + '</div>' +
    '</div>';
  }}).join('');
  var hasAnchor = !!sheet.querySelector('[data-cmt="' + c.id + '"]');
  var jumpBtn = hasAnchor
    ? '<button onclick="jumpToChange(\\'' + c.id + '\\')" style="background:#eff6ff;color:#1a56db;border:1px solid #bfdbfe;border-radius:5px;padding:3px 9px;font-size:10.5px;font-weight:700;cursor:pointer;margin:5px 0 0;">&#x2197; Jump to change</button>'
    : '';
  card.innerHTML =
    '<div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:5px;">' +
      '<span style="font-size:12px;font-weight:700;color:#374151;">' + (c.by||'Lead') +
        ' <span style="font-weight:400;color:#94a3b8;">(' + (c.party==='p1'?'Jane':'You') + ')</span></span>' +
      _statusBadge(status) +
    '</div>' +
    '<div style="font-size:10px;color:#94a3b8;margin-bottom:5px;">' + (c.at||'') + '</div>' +
    (jumpBtn ? '<div>' + jumpBtn + '</div>' : '') +
    '<div id="sb2-thread-' + c.id + '">' + threadHtml + '</div>';
  // P2 can Accept / Reject P1's pending or rejected comments
  if (c.party === 'p1' && (status === 'pending' || status === 'rejected')) {{
    var acts = document.createElement('div');
    acts.style.cssText = 'display:flex;gap:7px;margin-top:8px;flex-wrap:wrap;';
    if (status === 'pending') {{
      var acc = document.createElement('button');
      acc.textContent = '✓ Accept';
      acc.style.cssText = 'background:#16a34a;color:#fff;border:none;border-radius:6px;padding:5px 12px;font-size:12px;font-weight:700;cursor:pointer;';
      acc.onclick = (function(id) {{ return function() {{ _commentAction(id,'accept',''); }}; }})(c.id);
      acts.appendChild(acc);
    }}
    var rejBtn = document.createElement('button');
    rejBtn.textContent = status === 'rejected' ? '↩ Reply' : '✗ Reject';
    rejBtn.style.cssText = 'background:#fee2e2;color:#dc2626;border:1px solid #fca5a5;border-radius:6px;padding:5px 12px;font-size:12px;font-weight:700;cursor:pointer;';
    rejBtn.onclick = (function(id, container) {{ return function() {{ _showSb2RejBox(id, container); }}; }})(c.id, acts);
    acts.appendChild(rejBtn);
    card.appendChild(acts);
  }}
  return card;
}}

function _showSb2RejBox(commentId, container) {{
  if (container.querySelector('.rej-box')) return;
  var box = document.createElement('div');
  box.className = 'rej-box';
  box.style.cssText = 'margin-top:8px;width:100%;';
  var ta = document.createElement('textarea');
  ta.placeholder = 'Explain your concern (required)…';
  ta.style.cssText = 'width:100%;min-height:60px;padding:8px;border:1px solid #fca5a5;border-radius:6px;font-size:12px;font-family:inherit;resize:vertical;';
  var btns = document.createElement('div');
  btns.style.cssText = 'display:flex;gap:6px;margin-top:6px;';
  var sendBtn = document.createElement('button');
  sendBtn.textContent = 'Send';
  sendBtn.style.cssText = 'background:#dc2626;color:#fff;border:none;border-radius:6px;padding:5px 12px;font-size:12px;font-weight:700;cursor:pointer;';
  sendBtn.onclick = (function(cid, textarea) {{
    return function() {{
      var txt = textarea.value.trim();
      if (!txt) {{ alert('Please explain your concern.'); return; }}
      _commentAction(cid, 'reject', txt);
    }};
  }})(commentId, ta);
  var cancelBtn = document.createElement('button');
  cancelBtn.textContent = 'Cancel';
  cancelBtn.style.cssText = 'background:#eef1f6;color:#374151;border:1px solid #d4dae6;border-radius:6px;padding:5px 12px;font-size:12px;cursor:pointer;';
  cancelBtn.onclick = function() {{ box.remove(); }};
  btns.appendChild(sendBtn);
  btns.appendChild(cancelBtn);
  box.appendChild(ta);
  box.appendChild(btns);
  container.appendChild(box);
}}

// ── Main editor logic ──
(function() {{
  var INIT_HTML = {html_json};
  try {{ sheet.innerHTML = INIT_HTML; }} catch(e) {{}}

  var _hasEdit   = false;   // user has edited since last comment save
  var _comments  = [];      // {{id, text}} per saved comment
  var _cmtSeq    = 0;       // unique ID counter
  var _idleTimer = null;    // fires comment popup after 1.5s idle

  // ── Exit review mode before any edit so innerHTML isn't clobbered mid-input ──
  // ── Exit review mode before keystroke so innerHTML isn't clobbered mid-edit ──
  function _exitIfReviewing() {{
    if (window._reviewing && typeof window._tcExitReview === 'function') window._tcExitReview();
  }}
  sheet.addEventListener('keydown',    _exitIfReviewing);
  sheet.addEventListener('touchstart', _exitIfReviewing, {{passive: true}});
  sheet.addEventListener('click',      _exitIfReviewing);

  // ── Detect edits — show the inline comment bar immediately ──
  sheet.addEventListener('input', function() {{
    _hasEdit = true;
    // Stamp the block being edited
    var sel = window.getSelection();
    if (sel && sel.rangeCount) {{
      var node = sel.getRangeAt(0).startContainer;
      while (node && node.parentElement && node.parentElement !== sheet) node = node.parentElement;
      if (node && node !== sheet && node.isConnected && !node.getAttribute('data-cmt'))
        node.setAttribute('data-cmt', 'temp');
    }}
    _showCommentBar();
    _updateUI();
  }});

  // ── Show / hide the inline comment bar ──
  function _showCommentBar() {{
    var bar = document.getElementById('cmt-bar');
    if (bar) bar.style.display = 'flex';
  }}
  function _hideCommentBar() {{
    var bar = document.getElementById('cmt-bar');
    if (bar) bar.style.display = 'none';
    var ta  = document.getElementById('cmt-bar-ta');
    var err = document.getElementById('cmt-bar-err');
    if (ta)  ta.value = '';
    if (err) err.style.display = 'none';
  }}

  // ── UI state ──
  function _updateUI() {{
    var cc  = document.getElementById('chg-count');
    var inf = document.getElementById('cmt-saved-info');
    var btn = document.getElementById('sub-btn');
    var c   = _comments.length;
    cc.style.display = 'none';
    if (c > 0) {{
      inf.textContent = c + ' comment' + (c === 1 ? ' saved' : 's saved');
      inf.style.display = 'inline-block';
    }} else {{
      inf.style.display = 'none';
    }}
    if (c > 0 && !_hasEdit) {{
      btn.style.background = '#16a34a';
    }} else {{
      btn.style.background = '';
    }}
    _renderSessionComments();
  }}

  // ── Save a comment ──
  window._saveComment = function() {{
    var ta  = document.getElementById('cmt-bar-ta');
    var err = document.getElementById('cmt-bar-err');
    var txt = ta ? ta.value.trim() : '';
    if (!txt) {{
      if (err) {{ err.textContent = 'Please describe your change before saving.'; err.style.display = 'block'; }}
      if (ta) ta.focus();
      return;
    }}
    // Stamp all temp-marked elements with a permanent per-comment ID
    var cmtId = 'p2c_' + Date.now() + '_' + (++_cmtSeq);
    sheet.querySelectorAll('[data-cmt="temp"]').forEach(function(el) {{
      el.setAttribute('data-cmt', cmtId);
    }});
    _comments.push({{ id: cmtId, text: txt }});
    _hasEdit = false;
    _hideCommentBar();
    _updateUI();
  }};

  // ── Jump to changed paragraph ──
  window._jumpToChange = function(id) {{
    if (window._reviewing && typeof window._tcExitReview === 'function') window._tcExitReview();
    setTimeout(function() {{
      var el = sheet.querySelector('[data-cmt="' + id + '"]');
      if (!el) return;
      el.scrollIntoView({{ behavior: 'smooth', block: 'center' }});
      el.style.outline = '2.5px solid #f59e0b';
      el.style.borderRadius = '3px';
      setTimeout(function() {{ el.style.outline = ''; el.style.borderRadius = ''; }}, 1800);
    }}, 120);
  }};

  // ── In-session saved comment list ──
  function _esc(s) {{ return String(s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;'); }}
  function _renderSessionComments() {{
    var panel = document.getElementById('session-cmts');
    if (!panel) return;
    if (_comments.length === 0) {{ panel.style.display = 'none'; return; }}
    panel.style.display = 'block';
    panel.innerHTML = '<div style="font-size:11px;font-weight:700;color:#64748b;margin-bottom:6px;' +
      'text-transform:uppercase;letter-spacing:.5px;">Your saved comments this session</div>' +
      _comments.map(function(c, i) {{
        var preview = c.text.length > 80 ? c.text.slice(0, 80) + '…' : c.text;
        return '<div style="display:flex;align-items:center;gap:8px;padding:6px 10px;' +
          'background:#fff;border:1px solid #e2e8f0;border-radius:7px;margin-bottom:5px;">' +
          '<div style="flex:1;font-size:12px;color:#374151;">' +
            '<span style="font-weight:700;color:#d97706;">Change ' + (i+1) + ':</span> ' + _esc(preview) +
          '</div>' +
          '<button onclick="_jumpToChange(\\'' + c.id + '\\')" style="white-space:nowrap;' +
            'background:#eff6ff;color:#1d4ed8;border:1px solid #bfdbfe;border-radius:5px;' +
            'padding:3px 10px;font-size:11.5px;cursor:pointer;font-family:inherit;">&#x2192; Jump</button>' +
          '</div>';
      }}).join('');
  }}

  // Ctrl/Cmd+Enter in comment bar saves quickly
  var _cmtBarTa = document.getElementById('cmt-bar-ta');
  if (_cmtBarTa) _cmtBarTa.addEventListener('keydown', function(e) {{
    if ((e.ctrlKey || e.metaKey) && e.key === 'Enter') {{ e.preventDefault(); _saveComment(); }}
  }});

  // ── Submit all changes to Jane ──
  window.submitChanges = async function() {{
    if (_hasEdit) {{ _showCommentBar(); alert('Please save your comment first before submitting.'); return; }}
    if (_comments.length === 0) {{
      alert('Please make at least one change to the document and add a comment explaining it before submitting.');
      return;
    }}
    var combined = _comments.length === 1
      ? _comments[0].text
      : _comments.map(function(c, i) {{ return 'Change ' + (i+1) + ': ' + c.text; }}).join('\\n\\n');
    if (window._reviewing && typeof window._tcExitReview === 'function') window._tcExitReview();
    var html = sheet.innerHTML;
    var btn  = document.getElementById('sub-btn');
    btn.disabled = true; btn.textContent = 'Submitting…';
    try {{
      var r = await fetch(BASE + '/review/' + OID + '/' + DT + '/' + TOK, {{
        method: 'POST', headers: {{'Content-Type': 'application/json'}},
        body: JSON.stringify({{
          action: 'comment', name: '', comments: combined,
          comment_ids: _comments.map(function(c) {{ return c.id; }}),
          edited_html: html
        }})
      }});
      var d = await r.json().catch(function() {{ return {{}}; }});
      if (r.ok) {{
        window.location.href = '{review_url}';
      }} else {{
        alert(d.detail || 'Something went wrong. Please try again.');
        btn.disabled = false; btn.innerHTML = '&#x2709; Submit Changes';
      }}
    }} catch(e) {{
      alert('Network error: ' + e.message);
      btn.disabled = false;
    }}
  }};

  _updateUI();
  loadComments();
}})();
</script>
</body></html>""")


@router.get("/portal/{onboarding_id}/{doc_type}/{token}")
async def document_portal(onboarding_id: str, doc_type: str, token: str,
                          db: AsyncSession = Depends(get_db)):
    """Lead-facing document portal — validates token then serves the sign/review page.
    This URL is what gets emailed; the underlying sign page handles stage routing."""
    _check_doc_type(doc_type)
    _check_token(onboarding_id, doc_type, token, "sign")
    await _load(db, onboarding_id)   # confirm record exists before redirect
    return RedirectResponse(
        url=f"/api/v1/documents/sign/{onboarding_id}/{doc_type}/{token}",
        status_code=302,
    )


def _make_edit_url(_onboarding_id: str, _doc_type: str) -> str:
    """Returns the Jane team dashboard URL — P1 manages all document actions from there."""
    return f"{settings.APP_URL.rstrip('/')}/api/v1/dashboard"


import re as _re


def _strip_redlines(html_str: str) -> str:
    """Remove <del class="rl-del"> elements (deleted words) and unwrap <ins class="rl-ins">
    (keeping inserted text) to produce a clean agreed copy.  Also removes data-cmt markers."""
    # Remove deleted content
    html_str = _re.sub(
        r'<del\b[^>]*class="[^"]*rl-del[^"]*"[^>]*>.*?</del>',
        '', html_str, flags=_re.DOTALL | _re.IGNORECASE)
    # Unwrap inserted content (keep the text inside)
    html_str = _re.sub(
        r'<ins\b[^>]*class="[^"]*rl-ins[^"]*"[^>]*>(.*?)</ins>',
        r'\1', html_str, flags=_re.DOTALL | _re.IGNORECASE)
    # Strip data-cmt markers from all elements
    html_str = _re.sub(r'\s*data-cmt="[^"]*"', '', html_str)
    return html_str


_CHG_CLASSES = frozenset({'chg-block', 'chg-p1', 'chg-p2', 'chg-accepted', 'chg-rejected'})


def _strip_editor_marks(html_str: str) -> str:
    """Called at send time: strip P1-side ephemeral editing artefacts from HTML.

    Removes:
      - data-cmt="temp"  — unsaved temp anchors left by the editor JS
      - chg-block / chg-p1 / chg-p2 CSS classes — the editor highlights;
        p2editor re-applies them dynamically from comment data so storing them
        causes false blue/amber highlighting for P2 before comments load.

    Keeps:
      - data-cmt="<real-id>"  — real comment anchors that link bubbles to the text
      - All other attributes and content
    """
    # Remove orphan temp marks
    html_str = _re.sub(r'\s*data-cmt="temp"', '', html_str)

    # Strip chg-* CSS classes while leaving other classes intact
    def _clean_class_attr(m: "_re.Match[str]") -> str:
        tag = m.group(0)
        cls_m = _re.search(r'class\s*=\s*"([^"]*)"', tag)
        if not cls_m:
            return tag
        old_cls = cls_m.group(1)
        new_cls = ' '.join(c for c in old_cls.split() if c not in _CHG_CLASSES)
        if new_cls == old_cls:
            return tag
        if new_cls:
            return tag.replace(cls_m.group(0), f'class="{new_cls}"')
        # Remove the class attribute entirely if empty
        cleaned = tag.replace(' ' + cls_m.group(0), '')
        if cls_m.group(0) in cleaned:
            cleaned = cleaned.replace(cls_m.group(0), '')
        return cleaned

    html_str = _re.sub(r'<[a-zA-Z][^>]*class="[^"]*"[^>]*>', _clean_class_attr, html_str)
    return html_str


class _CommentActionBody(BaseModel):
    comment_id: str
    action: str        # accept | reject | reply
    text: str = ""     # required for reject/reply


@router.post("/comment-action/{onboarding_id}/{doc_type}/{token}")
async def document_comment_action(onboarding_id: str, doc_type: str, token: str,
                                  body: _CommentActionBody, db: AsyncSession = Depends(get_db)):
    """P1 or P2 accepts, rejects or replies to a comment thread.
    Accept: marks status=accepted and logs to thread.
    Reject: mandatory reply text; marks status=rejected and logs to thread.
    Reply: adds a message to the thread without changing status.
    When all comments reach accepted: auto-strips redlines and advances stage to 'accepted'.
    """
    _check_doc_type(doc_type)
    # Determine caller: P1 uses edit token, P2 uses sign token
    is_p1 = verify_doc_token(onboarding_id, doc_type, "edit", token)
    is_p2 = (not is_p1) and verify_doc_token(onboarding_id, doc_type, "sign", token)
    if not is_p1 and not is_p2:
        raise HTTPException(403, "Invalid or expired link")

    rec, lead = await _load(db, onboarding_id)
    data = _get_doc_data(rec, doc_type)

    if body.action not in ("accept", "reject", "reply"):
        raise HTTPException(400, "action must be accept | reject | reply")
    if body.action in ("reject", "reply") and not (body.text or "").strip():
        raise HTTPException(400, "A text explanation is required for reject/reply actions")

    # Deduplicate comments by id in-place before any lookup.
    # Duplicate entries arise when P2 submits the document more than once;
    # keeping only the last occurrence (most recent status) avoids phantom
    # "pending" cards after Jane has already accepted/rejected the comment.
    raw_comments = data.get("comments") or []
    seen: dict = {}
    for c in raw_comments:
        cid = c.get("id")
        seen[cid if cid else id(c)] = c
    comments = list(seen.values())
    data["comments"] = comments   # write deduped list back so it gets persisted

    target = next((c for c in comments if c.get("id") == body.comment_id), None)
    if target is None:
        raise HTTPException(404, "Comment not found")

    now = _now_ist()
    if is_p1:
        actor_name = settings.ORGANIZER_NAME or "Jane Aerospace"
        actor_party = "p1"
    else:
        actor_name = lead.business_name or lead.full_name or lead.email
        actor_party = "p2"

    thread_entry = {
        "by": actor_name,
        "party": actor_party,
        "action": body.action,
        "text": (body.text or "").strip()[:2000],
        "at": _fmt(now),
    }
    target.setdefault("thread", []).append(thread_entry)

    if body.action == "accept":
        target["status"] = "accepted"
    elif body.action == "reject":
        target["status"] = "rejected"
    # reply leaves status unchanged

    # When ALL comments are accepted: generate clean copy and advance stage
    all_accepted = bool(comments) and all(c.get("status") == "accepted" for c in comments)
    if all_accepted:
        if data.get("html"):
            data["html"] = _strip_redlines(data["html"])
        # Only advance if still in negotiation stages
        if (data.get("stage") or "") in ("review", "changes_requested"):
            data["stage"] = "accepted"

    _set_doc_data(rec, doc_type, data)
    await db.commit()

    logger.info("comment_action", onboarding_id=onboarding_id, doc_type=doc_type,
                action=body.action, comment_id=body.comment_id, party=actor_party)

    # When all comments are accepted: email P1 (Jane team) to proceed with countersignature
    if all_accepted:
        try:
            from app.services.onboarding_email import notify_team_terms_accepted
            accepted_by = (lead.business_name or lead.contact_name or lead.email)
            notify_team_terms_accepted(
                company_name=lead.business_name,
                lead_email=lead.email,
                doc_type=doc_type,
                accepted_by=f"{accepted_by} (negotiation complete — all changes agreed)",
                edit_url=_make_edit_url(onboarding_id, doc_type),
            )
        except Exception as _exc:
            logger.warning("notify_all_accepted_failed", error=str(_exc))

    return {
        "message": f"Comment {body.action}ed",
        "all_accepted": all_accepted,
        "open_count": sum(1 for c in comments if c.get("status") in ("pending", "rejected", None)),
    }


class _InternalSignBody(BaseModel):
    signed_name: str = ""
    designation: str = ""
    sig_font: str = "standard"
    sig_image: str = ""      # optional uploaded signature picture (data URL)


@router.post("/internal-sign/{onboarding_id}/{doc_type}/{token}")
async def document_internal_sign(onboarding_id: str, doc_type: str, token: str,
                                 body: _InternalSignBody, request: Request,
                                 background_tasks: BackgroundTasks,
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
    # Enforce: all comments from both parties must be accepted before internal signing
    unresolved = [c for c in data.get("comments", [])
                  if (c.get("status") or "pending") in ("pending", "rejected")]
    if unresolved:
        raise HTTPException(409, f"Cannot countersign yet: {len(unresolved)} comment(s) are still "
                                 "unresolved. Both parties must accept all comments before signing begins. "
                                 "Open the document editor and accept or reject each comment.")
    rep_name = body.signed_name.strip() or settings.ORGANIZER_NAME

    now = _now_ist()
    data["internal_signature"] = {
        "signed_name": rep_name[:200],
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
                  f"{short} Internally Signed by {rep_name} ({_fmt(now)}) — Sent to Lead for Signature")
    _set_doc_data(rec, doc_type, data)
    await db.commit()

    from app.services.onboarding_email import send_document_sign_email
    sign_url = make_doc_portal_url(onboarding_id, doc_type)
    to_email = data.get("signatory_email") or lead.email
    send_document_sign_email(
        to_email=to_email,
        lead_name=data.get("signatory_name") or lead.contact_name or lead.business_name,
        company_name=lead.business_name,
        doc_type=doc_type,
        sign_url=sign_url,
    )
    background_tasks.add_task(_crm_stage_safe, rec, lead, onboarding_id,
                              f"{short} Sent for E-Sign",
                              f"Internally signed by {rep_name}; signing link emailed to {to_email}")
    logger.info("document_internal_signed", onboarding_id=onboarding_id, doc_type=doc_type,
                by=rep_name, to=to_email)
    return {"message": f"Signed on behalf of Jane Aerospace and sent to {to_email} for counter-signature."}


# ── Signer-side e-signature widget (Type / Draw / Upload) ────────────────────
# Bulk CSS/JS live here as PLAIN (non-f-string) constants so the many JS/CSS
# braces need no {{ }} escaping. document_sign_page injects them with single
# {placeholders}; dynamic values reach the JS through window.SIGN_CFG.
_SIGN_WIDGET_CSS = """
  .sigtabs{display:flex;gap:0;border-bottom:2px solid #e5e9f2;margin:6px 0 14px;}
  .sigtab{background:none;border:none;padding:10px 22px;font-size:14px;font-weight:600;color:#64748b;
          cursor:pointer;border-bottom:3px solid transparent;margin-bottom:-2px;width:auto;border-radius:0;}
  .sigtab:hover{background:#f8fafc;color:#1a56db;}
  .sigtab.active{color:#1a3a6b;border-bottom-color:#1a56db;background:#f8fafc;}
  .sigpanel{display:none;}
  .sigpanel.active{display:block;}
  .pad-shell{position:relative;}
  #sig-pad{display:block;width:100%;height:190px;border:2px dashed #c7d4ee;border-radius:8px;
           background:#fbfdff;touch-action:none;cursor:crosshair;}
  .pad-baseline{position:absolute;left:16px;right:16px;bottom:42px;border-top:1px solid #e2e8f0;pointer-events:none;}
  .pad-hint{position:absolute;inset:0;display:flex;align-items:center;justify-content:center;color:#9fb0cc;
            font-size:14px;pointer-events:none;transition:opacity .15s;}
  .pad-tools{display:flex;align-items:center;gap:14px;margin-top:10px;flex-wrap:wrap;}
  .swatches{display:flex;gap:8px;}
  .swatch{width:22px;height:22px;border-radius:50%;cursor:pointer;border:2px solid #fff;box-shadow:0 0 0 1px #cbd5e1;}
  .swatch.active{box-shadow:0 0 0 2px #1a56db;}
  .mini-btn{background:#fff;border:1px solid #d1d5db;border-radius:6px;color:#475569;font-size:12.5px;
            font-weight:600;padding:6px 12px;cursor:pointer;width:auto;}
  .mini-btn:hover{background:#f1f5f9;}
  .mini-btn:disabled{background:#fff;opacity:.45;cursor:not-allowed;}
  #sig-preview{border:1px dashed #c7d4ee;border-radius:8px;background:#fbfdff;min-height:64px;
               display:flex;align-items:center;padding:8px 18px;font-size:30px;color:#10245c;
               font-family:Georgia,serif;margin-top:6px;overflow:hidden;}
  #sig-preview img{height:auto;}
  .resize-bar{display:flex;align-items:center;gap:8px;margin-top:8px;font-size:13px;color:#475569;}
  .resize-bar button{width:32px;height:32px;border:1px solid #d1d5db;border-radius:6px;background:#fff;
                     font-size:18px;font-weight:700;color:#1a3a6b;cursor:pointer;line-height:1;padding:0;}
  .resize-bar button:hover{background:#eff6ff;}
  .resize-bar .pt{min-width:64px;text-align:center;font-weight:600;color:#1a3a6b;}
"""

_SIGN_WIDGET_JS = r"""
/* Upload-only signature capture. Validates the data URL against the server
   regex before POSTing (_clean_sig_image silently drops non-matching images). */
window.SIGW = (function () {
  'use strict';
  var CFG = window.SIGN_CFG || {};
  var MIN_PT = CFG.minPt || 30, MAX_PT = CFG.maxPt || 500, STEP = CFG.stepPt || 15;
  var MAX_B64 = CFG.maxB64 || 780000;
  var UP_MAX_W = CFG.uploadMaxWidth || 480;
  var SIG_RE = /^data:image\/(png|jpeg);base64,[A-Za-z0-9+\/=]{40,800000}$/;
  var sigWidthPt = CFG.defaultPt || 150;
  var uploadURL = '', uploadImg = null;
  function $(id) { return document.getElementById(id); }

  function recomputeUpload() {
    if (!uploadImg) { uploadURL = ''; return; }
    var capW = Math.min(UP_MAX_W, Math.round(sigWidthPt * (CFG.pxPerPt || 3)));
    var scale = Math.min(1, capW / uploadImg.width), url = '';
    for (var i = 0; i < 6; i++) {
      var c = document.createElement('canvas');
      c.width = Math.max(1, Math.round(uploadImg.width * scale));
      c.height = Math.max(1, Math.round(uploadImg.height * scale));
      c.getContext('2d').drawImage(uploadImg, 0, 0, c.width, c.height);
      url = c.toDataURL('image/png');
      if ((url.length - url.indexOf(',') - 1) <= MAX_B64) break;
      scale *= 0.8;
    }
    uploadURL = url;
  }

  function updatePreview() {
    var prev = $('sig-preview');
    if (uploadURL) {
      prev.innerHTML = '<img src="' + uploadURL + '" style="width:' + sigWidthPt + 'px;max-width:100%;height:auto;">';
    } else {
      prev.style.color = '#9ca3af'; prev.style.fontSize = '14px';
      prev.textContent = 'Upload your signature image above';
    }
  }

  function loadUpload(input) {
    var f = input.files && input.files[0];
    if (!f) { uploadImg = null; uploadURL = ''; updatePreview(); return; }
    if (f.size > 5 * 1024 * 1024) { alert('Signature image is too large — maximum 5 MB.'); input.value = ''; return; }
    var img = new Image();
    img.onload = function () { uploadImg = img; recomputeUpload(); updatePreview(); };
    img.onerror = function () { alert('Could not read that image file.'); input.value = ''; };
    img.src = URL.createObjectURL(f);
  }

  function resize(dir) {
    sigWidthPt = Math.max(MIN_PT, Math.min(MAX_PT, sigWidthPt + dir * STEP));
    $('sig-pt').textContent = sigWidthPt + ' pt';
    recomputeUpload(); updatePreview();
  }

  function onNameInput() { /* no-op for upload-only mode */ }

  function showErr(msg) { var e = $('err'); e.textContent = msg; e.style.display = 'block'; }

  async function signDoc() {
    $('err').style.display = 'none';
    var name = ($('s-name').value || '').trim();
    if (!name) { showErr('Please enter your full legal name.'); return; }
    if (!uploadURL) { showErr('Please upload a signature image (PNG or JPG).'); return; }
    if (!SIG_RE.test(uploadURL)) {
      showErr('Signature image could not be processed — please try a different file.'); return;
    }
    if (!$('s-agree').checked) { showErr('Please tick the confirmation checkbox to proceed.'); return; }

    var btn = $('sign-btn'); btn.disabled = true; btn.textContent = 'Signing…';
    try {
      var r = await fetch(CFG.endpoint || window.location.pathname, {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          signed_name: name,
          designation: ($('s-desig').value || '').trim(),
          sig_font: 'standard',
          sig_image: uploadURL
        })
      });
      var d = await r.json().catch(function () { return {}; });
      if (r.ok) { window.location.href = d.redirect || window.location.pathname; }
      else { showErr(d.detail || 'Signing failed. Please try again.');
             btn.disabled = false; btn.textContent = '✍ I Agree & Sign'; }
    } catch (e) {
      showErr('Network error — ' + e.message);
      btn.disabled = false; btn.textContent = '✍ I Agree & Sign';
    }
  }

  function init() {
    $('sig-pt').textContent = sigWidthPt + ' pt';
    updatePreview();
  }
  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', init); else init();

  return { loadUpload: loadUpload, resize: resize, onNameInput: onNameInput, signDoc: signDoc };
})();
"""


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

    # If internal sign exists but comments are still unresolved, block P2 signing
    comments = data.get("comments") or []
    open_cmts = [c for c in comments if (c.get("status") or "pending") in ("pending", "rejected")]
    if open_cmts and internal_sig:
        import html as _hlib
        items_html = "".join(
            f'<li style="margin-bottom:8px;"><b>{_hlib.escape(c.get("by","?"))}</b> '
            f'({c.get("party","?").upper()}): '
            f'{_hlib.escape((c.get("thread") or [{}])[0].get("text","")[:120])} '
            f'<span style="background:#fee2e2;color:#991b1b;font-size:11px;padding:1px 6px;border-radius:5px;">'
            f'{c.get("status","pending")}</span></li>'
            for c in open_cmts
        )
        return HTMLResponse(_result_page(
            "Comments Pending Resolution",
            f"There are {len(open_cmts)} unresolved comment(s) on this {label}. "
            "Both parties must accept all comments before signing can proceed.",
            extra_html=f'<ul style="text-align:left;max-width:540px;margin:16px auto 0;'
                       f'font-size:13px;line-height:1.6;">{items_html}</ul>'))

    pdf_url = f"/api/v1/documents/pdf/{onboarding_id}/{doc_type}/{token}"
    if internal_sig:
        # show the internally countersigned document for the lead's signature
        pdf_url = f"/api/v1/documents/signed/{onboarding_id}/{doc_type}/{token}"
    import html as _hlib2
    sig_name = data.get("signatory_name", "") or (lead.contact_name or "")
    comments = data.get("comments") or []

    # Build comment history HTML for the legal record section on sign page
    _status_chip = {"accepted": ("#dcfce7", "#15803d", "Accepted"),
                    "rejected":  ("#fee2e2", "#991b1b", "Rejected")}
    _history_rows = ""
    for cmt in comments:
        if not isinstance(cmt, dict):
            continue
        by = _hlib2.escape(cmt.get("by") or "Unknown")
        party_lbl = "Jane Aerospace" if cmt.get("party") == "p1" else _hlib2.escape(lead.business_name)
        at = _hlib2.escape(cmt.get("at") or "")
        status = cmt.get("status") or "pending"
        chip_bg, chip_fg, chip_txt = _status_chip.get(status, ("#fef3c7", "#92400e", status.title()))
        thread = cmt.get("thread") or []
        thread_html = ""
        for t in thread:
            if not isinstance(t, dict):
                continue
            t_by = _hlib2.escape(t.get("by") or "")
            t_act = _hlib2.escape(t.get("action") or "comment")
            t_txt = _hlib2.escape((t.get("text") or "")[:500])
            t_at = _hlib2.escape(t.get("at") or "")
            thread_html += (f'<div style="margin-top:6px;padding-left:12px;border-left:2px solid #e5e7eb;'
                            f'font-size:11.5px;color:#374151;">'
                            f'<span style="font-weight:700;color:#1a3a6b;">{t_by}</span> '
                            f'<span style="color:#94a3b8;">({t_act}) &bull; {t_at}</span><br>'
                            f'<span style="white-space:pre-wrap;">{t_txt}</span></div>')
        _history_rows += (
            f'<div style="border:1px solid #e5e7eb;border-radius:8px;padding:12px 16px;margin-bottom:10px;">'
            f'<div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:4px;">'
            f'<span style="font-weight:700;font-size:13px;color:#1a3a6b;">{by}</span>'
            f'<span style="font-size:11px;color:#64748b;">{party_lbl} &bull; {at}</span>'
            f'<span style="background:{chip_bg};color:{chip_fg};font-size:11px;font-weight:700;'
            f'padding:2px 8px;border-radius:99px;">{chip_txt}</span></div>'
            f'{thread_html}</div>'
        )

    history_card = ""
    if _history_rows:
        history_card = f"""
  <div class="card">
    <div class="card-head">
      <div class="step-badge" style="background:#64748b;">&#x1F4DC;</div>
      <h2>Change &amp; Comment History</h2>
    </div>
    <div class="card-pad">
      <p style="font-size:12.5px;color:#64748b;margin-bottom:12px;">
        Complete audit trail of all proposed changes and comments by both parties.
        This history is preserved as part of the legal record.</p>
      {_history_rows}
    </div>
  </div>"""

    sign_cfg = {
        "endpoint": f"/api/v1/documents/sign/{onboarding_id}/{doc_type}/{token}",
        "maxB64": 780000, "uploadMaxWidth": 480, "drawMaxWidth": 600,
        "defaultPt": 150, "minPt": 30, "maxPt": 500, "stepPt": 15, "pxPerPt": 3,
    }
    sign_cfg_js = json.dumps(sign_cfg).replace("<", "\\u003c")  # safe to embed in <script>

    html = f"""<!DOCTYPE html>
<html lang="en">
<head><meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Sign {label} — Jane Aerospace</title>
<style>{_SIGN_WIDGET_CSS}</style>
<style>
  *{{box-sizing:border-box;margin:0;}}
  :root{{--navy:#15315f;--ink:#0f172a;--muted:#64748b;--line:#e6eaf0;--blue:#2563eb;--green:#16a34a;}}
  body{{font-family:'Segoe UI',system-ui,-apple-system,Roboto,Arial,sans-serif;color:var(--ink);
    background:#f0f4fa;min-height:100vh;padding:0 0 56px;-webkit-font-smoothing:antialiased;}}

  /* ── topbar ── */
  .topbar{{background:var(--navy);color:#fff;display:flex;align-items:center;justify-content:space-between;
    gap:12px;padding:0 24px;height:52px;position:sticky;top:0;z-index:100;
    box-shadow:0 2px 8px rgba(0,0,0,.18);}}
  .brand{{display:flex;align-items:center;gap:9px;font-weight:800;font-size:15px;letter-spacing:.02em;}}
  .brand .logo{{width:28px;height:28px;border-radius:6px;background:rgba(255,255,255,.15);
    display:flex;align-items:center;justify-content:center;font-size:14px;}}
  .doc-label{{font-size:12px;font-weight:600;color:rgba(255,255,255,.55);}}
  .secure{{display:inline-flex;align-items:center;gap:5px;font-size:11.5px;font-weight:700;color:#86efac;}}

  /* ── layout ── */
  .wrap{{max-width:860px;margin:0 auto;padding:22px 16px 0;}}

  /* ── cards ── */
  .card{{background:#fff;border:1px solid var(--line);border-radius:14px;overflow:hidden;margin-bottom:16px;
    box-shadow:0 1px 3px rgba(16,24,40,.05),0 8px 24px rgba(16,40,90,.07);}}
  .card-head{{display:flex;align-items:center;gap:12px;padding:15px 22px;border-bottom:1px solid var(--line);
    background:#f8fafc;}}
  .step-badge{{width:26px;height:26px;flex:none;border-radius:50%;background:var(--navy);color:#fff;font-weight:800;
    font-size:12px;display:flex;align-items:center;justify-content:center;}}
  .card-head h2{{font-size:15px;font-weight:700;color:var(--navy);}}
  .card-pad{{padding:20px 22px;}}

  /* ── parties ── */
  .parties{{display:flex;align-items:center;gap:10px;margin-bottom:14px;}}
  .party{{flex:1;background:#f8fafc;border:1px solid var(--line);border-radius:10px;padding:10px 14px;min-width:0;}}
  .party .role{{font-size:9.5px;text-transform:uppercase;letter-spacing:.08em;color:#94a3b8;font-weight:800;}}
  .party .pname{{font-size:13px;font-weight:700;color:var(--ink);margin-top:2px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;}}
  .vs{{color:#c2ccda;font-weight:800;font-size:16px;flex:none;}}

  /* ── document viewer ── */
  .viewer-wrap{{position:relative;border:1px solid var(--line);border-radius:10px;overflow:hidden;}}
  .viewer-bar{{display:flex;align-items:center;justify-content:space-between;gap:10px;padding:8px 14px;
    background:#f1f5f9;border-bottom:1px solid var(--line);font-size:12px;color:#475569;font-weight:600;}}
  .viewer-bar a{{display:inline-flex;align-items:center;gap:5px;font-size:11.5px;font-weight:700;color:var(--blue);
    text-decoration:none;background:#eff6ff;border:1px solid #dbeafe;border-radius:6px;padding:4px 10px;}}
  .viewer iframe{{width:100%;height:56vh;min-height:340px;border:none;display:block;background:#fff;}}
  .scroll-to-sign{{display:block;width:100%;background:linear-gradient(135deg,var(--navy),#1e4080);
    color:#fff;border:none;padding:12px;font-size:14px;font-weight:700;cursor:pointer;letter-spacing:.01em;
    text-align:center;transition:background .15s;}}
  .scroll-to-sign:hover{{background:linear-gradient(135deg,#1e4080,#2563eb);}}

  /* ── form fields ── */
  .frow{{display:grid;grid-template-columns:1fr 1fr;gap:14px;margin-bottom:16px;}}
  label.lbl{{display:block;font-size:12px;font-weight:700;color:#374151;margin-bottom:5px;text-transform:uppercase;letter-spacing:.04em;}}
  .req{{color:#e11d48;}}
  input[type=text]{{width:100%;padding:10px 13px;border:1.5px solid #d6dce6;border-radius:8px;font-size:14px;
    color:var(--ink);background:#fff;transition:border-color .15s,box-shadow .15s;}}
  input[type=text]:focus{{outline:none;border-color:var(--blue);box-shadow:0 0 0 3px rgba(37,99,235,.12);}}

  /* ── sig section ── */
  .sig-section-label{{font-size:12px;font-weight:700;color:#374151;text-transform:uppercase;letter-spacing:.04em;
    margin:0 0 8px;}}
  .upload-zone{{border:2px dashed #c7d4ee;border-radius:10px;background:#f8fafc;
    display:flex;flex-direction:column;align-items:center;justify-content:center;
    padding:28px 20px;cursor:pointer;transition:border-color .15s,background .15s;text-align:center;}}
  .upload-zone:hover{{border-color:var(--blue);background:#eff6ff;}}
  .upload-zone input[type=file]{{display:none;}}
  .upload-zone .upload-icon{{font-size:32px;margin-bottom:8px;}}
  .upload-zone .upload-lbl{{font-size:13.5px;font-weight:700;color:var(--navy);}}
  .upload-zone .upload-hint{{font-size:11.5px;color:var(--muted);margin-top:3px;}}
  #upload-preview{{margin-top:10px;display:none;border:1px solid var(--line);border-radius:8px;
    padding:10px;background:#fff;}}

  /* ── preview ── */
  #sig-preview{{border:1px dashed #c7d4ee;border-radius:8px;background:#fbfdff;min-height:60px;
    display:flex;align-items:center;padding:8px 18px;font-size:30px;color:#10245c;
    font-family:Georgia,serif;margin-top:6px;overflow:hidden;}}
  #sig-preview img{{height:auto;}}
  .resize-bar{{display:flex;align-items:center;gap:8px;margin-top:8px;font-size:13px;color:#475569;}}
  .resize-bar button{{width:30px;height:30px;border:1px solid #d1d5db;border-radius:6px;background:#fff;
    font-size:16px;font-weight:700;color:var(--navy);cursor:pointer;line-height:1;padding:0;}}
  .resize-bar button:hover{{background:#eff6ff;}}
  .resize-bar .pt{{min-width:56px;text-align:center;font-weight:700;color:var(--navy);font-size:12px;}}

  /* ── consent & submit ── */
  .consent{{display:flex;gap:11px;align-items:flex-start;margin:18px 0 0;font-size:12.5px;color:#374151;
    line-height:1.55;background:#f0fdf4;border:1px solid #bbf7d0;border-radius:10px;padding:13px 15px;}}
  .consent input{{margin-top:2px;width:17px;height:17px;flex:none;accent-color:var(--green);}}
  #err{{display:none;background:#fef2f2;color:#b91c1c;border:1px solid #fecaca;padding:10px 14px;
    border-radius:8px;font-size:13px;margin:12px 0 0;}}
  .sign-btn{{margin-top:16px;width:100%;border:none;border-radius:11px;padding:15px 28px;font-size:15px;
    font-weight:800;color:#fff;cursor:pointer;letter-spacing:.2px;
    background:linear-gradient(135deg,var(--green),#15803d);
    box-shadow:0 6px 18px rgba(22,163,74,.28);transition:transform .08s,box-shadow .15s;}}
  .sign-btn:hover{{box-shadow:0 8px 24px rgba(22,163,74,.36);}}
  .sign-btn:active{{transform:translateY(1px);}}
  .sign-btn:disabled{{background:#9ca3af;box-shadow:none;cursor:not-allowed;}}

  /* ── trust footer ── */
  .trust{{max-width:860px;margin:6px auto 0;padding:0 16px;display:flex;align-items:center;
    justify-content:center;gap:18px;flex-wrap:wrap;color:#94a3b8;font-size:11px;}}
  .trust span{{display:inline-flex;align-items:center;gap:5px;}}

  @media(max-width:600px){{
    .frow{{grid-template-columns:1fr;gap:11px;}}
    .card-pad{{padding:16px;}}
    .card-head{{padding:12px 16px;}}
    .viewer iframe{{height:52vh;}}
    .topbar{{padding:0 14px;}}
  }}
</style></head>
<body>
<div class="topbar">
  <div class="brand"><span class="logo">✈</span> Jane Aerospace</div>
  <span class="doc-label">{label}</span>
  <span class="secure">🔒 Secure Signing</span>
</div>
<div class="wrap">

  <!-- Card 1: Parties + Document -->
  <div class="card">
    <div class="card-head">
      <div class="step-badge">1</div>
      <h2>Review Document</h2>
    </div>
    <div class="card-pad">
      <div class="parties">
        <div class="party">
          <div class="role">Disclosing Party</div>
          <div class="pname">Jane Aerospace Private Limited</div>
        </div>
        <span class="vs">⇄</span>
        <div class="party">
          <div class="role">Counterparty</div>
          <div class="pname">{lead.business_name}</div>
        </div>
      </div>
      <div class="viewer-wrap">
        <div class="viewer-bar">
          <span>📄 {label}</span>
          <a href="{pdf_url}" target="_blank">⬇ Download PDF ↗</a>
        </div>
        <iframe src="{pdf_url}#toolbar=1" title="Document preview" id="doc-frame"></iframe>
        <button class="scroll-to-sign" onclick="document.getElementById('sign-section').scrollIntoView({{behavior:'smooth'}})">
          ✍ Ready to Sign? Click here →
        </button>
      </div>
    </div>
  </div>

  <!-- Card 2: Sign -->
  <div class="card" id="sign-section">
    <div class="card-head">
      <div class="step-badge">2</div>
      <h2>Sign Electronically</h2>
    </div>
    <div class="card-pad">
      <div class="frow">
        <div>
          <label class="lbl">Full Legal Name <span class="req">*</span></label>
          <input type="text" id="s-name" value="{sig_name}" placeholder="Your full name" oninput="SIGW.onNameInput()">
        </div>
        <div>
          <label class="lbl">Designation</label>
          <input type="text" id="s-desig" placeholder="e.g. Director">
        </div>
      </div>

      <div class="sig-section-label">Your Signature (Upload)</div>

      <div class="sigpanel active" data-panel="upload">
        <div class="upload-zone" onclick="document.getElementById('s-sigimg').click()">
          <div class="upload-icon">🖊</div>
          <div class="upload-lbl">Click to upload signature image</div>
          <div class="upload-hint">PNG or JPG · Max 5 MB</div>
          <input type="file" id="s-sigimg" accept=".png,.jpg,.jpeg" onchange="SIGW.loadUpload(this)">
        </div>
        <div id="upload-preview"></div>
      </div>

      <label class="lbl" style="margin-top:14px;">Signature Preview</label>
      <div id="sig-preview">Upload your signature image</div>
      <div class="resize-bar" id="resize-bar" style="display:flex;">
        <span>Size</span>
        <button type="button" onclick="SIGW.resize(-1)" title="Smaller">&minus;</button>
        <span class="pt" id="sig-pt">150 pt</span>
        <button type="button" onclick="SIGW.resize(1)" title="Larger">+</button>
      </div>

      <div class="consent">
        <input type="checkbox" id="s-agree">
        <span>I confirm I am an authorised signatory of <strong>{lead.business_name}</strong> and have read this
        {label} in full. Clicking "I Agree &amp; Sign" constitutes my legally binding electronic signature under the
        IT Act, 2000.</span>
      </div>
      <div id="err"></div>
      <button id="sign-btn" class="sign-btn" onclick="SIGW.signDoc()">✍ I Agree &amp; Sign</button>
    </div>
  </div>

  {history_card}

</div>
<div class="trust">
  <span>🔒 Encrypted in transit</span>
  <span>⚖ Legally binding · IT Act 2000</span>
  <span>📋 Audit-logged: IP, timestamp, device</span>
</div>
<script>window.SIGN_CFG = {sign_cfg_js};</script>
<script>{_SIGN_WIDGET_JS}</script>
<script>
  // Show upload preview when file is loaded
  document.getElementById('s-sigimg') && (function(){{
    var orig = SIGW.loadUpload;
    SIGW.loadUpload = function(inp) {{ orig(inp); updateUploadPreview(inp); }};
    function updateUploadPreview(inp) {{
      var pv = document.getElementById('upload-preview');
      if (!pv) return;
      var f = inp.files && inp.files[0];
      if (!f) {{ pv.style.display = 'none'; pv.innerHTML = ''; return; }}
      var url = URL.createObjectURL(f);
      pv.style.display = 'block';
      pv.innerHTML = '<img src="' + url + '" style="max-height:80px;max-width:100%;border-radius:6px;">';
    }}
  }})();

  // Auto-scroll to sign section when page loads (lead arrives via email link)
  window.addEventListener('load', function() {{
    setTimeout(function() {{
      var s = document.getElementById('sign-section');
      if (s) s.scrollIntoView({{behavior: 'smooth', block: 'start'}});
    }}, 600);
  }});
</script>
</body></html>"""
    return HTMLResponse(html)


class _SignBody(BaseModel):
    signed_name: str
    designation: str = ""
    sig_font: str = "standard"
    sig_image: str = ""      # optional uploaded signature picture (data URL)
    signatures_overlay: list[dict] | None = None   # lead-placed signatures on the page(s)


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
    data["stage"] = "signed"
    # merge the lead's placed signature overlays so they stamp onto the page(s)
    if body.signatures_overlay:
        merged = list(data.get("signatures_overlay") or [])
        for ov in body.signatures_overlay[:30]:
            img = _clean_sig_image(str((ov or {}).get("image", "")))
            if not img:
                continue
            try:
                merged.append({
                    "page": max(0, int(ov.get("page", 0))),
                    "x": max(0.0, min(1.0, float(ov.get("x", 0)))),
                    "y": max(0.0, min(1.0, float(ov.get("y", 0)))),
                    "w": max(0.0, min(1.0, float(ov.get("w", 0.2)))),
                    "h": max(0.0, min(1.0, float(ov.get("h", 0.08)))),
                    "image": img,
                })
            except (TypeError, ValueError):
                continue
        data["signatures_overlay"] = merged[:30]
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
