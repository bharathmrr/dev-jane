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

import base64
import datetime as dt
import html as _htmllib
import io
import re
import statistics
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
# Live Document Editor flow — template PDF → faithful editable HTML → final PDF
# ---------------------------------------------------------------------------

_NOISE_PAGE_NO = re.compile(r"^\d{1,3}(?:\s*\|\s*\d{1,3})?$")  # "8" or "28 | 31"
_NOISE_HEADERS = {"private and confidential", "privileged & confidential",
                  "privileged and confidential"}
# "1." / "14.1" / "A." / "(a)" / "(iv)" / "•" … at the start of a line
_ENUM_MARKER = re.compile(
    r"^(?:\d+(?:\.\d+)*[.)]?|[A-Z][.)]|\([a-zA-Z0-9]{1,4}\)|[•●▪○–—-])\s+\S")
_DOT_LEADER = re.compile(r"\.{8,}")  # TOC dot leaders
# Symbol-font bullets come through as Private Use Area codepoints (e.g. U+F097)
_PUA_RE = re.compile("[-]")


def _rect_overlap_area(r1: "fitz.Rect", x0: float, y0: float, x1: float, y1: float) -> float:
    """Area of the intersection between fitz.Rect r1 and the given bbox."""
    ix0 = max(r1.x0, x0); iy0 = max(r1.y0, y0)
    ix1 = min(r1.x1, x1); iy1 = min(r1.y1, y1)
    if ix1 <= ix0 or iy1 <= iy0:
        return 0.0
    return (ix1 - ix0) * (iy1 - iy0)


def _span_to_html(span: dict) -> str:
    t = _htmllib.escape(_PUA_RE.sub("•", span.get("text", "")))
    flags = span.get("flags", 0)
    fname = span.get("font", "").lower()
    if flags & 16 or "bold" in fname:
        t = f"<b>{t}</b>"
    if flags & 2 or "italic" in fname:
        t = f"<i>{t}</i>"
    return t


def _collect_rows(page: "fitz.Page", skip_rects: list) -> list[dict]:
    """Every visual text row on the page, in reading order.

    PDF text *blocks* don't match logical paragraphs (a block can merge two
    list items or split one mid-sentence), so we work line-by-line instead:
    fragments sharing a baseline are merged into one row, and running headers,
    bare page numbers and lines already captured by a table are dropped.
    """
    lines: list[dict] = []
    for b in page.get_text("dict")["blocks"]:
        if b.get("type") != 0:
            continue
        for ln in b.get("lines", []):
            spans = [s for s in ln.get("spans", []) if s.get("text", "")]
            text = "".join(s.get("text", "") for s in spans).strip()
            if not text:
                continue
            x0, y0, x1, y1 = ln["bbox"]
            area = max((x1 - x0) * (y1 - y0), 1.0)
            if any(_rect_overlap_area(fitz.Rect(r), x0, y0, x1, y1) > 0.5 * area
                   for r in skip_rects):
                continue
            sizes = [float(s.get("size", 11.0)) for s in spans if s.get("text", "").strip()]
            lines.append({"x0": x0, "x1": x1, "y0": y0, "y1": y1,
                          "size": statistics.median(sizes) if sizes else 11.0,
                          "spans": spans, "text": text})
    lines.sort(key=lambda r: (r["y0"], r["x0"]))

    rows: list[dict] = []
    for ln in lines:
        if rows:
            r = rows[-1]
            ov = min(r["y1"], ln["y1"]) - max(r["y0"], ln["y0"])
            if ov > 0.5 * min(r["y1"] - r["y0"], ln["y1"] - ln["y0"]):
                r["frags"].append(ln)
                r["x0"] = min(r["x0"], ln["x0"]); r["x1"] = max(r["x1"], ln["x1"])
                r["y0"] = min(r["y0"], ln["y0"]); r["y1"] = max(r["y1"], ln["y1"])
                r["text"] = " ".join(f["text"] for f in sorted(r["frags"], key=lambda f: f["x0"]))
                continue
        rows.append({**{k: ln[k] for k in ("x0", "x1", "y0", "y1", "size", "text")},
                     "frags": [ln]})
    return [r for r in rows
            if not (_NOISE_PAGE_NO.fullmatch(r["text"]) or r["text"].lower() in _NOISE_HEADERS)]


def _row_html(row: dict, single_line: bool) -> str:
    """One row's inline HTML. In single-line paragraphs (headings, TOC rows)
    wide gaps between fragments are kept as nbsp runs so e.g. '14   NOTICES'
    and 'title …… page' keep their visual spacing."""
    parts: list[str] = []
    prev_x1 = None
    for f in sorted(row["frags"], key=lambda f: f["x0"]):
        if prev_x1 is not None:
            gap = f["x0"] - prev_x1
            if single_line and gap > 4.0:
                parts.append("&nbsp;" * max(1, min(int(gap / 2.8), 30)))
            else:
                parts.append(" ")
        parts.append("".join(_span_to_html(s) for s in f["spans"]))
        prev_x1 = f["x1"]
    return "".join(parts).strip()


def _group_paras(rows: list[dict]) -> list[list[dict]]:
    """Group rows into logical paragraphs: a new paragraph starts on a clear
    vertical gap, an outdent, a font-size jump, or an enumeration marker
    ('A.', '14.1', '(a)' …) that sits left of the previous row (hanging indent)."""
    paras: list[list[dict]] = []
    cur: list[dict] = []
    for r in rows:
        if cur:
            p = cur[-1]
            gap = r["y0"] - p["y1"]
            size = max(r["size"], 6.0)
            new = (gap > max(3.0, 0.30 * size) or gap < -2.0
                   or r["x0"] < p["x0"] - 6.0
                   or abs(r["size"] - cur[0]["size"]) > 1.6
                   or bool(_ENUM_MARKER.match(r["text"]) and r["x0"] < p["x0"] - 1.5))
            if new:
                paras.append(cur)
                cur = []
        cur.append(r)
    if cur:
        paras.append(cur)
    return paras


def _para_html(rows: list[dict], body_left: float, body_right: float,
               prev_bottom: float | None) -> str:
    """Render one logical paragraph to a styled <p>, preserving font size,
    alignment, hanging indent, left offset and the gap above it."""
    first = rows[0]
    multi = len(rows) > 1
    size = round(statistics.median([r["size"] for r in rows]), 1)
    dotlead = any(_DOT_LEADER.search(r["text"]) for r in rows)

    # Centered text sits inset from BOTH content-box edges; in a multi-line
    # centered block each line also starts at its own x0 (justified/left lines
    # share x0 and reach the right edge).
    def _insets(r: dict) -> tuple[float, float]:
        return r["x0"] - body_left, body_right - r["x1"]

    if multi:
        xs = [r["x0"] for r in rows]
        centered = (max(xs) - min(xs) > 18
                    and all(min(_insets(r)) >= 24 for r in rows))
    else:
        li, ri = _insets(first)
        centered = li >= 50 and ri >= 50 and min(li, ri) > 0.5 * max(li, ri)

    if centered:
        align = "center"
    elif (multi and not dotlead
          and any(body_right - r["x1"] < 15 for r in rows[:-1])):
        align = "justify"   # at least one non-last line runs flush right
    else:
        align = "left"

    if multi:
        inner = " ".join(_row_html(r, single_line=False) for r in rows)
    else:
        inner = _row_html(first, single_line=True)
    if dotlead:
        inner = _DOT_LEADER.sub("." * 20, inner)

    mt = 6.0 if prev_bottom is None else min(max(first["y0"] - prev_bottom, 0.0), 18.0)
    style = f"font-size:{size:.1f}pt;text-align:{align};margin:{mt:.1f}pt 0 0 0;"
    if not centered:
        cont = min(r["x0"] for r in rows[1:]) if multi else first["x0"]
        pad = cont - body_left          # offset of the wrap/continuation lines
        ti = first["x0"] - cont         # negative → hanging indent
        if pad > 2.5:
            style += f"padding-left:{pad:.0f}pt;"
        if abs(ti) > 2.5:
            style += f"text-indent:{ti:.0f}pt;"
    return f'<p style="{style}">{inner}</p>'


def extract_editable_html(doc_type: str, is_overseas: bool,
                          replacements: list[dict] | None = None) -> str:
    """Extract the full template as editable HTML that mirrors the original
    layout: logical paragraphs (rebuilt line-by-line), font sizes, bold/italic
    runs, justification, hanging indents, indent hierarchy, inter-paragraph
    spacing, TOC dot leaders, and real ruled tables as <table> elements.

    find_tables(strategy='lines_strict') only uses genuinely stroked ruling
    lines, so the yellow highlight rectangles behind placeholders are no longer
    misread as tables. Running headers and page numbers are dropped (the PDF
    renderer stamps its own footer back on).
    """
    doc = fitz.open(template_path(doc_type, is_overseas))
    pages: list[tuple[list[dict], list[tuple[float, float, str]]]] = []
    body_left: float | None = None
    body_right: float | None = None
    for page in doc:
        # ── real ruled tables only ─────────────────────────────────────────
        table_items: list[tuple[float, float, str]] = []  # (y0, y1, html)
        table_rects: list[tuple[float, float, float, float]] = []
        try:
            tab_finder = page.find_tables(strategy="lines_strict")
            for tab in (tab_finder.tables if tab_finder else []):
                if tab.col_count < 2 or tab.row_count < 2:
                    continue
                cells = tab.extract() or []
                filled = sum(1 for row in cells for c in row if c and str(c).strip())
                if filled < 2:
                    continue
                # column widths — 2-col tables split 50/50 (signature blocks,
                # party tables); 3+ cols use sqrt(longest) for proportional fit
                ncols = max(len(r) for r in cells)
                if ncols == 2:
                    pcts = [50.0, 50.0]
                else:
                    weights = []
                    for c in range(ncols):
                        longest = max((len(str(r[c]).strip()) for r in cells
                                       if c < len(r) and r[c]), default=1)
                        weights.append(max(longest, 4) ** 0.5)
                    total_w = sum(weights)
                    pcts = [100.0 * wt / total_w for wt in weights]
                lines = ['<table style="border-collapse:collapse;width:100%;'
                         'margin:8pt 0;font-size:10pt;">']
                for ri, row in enumerate(cells):
                    lines.append("<tr>")
                    for ci, cell in enumerate(row):
                        raw = _PUA_RE.sub("•", str(cell).strip()) if cell is not None else ""
                        txt = _htmllib.escape(raw).replace("\n", "<br>")
                        wstyle = f"width:{pcts[ci]:.0f}%;" if ri == 0 and ci < len(pcts) else ""
                        lines.append(
                            f'<td style="{wstyle}border:1px solid #999;padding:4pt 6pt;'
                            f'vertical-align:top;">{txt}</td>')
                    lines.append("</tr>")
                lines.append("</table>")
                bx0, by0, bx1, by1 = tab.bbox
                table_items.append((by0, by1, "".join(lines)))
                table_rects.append((bx0, by0, bx1, by1))
        except Exception:
            pass

        rows = _collect_rows(page, table_rects)
        if rows:
            page_min = min(r["x0"] for r in rows)
            page_max = max(r["x1"] for r in rows)
            body_left = page_min if body_left is None else min(body_left, page_min)
            body_right = page_max if body_right is None else max(body_right, page_max)
        pages.append((rows, table_items))
    doc.close()

    left = body_left or 0.0
    right = body_right or left
    paras: list[str] = []
    for rows, table_items in pages:
        items: list[tuple[float, float, str | None, list[dict] | None]] = [
            (y0, y1, html, None) for (y0, y1, html) in table_items]
        items.extend((grp[0]["y0"], max(r["y1"] for r in grp), None, grp)
                     for grp in _group_paras(rows))
        items.sort(key=lambda t: t[0])
        prev_bottom: float | None = None
        for y0, y1, table_html, grp in items:
            paras.append(table_html if table_html is not None
                         else _para_html(grp, left, right, prev_bottom))
            prev_bottom = y1

    body = "\n".join(paras)
    if replacements:
        body = apply_replacements_html(body, replacements)
    logger.info("editable_html_extracted", doc_type=doc_type, overseas=is_overseas,
                paragraphs=len(paras), size=len(body))
    return body


_ALLOWED_TAGS = {"p", "b", "strong", "i", "em", "u", "s", "strike", "br", "span",
                 "div", "mark", "ul", "ol", "li", "h1", "h2", "h3", "h4", "font",
                 "blockquote", "sub", "sup", "hr", "img",
                 "table", "thead", "tbody", "tfoot", "tr", "th", "td"}
# inline signature/image data URLs — png/jpeg only, capped size
_DATA_IMG_SRC = re.compile(
    r'src\s*=\s*"(data:image/(?:png|jpeg);base64,[A-Za-z0-9+/=]{40,800000})"', re.I)
_ALLOWED_STYLE = ("font-size", "text-align", "padding-left", "text-indent", "color",
                  "background-color", "font-weight", "font-style", "text-decoration",
                  "margin", "margin-top", "margin-bottom", "margin-left",
                  "border-collapse", "width", "vertical-align",
                  "border", "border-top", "border-bottom", "border-left", "border-right",
                  "padding")
_FONT_SIZE_MAP = {"1": "8pt", "2": "10pt", "3": "11pt", "4": "13pt",
                  "5": "16pt", "6": "20pt", "7": "26pt"}


def sanitize_live_html(html: str) -> str:
    """Whitelist-clean browser contenteditable output so it is safe to store
    and renders predictably in the PDF engine."""
    html = re.sub(r"<(script|style|iframe|object|embed)[^>]*>.*?</\1>",
                  "", html, flags=re.S | re.I)
    html = re.sub(r"<!--.*?-->", "", html, flags=re.S)

    def _clean(m: "re.Match[str]") -> str:
        closing, name, attrs = m.group(1), m.group(2).lower(), m.group(3) or ""
        if name not in _ALLOWED_TAGS:
            return ""
        if name == "font":  # execCommand('fontSize') legacy output → span
            if closing:
                return "</span>"
            sz = re.search(r"size\s*=\s*['\"]?(\d)", attrs, re.I)
            pt = _FONT_SIZE_MAP.get(sz.group(1) if sz else "3", "11pt")
            return f'<span style="font-size:{pt}">'
        if name == "img":  # signature pictures — validated data URLs only
            if closing:
                return ""
            src_m = _DATA_IMG_SRC.search(attrs)
            if not src_m:
                return ""
            keep_img = ""
            # preserve class="sig-img" so the resize toolbar can target it
            cls_m = re.search(r'class\s*=\s*"([^"]*)"', attrs, re.I)
            if cls_m and "sig-img" in cls_m.group(1):
                keep_img += ' class="sig-img" draggable="true"'
            st = re.search(r"style\s*=\s*\"([^\"]*)\"|style\s*=\s*'([^']*)'", attrs, re.I)
            if st:
                props = []
                for part in (st.group(1) or st.group(2) or "").split(";"):
                    if ":" not in part:
                        continue
                    k, v = part.split(":", 1)
                    k, v = k.strip().lower(), v.strip()
                    if (k in ("width", "height", "margin", "margin-left", "vertical-align",
                              "cursor")
                            and re.fullmatch(r"[#%().,\w\s-]*", v)):
                        props.append(f"{k}:{v}")
                if props:
                    keep_img += f' style="{";".join(props)}"'
            return f'<img src="{src_m.group(1)}"{keep_img}/>'
        if closing:
            return f"</{name}>"
        keep = ""
        style_m = re.search(r"style\s*=\s*\"([^\"]*)\"|style\s*=\s*'([^']*)'", attrs, re.I)
        if style_m:
            props = []
            for part in (style_m.group(1) or style_m.group(2) or "").split(";"):
                if ":" not in part:
                    continue
                k, v = part.split(":", 1)
                k, v = k.strip().lower(), v.strip()
                if k in _ALLOWED_STYLE and re.fullmatch(r"[#%().,\w\s-]*", v):
                    props.append(f"{k}:{v}")
            if props:
                keep = f' style="{";".join(props)}"'
        if name == "br":
            return "<br/>"
        return f"<{name}{keep}>"

    html = re.sub(r"<\s*(/?)\s*([a-zA-Z0-9]+)((?:\s[^<>]*)?)/?>", _clean, html)
    # bare newlines inside table cells (legacy saved docs) → visible line breaks
    html = re.sub(r"(<td\b[^>]*>)(.*?)(</td>)",
                  lambda m: m.group(1) + m.group(2).replace("\n", "<br>") + m.group(3),
                  html, flags=re.S | re.I)
    return html


_LIVE_CSS_BASE = """
body { font-family: __FAMILY__; font-size: 10.5pt; line-height: 1.24; color: #111; }
p, div { margin: 3pt 0; }
mark { background-color: #fde047; }
b, strong { font-weight: bold; }
ul, ol { margin: 6pt 0 6pt 20pt; }
h1, h2, h3, h4 { margin: 10pt 0 5pt; }
"""

FONT_DIR = Path(__file__).resolve().parent.parent / "templates" / "fonts"

# Carlito is metrically identical to Calibri (the font the JAPL templates use),
# so rendering with it keeps line breaks and the overall look of the original.
_CARLITO_FACES = (
    ("Carlito-Regular.ttf", ""),
    ("Carlito-Bold.ttf", "font-weight: bold;"),
    ("Carlito-Italic.ttf", "font-style: italic;"),
    ("Carlito-BoldItalic.ttf", "font-weight: bold; font-style: italic;"),
)


def _live_font_assets() -> tuple["fitz.Archive | None", str]:
    """(archive, css) for the live PDF renderer — Carlito when bundled,
    base-14 helvetica otherwise."""
    if all((FONT_DIR / name).exists() for name, _ in _CARLITO_FACES):
        faces = "".join(
            f"@font-face {{ font-family: carlito; src: url({name}); {extra} }}\n"
            for name, extra in _CARLITO_FACES)
        return (fitz.Archive(str(FONT_DIR)),
                faces + _LIVE_CSS_BASE.replace("__FAMILY__", "carlito, sans-serif"))
    return None, _LIVE_CSS_BASE.replace("__FAMILY__", "helvetica, sans-serif")


def _tables_to_paragraphs(html: str) -> str:
    """Convert <table> elements to bordered <p> rows for reliable fitz.Story rendering.

    fitz.Story/MuPDF has limited table column-layout support — cells in the same
    row render inline rather than in a proper grid.  We keep <table> in the stored
    HTML (so the browser editor shows a real table) and only call this function in
    the PDF render path.  For each row, cells are joined with " | " separators
    inside a single bordered paragraph.
    """
    def _replace_table(m: "re.Match") -> str:
        table_html = m.group(0)
        rows_html = re.findall(r"<tr[^>]*>(.*?)</tr>", table_html, re.S | re.I)
        parts: list[str] = []
        for row_html in rows_html:
            cells_html = re.findall(r"<t[dh][^>]*>(.*?)</t[dh]>", row_html, re.S | re.I)
            cells_text: list[str] = []
            for c in cells_html:
                text = re.sub(r"<br\s*/?>", " ", c, flags=re.I)
                text = re.sub(r"<[^>]+>", "", text)
                text = re.sub(r"\s+", " ", text).strip()
                cells_text.append(text)
            cells_text = [c for c in cells_text if c]
            if not cells_text:
                continue
            row_content = " &nbsp;|&nbsp; ".join(_htmllib.escape(c) for c in cells_text)
            parts.append(
                f'<p style="border:1px solid #aaa;padding:5pt 8pt;'
                f'margin:0pt 0pt 2pt 0pt;font-size:10pt;">{row_content}</p>'
            )
        return "\n".join(parts)

    return re.sub(r"<table[^>]*>.*?</table>", _replace_table, html, flags=re.S | re.I)


_TABLE_SPLIT_RE = re.compile(r"<table[^>]*>.*?</table>", re.S | re.I)


def _parse_table_html(table_html: str) -> tuple[list[list[str]], list[float]]:
    """<table> HTML → (rows of cell inner-HTML, column width percents).

    Width percents come from width:NN% styles on the first row's cells (as
    stamped by extract_editable_html); empty when absent/inconsistent."""
    rows: list[list[str]] = []
    pcts: list[float] = []
    for row_html in re.findall(r"<tr[^>]*>(.*?)</tr>", table_html, re.S | re.I):
        cells = [c.replace("\n", "<br>")
                 for c in re.findall(r"<t[dh][^>]*>(.*?)</t[dh]>", row_html, re.S | re.I)]
        if not cells:
            continue
        if not rows:  # first row — try to read stamped column widths
            attrs = re.findall(r"<t[dh]([^>]*)>", row_html, re.I)
            for a in attrs:
                m = re.search(r"width\s*:\s*([\d.]+)\s*%", a)
                pcts.append(float(m.group(1)) if m else 0.0)
        rows.append(cells)
    ncols = max((len(r) for r in rows), default=0)
    if not ncols:
        return [], []
    rows = [r + [""] * (ncols - len(r)) for r in rows]
    if len(pcts) != ncols or not all(p > 0 for p in pcts):
        pcts = []
    return rows, pcts


def _place_chunk(html: str, w: float, h: float, css: str,
                 arch: "fitz.Archive | None") -> tuple["fitz.Story", "fitz.Document | None", float, bool]:
    """Render (part of) an HTML chunk into a w×h mini-PDF.

    Returns (story, mini_doc, used_height, has_more). Pass the returned story
    back in to continue placing the remainder on the next page."""
    story = fitz.Story(html=f"<body>{html}</body>", user_css=css, archive=arch)
    mini, used, more = _place_story(story, w, h)
    return story, mini, used, more


def _place_story(story: "fitz.Story", w: float, h: float) -> tuple["fitz.Document | None", float, bool]:
    buf = io.BytesIO()
    writer = fitz.DocumentWriter(buf)
    rect = fitz.Rect(0, 0, w, h)
    dev = writer.begin_page(rect)
    more, filled = story.place(rect)
    story.draw(dev)
    writer.end_page()
    writer.close()
    used = float(filled[3]) if filled else 0.0
    if used <= 0:
        return None, 0.0, bool(more)
    buf.seek(0)
    return fitz.open(stream=buf.getvalue(), filetype="pdf"), used, bool(more)


def live_html_to_pdf(body_html: str) -> bytes:
    """Lay live-edited document HTML out as an A4 PDF.

    Text flows via fitz.Story using Carlito (metric clone of the templates'
    Calibri) so line breaks match the original. <table> elements are drawn as
    real ruled grids — column widths weighted by content, one row at a time,
    page-breaking between rows. The standard running footer is stamped on."""
    body_html = sanitize_live_html(body_html)
    body_html = body_html.replace("●", "•")  # U+25CF has no glyph in base helvetica
    arch, css = _live_font_assets()

    # inline data-URL images (signatures) → in-memory archive entries
    _img_n = [0]

    def _img_to_archive(m: "re.Match[str]") -> str:
        nonlocal arch
        try:
            data = base64.b64decode(m.group(1).split(",", 1)[1])
        except Exception:
            return 'src=""'
        if arch is None:
            arch = fitz.Archive()
        _img_n[0] += 1
        name = f"_live_img_{_img_n[0]}"
        arch.add(data, name)
        return f'src="{name}"'

    body_html = _DATA_IMG_SRC.sub(_img_to_archive, body_html)

    # split into text / table segments
    segments: list[tuple[str, str]] = []
    pos = 0
    for m in _TABLE_SPLIT_RE.finditer(body_html):
        if m.start() > pos:
            segments.append(("html", body_html[pos:m.start()]))
        segments.append(("table", m.group(0)))
        pos = m.end()
    if pos < len(body_html):
        segments.append(("html", body_html[pos:]))

    mediabox = fitz.paper_rect("a4")
    x0, y_top = 52.0, 54.0
    x1, y_bot = mediabox.width - 52.0, mediabox.height - 66.0
    doc = fitz.open()
    page = doc.new_page(width=mediabox.width, height=mediabox.height)
    cy = y_top

    def _new_page():
        nonlocal page, cy
        page = doc.new_page(width=mediabox.width, height=mediabox.height)
        cy = y_top

    for kind, content in segments:
        if kind == "html":
            if not content.strip():
                continue
            story = fitz.Story(html=f"<body>{content}</body>", user_css=css, archive=arch)
            guard = 0
            while True:
                guard += 1
                if guard > 500:  # safety against a non-advancing story
                    break
                if y_bot - cy < 40:
                    _new_page()
                avail_h = y_bot - cy
                mini, used, more = _place_story(story, x1 - x0, avail_h)
                if mini is not None:
                    page.show_pdf_page(fitz.Rect(x0, cy, x1, cy + used), mini, 0,
                                       clip=fitz.Rect(0, 0, x1 - x0, used))
                    mini.close()
                    cy += used
                if not more:
                    break
                if used <= 1.0 and avail_h >= (y_bot - y_top) - 1:
                    break    # content that never fits — drop instead of looping
                _new_page()
        else:
            rows, pcts = _parse_table_html(content)
            if not rows:
                continue
            pad = 3.5
            avail_w = x1 - x0
            ncols = len(rows[0])
            if pcts:   # widths stamped at extraction — match the editor exactly
                total_p = sum(pcts)
                widths = [avail_w * p / total_p for p in pcts]
            else:
                weights = []
                for c in range(ncols):
                    longest = max((len(re.sub(r"<[^>]+>", "", r[c])) for r in rows), default=1)
                    weights.append(max(longest, 4) ** 0.5)
                total_w = sum(weights)
                widths = [avail_w * wt / total_w for wt in weights]
            cell_css = css + " body { font-size: 10pt; } p, div { margin: 1pt 0; }"
            cy += 4
            for row in rows:
                rendered: list[tuple["fitz.Document | None", float]] = []
                row_h = 0.0
                for c, cell in enumerate(row):
                    inner = cell.strip() or "&nbsp;"
                    _s, mini, used, _m = _place_chunk(inner, widths[c] - 2 * pad, 1400.0,
                                                      cell_css, arch)
                    rendered.append((mini, used))
                    row_h = max(row_h, used)
                row_h += 2 * pad
                if cy + row_h > y_bot and cy > y_top:
                    _new_page()
                cx = x0
                for c, (mini, used) in enumerate(rendered):
                    cell_rect = fitz.Rect(cx, cy, cx + widths[c], cy + row_h)
                    page.draw_rect(cell_rect, color=(0.55, 0.55, 0.55), width=0.7)
                    if mini is not None:
                        page.show_pdf_page(
                            fitz.Rect(cx + pad, cy + pad, cx + widths[c] - pad, cy + pad + used),
                            mini, 0, clip=fitz.Rect(0, 0, widths[c] - 2 * pad, used))
                        mini.close()
                    cx += widths[c]
                cy += row_h
            cy += 4

    for i in range(doc.page_count):
        pg = doc[i]
        w, h = pg.rect.width, pg.rect.height
        pg.insert_text((52, h - 32), "Private and confidential",
                       fontsize=8, fontname="helv", color=(0.45, 0.45, 0.45))
        tail = str(i + 1)
        tw = fitz.get_text_length(tail, fontname="helv", fontsize=8)
        pg.insert_text((w - 52 - tw, h - 32), tail,
                       fontsize=8, fontname="helv", color=(0.45, 0.45, 0.45))
    out = doc.tobytes(deflate=True, garbage=3)
    doc.close()
    return out


# ---------------------------------------------------------------------------
# Legacy HTML helpers (template_html / html_to_pdf kept for compatibility)
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
        if not txt or _NOISE_PAGE_NO.fullmatch(txt) or txt.lower() in _NOISE_HEADERS:
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


def _ov_frac(v, default: float = 0.0) -> float:
    """Clamp an overlay coordinate to the 0..1 fractional range."""
    try:
        return max(0.0, min(1.0, float(v)))
    except (TypeError, ValueError):
        return default


def stamp_overlays(pdf_bytes: bytes, overlays: list[dict] | None) -> bytes:
    """Flatten Adobe-style signature overlays onto a rendered PDF.

    Each overlay is ``{page, x, y, w, h, image}`` where x/y/w/h are fractions
    (0..1) of the target page's width/height and ``image`` is a validated
    ``data:image/...`` URL. The signature is painted on top of the page content
    (the document text underneath is never altered)."""
    if not overlays:
        return pdf_bytes
    doc = fitz.open(stream=pdf_bytes, filetype="pdf")
    try:
        changed = False
        for ov in overlays:
            try:
                img = (ov or {}).get("image") or ""
                if not img.startswith("data:image"):
                    continue
                pno = int(ov.get("page", 0))
                if not (0 <= pno < doc.page_count):
                    continue
                page = doc[pno]
                pr = page.rect
                x = _ov_frac(ov.get("x")) * pr.width
                y = _ov_frac(ov.get("y")) * pr.height
                w = max(0.02, _ov_frac(ov.get("w"), 0.2)) * pr.width
                h = max(0.01, _ov_frac(ov.get("h"), 0.08)) * pr.height
                raw = base64.b64decode(img.split(",", 1)[1])
                page.insert_image(fitz.Rect(x, y, x + w, y + h),
                                  stream=raw, keep_proportion=True, overlay=True)
                changed = True
            except Exception:
                continue
        if not changed:
            return pdf_bytes
        return doc.tobytes(deflate=True, garbage=3)
    finally:
        doc.close()


def append_signature_page(pdf_bytes: bytes, doc_type: str, sig: dict | None = None,  # noqa: ARG001
                          internal_sig: dict | None = None) -> bytes:
    """Append an Adobe-Sign-style e-signature certificate page.

    Layout (stacked vertically):
      - Dark header bar with title
      - Document info strip
      - Per-signer block: shaded header + large signature box + info grid
      - Legal footer
    """
    import datetime as _dt

    doc = fitz.open(stream=pdf_bytes, filetype="pdf")
    page = doc.new_page()
    w, h = page.rect.width, page.rect.height
    ML, MR = 54.0, w - 54.0
    CW = MR - ML

    # ── colour palette ────────────────────────────────────────────────────────
    C_HEADER_BG   = (0.07, 0.18, 0.36)   # dark navy
    C_HEADER_TXT  = (1.0,  1.0,  1.0)    # white
    C_BAND_BG     = (0.94, 0.96, 1.0)    # very light blue
    C_BAND_TXT    = (0.07, 0.18, 0.36)   # navy text
    C_GREEN       = (0.05, 0.6,  0.25)   # signed badge green
    C_GREEN_BG    = (0.88, 1.0,  0.91)
    C_ORANGE_BG   = (1.0,  0.96, 0.88)
    C_ORANGE      = (0.7,  0.35, 0.0)
    C_BORDER      = (0.78, 0.83, 0.92)
    C_LABEL       = (0.38, 0.42, 0.5)
    C_VALUE       = (0.07, 0.10, 0.18)
    C_SIG_BOX_BG  = (0.97, 0.98, 1.0)
    WHITE         = (1.0,  1.0,  1.0)

    def _rect_fill(r, fill, border=None, bw=0.5):
        page.draw_rect(r, color=border, fill=fill, width=bw)

    def _txt(x, y, text, size=9, bold=False, color=C_VALUE):
        page.insert_text((x, y), str(text)[:120], fontsize=size,
                         fontname="hebo" if bold else "helv", color=color)

    def _box(r, text, size=9, bold=False, color=C_VALUE, align=0):
        page.insert_textbox(r, str(text), fontsize=size,
                            fontname="hebo" if bold else "helv",
                            color=color, align=align)

    # ── header bar ────────────────────────────────────────────────────────────
    HDR_H = 44
    _rect_fill(fitz.Rect(0, 0, w, HDR_H), fill=C_HEADER_BG, border=C_HEADER_BG, bw=0.5)
    _box(fitz.Rect(ML, 8, MR - 120, 28),
         "JANE AEROSPACE PRIVATE LIMITED", size=9, bold=True, color=C_HEADER_TXT)
    _box(fitz.Rect(ML, 22, MR, 38),
         "Electronic Signature Certificate", size=11, bold=True, color=WHITE)

    # document label top-right
    doc_label = DOC_LABELS.get(doc_type, doc_type.upper())
    _box(fitz.Rect(MR - 160, 6, MR, 38), doc_label,
         size=8, color=(0.72, 0.82, 1.0), align=2)

    # ── document info strip ───────────────────────────────────────────────────
    INFO_Y = HDR_H + 4
    _rect_fill(fitz.Rect(ML, INFO_Y, MR, INFO_Y + 22),
               fill=C_BAND_BG, border=C_BORDER)
    now_str = _dt.datetime.now().strftime("%d %b %Y")
    _box(fitz.Rect(ML + 6, INFO_Y + 4, MR - 6, INFO_Y + 18),
         f"Document: {doc_label}   |   Certificate generated: {now_str}   |   "
         f"Platform: Jane Aerospace Secure Signing Portal",
         size=8, color=C_BAND_TXT)

    # ── signer block helper ───────────────────────────────────────────────────
    BLOCK_GAP = 10

    def _signer_block(y0: float, s: dict | None, role: str,
                      company: str, pending: bool = False) -> float:
        """Draw one signer block; return the bottom y."""
        BAND_H   = 26
        SIG_H    = 96    # signature image area height
        INFO_H   = 100   # info grid height (3 rows × ~28pt + footer ~16pt)
        TOTAL_H  = BAND_H + SIG_H + INFO_H

        # outer card border
        page.draw_rect(fitz.Rect(ML, y0, MR, y0 + TOTAL_H),
                       color=C_BORDER, fill=WHITE, width=0.6)

        # ── coloured header band ──
        badge_color  = C_GREEN    if not pending else C_ORANGE
        badge_bg     = C_GREEN_BG if not pending else C_ORANGE_BG
        badge_txt    = "SIGNED"   if not pending else "PENDING"
        _rect_fill(fitz.Rect(ML, y0, MR, y0 + BAND_H),
                   fill=badge_bg, border=badge_bg, bw=0.5)
        # role label left — insert_text for reliable placement on narrow strip
        band_txt = C_BAND_TXT  # dark navy, always readable on both green/orange bg
        label_str = f"{company[:45]}  |  {role}"
        page.insert_text((ML + 8, y0 + 17), label_str,
                         fontsize=8.5, fontname="hebo", color=band_txt)
        # badge right
        badge_r = fitz.Rect(MR - 64, y0 + 5, MR - 6, y0 + BAND_H - 5)
        _rect_fill(badge_r, fill=badge_color, border=None)
        _box(badge_r, badge_txt, size=8, bold=True, color=WHITE, align=1)

        # ── signature image area ──
        sig_area = fitz.Rect(ML + 8, y0 + BAND_H + 4,
                             MR - 8, y0 + BAND_H + SIG_H - 4)
        _rect_fill(sig_area, fill=C_SIG_BOX_BG, border=C_BORDER, bw=0.5)

        if pending or s is None:
            _box(fitz.Rect(sig_area.x0 + 10, sig_area.y0 + 28,
                           sig_area.x1 - 10, sig_area.y1 - 4),
                 "Signature pending", size=10, color=C_LABEL, align=1)
        else:
            img = (s or {}).get("sig_image") or ""
            drew = False
            if img.startswith("data:image"):
                try:
                    raw = base64.b64decode(img.split(",", 1)[1])
                    page.insert_image(fitz.Rect(sig_area.x0 + 8, sig_area.y0 + 4,
                                                sig_area.x1 - 8, sig_area.y1 - 4),
                                      stream=raw, keep_proportion=True)
                    drew = True
                except Exception:
                    pass
            if not drew:
                sf = "helv" if (s or {}).get("sig_font") == "standard" else "tiit"
                name = (s or {}).get("signed_name", "")
                page.insert_text(
                    (sig_area.x0 + 14, sig_area.y0 + (SIG_H - 8) / 2 + 4),
                    name[:60], fontsize=22, fontname=sf,
                    color=(0.05, 0.14, 0.38))

        # ── info grid ──
        gy = y0 + BAND_H + SIG_H
        page.draw_line((ML, gy), (MR, gy), color=C_BORDER, width=0.5)

        # _kv: small grey label line, then darker value line below it
        def _kv(lx, ly, label, value):
            _txt(lx, ly,      label,       size=7,   color=C_LABEL)
            _txt(lx, ly + 10, str(value)[:58], size=8.5, color=C_VALUE)

        if s:
            signed_at = s.get("signed_at", "")
            if signed_at and "T" in signed_at:
                d, t = signed_at.split("T", 1)
                signed_at = f"{d}  {t[:5]} IST"

            col1_x = ML + 8
            col2_x = ML + CW / 2 + 8
            # each kv row = label(7pt) + value(8.5pt) + gap = ~28pt
            row1 = gy + 12
            row2 = gy + 40
            row3 = gy + 68

            _kv(col1_x, row1, "Signed By",   s.get("signed_name", ""))
            _kv(col2_x, row1, "Title",        s.get("designation", "-"))
            _kv(col1_x, row2, "Email",        s.get("email", "-"))
            _kv(col2_x, row2, "IP Address",   s.get("ip", "-"))
            _kv(col1_x, row3, "Date & Time",  signed_at)
            _kv(col2_x, row3, "On behalf of", s.get("company_name", "-"))
            page.draw_line((ML, gy + INFO_H - 16), (MR, gy + INFO_H - 16),
                           color=C_BORDER, width=0.4)
            _box(fitz.Rect(ML + 8, gy + INFO_H - 15, MR - 8, gy + INFO_H),
                 "Signed electronically via Jane Aerospace Secure Signing Portal. "
                 "Signature constitutes legal acceptance.",
                 size=7, color=C_LABEL)

        return y0 + TOTAL_H

    # ── draw signer blocks ────────────────────────────────────────────────────
    japl_name  = (internal_sig or {}).get("company_name", "Jane Aerospace Private Limited")

    y = float(INFO_Y + 26 + BLOCK_GAP)
    y = _signer_block(y, internal_sig, "Authorised Signatory", japl_name,
                      pending=(internal_sig is None))

    # ── legal footer ──────────────────────────────────────────────────────────
    footer_y = y + 14
    page.draw_line((ML, footer_y), (MR, footer_y), color=C_BORDER, width=0.5)
    _box(fitz.Rect(ML, footer_y + 4, MR, footer_y + 28),
         "This certificate is an official record of the electronic signatures applied to the above "
         "document through the Jane Aerospace secure signing portal. Each signatory confirmed "
         "acceptance by entering their legal name and clicking 'I Agree & Sign'. "
         "This record, including timestamps and IP addresses, constitutes the full audit trail.",
         size=7.5, color=C_LABEL)

    out = doc.tobytes(deflate=True, garbage=3)
    doc.close()
    return out
