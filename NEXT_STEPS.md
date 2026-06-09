# Jane Aerospace — Complete Setup Guide
# Zoho Contracts · Zoho Dashboard · KARZA · End-to-End Test

All remaining steps to go live. Follow in order.

---

## STEP 1 — Zoho Contracts: Create 3 Templates

> In Zoho Contracts, merge fields in the document body use `##field_api_name##` format.
> When the pipeline creates a contract via API, every `##field##` is replaced with real data automatically.

---

### 1A — Template: Jane NDA India Auto

**Path:** Zoho Contracts → Admin → Contract Types → New Contract Type

#### Basic Details

| Field | Value |
|---|---|
| Name | `Jane NDA India Auto` |
| Category | `NDA` |
| Intent | `Protect confidential information` |
| Party A | My Organization (Jane Aerospace) |
| Party B | Counterparty |
| Approval Workflow | Select any existing / create "Auto Approve" |
| Time Zone | (UTC+05:30) India Standard Time |

Click **Next → Create From Scratch**

#### Document Fields to Register (Insert → Document Fields → + Add New Field)

| Display Label | API Name | Type |
|---|---|---|
| Company Name | `company_name` | Text |
| Contact Name | `contact_name` | Text |
| Contact Number | `contact_number` | Text |
| Effective Date | `effective_date` | Text |
| Signatory Email | `signatory_email` | Text |

#### Full Document Text — paste into editor

```
NON-DISCLOSURE AGREEMENT

This Non-Disclosure Agreement ("Agreement") is entered into
on ##effective_date##.

PARTIES

Party A (Disclosing Party):
Jane Aerospace Pvt. Ltd.
Registered Office: Hyderabad, Telangana, India
CIN: U35301TG2022PTC168234

Party B (Recipient):
Company Name  : ##company_name##
Represented by: ##contact_name##
Designation   : Authorised Signatory
Contact Number: ##contact_number##
Email Address : ##signatory_email##

1. CONFIDENTIAL INFORMATION
"Confidential Information" means any technical, commercial,
financial, or business information disclosed by Jane Aerospace
to ##company_name## in connection with aerospace procurement,
manufacturing, or supply-chain discussions.

2. OBLIGATIONS OF ##company_name##
(a) Keep all Confidential Information strictly confidential.
(b) Not disclose it to any third party without prior written
    consent from Jane Aerospace.
(c) Use it solely for evaluating a business relationship
    with Jane Aerospace.
(d) Apply the same degree of care used to protect its own
    confidential information.

3. EXCLUSIONS
This Agreement does not apply to information that:
(a) Is publicly known through no breach by ##company_name##.
(b) Was known to ##company_name## prior to disclosure.
(c) Is required to be disclosed by law or court order.

4. TERM
This Agreement remains in effect for two (2) years from
##effective_date##, unless terminated earlier in writing.

5. RETURN OF INFORMATION
On request, ##company_name## shall return or destroy all
Confidential Information and confirm this in writing.

6. NO LICENCE
Nothing herein grants ##company_name## any licence or right
in Jane Aerospace's intellectual property.

7. GOVERNING LAW
Governed by the laws of India. Disputes subject to exclusive
jurisdiction of courts in Hyderabad, Telangana.

IN WITNESS WHEREOF the parties execute this Agreement as of
##effective_date##.

For Jane Aerospace Pvt. Ltd.       For ##company_name##

________________________           ________________________
Authorised Signatory               ##contact_name##
Jane Aerospace Pvt. Ltd.           ##signatory_email##
Date: ##effective_date##           Date: ##effective_date##
```

#### How it looks with real data (example)

| Placeholder | Real Value |
|---|---|
| `##company_name##` | TATA AEROSPACE AND DEFENCE LIMITED |
| `##contact_name##` | Rajesh Kumar Sharma |
| `##contact_number##` | +91 9876543210 |
| `##effective_date##` | 08 June 2026 |
| `##signatory_email##` | rajesh.sharma@tataad.com |

Click **Save** → copy the Contract Type ID from the URL bar → paste in `.env`:
```
ZOHO_CONTRACTS_NDA_TEMPLATE_INDIAN=9931000000XXXXXX
```

---

### 1B — Template: Jane NDA Overseas Auto

**Path:** Same as above → New Contract Type

#### Basic Details

| Field | Value |
|---|---|
| Name | `Jane NDA Overseas Auto` |
| Category | `NDA` |
| Intent | `Protect confidential information` |
| Party A | My Organization |
| Party B | Counterparty |
| Approval Workflow | Auto Approve |
| Time Zone | (UTC+05:30) India Standard Time |

#### Document Fields to Register

| Display Label | API Name | Type |
|---|---|---|
| Company Name | `company_name` | Text |
| Contact Name | `contact_name` | Text |
| Contact Number | `contact_number` | Text |
| Effective Date | `effective_date` | Text |
| Signatory Email | `signatory_email` | Text |
| Country of Incorporation | `country_of_incorporation` | Text |
| Company Reg Number | `company_reg_number` | Text |
| Tax ID / TIN | `tax_id_tin` | Text |
| Country | `country` | Text |
| LEI Number | `lei_number` | Text |

#### Full Document Text — paste into editor

```
NON-DISCLOSURE AGREEMENT — INTERNATIONAL

This Non-Disclosure Agreement ("Agreement") is made and
entered into on ##effective_date##.

PARTIES

Party A (Disclosing Party):
Jane Aerospace Pvt. Ltd.
Registered Office: Hyderabad, Telangana, India

Party B (Recipient):
Company Name            : ##company_name##
Country of Incorporation: ##country_of_incorporation##
Company Registration No.: ##company_reg_number##
Country / Address       : ##country##
Tax ID / TIN            : ##tax_id_tin##
LEI Number              : ##lei_number##
Represented by          : ##contact_name##
Contact Number          : ##contact_number##
Email Address           : ##signatory_email##

1. CONFIDENTIAL INFORMATION
"Confidential Information" means any technical, commercial,
financial, or business information disclosed by Jane Aerospace
to ##company_name## in connection with aerospace procurement,
manufacturing, or supply-chain discussions.

2. OBLIGATIONS OF ##company_name##
(a) Keep all Confidential Information strictly confidential.
(b) Not disclose it to any third party without prior written
    consent from Jane Aerospace.
(c) Use it solely for evaluating a business relationship
    with Jane Aerospace.
(d) Comply with applicable data protection laws in
    ##country_of_incorporation## and India.

3. EXCLUSIONS
This Agreement does not apply to information that:
(a) Is publicly known through no breach by ##company_name##.
(b) Was already known to ##company_name## before disclosure.
(c) Is required by law or regulatory authority.

4. TERM
Two (2) years from ##effective_date##, unless terminated
earlier in writing by either party.

5. RETURN OF INFORMATION
On request, ##company_name## shall return or destroy all
Confidential Information and certify this in writing.

6. GOVERNING LAW & DISPUTE RESOLUTION
Governed by the laws of India. Disputes resolved by
arbitration under the Arbitration and Conciliation Act 1996,
seat in Hyderabad. Language: English.

7. SANCTIONS COMPLIANCE
##company_name## confirms it is not subject to sanctions
imposed by the UN, OFAC, EU, or any applicable authority.

8. ENTIRE AGREEMENT
This Agreement constitutes the entire confidentiality
agreement between the parties for the stated purpose.

IN WITNESS WHEREOF the parties execute this Agreement as of
##effective_date##.

For Jane Aerospace Pvt. Ltd.       For ##company_name##
                                   (##country_of_incorporation##)

________________________           ________________________
Authorised Signatory               ##contact_name##
Jane Aerospace Pvt. Ltd.           ##signatory_email##
Date: ##effective_date##           Date: ##effective_date##
```

#### How it looks with real data (example)

| Placeholder | Real Value |
|---|---|
| `##company_name##` | BOEING DEFENSE UK LIMITED |
| `##country_of_incorporation##` | UNITED KINGDOM |
| `##company_reg_number##` | 03483038 |
| `##country##` | UNITED KINGDOM |
| `##tax_id_tin##` | GB 928 3456 78 |
| `##lei_number##` | 5493001KJTIIGC8Y1R12 |
| `##contact_name##` | James Wilson |
| `##contact_number##` | +44 7700 900123 |
| `##effective_date##` | 08 June 2026 |
| `##signatory_email##` | james.wilson@boeing.com |

Click **Save** → copy Contract Type ID → paste in `.env`:
```
ZOHO_CONTRACTS_NDA_TEMPLATE_OVERSEAS=9931000000XXXXXX
```

---

### 1C — Template: Jane Customer Agreement Auto

**Path:** Same → New Contract Type

#### Basic Details

| Field | Value |
|---|---|
| Name | `Jane Customer Agreement Auto` |
| Category | `Customer Agreement` (create new if needed) |
| Intent | `Govern a commercial relationship` |
| Party A | My Organization |
| Party B | Counterparty |
| Approval Workflow | Auto Approve |
| Time Zone | (UTC+05:30) India Standard Time |

#### Document Fields to Register

| Display Label | API Name | Type |
|---|---|---|
| Company Name | `company_name` | Text |
| Contact Name | `contact_name` | Text |
| Contact Number | `contact_number` | Text |
| Effective Date | `effective_date` | Text |
| Signatory Email | `signatory_email` | Text |
| Escalation Contact Name | `escalation_contact_name` | Text |
| Escalation Contact Email | `escalation_contact_email` | Text |
| Country of Incorporation | `country_of_incorporation` | Text |
| Company Reg Number | `company_reg_number` | Text |
| Tax ID / TIN | `tax_id_tin` | Text |

#### Full Document Text — paste into editor

```
CUSTOMER SUPPLY AGREEMENT

This Customer Supply Agreement ("Agreement") is entered into
on ##effective_date##.

PARTIES

Supplier:
Jane Aerospace Pvt. Ltd.
Registered Office: Hyderabad, Telangana, India
GSTIN: 36AABCJ1234A1Z5

Customer:
Company Name            : ##company_name##
Company Registration No.: ##company_reg_number##
Country of Incorporation: ##country_of_incorporation##
Tax ID / TIN            : ##tax_id_tin##
Represented by          : ##contact_name##
Contact Number          : ##contact_number##
Email Address           : ##signatory_email##
Escalation Contact      : ##escalation_contact_name##
Escalation Email        : ##escalation_contact_email##

1. SCOPE OF AGREEMENT
Jane Aerospace agrees to supply aerospace components,
assemblies, and related services to ##company_name## in
accordance with purchase orders raised under this Agreement.

2. PRICING & PAYMENT
(a) Prices shall be as per the agreed price schedule
    confirmed in each purchase order.
(b) Payment terms: net 30 days from invoice date unless
    otherwise agreed in writing.
(c) Late payments attract interest at 1.5% per month.

3. DELIVERY
Delivery timelines shall be as per individual purchase
orders. Risk of loss passes to ##company_name## upon
delivery at the agreed delivery point.

4. QUALITY & COMPLIANCE
All products shall conform to applicable aerospace quality
standards (AS9100 / NADCAP as applicable) and be accompanied
by certificates of conformance.

5. INTELLECTUAL PROPERTY
All designs, drawings, and technical data provided by
Jane Aerospace remain the exclusive property of
Jane Aerospace Pvt. Ltd.

6. CONFIDENTIALITY
Each party shall maintain confidentiality of the other
party's proprietary information in accordance with the NDA
executed on or before ##effective_date##.

7. TERM & TERMINATION
Effective from ##effective_date## for one (1) year,
renewable annually. Either party may terminate with
60 days' written notice to the other.

8. ESCALATION
For any disputes or urgent matters, the designated
escalation contact for ##company_name## is:
##escalation_contact_name## (##escalation_contact_email##).

9. LIMITATION OF LIABILITY
Jane Aerospace's liability shall not exceed the value of
the specific purchase order giving rise to the claim.

10. FORCE MAJEURE
Neither party shall be liable for delays caused by
circumstances beyond reasonable control including acts of
God, war, pandemic, or government action.

11. GOVERNING LAW
Governed by the laws of India. Disputes are subject to
exclusive jurisdiction of courts in Hyderabad, Telangana.

IN WITNESS WHEREOF the parties execute this Agreement as of
##effective_date##.

For Jane Aerospace Pvt. Ltd.       For ##company_name##

________________________           ________________________
Authorised Signatory               ##contact_name##
Jane Aerospace Pvt. Ltd.           ##signatory_email##
Date: ##effective_date##           Date: ##effective_date##

Escalation: ##escalation_contact_name##
Email     : ##escalation_contact_email##
```

#### How it looks with real data (example)

| Placeholder | Real Value |
|---|---|
| `##company_name##` | HAL AEROSPACE SYSTEMS PVT LTD |
| `##company_reg_number##` | U35302KA1964GOI001622 |
| `##country_of_incorporation##` | INDIA |
| `##tax_id_tin##` | 29AAAAH0182H1Z7 |
| `##contact_name##` | Suresh Patel |
| `##contact_number##` | +91 8765432109 |
| `##effective_date##` | 08 June 2026 |
| `##signatory_email##` | suresh.patel@hal-india.co.in |
| `##escalation_contact_name##` | Priya Nair |
| `##escalation_contact_email##` | priya.nair@hal-india.co.in |

Click **Save** → copy Contract Type ID → paste in `.env`:
```
ZOHO_CONTRACTS_AGREEMENT_TEMPLATE_INDIAN=9931000000XXXXXX
ZOHO_CONTRACTS_AGREEMENT_TEMPLATE_OVERSEAS=9931000000XXXXXX
```
*(use same ID for both if you only create one Agreement template)*

---

## STEP 2 — Update .env With All 3 Template IDs

After creating all 3 templates, open `.env` and replace the IDs:

```env
# Zoho Contracts Template IDs — get from URL after saving each template
ZOHO_CONTRACTS_NDA_TEMPLATE_INDIAN=9931000000______
ZOHO_CONTRACTS_NDA_TEMPLATE_OVERSEAS=9931000000______
ZOHO_CONTRACTS_AGREEMENT_TEMPLATE_INDIAN=9931000000______
ZOHO_CONTRACTS_AGREEMENT_TEMPLATE_OVERSEAS=9931000000______
```

How to find the ID: after saving a template, go to its settings page.
The URL will be:
```
contracts.zoho.in/janeaerospace/settings/contract-type/jane-nda-india-auto/basic-info
```
The numeric ID is NOT in the URL slug. To get it, open **browser DevTools → Network tab**,
click on the template, and look for the API response — it contains `contractTypeId`.

OR use the Zoho Contracts API to list all contract types:
```
GET https://contracts.zoho.in/api/v1/contracttypes
Headers:
  Authorization: Zoho-oauthtoken {your_access_token}
  X-com-zoho-contracts-orgid: 9931000000128013
```

---

## STEP 3 — KARZA Credentials Setup

KARZA is the best provider for Indian company KYC (GSTIN + PAN + CIN).

### 3A — Get KARZA Trial API Key

1. Go to **karza.in**
2. Click **"Get Started"** or **"Try for Free"**
3. Fill the signup form — use your business email `poshak@janeaerospace.co.in`
4. They will email you a trial API key within 1–2 hours
5. Trial sandbox URL: `https://testapi.karza.in`
6. Trial gives ~100 free API calls per month

### 3B — Add to .env

```env
KYC_PROVIDER=karza
KARZA_API_KEY=your_karza_trial_key_here
KARZA_BASE_URL=https://testapi.karza.in
```

When ready for production:
```env
KARZA_BASE_URL=https://api.karza.in
```

### 3C — What KARZA Verifies Automatically

| Field from KYC Form | KARZA Endpoint | What It Returns |
|---|---|---|
| GSTIN Number | `POST /v2/gstin` | Company name, GST status, PAN |
| PAN Number | `POST /v2/pan` | PAN holder name, PAN type |
| CIN Number | `POST /v2/cin` | Company name, company status |
| IFSC Code | Razorpay free API | Bank name, branch, city |

### 3D — What Overseas Companies Get (Free, No Key Needed)

| Field | API | Cost |
|---|---|---|
| LEI Number | GLEIF `api.gleif.org` | Free, no key |
| Company Reg / Tax ID | Manual document review | — |

---

## STEP 4 — Zoho Analytics Dashboard Setup

### 4A — Create Workspace

```
analytics.zoho.in → New Workspace → Name: "Jane Aerospace Onboarding"
```

### 4B — Connect PostgreSQL (Neon DB)

```
Add Data Source → Database → PostgreSQL

Host     : ep-billowing-sunset-a810a1z5-pooler.eastus2.azure.neon.tech
Port     : 5432
Database : neondb
Username : neondb_owner
Password : npg_aOZsk6jJiS5X
SSL Mode : Required (check the SSL checkbox)

Tables to import:
  ✓ onboarding_records
  ✓ kyc_submissions
  ✓ leads_v2

Sync Schedule: Every 1 hour
```

### 4C — Build Report 1 — Pipeline Funnel

```
New Report → Chart View → Funnel Chart
Table     : onboarding_records
X-axis    : kyc_status
Stages    : KYC_FORM_SENT → UNDER_REVIEW → APPROVED → (nda_status) SENT → SIGNED
Title     : "Onboarding Pipeline Funnel"
```

### 4D — Build Report 2 — All Leads With Days in Stage

```
New Report → Table/Pivot View
Tables    : onboarding_records JOIN leads_v2 ON lead_id = id
Columns   :
  leads_v2.business_name   AS "Company"
  leads_v2.email           AS "Email"
  onboarding_records.kyc_status
  onboarding_records.nda_status
  onboarding_records.agreement_status
  onboarding_records.company_type
  onboarding_records.created_at

Add Formula Column:
  Name    : Days in Pipeline
  Formula : DATEDIFF(NOW(), created_at)

Sort by: created_at ASC
```

### 4E — Build Report 3 — Completions This Month

```
New Report → KPI / Summary View
Table     : onboarding_records
Filter    : pipeline_status = 'COMPLETE'
            AND created_at >= start of current month
Aggregate : COUNT(id)
Label     : "Completed This Month"
```

### 4F — Build Report 4 — Overdue Alert (>72 hours)

```
New Report → Table View
Table     : onboarding_records
Filter    : created_at < (NOW - 72 hours)
            AND pipeline_status != 'COMPLETE'
Columns   : company_name, email, kyc_status, nda_status, created_at
Sort      : created_at ASC
Color     : red highlight for rows where created_at < (NOW - 7 days)
```

### 4G — Embed Dashboard in Zoho CRM

```
1. Analytics → your dashboard → Share → Get Embed URL → copy URL

2. Zoho CRM → Setup → Developer Hub → Widgets → New Widget
   Type    : Home Page Dashboard
   Hosting : External
   URL     : paste the Analytics embed URL

3. CRM Home → Add Component → Widget → select your widget
```

---

## STEP 5 — Zoho CRM Custom Fields

**Path:** Setup → Customization → Modules and Fields → **Leads** → Fields → New Field

Add these 4 fields:

| Label | API Name | Type | Options |
|---|---|---|---|
| Onboarding Stage | `Onboarding_Stage` | Picklist | KYC_Sent, KYC_Approved, KYC_Rejected, NDA_Sent, NDA_Signed, NDA_Approved, Agreement_Sent, Agreement_Signed, Complete |
| Onboarding ID | `Onboarding_ID` | Single Line Text | — |
| NDA Contract ID | `NDA_Contract_ID` | Single Line Text | — |
| Agreement Contract ID | `Agreement_Contract_ID` | Single Line Text | — |

Repeat the same 4 fields on **Contacts** module.

---

## STEP 6 — Restart Services

After updating `.env`, restart the API and Celery worker:

```bash
# If running with Docker
docker-compose down
docker-compose up -d

# If running manually
uvicorn app.main:app --reload &
celery -A app.workers.celery_app worker --loglevel=info -Q onboarding &
celery -A app.workers.celery_app beat --loglevel=info &
```

---

## STEP 7 — End-to-End Test

### Test Indian Company (with KARZA sandbox)

```
1. Open Zoho CRM → find any Lead
2. Click "Start Onboarding" button
3. Check your email — you get: "KYC form sent" notification
4. Open the KYC form link from the notification
5. Fill it as:
     Company Type    : Indian Company
     Company Name    : TEST AEROSPACE PVT LTD
     GSTIN           : 29AABCT1332L1ZV  (KARZA sandbox test GSTIN)
     PAN             : AABCT1332L
     Contact Name    : Test User
     Contact Number  : +91 9999999999
     IFSC            : HDFC0001234
     Bank Name       : HDFC Bank
     Account Number  : 00000000000001
6. Upload any PDF as GST Certificate
7. Upload any PDF as Incorporation Certificate
8. Submit
9. Check team email — "KYC Submitted" notification arrives
10. Click "Approve KYC" in the email
11. After ~30s, check Zoho Contracts — NDA contract should appear
12. The lead receives NDA email with Zoho Sign link
```

### Test Overseas Company (LEI verification)

```
1. Same flow but fill:
     Company Type            : Overseas Company
     Company Name            : BOEING DEFENSE UK LIMITED
     Country of Incorporation: UNITED KINGDOM
     Company Reg Number      : 03483038
     Tax ID / TIN            : GB 928 3456 78
     LEI Number              : 5493001KJTIIGC8Y1R12
     (LEI is verified free via GLEIF API — real Boeing UK LEI)
     Contact Name            : Test User
     SWIFT Code              : BOFAUS3NXXX
     IBAN                    : GB29NWBK60161331926819
```

---

## FULL PIPELINE FLOW (Reference)

```
Zoho CRM Lead
     ↓  [click Start Onboarding]
KYC Form Sent (email to lead with secure link)
     ↓  [lead fills form]
KYC Under Review (team gets email)
     ↓  [team clicks Approve KYC]
KYC Approved
     ↓  [auto: create & send NDA via Zoho Contracts]
NDA Sent to Lead (Zoho Sign email)
     ↓  [lead signs via Zoho Sign]
NDA Signed (team gets email via polling every 2h)
     ↓  [team clicks Approve NDA]
NDA Approved
     ↓  [auto: create & send Agreement via Zoho Contracts]
Agreement Sent to Lead (Zoho Sign email)
     ↓  [lead signs]
Agreement Signed (team gets email)
     ↓  [team clicks Approve Agreement]
COMPLETE ✓
```

---

## QUICK REFERENCE — All .env Keys Needed

```env
# Zoho OAuth (shared by CRM + Contracts)
ZOHO_CLIENT_ID=your_zoho_client_id
ZOHO_CLIENT_SECRET=your_zoho_client_secret
ZOHO_REFRESH_TOKEN=your_crm_refresh_token

# Zoho Contracts
ZOHO_CONTRACTS_REFRESH_TOKEN=1000.3eade514f299d83b3d4bb260e1bda402.2c09f5966e380baf1f0245a49e96aff7
ZOHO_CONTRACTS_ORG_ID=9931000000128013
ZOHO_CONTRACTS_NDA_TEMPLATE_INDIAN=9931000000______     ← fill after Step 1A
ZOHO_CONTRACTS_NDA_TEMPLATE_OVERSEAS=9931000000______   ← fill after Step 1B
ZOHO_CONTRACTS_AGREEMENT_TEMPLATE_INDIAN=9931000000____ ← fill after Step 1C
ZOHO_CONTRACTS_AGREEMENT_TEMPLATE_OVERSEAS=9931000000__ ← fill after Step 1C

# KARZA KYC
KYC_PROVIDER=karza
KARZA_API_KEY=your_karza_key_here              ← fill after Step 3A
KARZA_BASE_URL=https://testapi.karza.in        ← sandbox; change to api.karza.in for prod

# Database (Neon)
DATABASE_URL=postgresql+asyncpg://neondb_owner:npg_aOZsk6jJiS5X@ep-billowing-sunset-a810a1z5-pooler.eastus2.azure.neon.tech/neondb?ssl=require

# App
APP_URL=https://barrier-thirteen-untidy.ngrok-free.dev
ONBOARDING_HMAC_SECRET=your_hmac_secret
```

---

## STATUS CHECKLIST

- [ ] **1A** — Created `Jane NDA India Auto` template + added 5 document fields
- [ ] **1B** — Created `Jane NDA Overseas Auto` template + added 10 document fields
- [ ] **1C** — Created `Jane Customer Agreement Auto` template + added 10 document fields
- [ ] **2**  — Updated all 4 template IDs in `.env`
- [ ] **3**  — Signed up for KARZA, got API key, added to `.env`
- [ ] **4**  — Created Zoho Analytics workspace + connected Neon DB + built 4 reports
- [ ] **5**  — Added 4 custom fields to CRM Leads + Contacts modules
- [ ] **6**  — Restarted API + Celery worker + Celery beat
- [ ] **7**  — Tested full pipeline with 1 Indian lead + 1 overseas lead
