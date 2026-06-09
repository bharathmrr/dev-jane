# Jane Aerospace — Complete Testing Playbook

This guide tells you exactly what to do, what to type, where to go, and what to expect for every scenario. No coding knowledge needed for most tests. Commands are given where you need them.

---

## Before You Start

**Checklist — confirm these are true:**

- [ ] Docker is running: open Docker Desktop and all 6 containers are green
- [ ] You have access to the **organizer email inbox** (the account in `.env` → `ORGANIZER_EMAIL`) — this is where you send outreach FROM
- [ ] You have access to a **test lead inbox** — a real email address you own that will receive the outreach emails
- [ ] You can open the API in a browser: `http://localhost:8000/docs`
- [ ] You have a DB viewer (TablePlus, DBeaver, pgAdmin, or just run SQL commands below)

**Your two key inboxes during testing:**
| Inbox | What it is | Used for |
|---|---|---|
| **Organizer inbox** | The email in `.env` → `SMTP_USERNAME` | Receives escalation alerts, sends outreach |
| **Test lead inbox** | Any email you own (Gmail etc.) | Receives outreach, you reply from here to simulate lead replies |

---

## Part 1 — The Golden Path (Test This First)

This is the normal happy path. Run this before any edge case tests to confirm the system works end to end.

### Step 1: Add a Test Lead

Go to your database and run:
```sql
INSERT INTO leads_v2 (business_name, contact_name, email, designation, location, status, created_at, updated_at)
VALUES ('Test Corp', 'Rahul Mehta', 'YOUR_TEST_EMAIL@gmail.com', 'Operations Head', 'Hyderabad', 'NEW', NOW(), NOW());
```

Replace `YOUR_TEST_EMAIL@gmail.com` with an email inbox you own.

---

### Step 2: Trigger the First Email

Run this in your terminal:
```bash
docker exec meeting-scheduler-worker-1 celery -A app.workers.celery_app call app.workers.v2_tasks.process_new_leads
```

**Wait 30–60 seconds.**

**Check your test lead inbox** — you should receive an outreach email from Jane Aerospace. It will introduce the company and ask if this week works for a call.

The email will have a **"This Week Works"** button or similar CTA at the bottom.

---

### Step 3: Reply "Yes" from the Lead

Open the outreach email in your test lead inbox. **Reply to it** with:

> Sure, let's connect.

Wait 30–60 seconds (the IMAP poller runs every minute).

**You should receive a second email** from Jane Aerospace with 3–5 time slot options (e.g. "Tuesday Jun 10 at 10:00 AM IST", "Wednesday Jun 11 at 2:00 PM IST", etc.).

---

### Step 4: Click a Slot

In the slot options email, click any one of the time slot links.

**Your browser should open** a "Confirm Your Booking" page that shows the date/time and a Confirm button.

---

### Step 5: Confirm the Booking

Click the **Confirm** button on the page.

**You should receive a confirmation email** at your test lead inbox with the Zoho meeting link.

---

### Step 6: Check the Database

```sql
SELECT email, status, selected_slot, booking_id, zoho_meeting_link FROM leads_v2
WHERE email = 'YOUR_TEST_EMAIL@gmail.com';
```

Should show:
- `status = 'BOOKED'`
- `selected_slot` = the time you clicked
- `booking_id` = a Zoho booking ID (e.g. `ZB-12345`)
- `zoho_meeting_link` = a valid meeting URL

**Golden path is working. Proceed to edge case tests.**

---

## Part 2 — Email & Delivery Tests

---

### Scenario #1 — Hard Bounce (Invalid Email Address)

**What this tests:** The system permanently stops emailing an address that doesn't exist.

**Step 1:** Add a lead with a fake email:
```sql
INSERT INTO leads_v2 (business_name, contact_name, email, status, created_at, updated_at)
VALUES ('Fake Corp', 'Test Person', 'nonexistent99999@fakdomain-xyz.com', 'NEW', NOW(), NOW());
```

**Step 2:** Trigger a send:
```bash
docker exec meeting-scheduler-worker-1 celery -A app.workers.celery_app call app.workers.v2_tasks.process_new_leads
```

**Step 3:** Your SMTP provider will bounce the email. The IMAP poller picks up the bounce notification (a "Mailer Daemon" failure email). This happens automatically — wait 2–5 minutes.

**Verify:**
```sql
SELECT email, status, email_bounced FROM leads_v2 WHERE email = 'nonexistent99999@fakdomain-xyz.com';
```
- `status` should be `INVALID_EMAIL`
- `email_bounced` should be `true`
- Run process_new_leads again — this lead should be skipped

---

### Scenario #2 — Soft Bounce (Temporary Failure)

**What this tests:** The system retries soft bounces up to 3 times with delays, then gives up.

**Step 1:** Add a test lead. Send them an email. Then simulate their inbox being temporarily full by injecting a soft-bounce DSN into the IMAP inbox (or directly update the DB):
```sql
UPDATE leads_v2 SET soft_bounce_count = 1, last_bounced_at = NOW()
WHERE email = 'YOUR_TEST_EMAIL@gmail.com';
```

**Step 2:** Trigger the retry task:
```bash
docker exec meeting-scheduler-worker-1 celery -A app.workers.celery_app call app.workers.v2_tasks.retry_soft_bounces
```

**Verify:** The lead was re-queued for a fresh send attempt. Check logs:
```bash
docker logs meeting-scheduler-worker-1 --tail 30
```
Look for `soft_bounce_retry` in the logs.

**Test the 3-strike rule:**
```sql
UPDATE leads_v2 SET soft_bounce_count = 3 WHERE email = 'YOUR_TEST_EMAIL@gmail.com';
```
Run retry task again — this lead should NOT be retried. Status stays as-is.

---

### Scenario #3 — Email Open Nudge

**What this tests:** When a lead opens the email multiple times but doesn't reply, the system sends a gentle nudge.

**Step 1:** Send the lead an outreach email (Step 2 of golden path).

**Step 2:** The outreach email contains a 1×1 invisible tracking pixel. Simulate the lead opening it 5 times by hitting the pixel URL. You can find the lead's ID from the DB:
```sql
SELECT id FROM leads_v2 WHERE email = 'YOUR_TEST_EMAIL@gmail.com';
```

Then visit in browser (5 times):
```
http://localhost:8000/api/v1/v2/track/open/{LEAD_ID}/{SIG}
```
Or just update the DB directly:
```sql
UPDATE leads_v2
SET email_open_count = 5, last_opened_at = NOW() - INTERVAL '3 hours'
WHERE email = 'YOUR_TEST_EMAIL@gmail.com';
```

**Step 3:** Run the open nudge checker:
```bash
docker exec meeting-scheduler-worker-1 celery -A app.workers.celery_app call app.workers.v2_tasks.check_open_nudges
```

**Verify:** A nudge email arrives at your test lead inbox. Something like "Saw you had a look — happy to answer any questions."

```sql
SELECT open_nudge_sent FROM leads_v2 WHERE email = 'YOUR_TEST_EMAIL@gmail.com';
-- Should be: true
```

Running the task again should NOT send a second nudge.

---

### Scenario #4 — Forwarded Email

**What this tests:** When a lead forwards the email to a colleague who then replies.

**Step 1:** From your test lead inbox, reply to the outreach email but include a forward header in the body:

> Subject: Fwd: Jane Aerospace — Quick Question
>
> Hi, forwarding this to you from my colleague.
>
> ---------- Forwarded message ----------
> From: colleague@acme.com
> Subject: Re: Quick sync this week?
>
> Yes happy to discuss

**Verify:**
```sql
SELECT booked_via_forward FROM leads_v2 WHERE email = 'YOUR_TEST_EMAIL@gmail.com';
-- Should be: true
```

The system continues normally — still books the meeting.

---

### Scenario #5 — Shared Inbox (Generic Company Email)

**What this tests:** Emails like `info@`, `contact@`, `admin@` are handled without using a personal name.

**Step 1:** Add a lead with a generic email:
```sql
INSERT INTO leads_v2 (business_name, email, status, created_at, updated_at)
VALUES ('Acme Logistics', 'info@acme-logistics.com', 'NEW', NOW(), NOW());
```
Note: no `contact_name` set.

**Step 2:** Trigger a send.

**Verify:** The outreach email uses the company name ("Acme Logistics") instead of "Hi ," with a blank name. Check `is_shared_inbox` in DB:
```sql
SELECT is_shared_inbox, contact_name FROM leads_v2 WHERE email = 'info@acme-logistics.com';
-- is_shared_inbox = true, contact_name = null (or the company name)
```

---

### Scenario #6 — Non-English Reply (Hindi / Tamil / Telugu)

**What this tests:** The system replies in the same language the lead wrote in.

**Step 1:** From your test lead inbox, reply to the outreach email with:

> हाँ, मुझे इसमें रुचि है। क्या आप मुझे अधिक जानकारी दे सकते हैं?

(This is Hindi for "Yes, I'm interested. Can you give me more information?")

**Wait for IMAP poll (1 minute).**

**Verify:** You receive a reply email in Hindi (or a bilingual response). Check DB:
```sql
SELECT reply_language FROM leads_v2 WHERE email = 'YOUR_TEST_EMAIL@gmail.com';
-- Should be: 'Hindi'
```

You can also test Tamil: `ஆம், எனக்கு ஆர்வம் உள்ளது` or Telugu: `అవును, నాకు ఆసక్తి ఉంది`

---

### Scenario #7 — Simple "Yes" or "OK" Reply

**What this tests:** Basic positive intent — lead says yes without asking anything.

**From your test lead inbox, reply:**

> Ok sure, let's do it

**Verify:** You receive the slot options email (Stage 2) within 1 minute. No confusion, no unnecessary questions asked back.

---

### Scenario #8 — Long Multi-Question Reply

**What this tests:** The AI answers all questions before offering a slot.

**From your test lead inbox, reply:**

> I have several questions before we meet:
> 1. What exactly is your service model for aerospace logistics?
> 2. Do you handle last-mile delivery or only mid-mile?
> 3. What industries have you worked with?
> 4. Are you DGCA compliant for drone operations?
> 5. What are your SLA commitments?

**Verify:** The reply email addresses each of the 5 questions specifically. It ends with a slot offer. No generic "let me know if you have questions" type replies.

---

### Scenario #9 — CC'd Colleague

**What this tests:** The system saves CC email addresses for follow-up.

**From your test lead inbox, reply to the outreach email and CC a colleague** (use any second email you own):

- In Gmail: click Reply → Add CC → type a second email
- Body: `Happy to discuss. I've looped in my colleague.`

**Verify:**
```sql
SELECT cc_emails FROM leads_v2 WHERE email = 'YOUR_TEST_EMAIL@gmail.com';
-- Should contain the CC'd email address
```

---

### Scenario #10 — Assistant Replying on Behalf of Lead

**What this tests:** System recognizes it's talking to a PA/secretary, not the lead directly.

**From your test lead inbox, reply:**

> Hi, I'm writing on behalf of Mr. Sharma. He's reviewed your email and would like to schedule a call. What times are available?

**Verify:** The reply acknowledges the assistant format — formal tone, asks the assistant to confirm a slot for Mr. Sharma. Check logs:
```bash
docker logs meeting-scheduler-worker-1 --tail 20
```
Look for `intent=assistant_reply`.

---

### Scenario #11 — Reply Received After Business Hours

**What this tests:** Replies outside 9am–9pm IST Monday–Saturday are queued and processed next morning.

**Step 1:** Temporarily update the code to force after-hours mode. In a new terminal:
```bash
docker exec -it meeting-scheduler-worker-1 bash
```
Then in the container, open a Python shell:
```bash
python3 -c "
from app.services.email_service import is_after_hours
print(is_after_hours())
"
```

**Step 2:** If you want to test without code changes, simply run this test between **9:00 PM and 9:00 AM IST**.

**Step 3:** From your test lead inbox, send a reply:

> Yes interested, let's talk

**Verify immediately:**
```sql
SELECT pending_reply_json FROM leads_v2 WHERE email = 'YOUR_TEST_EMAIL@gmail.com';
-- Should contain: [{"body": "Yes interested, let's talk", "received_at": "..."}]
```
No AI reply sent yet.

**Step 4:** Next morning (or trigger manually):
```bash
docker exec meeting-scheduler-worker-1 celery -A app.workers.celery_app call app.workers.v2_tasks.process_delayed_replies
```

**Verify:** The lead now receives the AI reply. `pending_reply_json` is cleared.

---

### Scenario #12 — Angry / Negative Reply

**What this tests:** System does NOT auto-reply to angry messages. It flags for human and alerts you.

**From your test lead inbox, reply:**

> This is absolutely outrageous! Stop spamming me! I am furious and I will report you!

**Verify:**
1. **No reply sent** to the lead
2. **You receive an escalation alert** in your organizer inbox — check it immediately
3. The alert email contains a snippet of the angry reply
```sql
SELECT escalated_to_human, priority_flag FROM leads_v2 WHERE email = 'YOUR_TEST_EMAIL@gmail.com';
-- Both should be: true
```

---

### Scenario #13 — Unsubscribe Request

**What this tests:** Lead opts out — gets one final polite email, never contacted again.

**From your test lead inbox, reply:**

> Please remove me from your list. I don't want any further emails.

**Verify:**
1. You receive **one final email** at the test lead inbox: "Noted — we've removed you, sorry to see you go"
2. No further emails after that
```sql
SELECT opted_out FROM leads_v2 WHERE email = 'YOUR_TEST_EMAIL@gmail.com';
-- Should be: true
```
Trigger `process_new_leads` again — this lead is skipped.

---

### Scenario #14 — "Call Me" Reply with Phone Number

**What this tests:** System extracts phone numbers and saves them.

**From your test lead inbox, reply:**

> I'd prefer a phone call. Please reach me at +91 98765 43210 after 3pm any day this week.

**Verify:**
```sql
SELECT phone_number FROM leads_v2 WHERE email = 'YOUR_TEST_EMAIL@gmail.com';
-- Should be: '+91 98765 43210'
```
The reply email acknowledges the call request and mentions the number back.

---

### Scenario #15 — Emoji-Only Reply

**What this tests:** Emoji-only messages are flagged for human review (no AI confusion).

**From your test lead inbox, reply with only emojis:**

> 👍🎉✅

**Verify:**
- No AI reply sent
```sql
SELECT escalated_to_human FROM leads_v2 WHERE email = 'YOUR_TEST_EMAIL@gmail.com';
-- Should be: true
```
Check logs for `emoji_only_reply_flagged_human`.

---

### Scenario #16 — Job Change Detection

**What this tests:** Lead has moved companies — system auto-creates a new lead for the new contact.

**From your test lead inbox, reply:**

> Hi, just to let you know I've actually moved on from this company. My new contact is Jane Doe at jane.doe@newcompany.in — she handles all logistics decisions now.

**Verify:**
```sql
SELECT status, new_contact_from_job_change FROM leads_v2 WHERE email = 'YOUR_TEST_EMAIL@gmail.com';
-- status = 'JOB_CHANGED'
-- new_contact_from_job_change contains 'jane.doe@newcompany.in'

SELECT email, status FROM leads_v2 WHERE email = 'jane.doe@newcompany.in';
-- A new lead exists for Jane Doe, status = 'NEW'
```

---

## Part 3 — Slot & Booking Tests

---

### Scenario #17 — Clicked Slot But Didn't Confirm

**What this tests:** Lead clicks a slot link but closes the browser before confirming. System sends a reminder.

**Step 1:** Get a slot options email sent to your test lead. Click one of the slot links. The confirm page opens. **Do NOT click Confirm** — just close the browser.

**Step 2:** Check the DB immediately:
```sql
SELECT pending_booking_slot_json, pending_booking_at, pending_nudge_sent FROM leads_v2
WHERE email = 'YOUR_TEST_EMAIL@gmail.com';
-- pending_booking_slot_json should be populated
-- pending_nudge_sent = false
```

**Step 3:** Simulate 21 minutes having passed:
```sql
UPDATE leads_v2 SET pending_booking_at = NOW() - INTERVAL '21 minutes'
WHERE email = 'YOUR_TEST_EMAIL@gmail.com';
```

**Step 4:** Run the nudge checker:
```bash
docker exec meeting-scheduler-worker-1 celery -A app.workers.celery_app call app.workers.v2_tasks.check_pending_bookings
```

**Verify:** A reminder email arrives: "You were close to booking — just one more click to confirm."
```sql
SELECT pending_nudge_sent FROM leads_v2 WHERE email = 'YOUR_TEST_EMAIL@gmail.com';
-- true
```

**Step 5:** Test slot expiry — simulate 31 minutes:
```sql
UPDATE leads_v2 SET pending_booking_at = NOW() - INTERVAL '31 minutes'
WHERE email = 'YOUR_TEST_EMAIL@gmail.com';
```
Run task again. `pending_booking_slot_json` is cleared — the slot is released back to the pool.

---

### Scenario #18 — Zoho Returns No Booking (Timeout)

**What this tests:** Zoho accepts the request but returns no confirmation — slot is treated as unavailable.

This test requires patching Zoho in code. Skip if you don't have dev access. Otherwise:

**In `app/services/zoho_bookings.py`**, temporarily change `create_booking` to return `(None, None)`.

**Verify after clicking a slot:** The browser or email shows "This slot is no longer available — here are alternatives" with 3 new options. Redis slot lock is released.

---

### Scenario #19 — Zoho Completely Down

**What this tests:** Zoho API throws an error — lead gets an apology, organizer gets an alert.

To simulate: temporarily add `raise ConnectionError("Zoho unreachable")` at the top of `create_booking`.

**Verify:**
1. Lead receives "Our booking system is temporarily offline — please reply with your preferred time" email
2. **Organizer inbox receives a Zoho-down alert email**
3. Check logs: `zoho_api_down`

---

### Scenario #20 — Lead Clicks a Past Slot

**What this tests:** Slot in the email has already passed — booking is blocked.

**Step 1:** Inject a past slot into the lead's offered slots:
```sql
UPDATE leads_v2
SET offered_slots_json = '["Monday, Jun 01 at 10:00 AM", "Tuesday, Jun 02 at 2:00 PM", "Wednesday, Jun 03 at 11:00 AM"]'
WHERE email = 'YOUR_TEST_EMAIL@gmail.com';
```

**Step 2:** Go to: `http://localhost:8000/api/v1/v2/book/{LEAD_ID}/0/{SIG}`

**Verify:** Browser shows "Sorry, this slot has passed. Please choose another time." — no Zoho call is made.

---

### Scenario #21 — Dual Timezone Display

**What this tests:** Leads outside IST see their local time alongside IST in slot cards.

**Step 1:** Update the lead's location:
```sql
UPDATE leads_v2 SET location = 'Dubai' WHERE email = 'YOUR_TEST_EMAIL@gmail.com';
```

**Step 2:** Trigger a reply processing so Stage 2 email is sent.

**Verify:** The slot options email shows two times for each slot:
```
Tuesday, Jun 10 at 10:00 AM IST / 07:30 AM GST
```

Test with different cities:
- `New York` → IST − 9:30h (summer)
- `London` → IST − 4:30h (summer)
- `Singapore` → IST + 2:30h

---

### Scenario #22 — Staff on Leave (Zoho Rejects Slot)

**What this tests:** Zoho says the booked slot isn't available (staff is on leave) — fresh alternatives sent.

Simulate by making Zoho return `(None, None)` for one specific slot while others succeed.

**Verify:** Lead receives an apology + 3 fresh alternative slots. The apology text specifically says "the person you were going to meet is unavailable."

---

### Scenario #23 — Lead Asks for a Specific Time

**What this tests:** Lead names a specific date/time they prefer — system fetches availability for that day.

**From your test lead inbox, reply:**

> Can we do next Friday around 4pm?

**Verify:** The reply email offers slots specifically from next Friday (or adjacent days if Friday is full). It does NOT just offer generic next-available slots.

---

### Scenario #24 — Two Leads from the Same Company Want the Same Slot

**What this tests:** If Alice from Acme is already booked in a slot, Bob from Acme gets redirected elsewhere.

**Step 1:** Create Alice, book her:
```sql
INSERT INTO leads_v2 (business_name, contact_name, email, designation, status, selected_slot, booked_at, created_at, updated_at)
VALUES ('Acme Corp', 'Alice Sharma', 'alice@acme-corp.in', 'Head of Ops', 'BOOKED', 'Tuesday, Jun 10 at 10:00 AM', NOW(), NOW(), NOW());
```

**Step 2:** Create Bob with the same slot offered:
```sql
INSERT INTO leads_v2 (business_name, contact_name, email, designation, status,
  offered_slots_json, sent_at, created_at, updated_at)
VALUES ('Acme Corp', 'Bob Verma', 'bob@acme-corp.in', 'Logistics Manager', 'SENT',
  '["Tuesday, Jun 10 at 10:00 AM", "Wednesday, Jun 11 at 2:00 PM", "Thursday, Jun 12 at 11:00 AM"]',
  NOW(), NOW(), NOW());
```

**Step 3:** Click the booking link for Bob's slot 0 (the same slot Alice has).

**Verify:** Bob receives an email: "Your colleague Alice Sharma has already connected with us on Tuesday Jun 10. Here are some other times:" — with different slots.

---

### Scenario #25 — Repeat Lead (Previously Booked, Coming Back)

**What this tests:** A lead who booked 3 months ago gets a warm reconnect email, not a cold intro.

**Step 1:** Mark the lead as previously booked:
```sql
UPDATE leads_v2 SET booked_at = NOW() - INTERVAL '90 days', status = 'NEW'
WHERE email = 'YOUR_TEST_EMAIL@gmail.com';
```

**Step 2:** Run `process_new_leads`.

**Verify:** The outreach email starts with something like "Great to reconnect, Rahul — it's been a while since we last spoke." — NOT the standard cold intro.
```sql
SELECT is_repeat_lead FROM leads_v2 WHERE email = 'YOUR_TEST_EMAIL@gmail.com';
-- true
```

---

### Scenario #26 — Lead Cancels via Zoho

**What this tests:** Zoho sends a cancellation webhook — system re-engages the lead automatically.

**Step 1:** Book a lead (status = BOOKED). Note the `booking_id`.

**Step 2:** Send the cancellation webhook from your terminal:
```bash
curl -X POST http://localhost:8000/api/v1/v2/webhook/zoho \
  -H "Content-Type: application/json" \
  -d '{"event_type": "booking_cancelled", "booking_id": "ZB-1234", "customer_email": "YOUR_TEST_EMAIL@gmail.com"}'
```
Replace `ZB-1234` with the actual booking_id from the DB.

**Verify:**
```sql
SELECT status FROM leads_v2 WHERE email = 'YOUR_TEST_EMAIL@gmail.com';
-- status = 'SENT' (back to sent, not booked)
```
Lead receives: "Sorry to see you cancel — here are some other times if you'd like to reschedule."

---

### Scenario #27 — No-Show (Lead Booked But Didn't Attend)

**What this tests:** If a booked meeting's time has passed and no attendance was logged, system re-engages.

**Step 1:** Create a "booked" lead with a past meeting time:
```sql
UPDATE leads_v2
SET status = 'BOOKED',
    selected_slot = 'Monday, Jun 01 at 10:00 AM',
    booking_id = 'ZB-NOSHOW-TEST'
WHERE email = 'YOUR_TEST_EMAIL@gmail.com';
```

**Step 2:** Run the no-show checker:
```bash
docker exec meeting-scheduler-worker-1 celery -A app.workers.celery_app call app.workers.v2_tasks.check_no_shows
```

**Verify:**
```sql
SELECT status, no_show_count FROM leads_v2 WHERE email = 'YOUR_TEST_EMAIL@gmail.com';
-- status = 'SENT', no_show_count = 1
```
Lead receives a warm re-engagement email: "Things come up — totally fine. Here are some fresh times whenever you're ready."

---

## Part 4 — Concurrency & Race Condition Tests

---

### Scenario #28 — 100 People Click the Same Slot at Once

**What this tests:** Only one booking is created even when many click simultaneously.

**Step 1:** Get a valid booking URL for a slot. Note the full URL from a test email.

**Step 2:** Open a terminal and run (Linux/Mac) or use PowerShell parallel jobs:
```bash
for i in $(seq 1 20); do
  curl -s "http://localhost:8000/api/v1/v2/book/{LEAD_ID}/0/{SIG}" > /dev/null &
done
wait
```

**Verify:**
```sql
SELECT COUNT(*) FROM leads_v2 WHERE status = 'BOOKED' AND selected_slot = 'Tuesday, Jun 10 at 10:00 AM';
-- Should be: 1 (exactly one booking)
```

```bash
docker exec meeting-scheduler-redis-1 redis-cli KEYS "slot:lock:*"
# Should show one lock key
```

---

### Scenario #29 — Redis Slot Lock Auto-Expires

**What this tests:** If a booking fails mid-way, the lock releases after 10 minutes.

```bash
# Set a test lock manually (expires in 5 seconds for this test)
docker exec meeting-scheduler-redis-1 redis-cli SET "slot:lock:10-Jun-2026:1000" "test@test.com" EX 5

# Immediately check it exists
docker exec meeting-scheduler-redis-1 redis-cli GET "slot:lock:10-Jun-2026:1000"

# Wait 6 seconds, check it's gone
sleep 6
docker exec meeting-scheduler-redis-1 redis-cli GET "slot:lock:10-Jun-2026:1000"
# Returns: (nil)
```

---

### Scenario #30 — Redis Goes Down During Booking

**What this tests:** If Redis is unavailable, booking still tries (fail-open — better than total failure).

**Step 1:**
```bash
docker stop meeting-scheduler-redis-1
```

**Step 2:** Trigger a booking.

**Verify:** Booking attempt proceeds to Zoho (the slot lock step fails open). Check logs for `slot_lock_redis_error`.

**Restore:**
```bash
docker start meeting-scheduler-redis-1
```

---

## Part 5 — Follow-Up & Timing Tests

---

### Scenario #33 — Out-of-Office Reply

**What this tests:** OOO replies are detected and the lead is contacted again after they return.

**From your test lead inbox, reply:**

> I am out of office until June 25th. I will respond when I return on June 26th. For urgent matters, contact my colleague Priya at priya@company.com.

**Verify:**
```sql
SELECT ooo_until FROM leads_v2 WHERE email = 'YOUR_TEST_EMAIL@gmail.com';
-- Should be: 2026-06-25 (approximately)
```

No further emails are sent until that date. After the date passes:
```bash
docker exec meeting-scheduler-worker-1 celery -A app.workers.celery_app call app.workers.v2_tasks.resume_ooo_leads
```
Lead gets a fresh outreach email.

---

### Scenario #34 — "Contact Me in July" (Scheduled Follow-Up)

**What this tests:** Lead asks to be contacted later — system saves the date and reaches out then.

**From your test lead inbox, reply:**

> This is interesting but we're really slammed right now. Can you reach out again in July? Would be a better time for us.

**Verify:**
```sql
SELECT scheduled_followup_at FROM leads_v2 WHERE email = 'YOUR_TEST_EMAIL@gmail.com';
-- Should be a date in July
```

Lead receives a confirmation: "Noted — I'll reach out in July. Looking forward to it."

**Test follow-up trigger:**
```sql
UPDATE leads_v2 SET scheduled_followup_at = NOW() - INTERVAL '1 minute'
WHERE email = 'YOUR_TEST_EMAIL@gmail.com';
```
```bash
docker exec meeting-scheduler-worker-1 celery -A app.workers.celery_app call app.workers.v2_tasks.send_scheduled_followups
```
Fresh outreach arrives in the test lead inbox.

---

### Scenario #35 — Holiday Skip

**What this tests:** No emails sent on Indian public holidays.

**Step 1:** Open `app/services/holiday_calendar.py` and temporarily add today's date to the holiday list.

**Step 2:** Run `process_new_leads`.

**Verify:** No emails sent. Check logs:
```bash
docker logs meeting-scheduler-worker-1 --tail 10
# Should show: process_new_leads_skipped_holiday
```

**Remove the test date from the file after verifying.**

---

### Scenario #36 — Opted-Out Lead's Domain Sends New Email

**What this tests:** If `old@acme.com` opted out and `new@acme.com` now emails in, the system flags it for human review rather than auto-replying.

**Step 1:**
```sql
-- Mark old lead as opted out
UPDATE leads_v2 SET opted_out = true WHERE email = 'old.contact@acme-corp.in';

-- Insert new lead from same company (different person)
INSERT INTO leads_v2 (business_name, contact_name, email, status, sent_at, created_at, updated_at)
VALUES ('Acme Corp', 'New Person', 'new.person@acme-corp.in', 'SENT', NOW(), NOW(), NOW());
```

**Step 2:** Simulate the new lead replying — run IMAP poll or use a real email reply from `new.person@acme-corp.in`.

**Verify:**
- No auto-reply sent
- `new_lead.escalated_to_human = true`
- Check logs for `opted_out_domain_re_engagement`

---

### Scenario #37 — Lead Replies After 2+ Months of Silence

**What this tests:** Long silence → re-engagement mode activated automatically.

**Step 1:**
```sql
UPDATE leads_v2 SET replied_at = NOW() - INTERVAL '75 days'
WHERE email = 'YOUR_TEST_EMAIL@gmail.com';
```

**Step 2:** From your test lead inbox, send any reply:

> Hi, sorry for the delay! Is your offer still available?

**Verify:**
```sql
SELECT is_repeat_lead FROM leads_v2 WHERE email = 'YOUR_TEST_EMAIL@gmail.com';
-- true
```

The AI reply uses warm re-engagement language rather than treating them as a cold lead.

---

### Scenario #38 — Senior Lead (CEO / Founder)

**What this tests:** C-suite leads trigger an escalation alert AND still get the AI email.

**Step 1:**
```sql
INSERT INTO leads_v2 (business_name, contact_name, email, designation, status, created_at, updated_at)
VALUES ('Big Enterprise Ltd', 'Arjun Kapoor', 'arjun@bigenterprise.com', 'CEO', 'NEW', NOW(), NOW());
```

**Step 2:** Run `process_new_leads`.

**Verify:**
1. **Organizer inbox receives an escalation alert** — "High-value lead: CEO at Big Enterprise Ltd"
2. The CEO **also receives the outreach email** (AI-drafted, goes out normally)
```sql
SELECT priority_flag, escalated_to_human FROM leads_v2 WHERE email = 'arjun@bigenterprise.com';
-- Both: true
```

---

## Part 6 — Infrastructure & Reliability Tests

---

### Scenario #39 — Worker Crash Recovery

**What this tests:** If the Celery worker crashes mid-task, the task is re-delivered when it restarts.

**Step 1:** Trigger a send cycle that has a long task running.

**Step 2:** Kill the worker:
```bash
docker stop meeting-scheduler-worker-1
```

**Step 3:** Restart it:
```bash
docker start meeting-scheduler-worker-1
```

**Verify:** The task was re-queued by Redis and completes on restart. Check logs — no duplicate sends due to idempotency keys.

---

### Scenario #40 — SMTP Daily Limit Reached

**What this tests:** System stops sending when it hits the daily email cap (default 400/day).

**Step 1:** Set the SMTP counter to the limit:
```bash
docker exec meeting-scheduler-redis-1 redis-cli SET "smtp:sent:$(date +%Y-%m-%d)" 400
docker exec meeting-scheduler-redis-1 redis-cli EXPIRE "smtp:sent:$(date +%Y-%m-%d)" 90000
```

**Step 2:** Run `process_new_leads`.

**Verify:** No emails sent. Check logs for `process_new_leads_smtp_limit_reached`.

**Reset counter after test:**
```bash
docker exec meeting-scheduler-redis-1 redis-cli DEL "smtp:sent:$(date +%Y-%m-%d)"
```

---

### Scenario #43 — Claude AI Rate Limit Retry

**What this tests:** When Claude API is rate-limited, the system retries with exponential backoff.

This is an infrastructure test. Check logs during a high-traffic period or inject a rate-limit error in code.

**Verify in logs:**
```bash
docker logs meeting-scheduler-worker-1 | grep "claude_rate_limit_retry"
# Should show: attempt=1 retry_in=5, attempt=2 retry_in=15
```

If all 3 retries fail, the email falls back to a hardcoded template — the lead still gets an email.

---

### Scenario #44 — Google Sheets Sync Failure → CSV Fallback

**What this tests:** When Sheets sync fails, data is saved to a local CSV instead of being lost.

**Step 1:** Temporarily break the Sheets config — set an invalid spreadsheet ID in `.env`:
```
GOOGLE_SHEETS_SPREADSHEET_ID=invalid_test_id_12345
```

Restart containers:
```bash
docker compose restart api worker beat
```

**Step 2:** Trigger the export:
```bash
docker exec meeting-scheduler-worker-1 celery -A app.workers.celery_app call app.workers.v2_tasks.export_pipeline_to_sheets
```

**Verify:** CSV file created inside the container:
```bash
docker exec meeting-scheduler-worker-1 ls -la /app/pipeline_fallback.csv
docker exec meeting-scheduler-worker-1 head -5 /app/pipeline_fallback.csv
```

Check logs for `pipeline_csv_fallback_written`.

**Restore:** Set the real spreadsheet ID back in `.env` and restart.

---

### Scenario #45 — Thread Summarization (Long Conversations)

**What this tests:** When a lead has had 5+ back-and-forth exchanges, the history is summarized via Claude Haiku before the next AI reply.

**Step 1:** Simulate a long conversation history:
```sql
UPDATE leads_v2
SET summary = 'Lead asked about service model, pricing, and DGCA compliance. Was sent case studies. Requested July follow-up.'
WHERE email = 'YOUR_TEST_EMAIL@gmail.com';
```

**Step 2:** From your test lead inbox, send a new reply:

> Hi, just checking in on the July follow-up we discussed.

**Verify:** The AI reply references the previous context (service model, case studies, compliance). Check logs for `thread_summarized_by_haiku`.

---

## Part 7 — Business Logic & Intent Tests

---

### Scenario #46 — NDA Request

**What this tests:** Lead asks for an NDA before proceeding — specific reply sent, human alerted.

**From your test lead inbox, reply:**

> Before we proceed further, our company policy requires an NDA to be in place. Can you send one over?

**Verify:**
1. Reply email explains the NDA process and next steps
2. `lead.escalated_to_human = true` — human team is alerted to follow up with the actual document

---

### Scenario #47 — Case Study Request

**What this tests:** Lead wants proof-of-concept before a meeting.

**From your test lead inbox, reply:**

> Do you have any case studies from similar companies in aerospace or heavy industry? I'd like to review before we meet.

**Verify:** Reply includes a pre-approved case study description. Still ends with a meeting offer.

---

### Scenario #48 — "We Already Have a Vendor"

**What this tests:** Lead says they're happy with their current provider — AI doesn't give up, finds a wedge.

**From your test lead inbox, reply:**

> We already work with TCI for our logistics needs and are pretty happy with them. Not sure there's a fit here.

**Verify:** Reply is a "complement, not compete" message — explains how Jane Aerospace works alongside existing vendors for specialist needs. Still ends with a meeting offer.

---

### Scenario #49 — Needs Boss Approval First

**What this tests:** Lead is interested but not the final decision-maker.

**From your test lead inbox, reply:**

> This sounds interesting but I'll need to run it by my Director, Mr. Rajesh Patel, before we can move forward.

**Verify:** Reply offers to send a short one-pager for Mr. Patel and proposes CC-ing him directly. Keeps the momentum going.

---

### Scenario #50 — Mentions a Deadline or Urgent Expansion

**What this tests:** Lead reveals a business deadline — system prioritizes this lead.

**From your test lead inbox, reply:**

> We're planning a major expansion in Q3 and need a reliable logistics partner secured by August. This is fairly urgent.

**Verify:**
```sql
SELECT priority_flag, priority_deadline FROM leads_v2 WHERE email = 'YOUR_TEST_EMAIL@gmail.com';
-- priority_flag = true
-- priority_deadline contains something like 'Q3 expansion, August deadline'
```

Reply personalizes around the deadline: "With your Q3 expansion in mind, let's make sure we connect before August..."

---

## Part 8 — A/B Testing Verification

---

### Check Variant Distribution

After running `process_new_leads` on 10+ leads:

```sql
SELECT ab_variant, ab_subject_variant, send_time_variant, COUNT(*)
FROM leads_v2
WHERE sent_at IS NOT NULL
GROUP BY ab_variant, ab_subject_variant, send_time_variant
ORDER BY ab_variant, ab_subject_variant;
```

Expected: Rough mix of A/B/C body variants, S1/S2/S3 subject variants, and morning/afternoon/evening send times. No single variant dominating completely (weights start equal, adjust over time).

---

### Check Redis A/B Counters

```bash
# How many sent per variant
docker exec meeting-scheduler-redis-1 redis-cli MGET ab:body:A:sent ab:body:B:sent ab:body:C:sent

# How many replied per variant
docker exec meeting-scheduler-redis-1 redis-cli MGET ab:body:A:replied ab:body:B:replied ab:body:C:replied

# Send-time tracking
docker exec meeting-scheduler-redis-1 redis-cli MGET ab:time:morning:sent ab:time:afternoon:sent ab:time:evening:sent

# Segment-level (CEO leads, South India)
docker exec meeting-scheduler-redis-1 redis-cli GET "ab:body:A:seg:ceo:sent"
docker exec meeting-scheduler-redis-1 redis-cli GET "ab:body:C:seg:supply_chain_south:replied"
```

---

### Check A/B Stats API

```bash
curl http://localhost:8000/api/v1/v2/ab-stats | python -m json.tool
```

This shows reply rates per variant. After sufficient data, variant C (if it has the highest reply rate) will get higher send weights automatically.

---

## Quick Reference — All Celery Tasks

Run any of these manually with:
```bash
docker exec meeting-scheduler-worker-1 celery -A app.workers.celery_app call <TASK_NAME>
```

| Task | What it does | Runs automatically |
|---|---|---|
| `app.workers.v2_tasks.process_new_leads` | Sends first outreach to all NEW leads | Every 10 min |
| `app.workers.v2_tasks.check_inbox_replies_v2` | Polls IMAP, processes replies | Every 1 min |
| `app.workers.v2_tasks.send_v2_reminders` | Sends follow-ups to non-responders | Every hour |
| `app.workers.v2_tasks.check_open_nudges` | Nudges leads who opened but didn't reply | Every 30 min |
| `app.workers.v2_tasks.check_pending_bookings` | Nudges unconfirmed clicks, expires stale holds | Every 5 min |
| `app.workers.v2_tasks.send_scheduled_followups` | Contacts leads whose scheduled date arrived | Hourly |
| `app.workers.v2_tasks.check_no_shows` | Re-engages leads who missed their meeting | Every 30 min |
| `app.workers.v2_tasks.retry_soft_bounces` | Retries leads with soft bounce (up to 3x) | Every 6 hours |
| `app.workers.v2_tasks.process_delayed_replies` | Processes after-hours queued replies | Every 10 min |
| `app.workers.v2_tasks.resume_ooo_leads` | Re-engages OOO leads once their return date passes | Daily 9am |
| `app.workers.v2_tasks.export_pipeline_to_sheets` | Syncs all leads to Google Sheets (CSV fallback if Sheets fails) | Every 4 hours |

---

## Quick Reference — Key SQL Queries

```sql
-- See all leads and their current status
SELECT email, contact_name, status, sent_at, replied_at, booked_at FROM leads_v2 ORDER BY created_at DESC;

-- All leads needing human review
SELECT email, contact_name, designation, priority_deadline FROM leads_v2 WHERE escalated_to_human = true;

-- Unconfirmed slot clicks (pending bookings)
SELECT email, pending_booking_at, pending_nudge_sent FROM leads_v2 WHERE pending_booking_slot_json IS NOT NULL;

-- Leads in OOO hold
SELECT email, ooo_until FROM leads_v2 WHERE ooo_until IS NOT NULL AND ooo_until > NOW();

-- Leads scheduled for future follow-up
SELECT email, scheduled_followup_at FROM leads_v2 WHERE scheduled_followup_at IS NOT NULL ORDER BY scheduled_followup_at;

-- Leads with bounce issues
SELECT email, soft_bounce_count, email_bounced, status FROM leads_v2 WHERE soft_bounce_count > 0 OR email_bounced = true;

-- A/B variant breakdown for sent leads
SELECT ab_variant, ab_subject_variant, COUNT(*) as sent,
       COUNT(replied_at) as replied,
       ROUND(COUNT(replied_at) * 100.0 / COUNT(*), 1) as reply_rate_pct
FROM leads_v2 WHERE sent_at IS NOT NULL GROUP BY ab_variant, ab_subject_variant;
```

---

## Recommended Full Test Run Order

Run these 10 steps in sequence for a complete end-to-end verification:

```
1.  Add a test lead → run process_new_leads → verify Stage 1 email arrives in test inbox
2.  Reply "Yes interested" → verify slot options email arrives
3.  Click a slot → verify confirm page opens in browser
4.  Wait 21 min (or update DB) → run check_pending_bookings → verify nudge email
5.  Click Confirm → verify booking confirmation + Zoho meeting link
6.  Reply "angry" email → verify no auto-reply + escalation alert in organizer inbox
7.  Add CEO lead → run process_new_leads → verify escalation alert + outreach both sent
8.  Reply with phone number → verify phone saved in DB
9.  Set soft_bounce_count=1 → run retry_soft_bounces → verify retry attempt
10. Send Zoho cancellation webhook → verify lead re-engagement email
```

After all 10 complete without errors, the system is working correctly.
