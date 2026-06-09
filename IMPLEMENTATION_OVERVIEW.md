# Jane Aerospace Meeting Scheduler — Implementation Overview

Full documentation of all 50 edge-case scenarios and the complete A/B testing system.

---

## Architecture Summary

| Layer | Technology | Role |
|-------|-----------|------|
| API | FastAPI | HTTP endpoints, webhooks, booking pages |
| Workers | Celery + Redis | Async task queue, beat scheduler |
| Database | PostgreSQL + SQLAlchemy | Lead state, bookings, onboarding |
| Email (outbound) | Gmail SMTP | Cold outreach, slot cards, confirmations |
| Email (inbound) | IMAP polling | Reply classification, bounce detection |
| AI | Claude Haiku / Sonnet | Intent classification, outreach generation |
| Booking | Zoho Bookings API | Calendar management, meeting creation |
| Tracking | Redis counters | A/B stats, email opens, SMTP throttle |
| Sheet sync | Google Sheets API + gspread | Live pipeline view + CSV fallback |

---

## Part 1 — All 50 Scenarios

### Section A: Email Delivery Scenarios (#1–#16)

#### #1 — Hard Bounce (Email Does Not Exist)
**Trigger:** IMAP receives a DSN / "Mail Delivery Failure" message.
**Detection:** `classify_bounce(subject, body)` in `app/services/bounce_handler.py` — matches "user unknown", "does not exist", "no such user".
**Action:** Lead `status` set to `INVALID_EMAIL`. All future sends are gated — the lead is never emailed again. Flagged in pipeline sheet for manual correction.
**Files:** `bounce_handler.py`, `v2_tasks.py → _process_reply_v2`, `db/models.py → LeadStatus.INVALID_EMAIL`

---

#### #2 — Soft Bounce (Mailbox Full / Server Busy)
**Trigger:** DSN with "mailbox full", "temporarily unavailable", "try again later".
**Detection:** `classify_bounce()` returns `type="soft_bounce"`.
**Action:** `lead.soft_bounce_count` incremented. Retry delays: 6h after 1st, 24h after 2nd, 72h after 3rd. After 3 soft bounces the `retry_soft_bounces` task stops retrying.
**Beat task:** `v2-retry-soft-bounces` — every 6 hours.
**Files:** `bounce_handler.py → soft_bounce_retry_delay()`, `v2_tasks.py → retry_soft_bounces`

---

#### #3 — Lead Opens Email 5+ Times, Never Clicks
**Trigger:** Tracking pixel loaded repeatedly; `email_open_count > 0`, opened > 2 hours ago, no booking.
**Detection:** `check_open_nudges` Celery task.
**Action:** Soft nudge sent: "Saw you had a look — happy to answer any questions first." `open_nudge_sent = True` prevents duplicates.
**Beat task:** `v2-check-open-nudges` — every 30 minutes.
**Pixel endpoint:** `GET /api/v1/v2/track/open/{lead_id}/{sig}` — HMAC-signed, returns 1×1 transparent GIF.
**Files:** `email_tracker.py`, `v2_tasks.py → check_open_nudges`, `v2_endpoints.py → track_open`

---

#### #4 — Lead Forwards Email, Someone Else Clicks
**Detection:** Body scanned for "Forwarded message", "FWD:", "FW:" in `_process_reply_v2`.
**Action:** `lead.booked_via_forward = True` flagged for team awareness. Booking proceeds normally.
**Files:** `db/models.py → booked_via_forward`, `v2_tasks.py`

---

#### #5 — Lead Uses Shared Inbox (info@, contact@, hello@)
**Detection:** `is_shared_inbox(email)` checks common generic prefixes.
**Action:** `lead.is_shared_inbox = True`. `lead.contact_name` cleared — outreach uses company name only ("Hi Acme Corp,").
**Files:** `bounce_handler.py → is_shared_inbox()`, `v2_tasks.py → _process_new_leads`

---

#### #6 — Lead Replies in Hindi / Tamil / Another Language
**Detection:** `analyze_reply_intent()` returns `language` field.
**Action:** `lead.reply_language` saved. `generate_multilingual_reply()` called — Claude replies in the detected language natively.
**Files:** `email_generator.py`, `marketing_agent.py → generate_multilingual_reply()`, `v2_tasks.py`

---

#### #7 — Lead Replies with "Yes" / "OK" (Soft Interest)
**Detection:** AI classifies as `list_slots` or unclear-positive (falls to default slot-sending path).
**Action:** Slot options sent immediately with a warm one-liner intro.
**Files:** `v2_tasks.py → _process_reply_v2` (default fallback path)

---

#### #8 — Lead Replies with a Long Email (Multiple Questions)
**Detection:** AI classifies as `question`. Full body passed to `generate_question_reply()`.
**Action:** Marketing Expert Agent answers each question specifically, keeps reply concise, ends with slot offer.
**Files:** `marketing_agent.py → generate_question_reply()`, `v2_tasks.py`

---

#### #9 — Lead CC's Their Colleague
**Detection:** `reply.get("cc")` header parsed.
**Action:** CC addresses appended to `lead.cc_emails`. Acknowledged in reply context.
**Files:** `db/models.py → cc_emails`, `v2_tasks.py`

---

#### #10 — Assistant Replies on Behalf of Lead
**Detection:** AI detects "on behalf of", "Dr. X asked me to reply". Intent = `assistant_reply`.
**Action:** `generate_assistant_reply()` adjusts tone, directs reply to the assistant, asks them to confirm a slot with their principal.
**Files:** `marketing_agent.py → generate_assistant_reply()`, `v2_tasks.py`

---

#### #11 — Lead Replies at 11pm / Weekend
**Detection:** `is_after_hours()` — outside 9am–9pm IST or Sunday.
**Action:** Reply stored in `lead.pending_reply_json`. No AI reply sent at odd hours. `process_delayed_replies` drains the queue at 9am next business day.
**Beat task:** `v2-process-delayed-replies` — every 10 minutes (skips if still after hours).
**Files:** `email_service.py → is_after_hours()`, `db/models.py → pending_reply_json`, `v2_tasks.py → process_delayed_replies`

---

#### #12 — Lead Replies with Angry / Negative Tone
**Detection:** AI classifies as `angry_negative`.
**Action:** AI does NOT reply. `lead.escalated_to_human = True`, `lead.priority_flag = True`. `send_escalation_alert()` fires immediately with the reply snippet.
**Files:** `email_service.py → send_escalation_alert()`, `v2_tasks.py`

---

#### #13 — Lead Asks to Unsubscribe
**Detection:** "unsubscribe", "remove me", "do not contact" inside the `decline` handler.
**Action:** `lead.opted_out = True`. One final acknowledgment sent. Never emailed again.
**Files:** `v2_tasks.py → decline handler`

---

#### #14 — Lead Says "Call Me, Here's My Number"
**Detection:** AI extracts phone from `analysis["phone_number"]`. Fallback regex: `[\+\d][\d\s\-\(\)]{7,14}\d` scans body.
**Action:** Phone saved to `lead.phone_number`. Reply acknowledges the call request.
**Files:** `db/models.py → phone_number`, `v2_tasks.py → callback_request handler`

---

#### #15 — Lead's Reply Is Just Emojis
**Detection:** Body stripped of all emoji Unicode ranges — if nothing remains, classified as emoji-only.
**Action:** `lead.escalated_to_human = True`. No AI reply.
**Files:** `v2_tasks.py → emoji-only detection block`

---

#### #16 — Lead Changes Job
**Detection:** `classify_bounce()` returns `type="job_change"` (DSN path). Also caught via AI intent `job_change` in live replies.
**Action:** Old lead marked `LeadStatus.JOB_CHANGED`. New contact extracted → new `LeadV2` record created with `status=NEW`.
**Files:** `bounce_handler.py`, `db/models.py → LeadStatus.JOB_CHANGED`, `v2_tasks.py`

---

### Section B: Slot & Booking Scenarios (#17–#27)

#### #17 — Lead Clicks Slot but Doesn't Complete the Form
**Flow:**
1. `/book/{lead_id}/{slot_idx}/{sig}` → slot stored in `pending_booking_slot_json`, shown **Confirm Your Booking** page.
2. No confirm within 20 minutes → `check_pending_bookings` sends nudge email.
3. 30 minutes total → `pending_booking_slot_json` cleared, slot released.
4. Lead clicks **Confirm** → `/confirm-booking/{lead_id}/{slot_idx}/{sig}` → Zoho booking created.

**Beat task:** `v2-check-pending-bookings` — every 5 minutes.
**Files:** `v2_endpoints.py → one_click_book, confirm_booking`, `v2_tasks.py → check_pending_bookings`, `marketing_agent.py → generate_pending_booking_nudge()`

---

#### #18 — Zoho API Times Out During Booking
**Detection:** `create_booking()` returns `booking_id = None`.
**Action:** Slot lock released. `_send_alternatives()` sends fresh slots. Lead stays in SENT status.
**Files:** `v2_tasks.py → _do_booking`

---

#### #19 — Zoho API Is Completely Down
**Detection:** `create_booking()` raises an exception (connection refused, DNS failure).
**Action:** Slot lock released. `send_zoho_down_alert()` emails the lead: "Our booking system is briefly offline — reply with your preferred time." Organizer alerted.
**Files:** `email_service.py → send_zoho_down_alert()`, `v2_tasks.py → _do_booking`

---

#### #20 — Lead Selects a Slot That Is in the Past
**Detection:** `slot_dt_ist < datetime.now(_IST)` checked before calling Zoho in both `_do_booking` and `/confirm-booking`.
**Action:** Slot rejected. Lead shown "This slot has passed" page with fresh alternatives.
**Files:** `v2_tasks.py → _do_booking`, `v2_endpoints.py → confirm_booking`

---

#### #21 — Lead Is in a Different Timezone
**Detection:** `lead.location` mapped to UTC offset via `_LOCATION_TZ` / `_TZ_OFFSET_MINS` (30+ Indian cities, 10+ international).
**Action:** Every slot card shows dual time: "10:00 AM IST / 4:30 AM GMT".
**Files:** `email_service.py → _slot_cards_dual_tz()`, `send_v2_slots_email(lead_location=...)`

---

#### #22 — Staff Goes on Leave After Slots Were Emailed
**Detection:** Zoho rejects the booking because staff is unavailable.
**Action:** `_send_alternatives(apology_text=...)` fetches fresh Zoho slots (unavailable staff excluded automatically), sends apology + new options.
**Files:** `v2_tasks.py → _do_booking → _send_alternatives()`

---

#### #23 — Lead Wants a Slot Outside Offered Windows
**Detection:** AI classifies as `list_slots` with `specific_date` or `after_date`.
**Action:** Fresh Zoho slots fetched for that day → adjacent days → 14-day window. New Stage 2 email sent.
**Files:** `v2_tasks.py → list_slots handler`, `availability_v2.py`

---

#### #24 — Two Leads from Same Company Get the Same Slot
**Detection:** Before Redis lock in `_do_booking`, domain of `lead.email` matched against BOOKED leads with same `selected_slot` and `@domain`.
**Action:** `generate_same_company_slot_reply()` crafts: "Your colleague [Name] already has time with us on [date]." Different slots offered.
**Files:** `v2_tasks.py → _do_booking → same-company check`, `marketing_agent.py → generate_same_company_slot_reply()`

---

#### #25 — Lead Has Previously Met Jane Aerospace
**Detection:** `lead.booked_at is not None` in `_process_new_leads` → `is_repeat_lead = True`.
**Action:** `generate_outreach(is_repeat_lead=True)` prepends: "Great to reconnect, [Name] — it's been a while since we last spoke."
**Files:** `db/models.py → is_repeat_lead`, `marketing_agent.py → generate_outreach()`, `v2_tasks.py`

---

#### #26 — Lead Cancels Directly from Zoho
**Trigger:** `POST /api/v1/v2/webhook/zoho` with `event_type = "booking_cancelled"`.
**Action:** Lead reset to SENT. `generate_zoho_cancelled_reply()` sends: "Sorry to see you cancel — want to find a better time?" with 3 new slots.
**Files:** `v2_endpoints.py → webhook_zoho`, `marketing_agent.py → generate_zoho_cancelled_reply()`

---

#### #27 — Lead Books, No-Shows, Asks to Rebook
**Detection:** `check_no_shows` — queries BOOKED leads whose slot datetime + 30 min is in the past.
**Action:** Lead reset to SENT. `generate_no_show_reply()` sends empathetic email + fresh slots. `no_show_count` incremented.
**Beat task:** `v2-check-no-shows` — every 30 minutes.
**Files:** `v2_tasks.py → check_no_shows`, `marketing_agent.py → generate_no_show_reply()`

---

### Section C: Concurrency Scenarios (#28–#32)

#### #28 — 100 Leads Click the Same Slot Simultaneously
**Solution:** Redis `SET NX EX` — atomic SETNX. First caller wins in microseconds. All others get `_send_alternatives()` with next 3 available slots instantly.
**Files:** `slot_lock.py → acquire_slot_lock()`

---

#### #29 — Lead A Has Redis Lock, Goes Idle for 11 Minutes
**Solution:** Lock TTL = 600 seconds. Auto-expires. Lead B can acquire it. Lead A's pending booking is cleared by `check_pending_bookings` at 30-minute expiry.
**Files:** `slot_lock.py → _LOCK_TTL = 600`, `v2_tasks.py → check_pending_bookings`

---

#### #30 — Redis Goes Down During Slot Reservation
**Solution:** `acquire_slot_lock()` fails open (returns `True` on Redis exception). Zoho becomes the final guard — it rejects duplicate bookings natively. Losers get `_send_alternatives()`.
**Files:** `slot_lock.py → acquire_slot_lock()`

---

#### #31 — Slot Lock Acquired but Zoho Rejects Booking
**Solution:** `release_slot_lock()` called immediately after Zoho returns `None`. Slot freed for others. User told "Booking didn't confirm — pick another slot."
**Files:** `v2_tasks.py → _do_booking`, `slot_lock.py → release_slot_lock()`

---

#### #32 — Two Redis Instances Disagree (Split Brain)
**Status:** Documented known risk. Redlock (multi-node consensus) is the formal solution but overkill for single-Redis deployment. Zoho acts as final arbitration.

---

### Section D: Follow-Up & Reminder Scenarios (#33–#38)

#### #33 — Lead Goes OOO for 3 Weeks
**Detection:** AI classifies as `out_of_office`, extracts `return_date`.
**Action:** `lead.ooo_until` set. `resume_ooo_leads` fires a fresh outreach on return date + 1 day. No emails during absence.
**Beat task:** `v2-resume-ooo-leads` — daily at 07:30 UTC.
**Files:** `v2_tasks.py → out_of_office handler + resume_ooo_leads`

---

#### #34 — Lead Says "Contact Me Next Month"
**Detection:** AI classifies as `scheduled_followup`, extracts `followup_date`.
**Action:** `lead.scheduled_followup_at` set. Reply confirms: "Noted — I'll reach out on [date]." `send_scheduled_followups` fires the outreach on that date.
**Beat task:** `v2-send-scheduled-followups` — every hour.
**Files:** `v2_tasks.py → scheduled_followup handler + send_scheduled_followups`, `marketing_agent.py → generate_scheduled_followup_confirm()`

---

#### #35 — Follow-Up Lands on a Public Holiday
**Solution:** `should_send_today()` checked at the top of `_process_new_leads`. If today is a holiday, entire send cycle skipped.

Indian fixed holidays: Republic Day (Jan 26), Independence Day (Aug 15), Gandhi Jayanti (Oct 2), Christmas (Dec 25), New Year (Jan 1).
Variable 2025–2026: Diwali, Holi, Eid al-Fitr, Eid al-Adha, Good Friday, Janmashtami, Navratri, etc.

**Files:** `holiday_calendar.py → should_send_today(), is_holiday(), next_business_day()`

---

#### #36 — Opted-Out Lead Returns with a New Email Address
**Detection:** Domain of incoming email matched against opted-out leads with same `@domain`. Skips generic domains (Gmail, Yahoo, Hotmail, Outlook).
**Action:** `lead.escalated_to_human = True`. No automated reply — human decides whether to re-engage.
**Files:** `v2_tasks.py → opted-out domain check`

---

#### #37 — Lead Replies to Old Thread After 2 Months
**Detection:** `lead.replied_at` gap ≥ 60 days → `is_repeat_lead = True`.
**Action:** Reply processed normally. Re-engagement flag triggers warm opener on any subsequent outreach.
**Files:** `v2_tasks.py → re-engagement detection block`

---

#### #38 — Very Senior Lead Replies (CEO / Founder / MD)
**Detection:** `is_senior_lead(designation)` matches CEO, Founder, Managing Director, Chairman, Owner.
**Action:** `lead.priority_flag = True`, `lead.escalated_to_human = True`. `send_escalation_alert()` fires immediately. AI outreach still generated but team is alerted.
**Files:** `bounce_handler.py → is_senior_lead()`, `email_service.py → send_escalation_alert()`, `v2_tasks.py → _process_new_leads`

---

### Section E: System & Infrastructure Scenarios (#39–#45)

#### #39 — Celery Worker Crashes Mid-Send
**Solution:** `task_acks_late = True` in `celery_app.py`. Tasks acknowledged only after successful completion. Crashed worker causes broker to redeliver to next worker.
**Files:** `celery_app.py`

---

#### #40 — SMTP Daily Limit Hit
**Solution:** `smtp_can_send()` checks Redis counter `smtp:sent:{YYYY-MM-DD}` (IST date). Hard cap: 400/day. Exceeded → `_process_new_leads` returns early. Counter TTL: 25 hours.
**Files:** `email_tracker.py → smtp_can_send(), smtp_record_send()`, `v2_tasks.py → _process_new_leads`

---

#### #41 — IMAP Polling Misses a Reply
**Solution:** IMAP polled every 20 seconds. Messages deduplicated by `message_id`.
**Files:** `celery_app.py`, `email_tasks.py → poll_imap`

---

#### #42 — Same Email Processed Twice
**Solution:** `message_id` UNIQUE constraint on `EmailMessage` table. Second insert raises `IntegrityError`, silently caught.
**Files:** `db/models.py → EmailMessage.message_id UNIQUE`

---

#### #43 — Claude API Rate Limited
**Solution:** `_ask()` in `email_generator.py` retries with exponential backoff: 5s → 15s → 45s. Detects HTTP 429, "rate_limit", "overloaded", "too_many_requests". After 3 failures returns `""` — callers use hardcoded fallback templates.
**Files:** `email_generator.py → _ask()`

---

#### #44 — Google Sheets Sync Fails
**Solution:** On `gspread` exception, all rows written to `/app/pipeline_fallback.csv` locally. On next successful sync cycle the CSV is overwritten with current data.
**Files:** `v2_tasks.py → _export_pipeline_to_sheets`

---

#### #45 — Long Email Thread (20 Exchanges) — AI Loses Context
**Solution:** `should_summarize(reply_count)` returns `True` every 5 messages. `summarize_thread()` compresses thread with Claude Haiku (max 200 words). `build_context_for_ai()` passes summary + last 3 messages to classifier.
**Files:** `thread_summarizer.py`, `v2_tasks.py → _process_reply_v2`

---

### Section F: Business Logic Scenarios (#46–#50)

#### #46 — Lead Asks for NDA Before Meeting
**Detection:** AI classifies as `nda_request`.
**Action:** `generate_nda_reply()` explains NDA process. `lead.escalated_to_human = True` — team sends actual NDA document manually.
**Files:** `marketing_agent.py → generate_nda_reply()`, `v2_tasks.py`

---

#### #47 — Lead Asks for Case Studies / References
**Detection:** AI classifies as `case_study_request`.
**Action:** `generate_case_study_reply()` sends pre-approved case study email (category-level examples). Slot follow-up included at the end.
**Files:** `marketing_agent.py → generate_case_study_reply()`, `v2_tasks.py`

---

#### #48 — Lead Says "We Already Have a Vendor"
**Detection:** AI classifies as `existing_vendor`.
**Action:** `generate_existing_vendor_reply()` sends: "We often work alongside existing vendors — we complement rather than replace." Still pushes for a meeting.
**Files:** `marketing_agent.py → generate_existing_vendor_reply()`, `v2_tasks.py`

---

#### #49 — Lead Says "Needs Boss Approval First"
**Detection:** AI classifies as `not_decision_maker`, extracts `referred_to` name.
**Action:** `generate_not_decision_maker_reply()` offers a one-pager for the boss and proposes looping them in via CC.
**Files:** `marketing_agent.py → generate_not_decision_maker_reply()`, `v2_tasks.py`

---

#### #50 — Lead Mentions a Specific Deadline ("Expansion in Q3")
**Detection:** AI classifies as `deadline_mention`, extracts `deadline_text`.
**Action:** `lead.priority_flag = True`, `lead.priority_deadline = deadline_text`. `generate_deadline_reply()` personalises: "With your Q3 expansion in mind, timing is probably right now."
**Files:** `marketing_agent.py → generate_deadline_reply()`, `db/models.py → priority_deadline`, `v2_tasks.py`

---

## Part 2 — A/B Testing System

### Three Independent Test Layers

| Layer | Options | Stored In |
|-------|---------|-----------|
| Email body variant | A (Challenge), B (Industry Insight), C (Social Proof) | `lead.ab_variant` |
| Subject line variant | S1 (Personal), S2 (Company name), S3 (Industry) | `lead.ab_subject_variant` |
| Send-time variant | morning (8:30 IST), afternoon (1:30 IST), evening (5:00 IST) | `lead.send_time_variant` |

### Body Variant Psychology

**Variant A — Challenge Angle:** Names the lead's operational pain. Builds instant trust.
Best for COO, VP Operations, Supply Chain Head.

**Variant B — Industry Insight Angle:** Frames current practices as outdated. Creates FOMO.
Best for CEO, MD, Director — strategic thinkers.

**Variant C — Social Proof Angle:** References a similar company + specific outcome. Removes "are you proven?" objection.
Best for risk-averse leads, growth-phase companies.

### Subject Line Variants

| Variant | Subject | Psychology |
|---------|---------|-----------|
| S1 | "Quick question, [First Name]" | Curiosity + personal |
| S2 | "Supply chain for [Company Name]" | Hyper-relevant |
| S3 | "[Industry] supply chain — worth 15 min?" | Low pressure |

### Weight Auto-Adjustment (Steps 1–5)

**Step 1 — Assignment on send:**
```python
body_variant    = random.choices(["A","B","C"], weights=get_variant_weights(segment))
subject_variant = random.choice(["S1","S2","S3"])
send_time       = random.choice(["morning","afternoon","evening"])
```

**Step 2 — Redis event recording:**
```
ab:body:{A|B|C}:sent / :replied
ab:body:{A|B|C}:seg:{segment}:sent / :replied
ab:time:{morning|afternoon|evening}:sent / :replied
```

**Step 3 — Weight calculation (after 5+ sends per variant):**
```python
reply_rate = replied / sent
weight     = max(int(reply_rate * 100), 5)   # floor at 5
```

**Step 4 — Segment classification (`classify_segment`):**

| Designation keywords | Segment |
|---------------------|---------|
| CEO, Founder, MD, Chairman, Owner | `ceo` |
| COO, VP Operations, Head of Ops | `coo` |
| Supply Chain, Procurement, Logistics | `supply_chain` |
| Director, Head, Manager | `manager` |
| Other | `other` |

South India locations append `_south` (e.g., `ceo_south`).

**Step 5 — Fallback:** If segment has fewer than 5 sends, `get_variant_weights()` falls back to global weights.

**Live stats:** `GET /api/v1/v2/ab-stats`

---

## Celery Beat Schedule

| Task | Schedule | Purpose |
|------|----------|---------|
| `v2-process-leads` | every 20s | Stage 1 emails to new leads |
| `v2-check-inbox` | every 20s | Poll IMAP, classify replies |
| `v2-send-reminders` | every 5 min | Stage 1 follow-up reminders |
| `v2-sync-sheets` | every 30s | Google Sheet → leads_v2 |
| `v2-sync-csv-leads` | every 4h | leads.csv → leads_v2 |
| `pipeline-sheet-export` | every 60s | Export pipeline to Sheets (+ CSV fallback) |
| `v2-resume-ooo-leads` | daily 07:30 UTC | Resume OOO leads |
| `v2-process-delayed-replies` | every 10 min | Drain after-hours queue (#11) |
| `v2-check-open-nudges` | every 30 min | Nudge high-open/no-click leads (#3) |
| `v2-check-pending-bookings` | every 5 min | Nudge unconfirmed slot clicks (#17) |
| `v2-send-scheduled-followups` | every hour | Requested follow-up dates (#34) |
| `v2-check-no-shows` | every 30 min | Detect and re-engage no-shows (#27) |
| `v2-retry-soft-bounces` | every 6h | Retry soft-bounced leads (#2) |

---

## Key API Endpoints

| Method | Path | Purpose |
|--------|------|---------|
| GET | `/api/v1/v2/week/{lead_id}/{week}/{sig}` | Week selection (Stage 1 → Stage 2) |
| GET | `/api/v1/v2/book/{lead_id}/{slot_idx}/{sig}` | Slot click → pending confirm page (#17) |
| GET | `/api/v1/v2/confirm-booking/{lead_id}/{slot_idx}/{sig}` | Final Zoho booking (#17, #20) |
| GET | `/api/v1/v2/track/open/{lead_id}/{sig}` | Email open tracking pixel (#3) |
| POST | `/api/v1/v2/webhook/zoho` | Zoho cancellation / no-show (#26, #27) |
| GET | `/api/v1/v2/leads` | List all leads |
| GET | `/api/v1/v2/ab-stats` | A/B testing live stats |
| GET | `/api/v1/v2/dashboard/stats` | Conversion funnel |
| POST | `/api/v1/v2/leads/import-crm` | Zoho CRM → leads_v2 |

---

## New LeadV2 Fields

| Field | Type | Scenario |
|-------|------|---------|
| `bounce_count` | int | #1 |
| `soft_bounce_count` | int | #2 |
| `last_bounced_at` | datetime | #1, #2 |
| `email_open_count` | int | #3 |
| `last_opened_at` | datetime | #3 |
| `open_nudge_sent` | bool | #3 |
| `send_time_variant` | str | A/B |
| `priority_flag` | bool | #38, #50 |
| `priority_deadline` | str | #50 |
| `escalated_to_human` | bool | #12, #38 |
| `is_shared_inbox` | bool | #5 |
| `reply_language` | str | #6 |
| `cc_emails` | text | #9 |
| `booked_via_forward` | bool | #4 |
| `new_contact_from_job_change` | str | #16 |
| `scheduled_followup_at` | datetime | #34 |
| `no_show_count` | int | #27 |
| `pending_booking_slot_json` | text | #17 |
| `pending_booking_at` | datetime | #17 |
| `pending_nudge_sent` | bool | #17 |
| `pending_reply_json` | text | #11 |
| `phone_number` | str | #14 |
| `is_repeat_lead` | bool | #25, #37 |

---

## New Service Files

| File | Handles |
|------|---------|
| `app/services/bounce_handler.py` | #1, #2, #5, #16, #38 |
| `app/services/holiday_calendar.py` | #35 |
| `app/services/email_tracker.py` | #3, #40 |
| `app/services/thread_summarizer.py` | #45 |

---

## All 17 AI Intent Classes

| Intent | Triggered By | Action |
|--------|-------------|--------|
| `book` | Specific date/time mentioned | Direct Zoho booking |
| `list_slots` | "this week", "Monday", "after 2pm" | Fetch and send slots |
| `out_of_office` | OOO auto-reply | Queue for return date |
| `question` | General inquiry | Specific answer |
| `callback_request` | "call me", phone number | Save phone, acknowledge |
| `pricing` | Price/cost questions | Pricing narrative |
| `not_decision_maker` | "talk to my boss" | Loop in decision maker |
| `decline` | "not interested" | Soft reply or opt-out |
| `reschedule` | "cancel", "different time" | 30-min undo or manual |
| `angry_negative` | Angry tone | Escalate, no AI reply |
| `assistant_reply` | "on behalf of" | Adjust tone for gatekeeper |
| `job_change` | "no longer here" | New lead created |
| `nda_request` | "NDA", "non-disclosure" | NDA reply + human flag |
| `case_study_request` | "case study", "references" | Case study template |
| `existing_vendor` | "already have a vendor" | Complement-not-compete |
| `deadline_mention` | "Q3 expansion", "by December" | Priority flag + urgent reply |
| `scheduled_followup` | "next month", "in 3 weeks" | Save date, confirm |
