"""Generates NDA HTML for email delivery when Zoho Contracts API is unavailable.

Produces a formatted HTML document that can be sent as the email body,
suitable for print → sign → scan → return workflow.
"""
from __future__ import annotations

import datetime as dt
from zoneinfo import ZoneInfo

_IST = ZoneInfo("Asia/Kolkata")


_CSS = """
<style>
  body { font-family: Georgia, 'Times New Roman', serif; font-size: 14px; color: #1a1a1a; max-width: 750px; margin: 0 auto; padding: 20px; }
  h1 { font-size: 20px; text-align: center; text-transform: uppercase; letter-spacing: 2px; border-bottom: 2px solid #000; padding-bottom: 10px; }
  h2 { font-size: 15px; margin-top: 22px; text-transform: uppercase; }
  p { line-height: 1.75; margin: 10px 0; text-align: justify; }
  .parties { background: #f7f7f7; border: 1px solid #ddd; padding: 14px 18px; border-radius: 4px; margin: 18px 0; }
  .parties p { margin: 5px 0; }
  .signature-block { margin-top: 40px; border-top: 1px solid #ccc; padding-top: 20px; }
  .sig-row { display: inline-block; width: 45%; margin-right: 8%; vertical-align: top; }
  .sig-line { border-bottom: 1px solid #333; height: 30px; margin-bottom: 6px; }
  .sig-label { font-size: 12px; color: #555; }
  .watermark { color: #cc0000; font-size: 12px; text-align: center; margin: 10px 0 20px; font-style: italic; }
</style>
"""


def _row(label: str, value: str) -> str:
    if not value:
        return ""
    return f'<p><strong>{label}:</strong> {value}</p>'


def render_nda_html(merge_fields: dict, is_overseas: bool) -> str:
    """Return a complete HTML NDA document with merge fields filled."""
    mf = {k: (str(v).strip() if v else "") for k, v in merge_fields.items()}

    company_name = mf.get("company_name") or "the Counterparty"
    contact_name = mf.get("contact_name") or company_name
    effective_date = mf.get("effective_date") or dt.datetime.now(_IST).strftime("%d %B %Y")
    signatory_email = mf.get("signatory_email") or ""

    if is_overseas:
        governing_law = "the laws of England and Wales"
        jurisdiction = "the courts of England and Wales"
        party_b_desc = f'a company incorporated in {mf.get("country_of_incorporation") or "its country of incorporation"}'
    else:
        governing_law = "the laws of India"
        jurisdiction = "the courts of Bengaluru, Karnataka, India"
        party_b_desc = f'a company incorporated in India ({"CIN: " + mf.get("cin_number") if mf.get("cin_number") else "registered company"})'

    pan_line = _row("PAN", mf.get("pan_number", ""))
    gst_line = _row("GSTIN", mf.get("gstin_number", ""))
    cin_line = _row("CIN", mf.get("cin_number", ""))
    lei_line = _row("LEI", mf.get("lei_number", ""))
    country_line = _row("Country of Incorporation", mf.get("country_of_incorporation", ""))
    reg_line = _row("Company Registration Number", mf.get("company_reg_number", ""))

    party_b_details = pan_line + gst_line + cin_line + lei_line + country_line + reg_line

    html = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
{_CSS}
</head>
<body>

<h1>Mutual Non-Disclosure Agreement</h1>
<p class="watermark">PLEASE SIGN AND RETURN THIS DOCUMENT VIA EMAIL</p>

<p>This Mutual Non-Disclosure Agreement ("<strong>Agreement</strong>") is entered into as of
<strong>{effective_date}</strong> ("<strong>Effective Date</strong>") by and between:</p>

<div class="parties">
  <p><strong>Party A (Disclosing / Receiving Party):</strong></p>
  <p><strong>Jane Aerospace Private Limited</strong>, a company incorporated under the
  Companies Act, 2013 with its registered office in India ("<strong>Jane Aerospace</strong>" or "<strong>Party A</strong>").</p>
  <br>
  <p><strong>Party B (Disclosing / Receiving Party):</strong></p>
  <p><strong>{company_name}</strong>, {party_b_desc} ("<strong>Counterparty</strong>" or "<strong>Party B</strong>").</p>
  {party_b_details}
  <p>Represented by: <strong>{contact_name}</strong>{(' — ' + signatory_email) if signatory_email else ''}</p>
</div>

<p>Jane Aerospace and the Counterparty are each a "<strong>Party</strong>" and collectively the "<strong>Parties</strong>".</p>

<h2>1. Purpose</h2>
<p>The Parties intend to explore a potential business relationship in the field of aerospace
components, supply chain, manufacturing services, and/or related technologies (the
"<strong>Purpose</strong>"). In connection with the Purpose, each Party may disclose to the other certain
confidential and proprietary information.</p>

<h2>2. Definition of Confidential Information</h2>
<p>"<strong>Confidential Information</strong>" means any non-public information disclosed by one Party
(the "<strong>Disclosing Party</strong>") to the other Party (the "<strong>Receiving Party</strong>"), whether orally,
in writing, electronically, or by any other means, that is designated as confidential or
that reasonably should be understood to be confidential given the nature of the information
and the circumstances of disclosure. Confidential Information includes, without limitation:
technical data, trade secrets, know-how, research, product plans, products, services,
customers, markets, software, developments, inventions, processes, formulas, technology,
designs, drawings, engineering, hardware configuration, financial information, and business
plans.</p>

<h2>3. Obligations of Receiving Party</h2>
<p>Each Receiving Party agrees to: (a) hold the Disclosing Party's Confidential Information
in strict confidence using at least the same degree of care it uses to protect its own
confidential information, but in no event less than reasonable care; (b) not to disclose
the Confidential Information to any third party without the prior written consent of the
Disclosing Party; (c) use the Confidential Information solely for the Purpose; and
(d) limit access to the Confidential Information to those employees, contractors, or agents
who need to know such information for the Purpose and who are bound by confidentiality
obligations no less restrictive than those herein.</p>

<h2>4. Exceptions</h2>
<p>The obligations under Section 3 shall not apply to Confidential Information that: (a) is
or becomes publicly known through no breach of this Agreement; (b) was rightfully known by
the Receiving Party before disclosure hereunder; (c) is rightfully received from a third
party without restriction; or (d) was independently developed by the Receiving Party without
use of or reference to the Confidential Information. Disclosure required by law or court order
shall be permitted provided the Receiving Party gives the Disclosing Party prompt written
notice and cooperates in seeking a protective order.</p>

<h2>5. No Licence</h2>
<p>Nothing in this Agreement grants either Party any right, title, interest, or licence in
or to the other Party's Confidential Information, intellectual property, or technology,
except as expressly provided herein for the Purpose.</p>

<h2>6. Term and Survival</h2>
<p>This Agreement shall commence on the Effective Date and continue for a period of
<strong>three (3) years</strong>, unless earlier terminated by either Party upon thirty (30) days' written
notice. The obligations of confidentiality shall survive termination for a further period
of two (2) years with respect to any Confidential Information disclosed during the term.</p>

<h2>7. Return of Information</h2>
<p>Upon the written request of the Disclosing Party or upon termination of this Agreement,
the Receiving Party shall promptly return or destroy all Confidential Information received
(including all copies, notes, and summaries) and certify the same in writing.</p>

<h2>8. Governing Law and Dispute Resolution</h2>
<p>This Agreement shall be governed by and construed in accordance with {governing_law}.
Any dispute arising out of or in connection with this Agreement shall be subject to the
exclusive jurisdiction of {jurisdiction}. The Parties agree to attempt to resolve any
dispute through good-faith negotiations for at least thirty (30) days before commencing
formal proceedings.</p>

<h2>9. Entire Agreement; Amendment</h2>
<p>This Agreement constitutes the entire agreement between the Parties with respect to the
subject matter hereof and supersedes all prior discussions and agreements. This Agreement
may not be amended or modified except by a written instrument signed by both Parties.</p>

<h2>10. Counterparts</h2>
<p>This Agreement may be executed in counterparts (including scanned/emailed copies), each
of which shall be deemed an original and together shall constitute one and the same instrument.</p>

<div class="signature-block">
  <p><strong>IN WITNESS WHEREOF</strong>, the Parties have executed this Agreement as of the Effective Date
  set forth above.</p>
  <br>
  <div class="sig-row">
    <p><strong>Jane Aerospace Private Limited</strong><br>("<strong>Party A</strong>")</p>
    <div class="sig-line"></div>
    <p class="sig-label">Authorised Signatory</p>
    <div class="sig-line"></div>
    <p class="sig-label">Name &amp; Designation</p>
    <div class="sig-line"></div>
    <p class="sig-label">Date</p>
  </div>
  <div class="sig-row">
    <p><strong>{company_name}</strong><br>("<strong>Party B</strong>")</p>
    <div class="sig-line"></div>
    <p class="sig-label">Authorised Signatory</p>
    <div class="sig-line"></div>
    <p class="sig-label">Name &amp; Designation</p>
    <div class="sig-line"></div>
    <p class="sig-label">Date</p>
  </div>
</div>

<br><br>
<p style="font-size:12px;color:#888;text-align:center;">
  Please print this document, sign, scan, and reply to this email with the signed copy attached.<br>
  For any questions, contact us at bharath.p@janeaerospace.co.in
</p>

</body>
</html>"""

    return html
