# Email Flow Comparison — Sent → Reply → Action

All tests run against `desaithryakshari@gmail.com` (test lead) via Zoho SMTP from `bharath.p@janeaerospace.co.in`.

---

## 1. Initial Outreach (Golden Path Start)

| | Details |
|---|---|
| **Email Sent** | Subject: `Quick question, Thrya` |
| **Body** | Cold outreach introducing Jane Aerospace, asking if this week works for a quick call. Variant C / Subject S1 / Morning send-time. |
| **Reply Received** | *(No reply yet — lead just received it)* |
| **Action Taken** | Lead status set to `SENT`. A/B tracking recorded: variant C sent, segment `manager_south`. |

---

## 2. "Yes / OK" Reply → Slot Options

| | Details |
|---|---|
| **Email Sent** | *(Same outreach from #1 above)* |
| **Reply Received** | `Sure, let's connect. This week works for me.` |
| **Action Taken** | Classified as `list_slots`. Fetched 6 real slots from Zoho. Sent slot-options email titled **"Available Times — Jane Aerospace"** → `desaithryakshari@gmail.com` |

---

## 3. Hindi Reply → Multilingual Response

| | Details |
|---|---|
| **Email Sent** | Outreach (English) |
| **Reply Received** | `हाँ, मुझे इसमें रुचि है। क्या आप मुझे अधिक जानकारी दे सकते हैं?` *(Yes, I'm interested. Can you give me more info?)* |
| **Action Taken** | Language detected as `hindi`. Sent reply in Hindi. `reply_language = hindi` saved to DB. Subject: **"Re: Partnership Opportunity — Jane Aerospace"** |

---

## 4. Multi-Question Reply → AI Answers All 5 Questions

| | Details |
|---|---|
| **Email Sent** | Outreach |
| **Reply Received** | `I have several questions before we meet:` *(5 questions about service model, last-mile, industries, DGCA compliance, SLAs)* |
| **Action Taken** | Classified as `question`. Claude AI generated answers addressing each question individually. Reply email sent with all 5 answered + slot offer at the end. |

---

## 5. Angry Reply → Escalation, No Auto-Reply

| | Details |
|---|---|
| **Email Sent** | Outreach |
| **Reply Received** | `This is absolutely outrageous! Stop spamming me immediately! I am furious and will report this!` |
| **Action Taken** | Classified as `angry_negative`. **No reply sent to lead.** Escalation alert email sent to organizer: **"[ACTION NEEDED] Angry/negative reply received — human intervention required — thrya"** → `bharath.p@janeaerospace.co.in`. DB: `escalated_to_human = true`, `priority_flag = true`. |

---

## 6. Unsubscribe Request → Opt-Out + Final Goodbye

| | Details |
|---|---|
| **Email Sent** | Outreach |
| **Reply Received** | `Please remove me from your mailing list. I do not want any further emails.` |
| **Action Taken** | Classified as `opt_out`. One final polite goodbye email sent: **"Re: Jane Aerospace"** → `desaithryakshari@gmail.com`. DB: `opted_out = true`. All future emails to this lead are permanently blocked. |

---

## 7. Phone Number Reply → Number Extracted & Saved

| | Details |
|---|---|
| **Email Sent** | Outreach |
| **Reply Received** | `I'd prefer a phone call. Please reach me at +91 98765 43210 after 3pm any day this week.` |
| **Action Taken** | Classified as `callback_request`. Phone number `+91 98765 43210` extracted and saved to `lead.phone_number`. Reply email sent acknowledging the callback request and confirming the number. Subject: **"Re: Partnership Opportunity — Jane Aerospace"** |

---

## 8. Emoji-Only Reply → Flagged for Human, No Reply

| | Details |
|---|---|
| **Email Sent** | Outreach |
| **Reply Received** | `👍🎉✅` *(nothing but emojis)* |
| **Action Taken** | Detected as emoji-only. **No AI reply sent.** DB: `escalated_to_human = true`. Log: `emoji_only_reply_flagged_human`. Human team must review manually. |

---

## 9. Job Change Notification → Old Lead Closed, New Lead Created

| | Details |
|---|---|
| **Email Sent** | Outreach |
| **Reply Received** | `Hi, I've actually moved on from this company. My new contact is Priya Nair at priya.nair@newcompany.in — she handles all logistics decisions now.` |
| **Action Taken** | Classified as `job_change`. Old lead status → `JOB_CHANGED`. New lead `priya.nair@newcompany.in` created with status `NEW` and will receive outreach in the next send cycle. DB: `new_contact_from_job_change = "Priya Nair <priya.nair@newcompany.in>"`. |

---

## 10. Assistant Replying on Behalf of Lead

| | Details |
|---|---|
| **Email Sent** | Outreach |
| **Reply Received** | `Hi, I'm writing on behalf of Mr. Desai. He has reviewed your email and would like to schedule a call. What times are available?` |
| **Action Taken** | Classified as `assistant_reply`. Reply sent in formal tone addressing the assistant, acknowledging Mr. Desai, and offering slot times. `on_behalf_of = "Mr. Desai"` parsed. Subject: **"Re: Partnership Opportunity — Jane Aerospace"** |

---

## 11. "Contact Me in July" → Follow-Up Scheduled

| | Details |
|---|---|
| **Email Sent** | Outreach |
| **Reply Received** | `This looks interesting but we are really slammed right now. Can you reach out again in July? Would be a much better time for us.` |
| **Action Taken** | Parsed `followup_date = 2026-07-01`. Sent a confirmation reply: "Noted — I'll reach out in July." DB: `scheduled_followup_at` set to July 1. The `send_scheduled_followups` task will auto-send fresh outreach when that date arrives. A follow-up email was also sent as a test: **"areallory — supply chain"** → `desaithryakshari@gmail.com` |

---

## 12. NDA Request → NDA Reply + Human Flagged

| | Details |
|---|---|
| **Email Sent** | Outreach |
| **Reply Received** | `Before we proceed further, our company policy requires an NDA to be signed. Can you send one over for review?` |
| **Action Taken** | Classified as `nda_request`. Reply email sent explaining NDA process and next steps. DB: `escalated_to_human = true` — human team is alerted to send the actual NDA document. Subject: **"Re: Partnership Opportunity — Jane Aerospace"** |

---

## 13. Case Study Request → Pre-Approved Reply Sent

| | Details |
|---|---|
| **Email Sent** | Outreach |
| **Reply Received** | `Do you have any case studies or references from similar companies in aerospace or heavy manufacturing?` |
| **Action Taken** | Classified as `case_study_request`. Pre-approved case study content sent in reply. Email ends with a slot offer. Subject: **"Re: Partnership Opportunity — Jane Aerospace"** |

---

## 14. "We Already Have a Vendor" → Complement Reply

| | Details |
|---|---|
| **Email Sent** | Outreach |
| **Reply Received** | `We already work with TCI for our logistics needs and are pretty happy with them. Not sure there's a fit here.` |
| **Action Taken** | Classified as `existing_vendor`. "Complement, not compete" reply sent — explains how Jane Aerospace fills a different niche alongside existing vendors. Still ends with a meeting offer. Subject: **"Re: Partnership Opportunity — Jane Aerospace"** |

---

## 15. "Need Boss Approval" → Decision-Maker Reply

| | Details |
|---|---|
| **Email Sent** | Outreach |
| **Reply Received** | `Sounds interesting but I'll need to run this by my Director, Mr. Rajesh Patel, before we can move forward with anything.` |
| **Action Taken** | Classified as `not_decision_maker`. `referred_to = "Mr. Rajesh Patel"` extracted. Reply offers to send a one-pager for Mr. Patel and proposes CC-ing him directly. Subject: **"Re: Partnership Opportunity — Jane Aerospace"** |

---

## 16. Deadline Mention → Priority Lead

| | Details |
|---|---|
| **Email Sent** | Outreach |
| **Reply Received** | `We are planning a major expansion in Q3 and need a reliable logistics partner secured by August. This is fairly urgent for us.` |
| **Action Taken** | Classified as `deadline_mention`. `deadline_text = "Q3 expansion, secured by August"` extracted. Reply personalizes around the deadline: "With your Q3 expansion in mind...". DB: `priority_flag = true`, `priority_deadline = "Q3 expansion, secured by August"`. |

---

## 17. CC'd Colleague in Reply

| | Details |
|---|---|
| **Email Sent** | Outreach |
| **Reply Received** | `Happy to discuss this further. I've looped in my colleague Arun Sharma who handles procurement.` |
| **Action Taken** | Colleague detected in body. `cc_emails` saved to DB. Reply sent acknowledging the loop-in and addressing both the lead and Arun Sharma. Subject: **"Re: Partnership Opportunity — Jane Aerospace"** |

---

## 18. Forwarded Email Detected

| | Details |
|---|---|
| **Email Sent** | Outreach |
| **Reply Received** | Body contained `---------- Forwarded message ----------` header with a colleague's message inside. |
| **Action Taken** | Forward header detected. `booked_via_forward = true` saved. System proceeded normally — offered 6 slots. Subject: **"Available Times — Jane Aerospace"** sent to `desaithryakshari@gmail.com`. |

---

## 19. Email Opened 5 Times → Nudge Sent

| | Details |
|---|---|
| **Email Sent** | Outreach (contains invisible 1×1 tracking pixel) |
| **Reply Received** | *(No reply — lead just kept opening the email)* |
| **Action Taken** | `email_open_count` set to 5. `check_open_nudges` task detected high-open / no-reply pattern. Nudge email sent: **"Re: Partnership Opportunity — Jane Aerospace"** → `desaithryakshari@gmail.com`. DB: `open_nudge_sent = true`. Will not nudge again. |

---

## 20. Soft Bounce → Retry Task

| | Details |
|---|---|
| **Email Sent** | Outreach |
| **Reply Received** | *(DSN bounce message — "Mailbox temporarily unavailable")* |
| **Action Taken** | `soft_bounce_count = 1` set. `retry_soft_bounces` task queued the lead for a retry send after a delay. After 3 soft bounces the lead is permanently skipped. |

---

## 21. Repeat Lead (Booked 90 Days Ago) → Warm Reconnect

| | Details |
|---|---|
| **Email Sent** | Fresh outreach — but tone is different |
| **Reply Received** | *(Lead re-entered the system after 90 days)* |
| **Action Taken** | `is_repeat_lead = true` detected (booked_at was 90 days ago). Outreach sent with warm reconnect opener instead of cold intro. Subject: **"15 min? Operations × supply chain"** → `desaithryakshari@gmail.com`. |

---

## 22. CEO Lead Detected → Alert + Outreach Simultaneously

| | Details |
|---|---|
| **Email Sent** | Two emails simultaneously |
| **Reply Received** | *(Lead is NEW — no reply yet)* |
| **Action Taken** | `designation = CEO` detected on NEW lead. **Alert email** sent to organizer: **"[ACTION NEEDED] Senior lead detected (CEO) — ready to receive initial email — thrya"** → `bharath.p@janeaerospace.co.in`. **Outreach email** also sent normally: **"Quick question, Thrya"** → `desaithryakshari@gmail.com`. DB: `priority_flag = true`, `escalated_to_human = true`. |

---

## 23. No-Show (Missed Booked Meeting) → Re-Engagement

| | Details |
|---|---|
| **Email Sent** | Re-engagement email after missed meeting |
| **Reply Received** | *(Lead didn't show up to their booked slot)* |
| **Action Taken** | `check_no_shows` task detected the booking slot was in the past. Status reset to `SENT`. `no_show_count = 1` incremented. Re-engagement email sent: **"Re: Missed Meeting — Jane Aerospace"** → `desaithryakshari@gmail.com`. Tone is empathetic — "Things come up, no worries." |

---

## 24. Zoho Cancellation Webhook → Re-Engagement Email

| | Details |
|---|---|
| **Email Sent** | Cancellation follow-up |
| **Reply Received** | *(Zoho sent a webhook: lead cancelled their booking)* |
| **Action Taken** | Webhook received: `event_type = booking_cancelled`, `booking_id = ZB-CANCEL-TEST`. Lead status reset to `SENT`, slot/booking_id cleared. Cancellation reply sent: **"Re: Your Meeting — Jane Aerospace"** → `desaithryakshari@gmail.com`. Offers fresh slot options. |

---

## 25. SMTP Daily Limit Reached → No Email Sent

| | Details |
|---|---|
| **Email Sent** | *(Nothing — blocked)* |
| **Reply Received** | *(No reply — emails were never sent)* |
| **Action Taken** | Redis counter `smtp:sent:2026-06-08 = 400` detected at send time. All outreach deferred. Log: `process_new_leads_smtp_limit_reached`. No emails sent until next day when counter resets to 0. |

---

## 26. Scheduled Follow-Up Date Arrived → Auto-Send

| | Details |
|---|---|
| **Email Sent** | Follow-up outreach on the scheduled date |
| **Reply Received** | *(Lead had previously asked to be contacted in July)* |
| **Action Taken** | `send_scheduled_followups` task found lead with `scheduled_followup_at` in the past. Fresh outreach email sent: **"areallory — supply chain"** → `desaithryakshari@gmail.com`. `scheduled_followup_at` cleared after send. |

---

## 27. Google Sheets Sync Failed → CSV Fallback

| | Details |
|---|---|
| **Email Sent** | *(No email — this is a data export)* |
| **Reply Received** | *(Not an email scenario — pipeline export)* |
| **Action Taken** | Sheets API returned `403 — The caller does not have permission`. System automatically wrote all 9 lead rows to `/tmp/pipeline_fallback.csv` instead. Log: `pipeline_csv_fallback_written rows=9`. No data was lost. |

---

## Summary Table

| # | Reply Text (short) | Classified As | Email Sent Back | DB Change |
|---|---|---|---|---|
| 1 | *(no reply)* | — | Outreach sent | `status=SENT` |
| 2 | "Sure, let's connect" | `list_slots` | 6 slot options | `status=SENT` |
| 3 | Hindi text | `question` | Hindi reply | `reply_language=hindi` |
| 4 | 5 questions | `question` | AI answers all 5 | `status=REPLIED` |
| 5 | "This is outrageous!" | `angry_negative` | **None** (escalation only) | `escalated_to_human=true` |
| 6 | "Remove me from list" | `opt_out` | Goodbye email | `opted_out=true` |
| 7 | "+91 98765 43210 call me" | `callback_request` | Callback ack | `phone_number=+91 98765 43210` |
| 8 | "👍🎉✅" | *(emoji block)* | **None** | `escalated_to_human=true` |
| 9 | "I've moved to new company" | `job_change` | — | `status=JOB_CHANGED`, new lead created |
| 10 | "Writing on behalf of Mr. Desai" | `assistant_reply` | Formal assistant reply | `status=REPLIED` |
| 11 | "Contact me in July" | `unclear` + followup date | Confirmation + slots | `scheduled_followup_at=Jul 1` |
| 12 | "We need an NDA" | `nda_request` | NDA process reply | `escalated_to_human=true` |
| 13 | "Do you have case studies?" | `case_study_request` | Case study email | `status=REPLIED` |
| 14 | "We use TCI already" | `existing_vendor` | Complement reply | `status=REPLIED` |
| 15 | "Need to ask my Director" | `not_decision_maker` | DM reply + one-pager offer | `referred_to=Mr. Rajesh Patel` |
| 16 | "Major expansion in Q3, urgent" | `deadline_mention` | Urgency-aware reply | `priority_flag=true`, `priority_deadline` saved |
| 17 | "Looped in Arun Sharma" | `assistant_reply` | Loop-in acknowledgment | `cc_emails` saved |
| 18 | Forwarded message body | `unclear` | 6 slot options | `booked_via_forward=true` |
| 19 | *(opened email 5×, no reply)* | *(pixel trigger)* | Nudge email | `open_nudge_sent=true` |
| 20 | *(DSN bounce)* | `soft_bounce` | *(retry queued)* | `soft_bounce_count=1` |
| 21 | *(returning after 90 days)* | *(repeat_lead flag)* | Warm reconnect email | `is_repeat_lead=true` |
| 22 | *(CEO designation, new lead)* | *(senior_lead flag)* | Alert to organizer + outreach to lead | `priority_flag=true` |
| 23 | *(no-show on booked slot)* | *(check_no_shows task)* | Empathetic re-engagement | `no_show_count=1` |
| 24 | *(Zoho webhook: cancelled)* | `booking_cancelled` | Cancellation + fresh slots offer | `status=SENT`, booking cleared |
| 25 | *(SMTP limit hit)* | *(smtp_can_send=False)* | **Nothing sent** | Deferred to next day |
| 26 | *(scheduled date arrived)* | *(send_scheduled_followups)* | Fresh outreach on schedule | `scheduled_followup_at` cleared |
| 27 | *(Sheets 403 error)* | *(CSV fallback)* | **No email** — CSV written | 9 rows → `/tmp/pipeline_fallback.csv` |

---

## Phase 3 — Infrastructure & Edge-Case Scenarios (Tested 2026-06-09)

| # | Trigger / Setup | Reply / Event | Action Taken | Key Assertion |
|---|---|---|---|---|
| 28 | `info@testcorp-aerospace.in` (no contact_name) | *(new lead outreach)* | Sent outreach; no personal name used | `is_shared_inbox=True` set correctly |
| 29 | Reply sent at 23:30 IST (after hours) | `"Yes I am interested, let's talk!"` | **Queued** in `pending_reply_json`; processed next morning → sent 6 slots | `reply_queued_after_hours` log fired; `Processed 1 queued after-hours replies` |
| 30 | Lead clicked booking link 21 min ago, not confirmed | *(check_pending_bookings task)* | **Nudge email** sent: "Your slot is still waiting" | `pending_nudge_sent=True`; 0 expired |
| 31 | Zoho mocked to return `(None, None)` | *(slot click idx=0)* | Slot treated as taken → 6 alternative slots sent | `zoho_booking_rejected_slot_taken` warning logged |
| 32 | Zoho raises `ConnectionError("Zoho unreachable")` | *(slot click)* | Alert email → `bharath.p@janeaerospace.co.in`; lead told to reply with preferred time | `[ZOHO DOWN] Manual booking needed` email sent |
| 33 | Slot `01-Jun-2026 10:00` (past date) clicked | *(slot click)* | Past slot guard fires → 6 fresh alternatives sent | `zoho_booking_rejected_slot_taken — sent 6 alternatives` |
| 34 | Lead in Dubai; replies "Yes sure, let's connect" | `"Yes sure, let's connect this week."` | 6 slots fetched; email sent | `Dual-timezone slot email result: Sent 6 slots`; inbox should show IST/GST |
| 35 | Alice (same company) already BOOKED on Jun 10 10AM; Bob tries same slot | *(Bob clicks same slot)* | Conflict detected → 6 alternatives sent to Bob | `zoho_booking_rejected_slot_taken — sent 6 alternatives` |
| 36 | OOO reply: "Out of office until June 25th, back June 26th" | Auto-reply OOO body | `ooo_until` set to `2026-06-26`; no immediate reply | `reply_ooo` logged; `ooo_until=2026-06-26 00:00:00+00:00` |
| 37 | `should_send_today` patched → False (holiday) | *(process_new_leads task)* | **Nothing sent** — skipped entirely | `process_new_leads_skipped_holiday` logged |
| 38 | `old@blocked-corp.in` opted-out; `new@blocked-corp.in` replies | `"Yes I'm interested"` | Flagged as opted-out domain re-engagement → `escalated_to_human=True` | `opted_out_domain_re_engagement` warning; human review required |
| 39 | Lead has `replied_at` 75 days ago; sends fresh reply | `"Sorry for the long silence! Is your offer still available?"` | Treated as repeat lead → fresh slots sent | `is_repeat_lead=True`; `Sent 6 general slots` |
| 40 | 10 rapid sequential booking attempts on same slot | *(sequential slot clicks)* | Slot locking / Zoho rejection prevents double-booking | Each attempt after first returns `zoho_booking_rejected_slot_taken` or alternatives |
| 41 | Redis key set with 5s TTL | *(6 second wait)* | Key auto-expires | `key exists: 1` → `key exists: 0` after 6s |
| 42 | `acquire_slot_lock` patched → `False` (Redis down) | *(slot click)* | Fail-closed → alternatives sent to lead | `redis_slot_lock_taken_concurrent_booking — sent 6 alternatives` |
| 43 | Lock acquired but Zoho returns `(None, None)` | *(slot click)* | Lock released; 6 alternatives sent | `zoho_booking_rejected_slot_taken — sent 6 alternatives`; `slot_lock_released` logged |
| 44 | `"Can we do next Friday around 4pm?"` reply | Custom time request | Parsed as `reschedule` intent (date=2026-06-19, time=16:00) → manual intervention email sent | `Reschedule: beyond 30-min window — manual intervention email sent` |
| 45 | Same reply body injected twice | IMAP duplicate simulation | Both processed (no dedup at layer tested); no crash | Second call handled gracefully — same result path |
| 46 | `follow_up_count=5`; asks about prior case studies | `"Are those case studies still available?"` | Case study email sent; `follow_up_count` incremented; `summary` updated | `Case study reply sent`; DB committed |
| 47 | `"This is exactly what we need! Urgent call this week?"` | High-urgency positive | Classified as `callback_request` → callback-ack email sent | `reply_callback_ack` logged; email sent |
| 48 | Worker container checked for task queue depth | *(Redis queue inspection)* | Queue length = 0 (all tasks processed); instructions printed for manual crash test | Worker healthy; `email` queue length = 0 |
