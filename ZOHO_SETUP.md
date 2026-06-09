# Zoho One Setup Guide — Jane Aerospace Onboarding

Complete guide to configure Zoho CRM, Analytics, and Contracts to track the
onboarding pipeline. After these steps, the team never needs to open the
external dashboard — everything is managed from within Zoho.

---

## STEP 1 — Add Custom Fields to Zoho CRM Contacts Module

**Path:** Setup → Customization → Modules and Fields → Contacts → Fields → New Field

Add these four fields:

| Field Label | API Name | Field Type | Values |
|---|---|---|---|
| Onboarding Stage | Onboarding_Stage | Picklist | KYC_Sent, KYC_Approved, KYC_Rejected, NDA_Sent, NDA_Signed, NDA_Approved, Agreement_Sent, Agreement_Signed, Complete |
| Onboarding ID | Onboarding_ID | Single Line Text | — |
| NDA Contract ID | NDA_Contract_ID | Single Line Text | — |
| Agreement Contract ID | Agreement_Contract_ID | Single Line Text | — |

---

## STEP 2 — Build the Onboarding Dashboard in Zoho CRM

**Path:** CRM Home → Dashboards → New Dashboard → Name: "Onboarding Pipeline"

### Widget A — Funnel Chart (Drop-off rate)
- Type: Funnel
- Module: Contacts
- Group by: Onboarding_Stage
- Funnel order: KYC_Sent → KYC_Approved → NDA_Sent → NDA_Signed → Agreement_Signed → Complete

### Widget B — KPI Cards
Create one card per metric:
- Count of Contacts where Onboarding_Stage = "KYC_Sent" → Label: "Pending KYC Review"
- Count of Contacts where Onboarding_Stage = "NDA_Signed" → Label: "NDA Awaiting Approval"
- Count of Contacts where Onboarding_Stage = "Agreement_Signed" → Label: "Agreement Awaiting Approval"
- Count of Contacts where Onboarding_Stage = "Complete" (this month) → Label: "Completed This Month"

### Widget C — Pipeline Summary (External Widget)
- Type: Home Page Dashboard
- Hosting: External
- URL: `https://barrier-thirteen-untidy.ngrok-free.dev/api/v1/onboarding/pipeline-summary`
- This shows live KYC/NDA/Agreement counts and leads stuck >48h

### Widget D — Bar Chart (Indian vs Overseas)
- Type: Bar/Donut Chart
- Module: Contacts
- Group by: a custom field "Company_Type" (or filter by Onboarding_Stage is not empty)

---

## STEP 3 — Install the Onboarding Widget on Lead/Contact Pages

**Path:** Setup → Developer Hub → Widgets

The widget `onboarding_widget.zip` is already uploaded. To add it to Lead/Contact
detail pages:

1. Open the widget → click "Associate"
2. Select module: **Leads** — save
3. Select module: **Contacts** — save
4. It appears as a Related List tab called "Onboarding Pipeline" on every record
5. From within Zoho, admin can see KYC/NDA/Agreement status and take action

---

## STEP 4 — Connect Zoho Analytics to PostgreSQL

**Path:** analytics.zoho.in → New Workspace → "Jane Aerospace Onboarding"

### Data Source Setup
- Source: Database → PostgreSQL
- Host: `ep-billowing-sunset-a810a1z5-pooler.eastus2.azure.neon.tech`
- Port: `5432`
- Database: `neondb`
- Username: `neondb_owner`
- SSL: Required
- Tables to import: `onboarding_records`, `leads_v2`, `kyc_submissions`
- Sync schedule: Every 1 hour

### Reports to Build

**Report 1 — Pipeline Funnel**
- Table: onboarding_records
- Chart: Funnel
- X-axis: kyc_status → nda_status → agreement_status

**Report 2 — All Leads with Days-in-Stage**
- Table: onboarding_records JOIN leads_v2 ON lead_id
- Columns: company_name, email, kyc_status, nda_status, agreement_status, created_at
- Add formula column: `DATEDIFF(NOW(), created_at)` → "Days in Pipeline"

**Report 3 — KPI: Completions This Month**
- Table: onboarding_records
- Filter: agreement_status = 'PROCEED_NEXT' AND created_at >= start of current month
- Aggregate: COUNT

**Report 4 — Overdue Alert**
- Table: onboarding_records
- Filter: created_at < (NOW - 72 hours) AND agreement_status != 'PROCEED_NEXT'
- Sort: created_at ASC

### Embed in Zoho CRM
1. Analytics Dashboard → Share → Get Embed URL
2. CRM → Setup → Developer Hub → Widgets → New Widget
3. Type: Home Page Dashboard | Hosting: External | URL: paste embed URL
4. CRM Home → Add Component → Widget → select it

---

## STEP 5 — Zoho Contracts Tracking View

**Path:** Zoho Contracts → Reports → New Report

- Group contracts by: Status (Draft / Sent / Signed / Completed)
- Add column: Related Contact (links back to CRM)
- Filter: created in last 90 days
- Save as: "Onboarding Contracts"

---

## HOW THE TEAM WORKS (No Dashboard Needed)

| What happens | What team receives | What to do |
|---|---|---|
| Lead books meeting | Email: "New Meeting Booked" + [Start Onboarding] button | Click button when meeting is done |
| Lead submits KYC | Email: "KYC Submitted" + [Approve KYC] [Reject KYC] buttons | Click one button |
| NDA draft ready | Email: "NDA Draft Ready" + [Approve & Send] button | Click to send to lead |
| Lead signs NDA | Email: "Signed NDA Received" + [Approve] [Reject] buttons | Click one button |
| Agreement draft ready | Email: "Agreement Draft Ready" + [Approve & Send] button | Click to send |
| Lead signs Agreement | Email: "Signed Agreement Received" + [Approve] [Reject] buttons | Click to complete |

All action links are HMAC-signed and expire in 7 days (30 days for Start Onboarding).
Each click shows a confirmation page — no login required.

---

## ZOHO TOOLS SUMMARY

| Tool | Purpose |
|---|---|
| Zoho CRM | Lead tracking, contact timeline notes, pipeline dashboard |
| Zoho Analytics | Deep funnel reports, time-in-stage, overdue alerts |
| Zoho Contracts | NDA + Agreement e-signature, contract tracking |
| Zoho WorkDrive | KYC document storage |
| Zoho Mail | Outbound emails + IMAP reply reading |
| Zoho Bookings | Meeting slot management |
