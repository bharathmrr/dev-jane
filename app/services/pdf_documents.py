"""Fill the official JAPL PDF templates with KYC data and append a signature page.

Templates (app/templates/documents/):
  nda_indian.pdf        — JAPL NDA, Indian customer
  nda_overseas.pdf      — JAPL NDA, overseas customer
  supply_agreement.pdf  — JAPL Supply/Customer Agreement (both)

Placeholders inside the PDFs are literal text like [Date], [Company Name],
[Company Registration Number], [Address], [ABC]. We erase each occurrence
with a redaction and write the replacement value into the same box,
shrinking the font until it fits.
"""
from __future__ import annotations

import datetime as dt
from pathlib import Path
from zoneinfo import ZoneInfo

import fitz  # PyMuPDF

from app.core.logging import get_logger

logger = get_logger("pdf_documents")

_IST = ZoneInfo("Asia/Kolkata")

TEMPLATE_DIR = Path(__file__).resolve().parent.parent / "templates" / "documents"

DOC_TEMPLATES: dict[tuple[str, bool], str] = {
    ("nda", False):       "nda_indian.pdf",
    ("nda", True):        "nda_overseas.pdf",
    ("agreement", False): "supply_agreement.pdf",
    ("agreement", True):  "supply_agreement.pdf",
}

DOC_LABELS = {"nda": "Non-Disclosure Agreement", "agreement": "Supply / Customer Agreement"}


def template_path(doc_type: str, is_overseas: bool) -> Path:
    name = DOC_TEMPLATES.get((doc_type, is_overseas))
    if not name:
        raise ValueError(f"Unknown document type: {doc_type}")
    return TEMPLATE_DIR / name


def default_replacements(doc_type: str, kyc_fields: dict) -> list[dict]:
    """Build the default find→replace list from KYC data.

    kyc_fields keys: company_name, reg_number, address, effective_date
    """
    company = (kyc_fields.get("company_name") or "").strip()
    reg_no = (kyc_fields.get("reg_number") or "").strip()
    address = (kyc_fields.get("address") or "").strip()
    eff_date = (kyc_fields.get("effective_date") or "").strip() or dt.datetime.now(_IST).strftime("%d %B %Y")

    # Templates define the short reference name as [ABC] and then use bare ABC
    # throughout — a short name fits those narrow text boxes far better.
    short_name = company.split(" ")[0].strip("(),.") if company else ""
    rows = [
        {"find": "[Date]", "replace": eff_date},
        {"find": "[Company Name]", "replace": company},
        {"find": "[Company Registration Number]", "replace": reg_no},
        {"find": "[Address]", "replace": address},
        {"find": "[ABC]", "replace": short_name},
        {"find": "ABC", "replace": short_name},
    ]
    if doc_type == "agreement":
        # Supply agreement dates its preamble as "[●] 2024"
        rows.insert(0, {"find": "[●] 2024", "replace": eff_date})
    return rows


def kyc_fields_from_submission(
    company_name: str | None,
    cin_number: str | None,
    extra: dict,
    is_overseas: bool,
) -> dict:
    """Map a KYC submission to the template's standard fields."""
    addr_parts = [
        extra.get("registered_address"),
        extra.get("city"),
        extra.get("state"),
        extra.get("pin_code"),
    ]
    if is_overseas:
        addr_parts.append(extra.get("country"))
    address = ", ".join(str(p).strip() for p in addr_parts if p)
    reg = (extra.get("company_reg_number") if is_overseas else cin_number) or cin_number or ""
    return {
        "company_name": (company_name or "").strip(),
        "reg_number": str(reg).strip(),
        "address": address,
        "effective_date": dt.datetime.now(_IST).strftime("%d %B %Y"),
    }


def _span_style(page: "fitz.Page", rect: "fitz.Rect") -> tuple[float, bool]:
    """Font size and boldness of the text occupying `rect` (defaults 11pt regular)."""
    try:
        d = page.get_text("dict", clip=fitz.Rect(rect.x0 - 1, rect.y0 - 1, rect.x1 + 1, rect.y1 + 1))
        for block in d.get("blocks", []):
            for line in block.get("lines", []):
                for span in line.get("spans", []):
                    if span.get("text", "").strip():
                        bold = "bold" in span.get("font", "").lower() or bool(span.get("flags", 0) & 16)
                        return float(span.get("size", 11.0)), bold
    except Exception:
        pass
    return 11.0, False


def _replace_in_page(page: "fitz.Page", find: str, replace: str) -> int:
    """Erase every occurrence of `find` and write `replace` in its place,
    matching the original font size/weight and covering any highlight fill."""
    rects = page.search_for(find)
    if not rects:
        return 0
    styles = [_span_style(page, r) for r in rects]
    for r in rects:
        page.add_redact_annot(r)
    page.apply_redactions(images=getattr(fitz, "PDF_REDACT_IMAGE_NONE", 0))
    for r, (size, bold) in zip(rects, styles):
        # the templates draw a yellow highlight rect behind each placeholder —
        # paint white over it (it extends slightly above/below the text box)
        page.draw_rect(fitz.Rect(r.x0 - 0.5, r.y0 - 3.5, r.x1 + 0.5, r.y1 + 1.5),
                       color=None, fill=(1, 1, 1))
        if not replace:
            continue
        font = "hebo" if bold else "helv"
        box = fitz.Rect(r.x0, r.y0 - 2, r.x1 + 6, r.y1 + 3)
        inserted = False
        for fs in (size, size - 1, size - 2, size - 3):
            if fs < 6:
                break
            rc = page.insert_textbox(box, replace, fontname=font, fontsize=fs,
                                     color=(0, 0, 0), align=0)
            if rc >= 0:
                inserted = True
                break
        if not inserted:
            # value longer than the placeholder box — draw it unclipped at a
            # reduced size rather than dropping it
            page.insert_text((r.x0, r.y1 - 2), replace, fontname=font,
                             fontsize=max(size - 3, 7), color=(0, 0, 0))
    return len(rects)


def _cover_highlights(page: "fitz.Page") -> int:
    """Paint white over every yellow/amber highlight rectangle drawn on the page.

    The templates use drawn highlight rects only behind fill-in placeholders,
    so removing them all leaves a clean printed look.
    """
    covered = 0
    try:
        for dr in page.get_drawings():
            fill = dr.get("fill")
            if not fill or len(fill) < 3:
                continue
            r, g, b = fill[0], fill[1], fill[2]
            if r > 0.85 and g > 0.7 and b < 0.45:  # yellow / amber tones
                page.draw_rect(dr["rect"], color=None, fill=(1, 1, 1))
                covered += 1
    except Exception:
        pass
    return covered


def fill_document(doc_type: str, is_overseas: bool, replacements: list[dict]) -> bytes:
    """Open the template PDF and apply all find→replace rows. Returns PDF bytes."""
    path = template_path(doc_type, is_overseas)
    doc = fitz.open(path)
    total = 0
    # Longest finds first so "[Company Name]" wins over "ABC" etc.
    ordered = sorted(
        [r for r in replacements if (r.get("find") or "").strip()],
        key=lambda r: -len(r["find"]),
    )
    for page in doc:
        # white-out all highlight rects first, then erase placeholders and
        # write the replacement values on top
        _cover_highlights(page)
        for row in ordered:
            total += _replace_in_page(page, row["find"], (row.get("replace") or "").strip())
    out = doc.tobytes(deflate=True, garbage=3)
    doc.close()
    logger.info("pdf_filled", doc_type=doc_type, overseas=is_overseas, replaced=total)
    return out


def append_signature_page(pdf_bytes: bytes, doc_type: str, sig: dict) -> bytes:
    """Append an e-signature certificate page to the filled PDF.

    sig keys: signed_name, designation, email, company_name, signed_at, ip, user_agent
    """
    doc = fitz.open(stream=pdf_bytes, filetype="pdf")
    page = doc.new_page()
    w = page.rect.width

    def _line(y: float, text: str, size: float = 11, bold: bool = False, color=(0, 0, 0)):
        page.insert_text((72, y), text, fontsize=size,
                         fontname="hebo" if bold else "helv", color=color)

    title = f"Electronic Signature Certificate — {DOC_LABELS.get(doc_type, doc_type.upper())}"
    _line(90, title, 15, bold=True, color=(0.1, 0.23, 0.42))
    page.draw_line((72, 102), (w - 72, 102), color=(0.1, 0.23, 0.42), width=1.2)

    y = 140
    rows = [
        ("Signed by",        sig.get("signed_name", "")),
        ("Designation",      sig.get("designation", "") or "—"),
        ("Email",            sig.get("email", "")),
        ("On behalf of",     sig.get("company_name", "")),
        ("Signed at",        sig.get("signed_at", "")),
        ("IP address",       sig.get("ip", "") or "—"),
    ]
    for label, value in rows:
        _line(y, f"{label}:", 11, bold=True)
        _line(y + 16, str(value), 11)
        y += 44

    y += 10
    page.insert_textbox(
        fitz.Rect(72, y, w - 72, y + 120),
        "The signatory confirmed acceptance of this document via the Jane Aerospace "
        "secure signing link by entering their full legal name and clicking "
        "'I Agree & Sign'. This record, including the timestamp and originating "
        "IP address above, constitutes the audit trail of the electronic signature.",
        fontsize=10, fontname="helv", color=(0.25, 0.25, 0.25),
    )

    # Typed signature representation (cursive styles render as serif italic in PDF)
    y += 110
    sig_font = "helv" if sig.get("sig_font") == "standard" else "tiit"
    page.insert_text((72, y), sig.get("signed_name", ""), fontsize=22,
                     fontname=sig_font, color=(0.06, 0.14, 0.36))
    page.draw_line((72, y + 8), (300, y + 8), color=(0.3, 0.3, 0.3), width=0.8)
    _line(y + 22, "Authorised Signatory", 9, color=(0.4, 0.4, 0.4))

    out = doc.tobytes(deflate=True, garbage=3)
    doc.close()
    return out
