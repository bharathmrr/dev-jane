# Flattening a Captured Graphical Signature into PDFs

How the transparent PNG produced by `signature_pad.html` (`data:image/png;base64,…`)
gets structurally embedded into a PDF. Two honest paths — pick based on whether you
want a **self-hosted stamp** or an **Adobe-managed, audit-tracked agreement**.

> **Accuracy note up front.** Adobe Acrobat Sign normally *captures the signature
> itself* when the signer signs inside Adobe — you generally do **not** push your own
> raster into Adobe's signature field. So:
> - Want your captured raster flattened into the file → **Path A (stamp it yourself)**.
>   This is what your existing `append_signature_page()` already does, and it is the
>   correct home for the PNG from the canvas tool.
> - Want Adobe to run the legal signing ceremony + audit trail → **Path B (Acrobat
>   Sign + Text Tags)**, where Adobe collects the signature and the text tags only
>   tell Adobe *where* the field goes.

---

## What the canvas exports, and why it flattens cleanly

`SignaturePad.toDataURL()` returns a **transparent**, **ink-trimmed**, **downscaled**
PNG. Each property matters for PDF embedding:

| Property | Why it matters for flattening |
|----------|------------------------------|
| Transparent background | The signature composites over document text/lines with no white box occluding content. |
| Ink-trimmed (alpha bbox crop) | The placed rectangle is the signature, not 450×200 of mostly-empty pad — so aspect ratio and positioning are predictable. |
| Downscaled to ≤ 600 px / ≤ 800 KB base64 | Stays inside your server regex `^data:image/png;base64,[A-Za-z0-9+/=]{40,800000}$` and keeps the PDF small. |
| PNG (lossless) | No JPEG ringing around thin strokes. |

---

## Path A — Stamp the raster into the PDF yourself (recommended for your stack)

You already have this with PyMuPDF in `app/services/pdf_documents.py`:

```python
raw = base64.b64decode(img.split(",", 1)[1])
page.insert_image(rect, stream=raw, keep_proportion=True)   # append_signature_page()
```

That hardcodes the rectangle on a generated certificate page. To flatten the signature
**onto an existing PDF at a logical anchor** (so layout changes don't break placement),
search for an Acrobat-style **Text Tag** and stamp into the rectangle it occupies:

```python
import base64, fitz   # PyMuPDF

def stamp_signature(pdf_bytes: bytes, sig_data_url: str,
                    anchor: str = "{{Sig_es_:signer1:signature}}") -> bytes:
    """Replace the first occurrence of a text-tag anchor with the captured PNG."""
    doc = fitz.open(stream=pdf_bytes, filetype="pdf")
    png = base64.b64decode(sig_data_url.split(",", 1)[1])
    for page in doc:
        hits = page.search_for(anchor)          # rect(s) where the tag text sits
        if not hits:
            continue
        r = hits[0]
        # 1) remove the visible tag text so only the signature remains
        page.add_redact_annot(r, fill=(1, 1, 1))
        page.apply_redactions()
        # 2) drop the signature into a box anchored on the tag (a touch taller)
        box = fitz.Rect(r.x0, r.y0 - 4, r.x0 + 150, r.y0 + 34)
        page.insert_image(box, stream=png, keep_proportion=True, overlay=True)
        break
    out = doc.tobytes(deflate=True, garbage=4)   # garbage=4 → flatten/compact
    doc.close()
    return out
```

**Why anchor by text tag instead of fixed coordinates:** authors drop
`{{Sig_es_:signer1:signature}}` into the source document (white or 1-pt font so it's
invisible). The stamper finds it at render time, so the signature lands correctly even
after the document text reflows — no magic `Rect(72, 690, …)` constants to maintain.

**"Flatten" specifically** means the image becomes part of the page content stream with
no editable annotation layer. `doc.tobytes(garbage=4, deflate=True)` rewrites the file
so the stamp can't be moved or deleted in a viewer. For a tamper-evident result, follow
with a document-level digital signature / PAdES seal (see Path C).

---

## Path B — Acrobat Sign REST API with Text Tags (Adobe runs the ceremony)

Use this when you need Adobe's legally-binding workflow, audit trail, and reminders —
and you let the **signer** sign inside Adobe rather than injecting your own raster.

### B.1 Acrobat Sign Text Tags (a.k.a. directives)

Text tags are inline strings in the source document that Adobe parses into form fields:

```
{{Sig_es_:signer1:signature}}      → signature field for signer 1
{{*ES_:signer1:signature}}         → required signature (short form)
{{N_es_:signer1:fullname}}         → auto-filled signer name
{{Dte_es_:signer1:date}}           → auto-filled signing date
{{Int_es_:signer1:initials}}       → initials field
```

Place the tag where the field should appear; render the tag text white/invisible.
On upload, Adobe converts each tag into an interactive field bound to that recipient.

### B.2 Create an agreement (Acrobat Sign REST API v6)

```bash
# 1) Upload the base PDF (with text tags) as a transient document
curl -X POST https://api.{shard}.adobesign.com/api/rest/v6/transientDocuments \
  -H "Authorization: Bearer $ADOBE_TOKEN" \
  -F "File=@nda_with_tags.pdf;type=application/pdf"
# → { "transientDocumentId": "3AAA..." }
```

```bash
# 2) Create the agreement; Adobe reads the text tags for field placement
curl -X POST https://api.{shard}.adobesign.com/api/rest/v6/agreements \
  -H "Authorization: Bearer $ADOBE_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
        "fileInfos": [{ "transientDocumentId": "3AAA..." }],
        "name": "Jane Aerospace NDA",
        "participantSetsInfo": [{
          "order": 1, "role": "SIGNER",
          "memberInfos": [{ "email": "signer@client.com" }]
        }],
        "signatureType": "ESIGN",
        "state": "IN_PROCESS"
      }'
# → { "id": "CBJCHBCAAB..." }   (agreementId)
```

```bash
# 3) After signing, download the flattened, certified PDF + audit report
curl https://api.{shard}.adobesign.com/api/rest/v6/agreements/$ID/combinedDocument \
  -H "Authorization: Bearer $ADOBE_TOKEN" -o signed.pdf
```

The returned PDF is already **flattened and certified** by Adobe (LTV/PAdES), with the
field replaced by the signer's drawn or typed signature and an attached audit trail.

> If you must seed Adobe with a *pre-drawn* image (e.g. a wet-ink scan rather than live
> capture), that is **not** the signature field — set it as the default value of an
> image/stamp form field, or use Path A to burn it in before upload. Adobe's signature
> field is filled by the signer, by design.

### B.3 Where your canvas fits

| Scenario | Use |
|----------|-----|
| Signer signs in *your* UI, you own the file | Path A — stamp the canvas PNG, optionally seal (Path C). |
| Signer signs in *Adobe's* UI, you want Adobe's audit trail | Path B — text tags + Acrobat Sign; the canvas tool is not used. |
| Hybrid: capture in your UI, archive in Adobe | Path A to stamp, then upload the flattened PDF to Adobe as a signed-record transient document. |

---

## Path C — Tamper-evidence after stamping (Adobe PDF Services / PAdES)

Stamping an image is *visual* only — it does not cryptographically lock the file. To make
the flattened signature tamper-evident, apply a PAdES digital signature with Adobe's
PDF Services **Electronic Seal** API (organization-level certificate):

```bash
curl -X POST https://pdf-services.adobe.io/operation/electronicseal \
  -H "Authorization: Bearer $PDF_SERVICES_TOKEN" \
  -H "x-api-key: $CLIENT_ID" \
  -H "Content-Type: application/json" \
  -d '{
        "inputDocumentAssetID": "<uploaded-flattened-pdf>",
        "sealOptions": {
          "signatureFormat": "PADES",
          "cscCredentialOptions": { "providerName": "<TSP>", "credentialId": "<id>", "pin": "<pin>" },
          "sealFieldOptions": { "pageNumber": 1, "fieldName": "Signature1",
                                "location": { "left": 72, "top": 700, "right": 222, "bottom": 740 } }
        }
      }'
```

This yields a PDF whose integrity is verifiable in any reader — the green-check
"signature valid, document not modified" state — with your stamped raster as the visible
appearance and the certificate providing the cryptographic guarantee.

---

## Compliance checklist (ESIGN / eIDAS / IT Act 2000)

Your backend already captures most of this in the `signature` dict — keep it:

- [x] **Intent + consent** — explicit "I agree & sign" action with a logged checkbox.
- [x] **Attribution** — `signed_name`, `email`, `ip`, `user_agent`, `signed_at` (IST).
- [x] **Integrity** — flatten with `garbage=4`; for legal weight add a PAdES seal (Path C).
- [x] **Transparent PNG** — composites over content without occluding it.
- [ ] **Tamper-evidence** — add Path C if these documents must survive a legal challenge.
- [ ] **Retention** — store the signed+sealed PDF and the audit trail immutably.

---

### TL;DR for this repo
The canvas tool's PNG should flow into **Path A** — extend `append_signature_page()` /
add `stamp_signature()` to place it at a `{{Sig_es_:signer1:signature}}` anchor, then
`tobytes(garbage=4, deflate=True)` to flatten. Reach for Acrobat Sign (Path B) only when
you want Adobe to own the signing ceremony and audit trail, and for legal-grade
tamper-evidence layer a PAdES seal (Path C) on top.
