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
import html as _htmllib
import io
import re
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

    # [ABC] / bare ABC are the counterparty reference — always the company name
    rows = [
        {"find": "[Date]", "replace": eff_date},
        {"find": "[Company Name]", "replace": company},
        {"find": "[Company Registration Number]", "replace": reg_no},
        {"find": "[Address]", "replace": address},
        {"find": "[ABC]", "replace": company},
        {"find": "ABC", "replace": company},
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


def _find_present(plain: str, find: str) -> bool:
    if re.fullmatch(r"\w+", find):
        return re.search(rf"\b{re.escape(find)}\b", plain) is not None
    return find in plain


def _block_to_html(block: dict) -> tuple[str, float, float, str]:
    """Convert one PDF text block to inline HTML, keeping bold/italic runs.

    Returns (html, dominant_font_size, hanging_indent_pt, alignment).
    """
    import statistics
    sizes: list[float] = []
    parts: list[str] = []
    lines = block.get("lines", [])
    for line in lines:
        for span in line.get("spans", []):
            t = _htmllib.escape(span.get("text", ""))
            flags = span.get("flags", 0)
            fname = span.get("font", "").lower()
            if flags & 16 or "bold" in fname:
                t = f"<b>{t}</b>"
            if flags & 2 or "italic" in fname:
                t = f"<i>{t}</i>"
            parts.append(t)
            if span.get("text", "").strip():
                sizes.append(float(span.get("size", 11.0)))
        parts.append(" ")
    html = "".join(parts).strip()
    size = round(statistics.median(sizes), 2) if sizes else 11.0
    indent = 0.0
    if len(lines) > 1:
        x0_first = lines[0]["bbox"][0]
        x0_rest = min(ln["bbox"][0] for ln in lines[1:])
        indent = max(0.0, round(x0_rest - x0_first, 1))
    align = "justify" if len(lines) > 1 else "left"
    return html, size, indent, align


def _render_block(html: str, w: float, h: float, size: float,
                  indent: float, align: str) -> "fitz.Document":
    """Render rich block HTML into a w×h mini-PDF, shrinking the font
    fraction by fraction until the text fits the original block rectangle."""
    fs = size
    buf = io.BytesIO()
    while True:
        css = (f"body{{margin:0;font-family:helvetica;font-size:{fs:.2f}pt;"
               f"line-height:1.24;color:#111111;}}"
               f"p{{margin:0;text-align:{align};"
               f"padding-left:{indent:.1f}pt;text-indent:{-indent:.1f}pt;}}")
        story = fitz.Story(html=f"<p>{html}</p>", user_css=css)
        buf = io.BytesIO()
        writer = fitz.DocumentWriter(buf)
        rect = fitz.Rect(0, 0, w, h)
        dev = writer.begin_page(rect)
        more, _ = story.place(rect)
        story.draw(dev)
        writer.end_page()
        writer.close()
        if not more or fs <= 5.0:
            break
        fs -= 0.25
    buf.seek(0)
    return fitz.open(stream=buf.getvalue(), filetype="pdf")


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
    """Fill placeholders in the original PDF template. Returns PDF bytes.

    Any paragraph block that contains a placeholder is re-rendered inside its
    own original rectangle with the value substituted — the text reflows
    naturally (no overlaps), keeping size, bold runs, hanging indents and
    justification. Every other block of the PDF is left byte-identical.
    """
    path = template_path(doc_type, is_overseas)
    doc = fitz.open(path)
    total = 0
    # Longest finds first so "[Company Name]" wins over "ABC" etc.
    ordered = sorted(
        [r for r in replacements if (r.get("find") or "").strip()
         and (r.get("replace") or "").strip()],
        key=lambda r: -len(r["find"]),
    )
    for page in doc:
        _cover_highlights(page)
        blocks = [b for b in page.get_text("dict")["blocks"] if b.get("type") == 0]
        prepared = []
        for b in blocks:
            plain = "".join(s.get("text", "") for ln in b.get("lines", [])
                            for s in ln.get("spans", []))
            if not any(_find_present(plain, r["find"]) for r in ordered):
                continue
            html, size, indent, align = _block_to_html(b)
            new_html = apply_replacements_html(html, ordered)
            rect = fitz.Rect(b["bbox"])
            prepared.append((rect, new_html, size, indent, align))
            page.add_redact_annot(rect)
            total += 1
        if not prepared:
            continue
        page.apply_redactions(images=getattr(fitz, "PDF_REDACT_IMAGE_NONE", 0))
        _cover_highlights(page)
        for rect, block_html, size, indent, align in prepared:
            mini = _render_block(block_html, rect.width, rect.height, size, indent, align)
            page.show_pdf_page(rect, mini, 0)
            mini.close()
    out = doc.tobytes(deflate=True, garbage=3)
    doc.close()
    logger.info("pdf_filled", doc_type=doc_type, overseas=is_overseas, blocks_reflowed=total)
    return out


# ---------------------------------------------------------------------------
# HTML document flow — template PDF → editable HTML → final PDF
# ---------------------------------------------------------------------------

_HTML_CACHE: dict[tuple[str, bool], str] = {}

_DOC_CSS = """
body { font-family: helvetica, sans-serif; font-size: 10pt; line-height: 1.5; color: #111; }
p { margin: 5pt 0; text-align: justify; }
h1, h2, h3, h4 { color: #000; margin: 9pt 0 4pt; }
b { font-weight: bold; }
"""


def template_html(doc_type: str, is_overseas: bool) -> str:
    """Extract the template PDF as flowing, editable HTML (cached)."""
    key = (doc_type, is_overseas)
    if key in _HTML_CACHE:
        return _HTML_CACHE[key]
    doc = fitz.open(template_path(doc_type, is_overseas))
    parts = []
    for page in doc:
        x = page.get_text("xhtml").strip()
        x = re.sub(r"<img[^>]*>", "", x)                       # drop embedded images
        x = re.sub(r"^<div[^>]*>", "", x)
        x = re.sub(r"</div>$", "", x)
        parts.append(x.strip())
    doc.close()
    html = "\n".join(parts)

    # Drop paragraphs that are empty, bare page numbers, or per-page running
    # headers ("Private and confidential") picked up from each PDF page.
    def _drop_noise(m: "re.Match[str]") -> str:
        txt = re.sub(r"<[^>]+>", "", m.group()).replace("&#xa0;", " ").strip()
        if not txt or re.fullmatch(r"\d{1,3}", txt) or txt.lower() == "private and confidential":
            return ""
        return m.group()

    html = re.sub(r"<p>.*?</p>", _drop_noise, html, flags=re.S)
    _HTML_CACHE[key] = html
    logger.info("template_html_built", doc_type=doc_type, overseas=is_overseas, size=len(html))
    return html


def apply_replacements_html(html: str, replacements: list[dict]) -> str:
    """Fill placeholders in the document HTML. Word-boundary safe for bare words.

    PDF extraction can split a placeholder across formatting tags (e.g.
    "[<i>Address</i>]"), so after the plain replace a tag-tolerant regex pass
    catches those occurrences too.
    """
    ordered = sorted(
        [r for r in replacements if (r.get("find") or "").strip()],
        key=lambda r: -len(r["find"]),
    )
    for row in ordered:
        find = row["find"]
        repl = _htmllib.escape((row.get("replace") or "").strip())
        if not repl:
            continue
        if re.fullmatch(r"\w+", find):
            html = re.sub(rf"\b{re.escape(find)}\b", repl, html)
            continue
        html = html.replace(find, repl)
        # tag-tolerant pass: allow markup/whitespace between the characters
        pattern = r"(?:\s|<[^>]+>)*".join(re.escape(c) for c in find.replace(" ", ""))
        html = re.sub(pattern, lambda _m: repl, html)
    return html


def html_to_pdf(body_html: str, title: str = "") -> bytes:
    """Lay the document HTML out as an A4 PDF (PyMuPDF Story)."""
    head = f"<h1 style='text-align:center;font-size:13pt;'>{_htmllib.escape(title)}</h1>" if title else ""
    story = fitz.Story(html=f"<body>{head}{body_html}</body>", user_css=_DOC_CSS)
    buf = io.BytesIO()
    writer = fitz.DocumentWriter(buf)
    mediabox = fitz.paper_rect("a4")
    where = mediabox + (46, 50, -46, -56)
    more = 1
    while more:
        dev = writer.begin_page(mediabox)
        more, _ = story.place(where)
        story.draw(dev)
        writer.end_page()
    writer.close()
    buf.seek(0)
    return buf.getvalue()


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
