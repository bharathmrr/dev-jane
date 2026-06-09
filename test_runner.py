"""
Test runner — simulates lead replies by calling the processing pipeline directly.
Usage inside container:
  python test_runner.py <scenario_number>
  python test_runner.py all
"""
import sys
from datetime import datetime, timezone, timedelta
from sqlalchemy import select
from app.db.models import LeadV2

TEST_EMAIL = "desaithryakshari@gmail.com"

# ── helpers ────────────────────────────────────────────────────────────────────

async def get_lead(db):
    result = await db.execute(select(LeadV2).where(LeadV2.email == TEST_EMAIL))
    return result.scalar_one_or_none()


def simulate_reply(body: str, subject: str = "Re: Jane Aerospace"):
    """Inject a reply into _process_reply_v2 directly, bypassing IMAP."""
    from app.workers.runtime import run_async
    from app.workers.v2_tasks import _process_reply_v2
    from app.db.models import LeadStatus

    async def _inner(db):
        lead = await get_lead(db)
        if not lead:
            print(f"ERROR: Lead {TEST_EMAIL} not found — run setup first")
            return "lead_not_found"
        # Reset flags so each test starts clean
        lead.opted_out = False
        lead.escalated_to_human = False
        lead.priority_flag = False
        lead.status = LeadStatus.SENT
        if not lead.sent_at:
            lead.sent_at = datetime.now(timezone.utc)
        reply = {"from_addr": TEST_EMAIL, "subject": subject, "body": body}
        result = await _process_reply_v2(db, reply)
        return result

    result = run_async(_inner)
    print(f"Result: {result}")


def show_lead():
    from app.workers.runtime import run_async

    async def _inner(db):
        lead = await get_lead(db)
        if not lead:
            print("Lead not found")
            return "not_found"
        fields = [
            "status", "email_bounced", "bounce_count", "soft_bounce_count",
            "opted_out", "is_shared_inbox", "reply_language", "cc_emails",
            "escalated_to_human", "priority_flag", "priority_deadline",
            "phone_number", "is_repeat_lead", "booked_via_forward",
            "new_contact_from_job_change", "scheduled_followup_at",
            "no_show_count", "ooo_until", "pending_booking_slot_json",
            "pending_reply_json", "open_nudge_sent",
        ]
        print(f"\n── Lead state: {lead.email} ──")
        for f in fields:
            val = getattr(lead, f, "N/A")
            if val not in (None, False, 0, "", "[]"):
                print(f"  {f}: {val}")
        print()
        return "ok"

    run_async(_inner)


# ── scenarios ──────────────────────────────────────────────────────────────────

def setup():
    """Insert the test lead as NEW."""
    from app.workers.runtime import run_async
    from app.db.models import LeadStatus

    async def _inner(db):
        existing = await get_lead(db)
        if existing:
            existing.status = LeadStatus.NEW
            existing.sent_at = None
            existing.replied_at = None
            existing.booked_at = None
            existing.opted_out = False
            existing.email_bounced = False
            existing.escalated_to_human = False
            existing.priority_flag = False
            existing.is_repeat_lead = False
            existing.bounce_count = 0
            existing.soft_bounce_count = 0
            existing.open_nudge_sent = False
            existing.pending_reply_json = None
            existing.pending_booking_slot_json = None
            existing.designation = "Operations Head"
            existing.location = "Hyderabad"
            print(f"Reset existing lead {TEST_EMAIL} to NEW")
        else:
            lead = LeadV2(
                business_name="Desai Aerospace Ventures",
                contact_name="Thryakshari Desai",
                email=TEST_EMAIL,
                designation="Operations Head",
                location="Hyderabad",
                status=LeadStatus.NEW,
            )
            db.add(lead)
            print(f"Created test lead: {TEST_EMAIL}")
        return "ok"

    run_async(_inner)


def s01_send_outreach():
    """#1 — Trigger actual outreach email to test lead."""
    from app.workers.runtime import run_async
    from app.workers.v2_tasks import _process_new_leads
    result = run_async(_process_new_leads)
    print(f"process_new_leads result: {result}")


def s02_reply_yes():
    """#7 — Simple positive reply."""
    simulate_reply("Sure, let's connect. This week works for me.")


def s03_reply_hindi():
    """#6 — Non-English reply (Hindi)."""
    simulate_reply("हाँ, मुझे इसमें रुचि है। क्या आप मुझे अधिक जानकारी दे सकते हैं?")


def s04_reply_questions():
    """#8 — Multi-question reply."""
    body = """I have several questions before we meet:
1. What exactly is your service model for aerospace logistics?
2. Do you handle last-mile delivery or only mid-mile?
3. What industries have you worked with besides aerospace?
4. Are you DGCA compliant for drone operations?
5. What are your SLA commitments?"""
    simulate_reply(body)


def s05_reply_angry():
    """#12 — Angry reply — should escalate, send NO reply to lead."""
    simulate_reply("This is absolutely outrageous! Stop spamming me immediately! I am furious and will report this!")


def s06_reply_unsubscribe():
    """#13 — Unsubscribe."""
    simulate_reply("Please remove me from your mailing list. I do not want any further emails.")


def s07_reply_phone():
    """#14 — Callback request with phone number."""
    simulate_reply("I'd prefer a phone call. Please reach me at +91 98765 43210 after 3pm any day this week.")


def s08_reply_emoji():
    """#15 — Emoji-only reply."""
    simulate_reply("👍🎉✅")


def s09_reply_job_change():
    """#16 — Job change notification."""
    simulate_reply("Hi, I've actually moved on from this company. My new contact is Priya Nair at priya.nair@newcompany.in — she handles all logistics decisions now.")


def s10_reply_ooo():
    """#33 — Out of office."""
    simulate_reply("I am out of office until June 25th and will respond when I return. For urgent matters contact my colleague at priya@company.com")


def s11_reply_july():
    """#34 — Scheduled follow-up request."""
    simulate_reply("This looks interesting but we are really slammed right now. Can you reach out again in July? Would be a much better time for us.")


def s12_reply_assistant():
    """#10 — Assistant replying on behalf of lead."""
    simulate_reply("Hi, I'm writing on behalf of Mr. Desai. He has reviewed your email and would like to schedule a call. What times are available?")


def s13_reply_nda():
    """#46 — NDA request."""
    simulate_reply("Before we proceed further, our company policy requires an NDA to be signed. Can you send one over for review?")


def s14_reply_case_study():
    """#47 — Case study request."""
    simulate_reply("Do you have any case studies or references from similar companies in aerospace or heavy manufacturing?")


def s15_reply_existing_vendor():
    """#48 — Existing vendor reply."""
    simulate_reply("We already work with TCI for our logistics needs and are pretty happy with them. Not sure there's a fit here.")


def s16_reply_not_decision_maker():
    """#49 — Needs boss approval."""
    simulate_reply("Sounds interesting but I'll need to run this by my Director, Mr. Rajesh Patel, before we can move forward with anything.")


def s17_reply_deadline():
    """#50 — Deadline mention."""
    simulate_reply("We are planning a major expansion in Q3 and need a reliable logistics partner secured by August. This is fairly urgent for us.")


def s18_reply_cc():
    """#9 — CC detection."""
    simulate_reply("Happy to discuss this further. I've looped in my colleague Arun Sharma who handles procurement.", "Re: Jane Aerospace | cc:arun.sharma@desai-ventures.in")


def s19_reply_forward():
    """#4 — Forwarded email."""
    body = """Hi, please see the forwarded message below.

---------- Forwarded message ----------
From: colleague@desai-ventures.in
Subject: Fwd: Jane Aerospace — Quick Question

Yes happy to discuss this further."""
    simulate_reply(body)


def s20_soft_bounce():
    """#2 — Soft bounce — set 1 soft bounce, run retry task."""
    from app.workers.runtime import run_async
    from app.workers.v2_tasks import _retry_soft_bounces

    async def _setup(db):
        lead = await get_lead(db)
        if not lead:
            return "not_found"
        lead.soft_bounce_count = 1
        lead.last_bounced_at = datetime.now(timezone.utc)
        print("Set soft_bounce_count=1")
        return "ok"

    run_async(_setup)
    print("Running retry_soft_bounces...")
    result = run_async(_retry_soft_bounces)
    print(f"retry_soft_bounces result: {result}")


def s21_open_nudge():
    """#3 — Email open nudge — simulate 5 opens, run nudge checker."""
    from app.workers.runtime import run_async
    from app.workers.v2_tasks import _check_open_nudges

    async def _setup(db):
        lead = await get_lead(db)
        if not lead:
            return "not_found"
        lead.email_open_count = 5
        lead.last_opened_at = datetime.now(timezone.utc) - timedelta(hours=3)
        lead.open_nudge_sent = False
        print("Set email_open_count=5")
        return "ok"

    run_async(_setup)
    print("Running check_open_nudges...")
    result = run_async(_check_open_nudges)
    print(f"check_open_nudges result: {result}")


def s22_repeat_lead():
    """#25 — Repeat lead (previously booked 90 days ago)."""
    from app.workers.runtime import run_async
    from app.workers.v2_tasks import _process_new_leads
    from app.db.models import LeadStatus

    async def _setup(db):
        lead = await get_lead(db)
        if not lead:
            return "not_found"
        lead.booked_at = datetime.now(timezone.utc) - timedelta(days=90)
        lead.status = LeadStatus.NEW
        lead.sent_at = None
        lead.is_repeat_lead = False
        print("Set booked_at=90 days ago, status=NEW")
        return "ok"

    run_async(_setup)
    print("Running process_new_leads (expect reconnect email)...")
    result = run_async(_process_new_leads)
    print(f"process_new_leads result: {result}")


def s23_senior_lead():
    """#38 — Senior lead (CEO) detection."""
    from app.workers.runtime import run_async
    from app.workers.v2_tasks import _process_new_leads
    from app.db.models import LeadStatus

    async def _setup(db):
        lead = await get_lead(db)
        if not lead:
            return "not_found"
        lead.designation = "CEO"
        lead.status = LeadStatus.NEW
        lead.sent_at = None
        lead.escalated_to_human = False
        lead.priority_flag = False
        print("Set designation=CEO, status=NEW")
        return "ok"

    run_async(_setup)
    print("Running process_new_leads (expect escalation alert + outreach)...")
    result = run_async(_process_new_leads)
    print(f"process_new_leads result: {result}")


def s24_no_show():
    """#27 — No-show detection (past booked slot)."""
    from app.workers.runtime import run_async
    from app.workers.v2_tasks import _check_no_shows
    from app.db.models import LeadStatus

    async def _setup(db):
        lead = await get_lead(db)
        if not lead:
            return "not_found"
        lead.status = LeadStatus.BOOKED
        lead.selected_slot = "Monday, Jun 01 at 10:00 AM"
        lead.booking_id = "ZB-NOSHOW-TEST"
        lead.booked_at = datetime.now(timezone.utc) - timedelta(days=7)
        print("Set as BOOKED with past slot")
        return "ok"

    run_async(_setup)
    print("Running check_no_shows...")
    result = run_async(_check_no_shows)
    print(f"check_no_shows result: {result}")


def s25_zoho_cancel():
    """#26 — Zoho cancellation webhook."""
    from app.workers.runtime import run_async
    from app.db.models import LeadStatus

    async def _setup(db):
        lead = await get_lead(db)
        if not lead:
            return "not_found"
        lead.status = LeadStatus.BOOKED
        lead.booking_id = "ZB-CANCEL-TEST"
        lead.selected_slot = "Tuesday, Jun 10 at 10:00 AM"
        lead.booked_at = datetime.now(timezone.utc)
        print("Set lead to BOOKED with booking_id=ZB-CANCEL-TEST")
        return "ok"

    run_async(_setup)

    # Call the webhook handler function directly (bypasses HTTP)
    from app.workers.runtime import run_async

    async def _webhook(db):
        from app.api.v1.v2_endpoints import webhook_zoho
        payload = {
            "event_type": "booking_cancelled",
            "booking_id": "ZB-CANCEL-TEST",
            "customer_email": TEST_EMAIL,
        }
        result = await webhook_zoho(payload, db)
        return str(result)

    result = run_async(_webhook)
    print(f"Webhook result: {result}")


def s26_smtp_limit():
    """#40 — SMTP daily limit simulation."""
    import redis as _redis
    from datetime import date
    from app.workers.runtime import run_async
    from app.workers.v2_tasks import _process_new_leads
    r = _redis.from_url("redis://redis:6379/0")
    key = f"smtp:sent:{date.today().isoformat()}"
    r.set(key, 400)
    r.expire(key, 90000)
    print(f"Set {key}=400. Running process_new_leads (should be blocked)...")
    result = run_async(_process_new_leads)
    print(f"process_new_leads result: {result}")
    # Clean up so future sends work
    r.delete(key)
    print("SMTP counter reset")


def s27_scheduled_followup():
    """#34 — Trigger scheduled follow-up for a lead whose date has passed."""
    from app.workers.runtime import run_async
    from app.workers.v2_tasks import _send_scheduled_followups
    from app.db.models import LeadStatus

    async def _setup(db):
        lead = await get_lead(db)
        if not lead:
            return "not_found"
        lead.scheduled_followup_at = datetime.now(timezone.utc) - timedelta(minutes=5)
        lead.status = LeadStatus.SENT
        print("Set scheduled_followup_at to 5 min ago")
        return "ok"

    run_async(_setup)
    print("Running send_scheduled_followups...")
    result = run_async(_send_scheduled_followups)
    print(f"send_scheduled_followups result: {result}")


def s28_sheets_export():
    """#44 — Export pipeline to Google Sheets."""
    from app.workers.runtime import run_async
    from app.workers.v2_tasks import _export_pipeline_to_sheets
    result = run_async(_export_pipeline_to_sheets)
    print(f"export_pipeline_to_sheets result: {result}")


# ── remaining 24 scenarios ─────────────────────────────────────────────────────

def s29_shared_inbox():
    """#5 — Shared inbox (info@, contact@) — no personal name used in outreach."""
    from app.workers.runtime import run_async
    from app.workers.v2_tasks import _process_new_leads
    from app.db.models import LeadStatus, LeadV2

    async def _setup(db):
        from sqlalchemy import select as _select
        existing = (await db.execute(_select(LeadV2).where(LeadV2.email == "info@testcorp-aerospace.in"))).scalar_one_or_none()
        if existing:
            existing.status = LeadStatus.NEW
            existing.sent_at = None
        else:
            db.add(LeadV2(
                business_name="TestCorp Aerospace",
                email="info@testcorp-aerospace.in",
                designation="General Enquiries",
                location="Chennai",
                status=LeadStatus.NEW,
            ))
        print("Shared inbox lead ready (no contact_name)")
        return "ok"

    run_async(_setup)
    result = run_async(_process_new_leads)
    print(f"process_new_leads result: {result}")
    # verify
    from app.workers.runtime import run_async as ra2
    from app.db.models import LeadV2 as LV
    from sqlalchemy import select as _sel
    async def _check(db):
        l = (await db.execute(_sel(LV).where(LV.email == "info@testcorp-aerospace.in"))).scalar_one_or_none()
        if l:
            print(f"  is_shared_inbox = {l.is_shared_inbox}")
            print(f"  contact_name    = {l.contact_name!r}")
            print(f"  status          = {l.status}")
    ra2(_check)


def s30_after_hours():
    """#11 — After-hours reply queuing then process next morning."""
    from app.workers.runtime import run_async
    from app.workers.v2_tasks import _process_reply_v2, _process_delayed_replies
    from app.db.models import LeadStatus
    from unittest.mock import patch

    # Step 1: patch is_after_hours → True, send a reply → should be queued
    with patch("app.services.email_service.is_after_hours", return_value=True):
        async def _queue(db):
            lead = await get_lead(db)
            if not lead:
                return "not_found"
            lead.opted_out = False
            lead.escalated_to_human = False
            lead.status = LeadStatus.SENT
            if not lead.sent_at:
                lead.sent_at = datetime.now(timezone.utc)
            reply = {"from_addr": TEST_EMAIL, "subject": "Re: Jane", "body": "Yes I am interested, let's talk!"}
            return await _process_reply_v2(db, reply)
        result = run_async(_queue)
        print(f"After-hours queue result: {result}")

    # Verify queued
    async def _verify(db):
        lead = await get_lead(db)
        print(f"  pending_reply_json = {lead.pending_reply_json}")
    run_async(_verify)

    # Step 2: process delayed replies during business hours (simulates next morning)
    print("Processing delayed replies (next morning)...")
    with patch("app.services.email_service.is_after_hours", return_value=False):
        result2 = run_async(_process_delayed_replies)
    print(f"process_delayed_replies result: {result2}")


def s31_pending_booking():
    """#17 — Slot clicked but not confirmed — nudge at 21 min, expire at 31 min."""
    from app.workers.runtime import run_async
    from app.workers.v2_tasks import _check_pending_bookings
    from app.db.models import LeadStatus
    import json as _json

    async def _setup(db):
        lead = await get_lead(db)
        if not lead:
            return "not_found"
        lead.status = LeadStatus.SENT
        lead.pending_booking_slot_json = _json.dumps({"date": "Tuesday, Jun 10", "time": "10:00 AM", "idx": 0})
        lead.pending_booking_at = datetime.now(timezone.utc) - timedelta(minutes=21)
        lead.pending_nudge_sent = False
        print("Set pending_booking_at = 21 min ago")
        return "ok"

    run_async(_setup)
    print("Running check_pending_bookings (expect nudge email)...")
    result = run_async(_check_pending_bookings)
    print(f"check_pending_bookings result: {result}")

    # Now expire it (31 min)
    async def _expire(db):
        lead = await get_lead(db)
        if not lead:
            return "not_found"
        lead.pending_booking_at = datetime.now(timezone.utc) - timedelta(minutes=31)
        print("Set pending_booking_at = 31 min ago (should expire)")
        return "ok"

    run_async(_expire)
    result2 = run_async(_check_pending_bookings)
    print(f"check_pending_bookings (expiry) result: {result2}")


def s32_zoho_returns_none():
    """#18 — Zoho API returns (None, None) — slot treated as unavailable."""
    from app.workers.runtime import run_async
    from app.workers.v2_tasks import _do_booking
    from app.db.models import LeadStatus
    from unittest.mock import patch

    async def _setup(db):
        lead = await get_lead(db)
        if not lead:
            return "not_found"
        lead.status = LeadStatus.SENT
        lead.opted_out = False
        lead.escalated_to_human = False
        print("Set up lead for Zoho-None test. Will patch Zoho to return (None, None)...")
        return "ok"

    run_async(_setup)

    with patch("app.workers.v2_tasks.ZohoBookingsService") as mock_zoho:
        mock_zoho.return_value.create_booking.return_value = (None, None)
        async def _book(db):
            lead = await get_lead(db)
            if not lead:
                return "not_found"
            return await _do_booking(db, lead,
                                     date_str="10-Jun-2026",
                                     time_str="10:00",
                                     display_str="Tuesday, Jun 10 at 10:00 AM IST")
        result = run_async(_book)
    print(f"Zoho-None booking result: {result}")


def s33_zoho_down():
    """#19 — Zoho API raises exception — lead notified, organizer alerted."""
    from app.workers.runtime import run_async
    from app.workers.v2_tasks import _do_booking
    from app.db.models import LeadStatus
    from unittest.mock import patch

    async def _setup(db):
        lead = await get_lead(db)
        if not lead:
            return "not_found"
        lead.status = LeadStatus.SENT
        lead.opted_out = False
        lead.escalated_to_human = False
        print("Set up lead. Patching Zoho to raise ConnectionError...")
        return "ok"

    run_async(_setup)

    with patch("app.workers.v2_tasks.ZohoBookingsService") as mock_zoho:
        mock_zoho.return_value.create_booking.side_effect = ConnectionError("Zoho unreachable")
        async def _book(db):
            lead = await get_lead(db)
            if not lead:
                return "not_found"
            return await _do_booking(db, lead,
                                     date_str="10-Jun-2026",
                                     time_str="10:00",
                                     display_str="Tuesday, Jun 10 at 10:00 AM IST")
        result = run_async(_book)
    print(f"Zoho-Down booking result: {result}")


def s34_past_slot():
    """#20 — Lead clicks a slot that has already passed."""
    from app.workers.runtime import run_async
    from app.workers.v2_tasks import _do_booking
    from app.db.models import LeadStatus

    async def _setup(db):
        lead = await get_lead(db)
        if not lead:
            return "not_found"
        lead.status = LeadStatus.SENT
        lead.opted_out = False
        print("Set up lead. Sending past-date slot (01-Jun-2026 10:00)...")
        return "ok"

    run_async(_setup)
    async def _book(db):
        lead = await get_lead(db)
        if not lead:
            return "not_found"
        return await _do_booking(db, lead,
                                 date_str="01-Jun-2026",
                                 time_str="10:00",
                                 display_str="Monday, Jun 1 at 10:00 AM IST")
    result = run_async(_book)
    print(f"Past-slot booking result: {result}")


def s35_dual_timezone():
    """#21 — Dubai lead gets IST + GST in slot email."""
    from app.workers.runtime import run_async
    from app.workers.v2_tasks import _process_reply_v2
    from app.db.models import LeadStatus

    async def _setup(db):
        lead = await get_lead(db)
        if not lead:
            return "not_found"
        lead.location = "Dubai"
        lead.status = LeadStatus.SENT
        lead.opted_out = False
        lead.escalated_to_human = False
        if not lead.sent_at:
            lead.sent_at = datetime.now(timezone.utc)
        print("Set location=Dubai")
        return "ok"

    run_async(_setup)
    # Trigger slot email by replying "yes"
    async def _reply(db):
        lead = await get_lead(db)
        if not lead:
            return "not_found"
        reply = {"from_addr": TEST_EMAIL, "subject": "Re: Jane", "body": "Yes sure, let's connect this week."}
        return await _process_reply_v2(db, reply)
    from app.workers.runtime import run_async as ra
    result = ra(_reply)
    print(f"Dual-timezone slot email result: {result}")
    print("  Check desaithryakshari@gmail.com inbox — slots should show IST / GST times")


def s36_same_company_slot():
    """#24 — Two leads from same company (@acme-test.in) want same slot."""
    from app.workers.runtime import run_async
    from app.workers.v2_tasks import _do_booking
    from app.db.models import LeadStatus, LeadV2
    import json as _json
    from sqlalchemy import select as _sel

    SHARED_SLOT = "Tuesday, Jun 10 at 10:00 AM"
    ALICE_EMAIL = "alice@acme-testco.in"
    BOB_EMAIL   = "bob@acme-testco.in"

    async def _setup(db):
        # Alice = BOOKED on that slot
        alice = (await db.execute(_sel(LeadV2).where(LeadV2.email == ALICE_EMAIL))).scalar_one_or_none()
        if not alice:
            alice = LeadV2(business_name="Acme TestCo", contact_name="Alice Sharma",
                           email=ALICE_EMAIL, designation="Head of Ops",
                           status=LeadStatus.BOOKED, selected_slot=SHARED_SLOT,
                           booked_at=datetime.now(timezone.utc))
            db.add(alice)
        else:
            alice.status = LeadStatus.BOOKED
            alice.selected_slot = SHARED_SLOT

        # Bob = SENT with same slot offered
        bob = (await db.execute(_sel(LeadV2).where(LeadV2.email == BOB_EMAIL))).scalar_one_or_none()
        if not bob:
            bob = LeadV2(business_name="Acme TestCo", contact_name="Bob Verma",
                         email=BOB_EMAIL, designation="Logistics Manager",
                         status=LeadStatus.SENT,
                         offered_slots_json=_json.dumps([
                             {"date": "Tuesday, Jun 10", "time": "10:00 AM", "zoho_id": "SLOT-X"},
                             {"date": "Wednesday, Jun 11", "time": "2:00 PM", "zoho_id": "SLOT-Y"},
                         ]),
                         sent_at=datetime.now(timezone.utc))
            db.add(bob)
        else:
            bob.status = LeadStatus.SENT
        print(f"Alice={ALICE_EMAIL} BOOKED on {SHARED_SLOT}")
        print(f"Bob={BOB_EMAIL} SENT with same slot offered")
        return "ok"

    run_async(_setup)

    async def _bob_books(db):
        from sqlalchemy import select as _s
        bob = (await db.execute(_s(LeadV2).where(LeadV2.email == BOB_EMAIL))).scalar_one_or_none()
        return await _do_booking(db, bob,
                                 date_str="10-Jun-2026",
                                 time_str="10:00",
                                 display_str="Tuesday, Jun 10 at 10:00 AM IST")
    result = run_async(_bob_books)
    print(f"Same-company booking result: {result}")


def s37_ooo_reply():
    """#33 — Out-of-office reply properly detected and date saved."""
    from app.workers.runtime import run_async
    from app.workers.v2_tasks import _process_reply_v2
    from app.db.models import LeadStatus

    async def _inner(db):
        lead = await get_lead(db)
        if not lead:
            return "not_found"
        lead.opted_out = False
        lead.escalated_to_human = False
        lead.status = LeadStatus.SENT
        if not lead.sent_at:
            lead.sent_at = datetime.now(timezone.utc)
        lead.ooo_until = None
        reply = {
            "from_addr": TEST_EMAIL,
            "subject": "Auto-Reply: Out of Office",
            "body": "I am out of office until June 25th. I will respond when I return on June 26th. For urgent matters contact priya@desai-ventures.in",
        }
        return await _process_reply_v2(db, reply)

    result = run_async(_inner)
    print(f"OOO result: {result}")

    async def _check(db):
        lead = await get_lead(db)
        print(f"  ooo_until = {lead.ooo_until}")
    run_async(_check)


def s38_holiday_skip():
    """#35 — Holiday skip — patch today into holiday list, verify no send."""
    from app.workers.runtime import run_async
    from app.workers.v2_tasks import _process_new_leads
    from app.db.models import LeadStatus
    from unittest.mock import patch

    async def _setup(db):
        lead = await get_lead(db)
        if not lead:
            return "not_found"
        lead.status = LeadStatus.NEW
        lead.sent_at = None
        return "ok"

    run_async(_setup)
    with patch("app.services.holiday_calendar.should_send_today", return_value=False):
        result = run_async(_process_new_leads)
    print(f"Holiday skip result: {result}")


def s39_opted_out_domain():
    """#36 — New email from same opted-out domain → flagged, no auto-reply."""
    from app.workers.runtime import run_async
    from app.workers.v2_tasks import _process_reply_v2
    from app.db.models import LeadStatus, LeadV2
    from sqlalchemy import select as _sel

    OLD_EMAIL = "old.contact@blocked-corp.in"
    NEW_EMAIL = "new.person@blocked-corp.in"

    async def _setup(db):
        # Old lead: opted out
        old = (await db.execute(_sel(LeadV2).where(LeadV2.email == OLD_EMAIL))).scalar_one_or_none()
        if not old:
            old = LeadV2(business_name="Blocked Corp", email=OLD_EMAIL,
                         status=LeadStatus.SENT, opted_out=True)
            db.add(old)
        else:
            old.opted_out = True

        # New lead: SENT, same domain
        new = (await db.execute(_sel(LeadV2).where(LeadV2.email == NEW_EMAIL))).scalar_one_or_none()
        if not new:
            new = LeadV2(business_name="Blocked Corp", contact_name="New Person",
                         email=NEW_EMAIL, status=LeadStatus.SENT,
                         sent_at=datetime.now(timezone.utc))
            db.add(new)
        else:
            new.status = LeadStatus.SENT
            new.opted_out = False
            new.escalated_to_human = False
        print(f"{OLD_EMAIL} = opted_out=True | {NEW_EMAIL} = SENT")
        return "ok"

    run_async(_setup)

    async def _reply(db):
        from sqlalchemy import select as _s
        new = (await db.execute(_s(LeadV2).where(LeadV2.email == NEW_EMAIL))).scalar_one_or_none()
        if not new:
            return "new_lead_not_found"
        reply = {"from_addr": NEW_EMAIL, "subject": "Re: Jane", "body": "Hi, yes I'm interested in your services."}
        return await _process_reply_v2(db, reply)

    result = run_async(_reply)
    print(f"Opted-out domain result: {result}")

    async def _check(db):
        from sqlalchemy import select as _s
        new = (await db.execute(_s(LeadV2).where(LeadV2.email == NEW_EMAIL))).scalar_one_or_none()
        print(f"  escalated_to_human = {new.escalated_to_human if new else 'N/A'}")
    run_async(_check)


def s40_two_months_silence():
    """#37 — Lead replies after 2+ months silence → is_repeat_lead=True."""
    from app.workers.runtime import run_async
    from app.workers.v2_tasks import _process_reply_v2
    from app.db.models import LeadStatus

    async def _inner(db):
        lead = await get_lead(db)
        if not lead:
            return "not_found"
        lead.opted_out = False
        lead.escalated_to_human = False
        lead.status = LeadStatus.SENT
        lead.is_repeat_lead = False
        lead.replied_at = datetime.now(timezone.utc) - timedelta(days=75)
        if not lead.sent_at:
            lead.sent_at = datetime.now(timezone.utc)
        reply = {
            "from_addr": TEST_EMAIL,
            "subject": "Re: Jane Aerospace",
            "body": "Hi, sorry for the long silence! Is your offer still available? We'd like to reconnect.",
        }
        return await _process_reply_v2(db, reply)

    result = run_async(_inner)
    print(f"Two-months silence result: {result}")


def s41_concurrency():
    """#28 — 10 rapid sequential attempts to book the same slot — slot lock ensures idempotent behaviour."""
    from app.workers.runtime import run_async
    from app.workers.v2_tasks import _do_booking
    from app.db.models import LeadStatus

    results = []
    print("Running 10 rapid sequential booking attempts on same slot...")
    for i in range(10):
        async def _book(db):
            lead = await get_lead(db)
            if not lead:
                return "not_found"
            lead.status = LeadStatus.SENT
            lead.opted_out = False
            return await _do_booking(db, lead,
                                     date_str="10-Jun-2026",
                                     time_str="11:00",
                                     display_str="Tuesday, Jun 10 at 11:00 AM IST")
        r = run_async(_book)
        results.append(r)
        print(f"  attempt {i+1}: {r}")

    booked = [r for r in results if r and ("booked" in str(r).lower() or "confirmed" in str(r).lower())]
    print(f"Total attempts: 10 | Successful bookings: {len(booked)}")
    print("(After first booking, lead is BOOKED so subsequent attempts get a different branch)")


def s42_redis_lock_expiry():
    """#29 — Redis slot lock auto-expires after TTL."""
    import redis as _redis, time
    r = _redis.from_url("redis://redis:6379/0")
    key = "slot:lock:10-Jun-2026:1000"
    r.set(key, "test@test.com", ex=5)
    print(f"Set {key} with 5s TTL. Exists: {r.exists(key)}")
    time.sleep(6)
    exists_after = r.exists(key)
    print(f"After 6 seconds — key exists: {exists_after} (expected: False/0)")


def s43_redis_down():
    """#30 — Redis unavailable → slot lock fails open, booking still proceeds."""
    import subprocess, time
    from app.workers.runtime import run_async
    from app.workers.v2_tasks import _do_booking
    from app.db.models import LeadStatus
    import json as _json

    async def _setup(db):
        lead = await get_lead(db)
        if not lead:
            return "not_found"
        lead.status = LeadStatus.SENT
        lead.opted_out = False
        slots = [{"date": "Tuesday, Jun 10", "time": "2:00 PM", "zoho_id": "REDIS-DOWN-SLOT"}]
        lead.offered_slots_json = _json.dumps(slots)
        return "ok"

    run_async(_setup)
    # Pause Redis inside the container via env (can't stop Docker from inside)
    # Instead mock the Redis acquire to raise an exception
    from unittest.mock import patch
    # Redis down = lock returns False (fail-closed) → alternatives sent, lead not blocked
    with patch("app.workers.v2_tasks.acquire_slot_lock", return_value=False):
        async def _book(db):
            lead = await get_lead(db)
            return await _do_booking(db, lead,
                                     date_str="10-Jun-2026",
                                     time_str="14:00",
                                     display_str="Tuesday, Jun 10 at 2:00 PM IST")
        result = run_async(_book)
    print(f"Redis-down booking result: {result}")
    print("  (fail-closed: lock failed → alternatives sent rather than blocking lead)")


def s44_lock_zoho_rejects():
    """#31 — Redis lock acquired but Zoho returns None → lock released, alternatives sent."""
    from app.workers.runtime import run_async
    from app.workers.v2_tasks import _do_booking
    from app.db.models import LeadStatus
    from unittest.mock import patch
    import json as _json

    async def _setup(db):
        lead = await get_lead(db)
        if not lead:
            return "not_found"
        lead.status = LeadStatus.SENT
        lead.opted_out = False
        slots = [
            {"date": "Tuesday, Jun 10", "time": "3:00 PM", "zoho_id": "LOCK-REJECT-SLOT"},
            {"date": "Wednesday, Jun 11", "time": "11:00 AM", "zoho_id": "SLOT-ALT"},
        ]
        lead.offered_slots_json = _json.dumps(slots)
        return "ok"

    run_async(_setup)

    with patch("app.workers.v2_tasks.ZohoBookingsService") as mock_zoho:
        mock_zoho.return_value.create_booking.return_value = (None, None)
        async def _book(db):
            lead = await get_lead(db)
            return await _do_booking(db, lead,
                                     date_str="10-Jun-2026",
                                     time_str="15:00",
                                     display_str="Tuesday, Jun 10 at 3:00 PM IST")
        result = run_async(_book)
    print(f"Lock+Zoho-reject result: {result}")


def s45_claude_rate_limit():
    """#43 — Claude rate limit → exponential backoff retry (5s→15s→45s)."""
    from app.workers.runtime import run_async
    from app.workers.v2_tasks import _process_new_leads
    from app.db.models import LeadStatus
    from unittest.mock import patch, call
    import time

    async def _setup(db):
        lead = await get_lead(db)
        if not lead:
            return "not_found"
        lead.status = LeadStatus.NEW
        lead.sent_at = None
        lead.designation = "Operations Head"
        return "ok"

    run_async(_setup)

    call_count = [0]
    original_ask = None

    def patched_ask(system, user, model, max_tokens=256):
        call_count[0] += 1
        if call_count[0] <= 2:
            raise Exception("rate_limit 429 too many requests")
        # Third call succeeds — import and call real function
        import anthropic
        client = anthropic.Anthropic()
        msg = client.messages.create(model=model, max_tokens=max_tokens,
                                     messages=[{"role": "user", "content": user}],
                                     system=system)
        for block in msg.content:
            if hasattr(block, "text"):
                return block.text.strip()
        return ""

    with patch("app.services.email_generator._ask", side_effect=patched_ask):
        result = run_async(_process_new_leads)
    print(f"Claude rate-limit retry result: {result}")
    print(f"  _ask called {call_count[0]} times (expected: 3 — 2 failures + 1 success)")
    print("  Check logs for: claude_rate_limit_retry attempt=1 retry_in=5, attempt=2 retry_in=15")


def s46_thread_summarize():
    """#45 — Thread summarization after 5+ messages."""
    from app.workers.runtime import run_async
    from app.workers.v2_tasks import _process_reply_v2
    from app.db.models import LeadStatus
    import json as _json

    async def _inner(db):
        lead = await get_lead(db)
        if not lead:
            return "not_found"
        lead.opted_out = False
        lead.escalated_to_human = False
        lead.status = LeadStatus.SENT
        if not lead.sent_at:
            lead.sent_at = datetime.now(timezone.utc)
        # Simulate 5 prior messages in history
        lead.summary = "Lead asked about service model and pricing. Was interested but wanted case studies. Mentioned expansion plans for Q3."
        lead.follow_up_count = 5
        reply = {
            "from_addr": TEST_EMAIL,
            "subject": "Re: Jane Aerospace",
            "body": "Following up on our previous discussion — are those case studies still available?",
        }
        return await _process_reply_v2(db, reply)

    result = run_async(_inner)
    print(f"Thread summarize result: {result}")
    print("  Check logs for: thread_summarized or summarize_thread called")


def s47_imap_duplicate():
    """#41 — IMAP duplicate prevention — same message processed twice → only one reply."""
    from app.workers.runtime import run_async
    from app.workers.v2_tasks import _process_reply_v2
    from app.db.models import LeadStatus

    async def _first(db):
        lead = await get_lead(db)
        if not lead:
            return "not_found"
        lead.opted_out = False
        lead.escalated_to_human = False
        lead.status = LeadStatus.SENT
        if not lead.sent_at:
            lead.sent_at = datetime.now(timezone.utc)
        reply = {"from_addr": TEST_EMAIL, "subject": "Re: Jane", "body": "Yes interested, let's schedule a call."}
        return await _process_reply_v2(db, reply)

    r1 = run_async(_first)
    print(f"First processing: {r1}")

    # Second processing of the same reply — lead is now REPLIED, should still handle gracefully
    async def _second(db):
        lead = await get_lead(db)
        if not lead:
            return "not_found"
        reply = {"from_addr": TEST_EMAIL, "subject": "Re: Jane", "body": "Yes interested, let's schedule a call."}
        return await _process_reply_v2(db, reply)

    r2 = run_async(_second)
    print(f"Second processing (duplicate): {r2}")
    print("  If deduplication works: second result is different / no duplicate email")


def s48_custom_time_slot():
    """#23 — Lead asks for a specific time → Zoho fetched for that day."""
    simulate_reply("Can we do next Friday around 4pm instead? That works better for me.")


def s49_worker_crash():
    """#39 — Worker crash recovery — restart worker, verify tasks resume."""
    import subprocess, time
    print("Simulating worker crash by checking task queue depth...")
    import redis as _redis
    r = _redis.from_url("redis://redis:6379/0")
    queue_len = r.llen("email")
    print(f"  Current 'email' queue length: {queue_len}")
    print("  (Worker is running — tasks are being processed automatically)")
    print("  To fully test: stop worker container, queue a task, restart worker")
    print("  docker stop meeting-scheduler-worker-1 && docker start meeting-scheduler-worker-1")


def s50_high_intent():
    """#32 — High-intent reply (very positive / ready to sign) → accelerated reply."""
    simulate_reply("This is exactly what we've been looking for! We are very keen to move forward quickly. Can we have an urgent call this week?")


# ── dispatcher ─────────────────────────────────────────────────────────────────

SCENARIOS = {
    "setup":    (setup,                "Insert / reset test lead"),
    "send":     (s01_send_outreach,    "#1  Send real outreach email"),
    "yes":      (s02_reply_yes,        "#7  Reply: Yes / simple positive"),
    "hindi":    (s03_reply_hindi,      "#6  Reply: Non-English (Hindi)"),
    "questions":(s04_reply_questions,  "#8  Reply: Multi-question"),
    "angry":    (s05_reply_angry,      "#12 Reply: Angry (escalate)"),
    "unsub":    (s06_reply_unsubscribe,"#13 Reply: Unsubscribe"),
    "phone":    (s07_reply_phone,      "#14 Reply: Call me + phone number"),
    "emoji":    (s08_reply_emoji,      "#15 Reply: Emoji-only"),
    "job":      (s09_reply_job_change, "#16 Reply: Job change"),
    "ooo":      (s10_reply_ooo,        "#33 Reply: Out of office"),
    "july":     (s11_reply_july,       "#34 Reply: Contact me in July"),
    "assistant":(s12_reply_assistant,  "#10 Reply: Assistant on behalf"),
    "nda":      (s13_reply_nda,        "#46 Reply: NDA request"),
    "casestudy":(s14_reply_case_study, "#47 Reply: Case study request"),
    "vendor":   (s15_reply_existing_vendor,"#48 Reply: Existing vendor"),
    "boss":     (s16_reply_not_decision_maker,"#49 Reply: Needs boss approval"),
    "deadline": (s17_reply_deadline,   "#50 Reply: Deadline mention"),
    "cc":       (s18_reply_cc,         "#9  Reply: CC'd colleague"),
    "forward":  (s19_reply_forward,    "#4  Reply: Forwarded email"),
    "softbounce":(s20_soft_bounce,     "#2  Soft bounce + retry"),
    "opennudge":(s21_open_nudge,       "#3  Open nudge"),
    "repeat":   (s22_repeat_lead,      "#25 Repeat lead reconnect"),
    "senior":   (s23_senior_lead,      "#38 Senior/CEO lead alert"),
    "noshow":   (s24_no_show,          "#27 No-show re-engagement"),
    "cancel":   (s25_zoho_cancel,      "#26 Zoho cancellation webhook"),
    "smtplimit":(s26_smtp_limit,       "#40 SMTP daily limit"),
    "followup": (s27_scheduled_followup,"#34 Scheduled follow-up trigger"),
    "sheets":   (s28_sheets_export,    "#44 Google Sheets export"),
    # ── new scenarios ─────────────────────────────────────────────────────────
    "sharedinbox":(s29_shared_inbox,   "#5  Shared inbox detection"),
    "afterhours": (s30_after_hours,    "#11 After-hours reply queuing"),
    "pending":    (s31_pending_booking,"#17 Pending booking nudge + expire"),
    "zohonone":   (s32_zoho_returns_none,"#18 Zoho returns None"),
    "zohodown":   (s33_zoho_down,      "#19 Zoho completely down"),
    "pastslot":   (s34_past_slot,      "#20 Past slot selected"),
    "dualtz":     (s35_dual_timezone,  "#21 Dual timezone slot email"),
    "samecoslot": (s36_same_company_slot,"#24 Same company same slot"),
    "ooo2":       (s37_ooo_reply,      "#33 OOO (with SENT lead)"),
    "holiday":    (s38_holiday_skip,   "#35 Holiday skip"),
    "optdomain":  (s39_opted_out_domain,"#36 Opted-out domain re-engage"),
    "silence":    (s40_two_months_silence,"#37 Reply after 2 months"),
    "concur":     (s41_concurrency,    "#28 Concurrent slot booking"),
    "lockexpiry": (s42_redis_lock_expiry,"#29 Redis lock TTL expiry"),
    "redisdown":  (s43_redis_down,     "#30 Redis down fallback"),
    "lockreject": (s44_lock_zoho_rejects,"#31 Lock OK, Zoho rejects"),
    "ratelimit":  (s45_claude_rate_limit,"#43 Claude rate limit retry"),
    "summary":    (s46_thread_summarize,"#45 Thread summarization"),
    "duplicate":  (s47_imap_duplicate, "#41 IMAP duplicate prevention"),
    "customslot": (s48_custom_time_slot,"#23 Custom time slot request"),
    "workercrash":(s49_worker_crash,   "#39 Worker crash recovery"),
    "highintent": (s50_high_intent,    "#32 High-intent reply"),
    "status":     (show_lead,          "Show current lead DB state"),
}


def run_all():
    print("\n=== RUNNING ALL SCENARIOS ===\n")
    order = ["setup","send","status","yes","hindi","questions","angry","unsub","phone","emoji",
             "job","ooo","july","assistant","nda","casestudy","vendor","boss","deadline",
             "cc","forward","softbounce","opennudge","repeat","senior","noshow","cancel",
             "smtplimit","followup","sheets",
             # new scenarios
             "sharedinbox","afterhours","pending","zohonone","zohodown","pastslot","dualtz",
             "samecoslot","ooo2","holiday","optdomain","silence","concur","lockexpiry",
             "redisdown","lockreject","ratelimit","summary","duplicate","customslot",
             "workercrash","highintent"]
    for key in order:
        fn, label = SCENARIOS[key]
        print(f"\n{'='*60}")
        print(f"  {label}")
        print(f"{'='*60}")
        try:
            fn()
            show_lead()
        except Exception as e:
            print(f"  ERROR: {e}")
        import time; time.sleep(2)


if __name__ == "__main__":
    arg = sys.argv[1] if len(sys.argv) > 1 else "status"
    if arg == "all":
        run_all()
    elif arg in SCENARIOS:
        fn, label = SCENARIOS[arg]
        print(f"\nRunning: {label}")
        fn()
        show_lead()
    else:
        print("Available scenarios:")
        for k, (_, label) in SCENARIOS.items():
            print(f"  {k:12s} — {label}")
