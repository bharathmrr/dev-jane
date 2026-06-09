"""KYC document OCR via Claude Vision.

Takes uploaded document images (PAN card, GST certificate, incorporation cert, etc.)
and extracts structured KYC fields using Claude's vision capability.

Supported document types:
  - PAN card              → pan_number, company_name
  - GST certificate       → gstin_number, company_name, registered_address
  - CIN / incorporation   → cin_number, company_name, registered_address, company_reg_number
  - Any business document → best-effort extraction of all KYC fields

Returns a dict with extracted fields + confidence indicators.
"""
from __future__ import annotations

import base64
import json
import re
from pathlib import Path
from typing import Union

import anthropic

from app.core.config import settings
from app.core.logging import get_logger

log = get_logger("kyc_ocr")

_EXTRACT_PROMPT = """You are a KYC document analyst. Extract ALL business/company information from this document image.

Return ONLY a valid JSON object with these exact keys (use null for fields not found):
{
  "company_name": "exact legal name",
  "pan_number": "10-char PAN like AARCA1234F",
  "gstin_number": "15-char GSTIN like 29AARCA1234F1ZV",
  "cin_number": "21-char CIN like U72900KA2019PTC123456",
  "registered_address": "full registered address",
  "company_reg_number": "registration/incorporation number",
  "country_of_incorporation": "country name (for overseas docs)",
  "tax_id_tin": "tax ID or TIN (for overseas docs)",
  "lei_number": "20-char LEI if present",
  "document_type": "pan_card|gst_certificate|cin_certificate|incorporation_cert|other",
  "confidence": "high|medium|low",
  "raw_text_snippet": "first 200 chars of raw text seen"
}

Rules:
- Extract EXACTLY as shown in the document — no corrections or guessing
- PAN format: 5 uppercase letters + 4 digits + 1 letter (e.g. AARCA1234F)
- GSTIN: 15 chars, starts with 2-digit state code
- CIN: starts with L or U, 21 chars total
- If multiple values found for same field, use the most prominent one
- Set confidence=high only if you can clearly read the value
"""


def extract_kyc_from_image(
    image_data: Union[bytes, str, Path],
    mime_type: str = "image/jpeg",
) -> dict:
    """Extract KYC fields from a document image using Claude Vision.

    Args:
        image_data: Raw bytes, base64 string, or Path to image file
        mime_type: image/jpeg, image/png, image/webp, image/gif, application/pdf

    Returns:
        dict with extracted KYC fields
    """
    if isinstance(image_data, Path):
        image_data = image_data.read_bytes()

    if isinstance(image_data, bytes):
        b64 = base64.standard_b64encode(image_data).decode("utf-8")
    else:
        b64 = image_data  # already base64

    client = anthropic.Anthropic(api_key=settings.ANTHROPIC_API_KEY)

    try:
        msg = client.messages.create(
            model="claude-haiku-4-5",
            max_tokens=1024,
            messages=[
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "image",
                            "source": {
                                "type": "base64",
                                "media_type": mime_type,
                                "data": b64,
                            },
                        },
                        {"type": "text", "text": _EXTRACT_PROMPT},
                    ],
                }
            ],
        )
        raw = msg.content[0].text.strip()

        # Strip markdown fences if present
        raw = re.sub(r"^```(?:json)?\s*", "", raw)
        raw = re.sub(r"\s*```$", "", raw)

        result = json.loads(raw)
        log.info("kyc_ocr_extracted", doc_type=result.get("document_type"), confidence=result.get("confidence"))
        return result

    except json.JSONDecodeError as e:
        log.error("kyc_ocr_json_parse_failed", error=str(e))
        return {"error": f"OCR parse failed: {e}", "confidence": "low"}
    except Exception as e:
        log.error("kyc_ocr_failed", error=str(e))
        return {"error": str(e), "confidence": "low"}


def extract_kyc_from_multiple_images(
    images: list[dict],
) -> dict:
    """Extract and merge KYC fields from multiple document images.

    Args:
        images: list of {"data": bytes|str, "mime_type": str, "label": str}

    Returns:
        Merged dict — later images override earlier ones only if confidence is higher
    """
    merged: dict = {}

    for img in images:
        result = extract_kyc_from_image(
            image_data=img["data"],
            mime_type=img.get("mime_type", "image/jpeg"),
        )
        if result.get("error"):
            log.warning("kyc_ocr_image_skipped", label=img.get("label"), error=result["error"])
            continue

        # Merge: overwrite only non-null values
        for key, val in result.items():
            if val is not None and key not in ("confidence", "raw_text_snippet", "document_type", "error"):
                if key not in merged or merged.get(key) is None:
                    merged[key] = val

        merged.setdefault("documents_processed", [])
        merged["documents_processed"].append({
            "label": img.get("label", "document"),
            "type": result.get("document_type"),
            "confidence": result.get("confidence"),
        })

    return merged


def generate_kyc_review_email(
    lead_name: str,
    lead_email: str,
    company_name: str,
    kyc_result: dict,
    ocr_result: dict | None = None,
) -> str:
    """Use Claude to generate a detailed KYC reviewer email with AI analysis.

    Includes: missing fields, suspicious items, summary, recommendation.
    """
    client = anthropic.Anthropic(api_key=settings.ANTHROPIC_API_KEY)

    issues = kyc_result.get("issues", [])
    notes = kyc_result.get("notes", [])
    overall_passed = kyc_result.get("overall_passed", False)
    auto_approvable = kyc_result.get("auto_approvable", False)

    prompt = f"""You are a KYC compliance analyst at Jane Aerospace Private Limited.
Generate a professional KYC review email for the onboarding team.

Lead: {lead_name} ({lead_email})
Company: {company_name}
Overall Passed: {overall_passed}
Auto-Approvable: {auto_approvable}

Verification Issues Found:
{chr(10).join(f'- {i}' for i in issues) if issues else '- None'}

Verification Notes:
{chr(10).join(f'- {n}' for n in notes) if notes else '- None'}

GSTIN Check: {json.dumps(kyc_result.get('gstin_check'), indent=2)}
PAN Check: {json.dumps(kyc_result.get('pan_check'), indent=2)}
CIN Check: {json.dumps(kyc_result.get('cin_check'), indent=2)}

OCR Extraction Result: {json.dumps(ocr_result, indent=2) if ocr_result else 'No documents uploaded'}

Write an HTML email with these sections:
1. <b>Status</b>: PASSED / NEEDS REVIEW / FAILED with color
2. <b>Missing Information</b>: list any required fields not provided
3. <b>Suspicious Items</b>: flag any mismatches, format errors, or concerns
4. <b>Summary</b>: 2-3 sentence plain English recommendation
5. <b>Action Required</b>: clear next step for reviewer

Keep it concise and professional. Use simple HTML formatting."""

    msg = client.messages.create(
        model="claude-haiku-4-5",
        max_tokens=1500,
        messages=[{"role": "user", "content": prompt}],
    )
    return msg.content[0].text
