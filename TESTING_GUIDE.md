# Jane Aerospace Meeting Scheduler — Testing Guide

Step-by-step test procedures for all 50 scenarios and the A/B testing system.

> **Prerequisites:** Docker running (`docker-compose up`), `.env` configured, Redis and PostgreSQL healthy, Alembic migrations applied.

---

## Quick Smoke Test (Run First)

```bash
# 1. Health check
curl http://localhost:8000/health

# 2. Trigger a manual lead process cycle
curl -X POST http://localhost:8000/api/v1/v2/send-email

# 3. Check Celery worker logs
docker logs meeting-scheduler-celery-1 --tail 50

# 4. Verify lead was picked up
curl http://localhost:8000/api/v1/v2/leads | python -m json.tool
```

---

## Section A: Email Delivery Tests (#1–#16)

### #1 — Hard Bounce

**Setup:** Add a lead with a known-invalid email (e.g., `nonexistent123@fakdomain.xyz`). Trigger a send cycle. When your SMTP provider bounces it, the IMAP poller will receive the DSN.

**Simulate (dev):** Call `_process_reply_v2` directly with a fake DSN reply:
```python
reply = {
    "from_addr": "mailer-daemon@mail.fakdomain.xyz",
    "subject": "Delivery Status Notification: Failure",
    "body": "User nonexistent123 does not exist at this address."
}
```

**Verify:**
- `lead.status == "INVALID_EMAIL"` in DB
- No future send attempts for that email
- Pipeline sheet shows INVALID_EMAIL status

---

### #2 — Soft Bounce

**Simulate:** Send a DSN reply with soft bounce keywords:
```python
reply = {
    "from_addr": "mailer-daemon@provider.com",
    "subject": "Temporary Delivery Failure",
    "body": "Mailbox temporarily unavailable. Please try again later."
}
```

**Verify:**
- `lead.soft_bounce_count` = 1
- `lead.last_bounced_at` is set
- After 6 hours (or change `delay_hours` in test), `retry_soft_bounces` task re-queues the lead
- After 3 bounces, lead is no longer retried

---

### #3 — Email Open Nudge

**Step 1:** Send a lead their week-selection email. The email contains a `<img>` tracking pixel.

**Step 2:** Simulate the pixel being loaded 5 times:
```bash
# Get the pixel URL from the email HTML or build it manually
curl "http://localhost:8000/api/v1/v2/track/open/{lead_id}/{sig}"
# Repeat 5 times or update the DB directly:
UPDATE leads_v2 SET email_open_count=5, last_opened_at=NOW()-INTERVAL '3 hours' WHERE id='...';
```

**Step 3:** Wait for `check_open_nudges` to run (every 30 min), or trigger manually:
```bash
docker exec meeting-scheduler-celery-1 celery -A app.workers.celery_app call app.workers.v2_tasks.check_open_nudges
```

**Verify:**
- Nudge email received: "Saw you had a look — happy to answer any questions first"
- `lead.open_nudge_sent = True` in DB
- Second run does NOT send another nudge

---

### #4 — Forwarded Email Detection

**Simulate:** Send a reply that contains a forwarded message header:
```
---------- Forwarded message ----------
From: original@example.com
...
```

**Verify:**
- `lead.booked_via_forward = True` in DB
- Booking proceeds normally

---

### #5 — Shared Inbox Detection

**Setup:** Add a lead with email `info@somecompany.com` or `contact@business.org`.

**Verify after send cycle:**
- `lead.is_shared_inbox = True`
- Outreach email uses company name, not a personal name
- No "Hi ," in the email (null name handled gracefully)

---

### #6 — Non-English Reply

**Simulate:** Send a reply in Hindi:
```python
reply = {
    "from_addr": "lead@company.com",
    "body": "हाँ, मुझे इसमें रुचि है। क्या आप मुझे अधिक जानकारी दे सकते हैं?"
}
```

**Verify:**
- `lead.reply_language = "Hindi"` saved
- Response email is in Hindi (check email body)

---

### #7 — "Yes" / "OK" Reply

**Simulate:**
```python
reply = {"from_addr": "lead@company.com", "body": "Ok sure"}
```

**Verify:** Slot options email is sent within 1 cycle. No AI confusion.

---

### #8 — Long Question Reply

**Simulate:**
```python
reply = {
    "from_addr": "lead@company.com",
    "body": """I have several questions before we meet:
    1. What exactly is your service model?
    2. Do you handle last-mile delivery or only mid-mile?
    3. What industries have you worked with?
    4. Are you compliant with DGCA regulations for drone operations?
    5. What are your SLA commitments?"""
}
```

**Verify:** Response answers each question specifically. Reply is concise. Ends with a slot offer.

---

### #9 — CC'd Colleague

**Simulate:**
```python
reply = {
    "from_addr": "lead@company.com",
    "cc": "colleague@company.com, another@company.com",
    "body": "Happy to discuss. I've looped in my colleague."
}
```

**Verify:** `lead.cc_emails` = `"colleague@company.com,another@company.com"` in DB.

---

### #10 — Assistant Replying on Behalf of Lead

**Simulate:**
```python
reply = {
    "from_addr": "assistant@company.com",
    "body": "Hi, I'm writing on behalf of Mr. Sharma. He would like to schedule a call."
}
```

**Verify:** Reply acknowledges assistant, asks them to confirm a slot. Tone is formal.

---

### #11 — After-Hours Reply

**Step 1:** Temporarily patch `is_after_hours()` to return `True`, or run this test between 9pm–9am IST.

**Simulate a reply** at that time.

**Verify:**
- `lead.pending_reply_json` contains the queued reply body
- No AI reply sent immediately
- After patching `is_after_hours()` back to `False`, run `process_delayed_replies` task
- The reply is processed and a proper AI response is sent

---

### #12 — Angry / Negative Reply

**Simulate:**
```python
reply = {
    "from_addr": "lead@company.com",
    "body": "This is absolutely outrageous! Stop spamming me immediately. I am furious!"
}
```

**Verify:**
- No AI reply sent to the lead
- `lead.escalated_to_human = True`
- `lead.priority_flag = True`
- Escalation alert email received at the organizer address
- Alert email contains a snippet of the original reply

---

### #13 — Unsubscribe Request

**Simulate:**
```python
reply = {"from_addr": "lead@company.com", "body": "Please unsubscribe me from this list."}
```

**Verify:**
- One final acknowledgment email sent ("Removed you — sorry to see you go")
- `lead.opted_out = True`
- On the next send cycle, this lead is skipped completely

---

### #14 — "Call Me" Reply with Phone Number

**Simulate:**
```python
reply = {
    "from_addr": "lead@company.com",
    "body": "I'd prefer a call. Reach me at +91 98765 43210 after 3pm."
}
```

**Verify:**
- `lead.phone_number = "+91 98765 43210"` saved in DB
- Reply acknowledges the call request with the extracted number

---

### #15 — Emoji-Only Reply

**Simulate:**
```python
reply = {"from_addr": "lead@company.com", "body": "👍🎉✅"}
```

**Verify:**
- No AI reply
- `lead.escalated_to_human = True`
- Entry in logs: `emoji_only_reply_flagged_human`

---

### #16 — Job Change Auto-Reply

**Simulate (DSN path):**
```python
reply = {
    "from_addr": "mailer-daemon@company.com",
    "subject": "Out of Office: No longer with the company",
    "body": "John Smith is no longer with Acme Corp. Please contact Jane Doe at jane@newcompany.com"
}
```

**Simulate (live reply path):**
```python
reply = {
    "from_addr": "john@acme.com",
    "body": "Hi, I've actually moved on from Acme. My new contact is jane@newcompany.com"
}
```

**Verify:**
- Old lead `status = "JOB_CHANGED"`, `new_contact_from_job_change` populated
- New `LeadV2` record created with `status = "NEW"` for `jane@newcompany.com`
- New lead picked up on next send cycle

---

## Section B: Slot & Booking Tests (#17–#27)

### #17 — Slot Clicked but Form Not Completed

**Step 1:** Click a booking link: `GET /api/v1/v2/book/{lead_id}/0/{sig}`

**Verify (immediately):**
- Browser shows "Confirm Your Booking" page
- `lead.pending_booking_slot_json` populated in DB
- `lead.pending_booking_at` set

**Step 2:** Wait 20 minutes (or manually set `pending_booking_at` 21 min in the past):
```sql
UPDATE leads_v2 SET pending_booking_at = NOW() - INTERVAL '21 minutes' WHERE id = '...';
```

**Trigger nudge task:**
```bash
celery call app.workers.v2_tasks.check_pending_bookings
```

**Verify:** Nudge email received. `lead.pending_nudge_sent = True`.

**Step 3:** Set time 31 minutes in the past:
```sql
UPDATE leads_v2 SET pending_booking_at = NOW() - INTERVAL '31 minutes' WHERE id = '...';
```
Run task again. `pending_booking_slot_json` cleared — slot released.

---

### #18 — Zoho API Timeout (Returns None)

**Simulate:** Patch `ZohoBookingsService.create_booking` to return `(None, None)`.

**Verify:**
- Lead receives alternative slots email
- Lead status remains SENT (not BOOKED)
- Redis slot lock released

---

### #19 — Zoho API Completely Down

**Simulate:** Patch `ZohoBookingsService.create_booking` to raise `ConnectionError("Zoho unreachable")`.

**Verify:**
- Lead receives "booking system offline" email
- Organizer receives a Zoho-down alert
- Redis slot lock released
- Log entry: `zoho_api_down`

---

### #20 — Past Slot Selected

**Setup:** In `lead.offered_slots_json`, inject a slot from yesterday.

**Step 1:** Click the booking link for that past slot.

**Verify:** Browser shows "This slot has passed" page. No Zoho call made.

**Step 2:** Test the confirm-booking endpoint directly with a past slot:
```bash
curl "http://localhost:8000/api/v1/v2/confirm-booking/{lead_id}/0/{sig}"
```
Should show slot-taken page, not a confirmation.

---

### #21 — Dual Timezone Display

**Setup:** Set `lead.location = "Dubai"` (or any non-IST city).

**Trigger Stage 2 email.**

**Verify:** Slot cards in email show two times:
```
Tuesday, Jun 10 at 10:00 AM IST / 07:30 AM GST
```

Test with multiple cities: Chennai (same as IST), London (IST −4:30h in summer), New York (IST −9:30h).

---

### #22 — Staff on Leave

**Simulate:** Patch `create_booking` to return `(None, None)` (Zoho rejects — staff unavailable).

**Verify:**
- `_send_alternatives()` called with apology body text
- Lead receives new slot options
- Log: `zoho_booking_rejected_slot_taken`

---

### #23 — Lead Wants a Custom Time Slot

**Simulate:**
```python
reply = {"from_addr": "lead@company.com", "body": "Can we do next Friday at 4pm?"}
```

**Verify:**
- AI classifies as `list_slots` with `specific_date` = Friday's date
- Fresh Zoho slots fetched for that day
- If none, expands to adjacent days

---

### #24 — Two Leads from Same Company, Same Slot

**Setup:**
1. Create two leads: `alice@acme.com` (BOOKED, `selected_slot = "Tuesday, Jun 10 at 10:00 AM"`) and `bob@acme.com` (SENT).
2. Give Bob the same slot in `offered_slots_json`.

**Trigger:** Bob clicks that slot's booking link.

**Verify:**
- Bob receives email: "Your colleague Alice already has time with us on Tuesday, Jun 10"
- Bob offered different slots
- Alice's booking is untouched

---

### #25 — Repeat Lead (Previously Booked)

**Setup:** Set `lead.booked_at = NOW() - INTERVAL '90 days'` on an existing SENT lead.

**Trigger:** Run `process_new_leads`.

**Verify:**
- `lead.is_repeat_lead = True`
- Outreach email starts with "Great to reconnect, [Name]..."

---

### #26 — Zoho Cancellation Webhook

**Step 1:** Book a lead (status = BOOKED).

**Step 2:** Send the webhook:
```bash
curl -X POST http://localhost:8000/api/v1/v2/webhook/zoho \
  -H "Content-Type: application/json" \
  -d '{"event_type": "booking_cancelled", "booking_id": "ZB-1234", "customer_email": "lead@company.com"}'
```

**Verify:**
- `lead.status = SENT`
- Lead receives "Sorry to see you cancel" email with 3 new slot options

---

### #27 — No-Show Detection

**Setup:**
```sql
UPDATE leads_v2
SET status = 'BOOKED',
    selected_slot = 'Tuesday, Jun 03 at 10:00 AM',  -- a past time
    booking_id = 'ZB-TEST'
WHERE email = 'lead@company.com';
```

**Trigger:**
```bash
celery call app.workers.v2_tasks.check_no_shows
```

**Verify:**
- `lead.status = SENT`
- `lead.no_show_count = 1`
- Lead receives empathetic "Things come up" email with fresh slots

---

## Section C: Concurrency Tests (#28–#32)

### #28 — 100 Simultaneous Slot Clicks

**Script:** Run this in parallel:
```bash
for i in $(seq 1 100); do
  curl "http://localhost:8000/api/v1/v2/book/{lead_id}/0/{sig}" &
done
wait
```

**Verify:**
- Only ONE booking created in Zoho
- Redis key `slot:lock:{date}:{time}` exists with one email value
- All other requests received alternative slot emails
- No duplicate bookings in DB

---

### #29 — Redis Lock Expiry

**Simulate:**
```bash
# Set a lock manually
redis-cli SET "slot:lock:10-Jun-2026:1000" "test@test.com" EX 5
# Wait 6 seconds — key should be gone
redis-cli GET "slot:lock:10-Jun-2026:1000"
```

**Verify:** Returns `nil`. Another lead can now acquire it.

---

### #30 — Redis Down Fallback

**Simulate:** Stop Redis: `docker stop meeting-scheduler-redis-1`

**Trigger a booking.**

**Verify:**
- `acquire_slot_lock()` returns `True` (fail-open)
- Booking attempt proceeds to Zoho
- Zoho rejects duplicate if one already exists
- Log: `slot_lock_redis_error`

**Restore Redis:** `docker start meeting-scheduler-redis-1`

---

### #31 — Lock Acquired, Zoho Rejects

**Simulate:** 
1. Manually acquire a lock for a slot
2. Patch `create_booking` to return `(None, None)` for that slot

**Verify:**
- `release_slot_lock()` called
- Redis key deleted
- Lead offered alternative slots

---

## Section D: Follow-Up & Reminder Tests (#33–#38)

### #33 — OOO Detection

**Simulate:**
```python
reply = {
    "from_addr": "lead@company.com",
    "body": "I am out of office until June 20th. I will respond when I return."
}
```

**Verify:**
- `lead.ooo_until = 2026-06-20 00:00:00+00:00`
- No further emails sent until that date
- Run `resume_ooo_leads` after setting date past: lead gets fresh outreach

---

### #34 — Scheduled Follow-Up

**Simulate:**
```python
reply = {
    "from_addr": "lead@company.com",
    "body": "This looks interesting but can you contact me in July? I'm tied up until then."
}
```

**Verify:**
- `lead.scheduled_followup_at` set to July date
- Confirmation reply sent: "Noted — I'll reach out in July"
- Set date to now in DB, run `send_scheduled_followups` → fresh outreach sent

---

### #35 — Holiday Skip

**Setup:** Temporarily add today's date to the holiday calendar in `holiday_calendar.py`.

**Trigger:** Run `process_new_leads`.

**Verify:**
- Returns "Skipped — today is a holiday"
- No emails sent
- Log: `process_new_leads_skipped_holiday`

---

### #36 — Opted-Out Lead, New Email Same Domain

**Setup:**
```sql
UPDATE leads_v2 SET opted_out = true WHERE email = 'old@acme.com';
INSERT INTO leads_v2 (email, business_name, status) VALUES ('new@acme.com', 'Acme', 'SENT');
```

**Simulate:** New `@acme.com` email sends a reply.

**Verify:**
- Reply processing stops
- `new_lead.escalated_to_human = True`
- Log: `opted_out_domain_re_engagement`
- No automated response sent

---

### #37 — Reply After 2 Months

**Setup:**
```sql
UPDATE leads_v2 SET replied_at = NOW() - INTERVAL '75 days' WHERE email = 'lead@company.com';
```

**Simulate a new reply.**

**Verify:**
- `lead.is_repeat_lead = True` set in DB
- Log: `re_engagement_detected` with `months_silent ≈ 2.5`
- Any subsequent outreach uses warm re-engagement opener

---

### #38 — Senior Lead Detection

**Setup:** Set `lead.designation = "CEO"` or `"Founder"` on a NEW lead.

**Trigger:** Run `process_new_leads`.

**Verify:**
- `lead.priority_flag = True`
- `lead.escalated_to_human = True`
- Escalation alert email received by organizer
- Normal outreach email ALSO sent (AI draft goes out; team is alerted simultaneously)

---

## Section E: Infrastructure Tests (#39–#45)

### #39 — Celery Worker Crash Recovery

**Step 1:** Start a long-running task, kill the worker mid-execution.

**Step 2:** Restart the worker: `docker restart meeting-scheduler-celery-1`

**Verify:** Task is re-delivered and completed. No duplicate processing (idempotency keys prevent it).

---

### #40 — SMTP Daily Limit

**Simulate:** Set the Redis counter to 400:
```bash
redis-cli SET "smtp:sent:$(date +%Y-%m-%d)" 400
redis-cli EXPIRE "smtp:sent:$(date +%Y-%m-%d)" 90000
```

**Trigger:** Run `process_new_leads`.

**Verify:**
- Returns "SMTP daily limit reached — deferred to tomorrow"
- No emails sent
- Log: `process_new_leads_smtp_limit_reached`

---

### #41 — IMAP Duplicate Prevention

**Simulate:** Deliver the same email twice to the inbox (same `message_id`).

**Verify:**
- First processing creates a DB record and sends a reply
- Second processing: `IntegrityError` caught, no duplicate reply, log shows deduplication

---

### #42 — Concurrent IMAP Processing

**Simulate:** Run two IMAP poll tasks simultaneously for the same inbox.

**Verify:** Only one reply sent per email — `message_id` UNIQUE constraint prevents the second.

---

### #43 — Claude API Rate Limit

**Simulate:** Patch `_ask()` to raise `Exception("rate_limit 429")` for the first two calls.

**Verify:**
- Logs show `claude_rate_limit_retry` with `attempt=1` (wait 5s) and `attempt=2` (wait 15s)
- Third attempt succeeds
- Email is generated and sent normally

**Simulate total failure (3 failures):** Patch all 3 attempts to fail.
- `_ask()` returns `""`
- Caller falls back to hardcoded template string
- Email sent with fallback copy (not Claude-generated)

---

### #44 — Google Sheets Sync Failure

**Simulate:** Set `GOOGLE_SHEETS_SPREADSHEET_ID = "invalid_id"` in `.env` and restart.

**Trigger:** `export_pipeline_to_sheets`

**Verify:**
- Log: `pipeline_sheet_export_failed`
- `/app/pipeline_fallback.csv` created with all lead data
- Log: `pipeline_csv_fallback_written` with row count

---

### #45 — Thread Summarization

**Setup:** Set `lead.pending_reply_json` to a JSON array of 5+ past messages.

**Trigger:** Process a new reply for that lead.

**Verify:**
- `should_summarize(5)` returns `True`
- `summarize_thread()` called — check logs for Claude Haiku invocation
- `build_context_for_ai()` passes enriched context to intent classifier
- Intent classification uses the summarized context

---

## Section F: Business Logic Tests (#46–#50)

### #46 — NDA Request

**Simulate:**
```python
reply = {
    "from_addr": "lead@company.com",
    "body": "Before we proceed, we'd need an NDA in place. Can you send one over?"
}
```

**Verify:**
- NDA reply email received
- `lead.escalated_to_human = True`

---

### #47 — Case Study Request

**Simulate:**
```python
reply = {
    "from_addr": "lead@company.com",
    "body": "Do you have any case studies or references from similar companies?"
}
```

**Verify:** Pre-approved case study email sent. Slot follow-up included at the end.

---

### #48 — Existing Vendor Reply

**Simulate:**
```python
reply = {
    "from_addr": "lead@company.com",
    "body": "We already work with TCI for our logistics needs. I'm not sure there's a fit."
}
```

**Verify:** "Complement not compete" reply sent. Still ends with meeting offer.

---

### #49 — Boss Approval Needed

**Simulate:**
```python
reply = {
    "from_addr": "lead@company.com",
    "body": "Sounds interesting but I'll need to run this by my COO, Mr. Patel, first."
}
```

**Verify:** Reply offers one-pager for Mr. Patel, proposes CC-ing him in.

---

### #50 — Deadline Mention

**Simulate:**
```python
reply = {
    "from_addr": "lead@company.com",
    "body": "We are planning a major expansion in Q3 and need a supply chain partner by then."
}
```

**Verify:**
- `lead.priority_flag = True`
- `lead.priority_deadline = "Q3 expansion"` (or similar extracted text)
- Reply personalises: "With your Q3 expansion in mind..."

---

## A/B Testing Verification

### Verify Variant Assignment

After a send cycle, check DB:
```sql
SELECT email, ab_variant, ab_subject_variant, send_time_variant FROM leads_v2
WHERE sent_at > NOW() - INTERVAL '1 hour';
```

Expected: Roughly even distribution of A/B/C, S1/S2/S3, morning/afternoon/evening.

### Verify Redis Counters

```bash
redis-cli GET ab:body:A:sent
redis-cli GET ab:body:B:sent
redis-cli GET ab:body:C:sent
redis-cli GET ab:body:A:replied
```

### Verify Weight Adjustment

After 5+ replies per variant, call:
```bash
curl http://localhost:8000/api/v1/v2/ab-stats
```

Expected response:
```json
{
  "variants": {
    "A": {"sent": 15, "replied": 4, "reply_rate_pct": 26.7, "current_weight": 27},
    "B": {"sent": 12, "replied": 2, "reply_rate_pct": 16.7, "current_weight": 17},
    "C": {"sent": 14, "replied": 7, "reply_rate_pct": 50.0, "current_weight": 50}
  }
}
```

### Verify Segment Tracking

```bash
redis-cli GET "ab:body:A:seg:ceo:sent"
redis-cli GET "ab:body:C:seg:supply_chain_south:replied"
```

### Verify Send-Time Stats

```bash
redis-cli GET ab:time:morning:sent
redis-cli GET ab:time:afternoon:replied
```

---

## Full Integration Test — Recommended Test Sequence

Run in this order to test the full funnel end-to-end:

```
1.  Add a test lead via CSV or Sheet sync
2.  Run process_new_leads → verify Stage 1 email arrives
3.  Click "This Week" button → verify Stage 2 slot email arrives
4.  Click a slot link → verify Confirm page appears
5.  Wait 20 min → verify nudge email arrives
6.  Click Confirm → verify Zoho booking created, confirmation email arrives
7.  Send a test reply from lead's email → verify AI response
8.  Send "unsubscribe" → verify opted-out, one final email, no more sends
9.  Run check_no_shows with past booking time → verify re-engagement email
10. Verify pipeline sheet updated with all statuses
11. Check ab-stats endpoint for variant distribution
```

---

## Useful SQL Queries

```sql
-- All leads by status
SELECT status, COUNT(*) FROM leads_v2 GROUP BY status;

-- Leads with priority flags
SELECT email, contact_name, designation, priority_deadline FROM leads_v2
WHERE priority_flag = true ORDER BY created_at DESC;

-- Leads escalated to human
SELECT email, contact_name, escalated_to_human, priority_flag FROM leads_v2
WHERE escalated_to_human = true;

-- Soft bounce candidates
SELECT email, soft_bounce_count, last_bounced_at FROM leads_v2
WHERE soft_bounce_count > 0 AND soft_bounce_count < 3;

-- Pending bookings (unconfirmed clicks)
SELECT email, pending_booking_at, pending_nudge_sent FROM leads_v2
WHERE pending_booking_slot_json IS NOT NULL;

-- After-hours queued replies
SELECT email, pending_reply_json FROM leads_v2
WHERE pending_reply_json IS NOT NULL AND pending_reply_json != '[]';

-- OOO leads
SELECT email, ooo_until FROM leads_v2 WHERE ooo_until IS NOT NULL;

-- Scheduled follow-ups
SELECT email, scheduled_followup_at FROM leads_v2
WHERE scheduled_followup_at IS NOT NULL ORDER BY scheduled_followup_at;

-- A/B variant distribution
SELECT ab_variant, ab_subject_variant, send_time_variant, COUNT(*)
FROM leads_v2 WHERE sent_at IS NOT NULL
GROUP BY ab_variant, ab_subject_variant, send_time_variant;
```

---

## Useful Redis Commands

```bash
# SMTP throttle counter
redis-cli GET "smtp:sent:$(date +%Y-%m-%d)"

# All active slot locks
redis-cli KEYS "slot:lock:*"

# A/B body variant counters
redis-cli MGET ab:body:A:sent ab:body:B:sent ab:body:C:sent
redis-cli MGET ab:body:A:replied ab:body:B:replied ab:body:C:replied

# Send-time counters
redis-cli MGET ab:time:morning:sent ab:time:afternoon:sent ab:time:evening:sent

# Open count for a specific lead
redis-cli GET "open:{lead_id}"

# Flush test data (CAUTION: dev only)
redis-cli FLUSHDB
```

---

## Celery Manual Task Triggers

```bash
# Short alias for docker exec
CELERY="docker exec meeting-scheduler-celery-1 celery -A app.workers.celery_app call"

$CELERY app.workers.v2_tasks.process_new_leads
$CELERY app.workers.v2_tasks.check_inbox_replies_v2
$CELERY app.workers.v2_tasks.send_v2_reminders
$CELERY app.workers.v2_tasks.check_open_nudges
$CELERY app.workers.v2_tasks.check_pending_bookings
$CELERY app.workers.v2_tasks.send_scheduled_followups
$CELERY app.workers.v2_tasks.check_no_shows
$CELERY app.workers.v2_tasks.retry_soft_bounces
$CELERY app.workers.v2_tasks.process_delayed_replies
$CELERY app.workers.v2_tasks.resume_ooo_leads
$CELERY app.workers.v2_tasks.export_pipeline_to_sheets
```
