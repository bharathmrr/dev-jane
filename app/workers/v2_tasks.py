import asyncio
import csv
import json
import os
import re
from datetime import datetime, timezone, timedelta

from celery import shared_task
from sqlalchemy.future import select
from structlog import get_logger

from app.db.models import LeadV2, LeadStatus, ZohoSlot, SlotStatus, OnboardingRecord
from app.services.availability_v2 import get_slots_for_week
from app.services.email_generator import (
    score_lead,
    extract_slot_from_reply,
    analyze_reply_intent,
)
from app.services.marketing_agent import (
    generate_outreach,
    generate_question_reply,
    generate_pricing_reply,
    generate_not_decision_maker_reply,
    generate_case_study_reply,
    generate_existing_vendor_reply,
    generate_deadline_reply,
    generate_assistant_reply,
    generate_nda_reply,
    generate_multilingual_reply,
    generate_high_intent_nudge,
    generate_pending_booking_nudge,
    generate_no_show_reply,
    generate_scheduled_followup_confirm,
    generate_same_company_slot_reply,
)
from app.services.slot_lock import (
    acquire_slot_lock,
    release_slot_lock,
    record_variant_sent,
    record_variant_reply,
    record_variant_sent_segment,
    record_variant_reply_segment,
    record_send_time_sent,
    record_send_time_reply,
    classify_segment,
)
from app.services.bounce_handler import classify_bounce, is_shared_inbox, is_senior_lead, soft_bounce_retry_delay
from app.services.holiday_calendar import should_send_today
from app.services.email_tracker import smtp_can_send
from app.services.sheets import GoogleSheetsService
from app.services.zoho import ZohoBookingsService
from app.services.email_service import (
    make_book_url,
    make_week_url,
    send_week_selection_email,
    send_v2_slots_email,
    send_v2_reminder_email,
    send_booking_confirmation_to_lead,
    send_organizer_booking_notification,
)
from app.core.pipeline_logger import log_pipeline
from app.workers.runtime import run_async

logger = get_logger(__name__)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _extract_min_hour(body: str) -> int | None:
    """Parse 'after 12', 'afternoon', '2pm onwards' etc. from a reply body.

    Returns the minimum hour (24h int) the lead wants, or None if no preference.
    """
    b = body.lower()
    # "after 12", "after 2pm", "from 3", "post 14", "starting from 2"
    m = re.search(
        r'\b(?:after|post|from|starting\s+from?)\s+(\d{1,2})(?:\s*(?:pm|am))?\b', b
    )
    if m:
        h = int(m.group(1))
        ctx = b[m.start(): m.start() + 18]
        if "pm" in ctx and h != 12:
            h += 12
        elif h <= 6:  # "after 2" without am/pm → assume afternoon
            h += 12
        return min(max(h, 9), 19)

    if any(w in b for w in ["afternoon", "post lunch", "post-lunch", "after lunch"]):
        return 13
    if any(w in b for w in ["evening", "evenings"]):
        return 17
    # Any bare "pm" reference (not "9am" context) → noon minimum
    if re.search(r'\bpm\b', b) and not re.search(r'\d\s*am\b', b):
        return 12
    return None


def _match_slot_simple(body: str, slot_strings: list[str]) -> str | None:
    """Pick the lead's chosen slot ONLY when they explicitly name it.

    Never auto-books based on vague words like 'ok', 'sure', 'great'.
    Those go to the AI intent layer which may show slots again or confirm.
    Only books when:
      1. Lead mentions a slot by ordinal (first, 1st, option 2...)
      2. Lead mentions the exact day + time of one of the offered slots
    """
    body_lower = body.lower()

    # Ordinal selection — "the first one", "option 2", "slot 3"
    ordinals = [
        (["first one", "1st one", "option 1", "number 1", "slot 1", "first slot", "1st slot"], 0),
        (["second one", "2nd one", "option 2", "number 2", "slot 2", "second slot", "2nd slot"], 1),
        (["third one", "3rd one", "option 3", "number 3", "slot 3", "third slot", "3rd slot"], 2),
        (["fourth one", "4th one", "option 4", "number 4", "slot 4", "fourth slot", "4th slot"], 3),
        (["fifth one", "5th one", "option 5", "number 5", "slot 5", "fifth slot", "5th slot"], 4),
        (["sixth one", "6th one", "option 6", "number 6", "slot 6", "sixth slot", "6th slot"], 5),
    ]
    for keywords, idx in ordinals:
        if idx < len(slot_strings) and any(kw in body_lower for kw in keywords):
            return slot_strings[idx]

    # Exact slot match — day name AND time both present in reply
    for slot_str in slot_strings:
        parts = slot_str.lower().replace(",", "").split()
        day_name = parts[0] if parts else ""
        # time_part e.g. "09:00 am"
        time_part = " ".join(parts[-2:]) if len(parts) >= 2 else ""
        if day_name and time_part and day_name in body_lower and time_part in body_lower:
            return slot_str

    return None


def _load_slot_infos(lead) -> tuple[list[dict], list[str]]:
    """Parse offered_slots_json — handles both old (list of str) and new (list of dict) formats."""
    if not lead.offered_slots_json:
        return [], []
    raw = json.loads(lead.offered_slots_json)
    if not raw:
        return [], []
    if isinstance(raw[0], dict):
        return raw, [s["display"] for s in raw]
    wrapped = [{"display": s, "date_str": None, "time_str": None, "iso": None} for s in raw]
    return wrapped, raw


# ---------------------------------------------------------------------------
# Sync Google Sheet → leads_v2
# ---------------------------------------------------------------------------

async def _sync_google_sheet(db):
    sheets_service = GoogleSheetsService()
    records = sheets_service.fetch_new_leads()
    if not records:
        logger.warning("sheet_sync_no_records")
        return "No records found or sheets not configured."

    logger.info("sheet_sync_fetched", total_rows=len(records), sample_keys=list(records[0].keys()) if records else [])

    added = skipped_existing = skipped_missing = 0
    for row in records:
        email = (
            row.get("email") or row.get("Email")
            or row.get("Business Email") or row.get("business_email") or ""
        ).strip()
        contact_name = (
            row.get("contact person") or row.get("Contact person")
            or row.get("contact_person") or row.get("Contact Name")
            or row.get("Person Name") or row.get("contact_name")
            or row.get("Full Name") or row.get("name") or ""
        ).strip() or None
        business_name = (
            row.get("name_of_the_company") or row.get("name of the company")
            or row.get("Name of the Company") or row.get("Name of Company")
            or row.get("name_of_company") or row.get("Company")
            or row.get("business_name") or row.get("Business Name") or ""
        ).strip() or contact_name
        designation = (row.get("Designation") or row.get("designation") or "").strip()
        raw_summary = (row.get("Summary") or row.get("summary") or "").strip()
        summary = raw_summary
        if designation and designation not in summary:
            summary = f"{designation}. {summary}".strip(" .")
        if not summary:
            summary = None
        phone = (
            row.get("phone_number") or row.get("phone number")
            or row.get("Phone") or row.get("phone") or ""
        ).strip() or None
        if not email or not re.match(r"^[^@]+@[^@]+\.[^@]+$", email):
            logger.warning("sheet_row_invalid_email", email=email)
            skipped_missing += 1
            continue
        if not business_name:
            logger.warning("sheet_row_missing_fields", row_keys=list(row.keys()), email=email, business=business_name)
            skipped_missing += 1
            continue
        location = (row.get("Location") or row.get("location") or row.get("City") or row.get("city") or "").strip() or None

        existing = (
            await db.execute(select(LeadV2).where(LeadV2.email == email))
        ).scalar_one_or_none()
        if not existing:
            db.add(LeadV2(
                business_name=business_name,
                contact_name=contact_name,
                email=email,
                summary=summary,
                designation=designation or None,
                location=location,
                phone_number=phone,
                status=LeadStatus.NEW,
            ))
            logger.info("added_lead_from_sheet", email=email, contact=contact_name, summary=summary)
            added += 1
        else:
            # Back-fill fields that were missing when the lead was first synced
            # (e.g. phone/company added to the sheet later, or synced before the
            # column-mapping fix) and push them to CRM.
            backfill: dict = {}
            if phone and not existing.phone_number:
                existing.phone_number = phone
                backfill["Phone"] = phone
            if contact_name and not existing.contact_name:
                existing.contact_name = contact_name
            if designation and not existing.designation:
                existing.designation = designation
            if location and not existing.location:
                existing.location = location
            if business_name and (
                not existing.business_name
                or existing.business_name == (existing.contact_name or "")
            ) and business_name != existing.business_name:
                existing.business_name = business_name
                backfill["Company"] = business_name
            if backfill:
                try:
                    from app.services.zoho_crm import find_lead_by_email, update_lead
                    _cid = find_lead_by_email(email)
                    if _cid:
                        update_lead(_cid, backfill)
                    logger.info("sheet_backfill", email=email, fields=list(backfill.keys()))
                except Exception as exc:
                    logger.warning("sheet_backfill_crm_failed", email=email, error=str(exc))
            skipped_existing += 1
            continue

        # Push new lead to Zoho CRM with all available fields
        try:
            from app.services.zoho_crm import upsert_lead, add_note
            crm_id = upsert_lead(
                email=email,
                contact_name=contact_name or business_name,
                company_name=business_name,
                phone=phone or "",
                summary=summary or "",
                designation=designation or "",
                city=location or "",
            )
            if crm_id:
                add_note(crm_id, f"Lead Imported — {business_name}",
                         f"Source: Google Sheet\nEmail: {email}\nContact: {contact_name or '—'}"
                         f"\nPhone: {phone or '—'}\nDesignation: {designation or '—'}"
                         f"\nSummary: {summary or '—'}")
        except Exception as exc:
            logger.warning("crm_sheet_sync_failed", email=email, error=str(exc))

    logger.info("sheet_sync_done", added=added, already_in_db=skipped_existing, missing_fields=skipped_missing)
    return f"Sync complete: +{added} new, {skipped_existing} existing, {skipped_missing} missing fields"


@shared_task
def sync_google_sheet():
    logger.info("running_sync_google_sheet")
    return run_async(_sync_google_sheet)


# ---------------------------------------------------------------------------
# Sync leads.csv → leads_v2
# ---------------------------------------------------------------------------

CSV_PATH = os.environ.get("LEADS_CSV_PATH", "/app/leads.csv")


async def _sync_csv_leads(db):
    if not os.path.exists(CSV_PATH):
        logger.warning("leads_csv_not_found", path=CSV_PATH)
        return "leads.csv not found"

    added = skipped_existing = skipped_missing = 0
    with open(CSV_PATH, newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        rows = list(reader)

    logger.info("csv_sync_fetched", total_rows=len(rows), path=CSV_PATH)

    for row in rows:
        email = (
            row.get("email") or row.get("Email")
            or row.get("Business Email") or row.get("business_email") or ""
        ).strip()
        contact_name = (
            row.get("contact_name") or row.get("Contact Name")
            or row.get("Contact person") or row.get("contact person")
            or row.get("Person Name") or row.get("Full Name") or row.get("name") or ""
        ).strip() or None
        business_name = (
            row.get("business_name") or row.get("Business Name")
            or row.get("Company") or row.get("Name of Company")
            or row.get("name of the company") or row.get("Name of the Company") or ""
        ).strip() or contact_name
        designation = (row.get("designation") or row.get("Designation") or "").strip()
        raw_summary = (row.get("summary") or row.get("Summary") or "").strip()
        summary = raw_summary
        if designation and designation not in summary:
            summary = f"{designation}. {summary}".strip(" .")
        if not summary:
            summary = None

        # Basic email validation: must have @ and a dot after the @
        if not email or not re.match(r"^[^@]+@[^@]+\.[^@]+$", email):
            logger.warning("csv_row_invalid_email", email=email)
            skipped_missing += 1
            continue

        if not business_name:
            logger.warning("csv_row_missing_fields", email=email, business=business_name)
            skipped_missing += 1
            continue

        location = (row.get("location") or row.get("Location") or row.get("City") or row.get("city") or "").strip() or None

        existing = (
            await db.execute(select(LeadV2).where(LeadV2.email == email))
        ).scalar_one_or_none()
        if not existing:
            new_lead = LeadV2(
                business_name=business_name,
                contact_name=contact_name,
                email=email,
                summary=summary,
                designation=designation or None,
                location=location,
                status=LeadStatus.NEW,
            )
            db.add(new_lead)
            await db.flush()
            logger.info("added_lead_from_csv", email=email, contact=contact_name)
            added += 1
            # Push to Zoho CRM with all available fields
            try:
                from app.services.zoho_crm import upsert_lead, add_note
                phone_csv = (row.get("phone") or row.get("Phone") or row.get("mobile") or "").strip()
                crm_id = upsert_lead(
                    email=email,
                    contact_name=contact_name or business_name,
                    company_name=business_name,
                    phone=phone_csv,
                    summary=summary or "",
                    designation=designation or "",
                    city=location or "",
                )
                if crm_id:
                    add_note(crm_id, f"Lead Imported — {business_name}",
                             f"Source: CSV\nEmail: {email}\nContact: {contact_name or '—'}"
                             f"\nPhone: {phone_csv or '—'}\nDesignation: {designation or '—'}"
                             f"\nSummary: {summary or '—'}")
            except Exception as _crm_err:
                logger.warning("csv_crm_sync_failed", email=email, error=str(_crm_err))

        else:
            skipped_existing += 1

    logger.info("csv_sync_done", added=added, already_in_db=skipped_existing, missing_fields=skipped_missing)
    return f"CSV sync: +{added} new, {skipped_existing} existing, {skipped_missing} missing fields"


@shared_task
def sync_csv_leads():
    logger.info("running_sync_csv_leads")
    return run_async(_sync_csv_leads)


# ---------------------------------------------------------------------------
# Sync Zoho slots → zoho_slots (reference table, not used for availability)
# ---------------------------------------------------------------------------

async def _sync_zoho_slots(db):
    from datetime import datetime as _dt
    zoho_service = ZohoBookingsService()
    today = _dt.now().strftime("%d-%b-%Y")
    slots_data = zoho_service.fetch_available_slots(today)
    if not slots_data:
        return "No slots found or zoho not configured."

    for slot_item in slots_data:
        if isinstance(slot_item, dict):
            raw_id = str(slot_item.get("id") or slot_item.get("time", slot_item))
            zoho_id = f"z_slot_{raw_id}"
            try:
                date_val = slot_item.get("date", "")
                time_val = slot_item.get("time", "")
                slot_time = _dt.strptime(
                    f"{date_val} {time_val}", "%Y-%m-%d %H:%M"
                ).replace(tzinfo=timezone.utc)
            except (ValueError, TypeError):
                slot_time = _dt.now(timezone.utc)
        else:
            slot_str = str(slot_item)
            zoho_id = f"z_slot_{slot_str}"
            try:
                slot_time = _dt.strptime(slot_str, "%Y-%m-%d %H:%M:%S").replace(
                    tzinfo=timezone.utc
                )
            except ValueError:
                slot_time = _dt.now(timezone.utc)

        existing = (
            await db.execute(select(ZohoSlot).where(ZohoSlot.zoho_slot_id == zoho_id))
        ).scalar_one_or_none()
        if not existing:
            db.add(ZohoSlot(
                zoho_slot_id=zoho_id, slot_time=slot_time, status=SlotStatus.AVAILABLE
            ))
    return "Slots synced"


@shared_task
def sync_zoho_slots():
    logger.info("running_sync_zoho_slots")
    return run_async(_sync_zoho_slots)


# ---------------------------------------------------------------------------
# Process new leads → send Stage 1 week-selection email
# ---------------------------------------------------------------------------

async def _process_new_leads(db):
    # ── Holiday check (#35): don't send on holidays ───────────────────────────
    if not should_send_today():
        logger.info("process_new_leads_skipped_holiday")
        return "Skipped — today is a holiday"

    # ── SMTP throttle check (#40) ─────────────────────────────────────────────
    if not smtp_can_send():
        logger.warning("process_new_leads_smtp_limit_reached")
        return "SMTP daily limit reached — deferred to tomorrow"

    leads = (
        await db.execute(
            select(LeadV2)
            .where(LeadV2.status == LeadStatus.NEW)
            .with_for_update(skip_locked=True)
        )
    ).scalars().all()
    if not leads:
        return "No new leads."

    ob_lead_ids = {
        row[0] for row in (await db.execute(select(OnboardingRecord.lead_id))).all()
    }

    sent = 0
    skipped = 0
    for lead in leads:
        if lead.id in ob_lead_ids:
            logger.info("lead_skipped_in_onboarding", email=lead.email)
            skipped += 1
            continue

        # ── Shared inbox detection (#5) ───────────────────────────────────────
        if is_shared_inbox(lead.email):
            lead.is_shared_inbox = True
            lead.contact_name = None   # personalise with company name only

        # ── Lead scoring ──────────────────────────────────────────────────────
        lead_score = score_lead(
            contact_name=lead.contact_name,
            business_name=lead.business_name,
            summary=lead.summary,
        )
        if lead_score < 2:
            logger.info("lead_skipped_low_score", email=lead.email, score=lead_score)
            skipped += 1
            continue

        logger.info("lead_qualified", email=lead.email, score=lead_score)

        # ── Repeat lead detection (#25): personalise if they've booked before ──
        if lead.booked_at is not None and not lead.is_repeat_lead:
            lead.is_repeat_lead = True

        # ── Senior lead escalation (#38) ──────────────────────────────────────
        if is_senior_lead(lead.designation):
            lead.priority_flag = True
            lead.escalated_to_human = True
            from app.services.email_service import send_escalation_alert
            send_escalation_alert(
                lead_name=lead.contact_name or lead.business_name,
                lead_email=lead.email,
                designation=lead.designation,
                reason=f"Senior lead detected ({lead.designation}) — ready to receive initial email",
            )
            logger.info("senior_lead_escalated", email=lead.email, designation=lead.designation)

        # ── Send-time variant assignment (#A/B) ───────────────────────────────
        import random as _random
        time_variant = _random.choice(["morning", "afternoon", "evening"])
        lead.send_time_variant = time_variant

        # Commit SENT status immediately — prevents concurrent workers sending the same email
        lead.status = LeadStatus.SENT
        lead.sent_at = datetime.now(timezone.utc)
        lead.follow_up_count = 0
        lead.reminder_sent_at = None
        await db.commit()

        outreach = generate_outreach(
            business_name=lead.business_name,
            contact_name=lead.contact_name,
            designation=getattr(lead, "designation", None),
            location=getattr(lead, "location", None),
            summary=lead.summary,
            is_repeat_lead=getattr(lead, "is_repeat_lead", False),
        )
        body_text = outreach["body"]
        subject = outreach["subject"]
        lead.ab_variant = outreach["variant"]
        lead.ab_subject_variant = outreach["subject_variant"]

        # ── Segment-aware A/B tracking (#A/B Step 5) ─────────────────────────
        seg = classify_segment(lead.designation, lead.location)
        record_variant_sent_segment(outreach["variant"], seg)
        record_send_time_sent(time_variant)

        this_url = make_week_url(str(lead.id), "this")
        next_url = make_week_url(str(lead.id), "next")
        success = send_week_selection_email(
            lead.email, lead.business_name,
            this_week_url=this_url,
            next_week_url=next_url,
            body_text=body_text,
            contact_name=lead.contact_name,
            subject_override=subject,
            lead_id=str(lead.id),
        )
        if success:
            logger.info("week_selection_email_sent", email=lead.email, score=lead_score,
                        variant=outreach["variant"], subject_variant=outreach["subject_variant"],
                        time_variant=time_variant, segment=seg)
            sent += 1
            # Update CRM — outreach email sent + sync phone if available
            try:
                from app.services.zoho_crm import update_lead, add_note, find_lead_by_email
                crm_id = find_lead_by_email(lead.email)
                if crm_id:
                    crm_fields: dict = {"Lead_Status": "Contacted"}
                    if lead.phone_number:
                        crm_fields["Phone"] = lead.phone_number
                    update_lead(crm_id, crm_fields)
                    add_note(crm_id, f"Outreach Email Sent — {lead.business_name}",
                             f"Subject: {subject}\nSlot selection email sent.\n"
                             f"Variant: {outreach['variant']} | Score: {lead_score}"
                             + (f"\nPhone: {lead.phone_number}" if lead.phone_number else ""))
            except Exception as _crm_e:
                logger.warning("crm_email_sent_sync_failed", email=lead.email, error=str(_crm_e))
        else:
            lead.status = LeadStatus.NEW
            lead.sent_at = None
            logger.warning("week_selection_email_failed_reverted", email=lead.email)

    return f"Stage 1 emails sent to {sent}/{len(leads)} leads. Skipped: {skipped}."


@shared_task
def process_new_leads():
    logger.info("running_process_new_leads")
    return run_async(_process_new_leads)


# ---------------------------------------------------------------------------
# Follow-up reminders — Stage 1 leads that haven't clicked yet
# First reminder: 35 min after initial email
# Second reminder: 25 min after first (≈60 min total), max 2 follow-ups
# ---------------------------------------------------------------------------

async def _send_v2_reminders(db) -> str:
    now = datetime.now(timezone.utc)
    first_cutoff = now - timedelta(minutes=35)
    second_cutoff = now - timedelta(minutes=25)

    result = await db.execute(
        select(LeadV2).where(
            LeadV2.status == LeadStatus.SENT,
            LeadV2.offered_slots_json.is_(None),  # Stage 1 only (no week chosen yet)
            LeadV2.follow_up_count < 2,
        )
    )
    leads = result.scalars().all()
    if not leads:
        return "No leads needing reminders."

    # Skip leads already in onboarding
    ob_lead_ids = {
        row[0] for row in (await db.execute(select(OnboardingRecord.lead_id))).all()
    }

    sent_count = 0
    for lead in leads:
        if not lead.sent_at:
            continue
        if lead.id in ob_lead_ids:
            continue

        if lead.follow_up_count == 0 and lead.sent_at <= first_cutoff:
            this_url = make_week_url(str(lead.id), "this")
            next_url = make_week_url(str(lead.id), "next")
            ok = send_v2_reminder_email(
                lead.email, lead.business_name, this_url, next_url,
                follow_up_count=1,
                contact_name=lead.contact_name,
            )
            if ok:
                lead.follow_up_count = 1
                lead.reminder_sent_at = now
                sent_count += 1
                logger.info("reminder_1_sent", email=lead.email)
                log_pipeline("EMAIL_REMINDER", company=lead.business_name, email=lead.email,
                             detail="Follow-up #1 sent")

        elif (
            lead.follow_up_count == 1
            and lead.reminder_sent_at is not None
            and lead.reminder_sent_at <= second_cutoff
        ):
            this_url = make_week_url(str(lead.id), "this")
            next_url = make_week_url(str(lead.id), "next")
            ok = send_v2_reminder_email(
                lead.email, lead.business_name, this_url, next_url,
                follow_up_count=2,
                contact_name=lead.contact_name,
            )
            if ok:
                lead.follow_up_count = 2
                sent_count += 1
                logger.info("reminder_2_sent", email=lead.email)
                log_pipeline("EMAIL_REMINDER", company=lead.business_name, email=lead.email,
                             detail="Follow-up #2 sent")

    return f"Reminders sent: {sent_count}"


@shared_task
def send_v2_reminders():
    logger.info("running_send_v2_reminders")
    return run_async(_send_v2_reminders)


# ---------------------------------------------------------------------------
# Booking — create Zoho booking, update lead, sync ZohoSlot, send emails
# ---------------------------------------------------------------------------

async def _do_booking(db, lead, date_str: str, time_str: str, display_str: str) -> str:
    from zoneinfo import ZoneInfo
    from app.services.availability_v2 import _get_booked_slots, _get_held_slots
    IST = ZoneInfo("Asia/Kolkata")

    async def _send_alternatives(reason: str, apology_text: str | None = None) -> str:
        logger.warning(reason, email=lead.email, slot=display_str)
        from app.services.availability_v2 import get_available_slots
        # Try this week → next week → broader 14-day search
        alt_slots = await get_slots_for_week(db, "this", 6)
        if not alt_slots:
            alt_slots = await get_slots_for_week(db, "next", 6)
        if not alt_slots:
            alt_slots = await get_available_slots(db, n=6, lookahead_days=14)
        if alt_slots:
            slot_strings = [s["display"] for s in alt_slots]
            book_urls = [make_book_url(str(lead.id), i) for i in range(len(alt_slots))]
            lead.offered_slots_json = json.dumps(alt_slots)
            lead.replied_at = datetime.now(timezone.utc)
            # Use apology text for staff-on-leave (#22), generic body otherwise
            body_text = apology_text
            send_v2_slots_email(
                lead.email, lead.business_name, slot_strings,
                book_urls=book_urls,
                body_text=body_text,
                contact_name=lead.contact_name,
            )
        else:
            _send_graceful_no_slots(lead)
            lead.replied_at = datetime.now(timezone.utc)
        return f"{reason} — sent {len(alt_slots) if alt_slots else 0} alternatives"

    # Build slot_dt_ist once — used by both guard checks below
    slot_dt_ist = None
    try:
        hour = int(time_str.split(":")[0])
        slot_date = datetime.strptime(date_str, "%d-%b-%Y").date()
        slot_dt_ist = datetime(slot_date.year, slot_date.month, slot_date.day, hour, 0, tzinfo=IST)
    except Exception as exc:
        logger.warning("booking_parse_slot_dt_error", error=str(exc))

    # ── Check 1: Hard DB exclusion (already confirmed-booked in our DB) ────────
    try:
        booked_iso, booked_display = await _get_booked_slots(db)
        if slot_dt_ist and (slot_dt_ist.isoformat() in booked_iso or display_str in booked_display):
            return await _send_alternatives("booking_rejected_hard_booked_in_db")
    except Exception as exc:
        logger.warning("booking_precheck_error", error=str(exc))

    # ── Check 2: Soft DB exclusion (held by another lead in last 2 h) ──────────
    try:
        held = await _get_held_slots(db, exclude_lead_id=lead.id)
        if slot_dt_ist and slot_dt_ist.isoformat() in held:
            return await _send_alternatives("booking_rejected_held_by_another_lead")
    except Exception as exc:
        logger.warning("booking_held_check_error", error=str(exc))

    # ── Check 3: Same-company colleague already has this slot (#24) ────────────
    try:
        lead_domain = lead.email.split("@")[1].lower() if "@" in lead.email else ""
        if lead_domain and lead_domain not in ("gmail.com", "yahoo.com", "hotmail.com",
                                                "outlook.com", "rediffmail.com"):
            colleague = (await db.execute(
                select(LeadV2).where(
                    LeadV2.status == LeadStatus.BOOKED,
                    LeadV2.selected_slot == display_str,
                    LeadV2.email.like(f"%@{lead_domain}"),
                    LeadV2.email != lead.email,
                )
            )).scalar_one_or_none()
            if colleague:
                colleague_name = (colleague.contact_name or colleague.email).split()[0]
                reply_text = generate_same_company_slot_reply(
                    lead.contact_name, lead.business_name,
                    colleague_name, display_str,
                )
                from app.services.availability_v2 import get_available_slots
                alt_slots = await get_slots_for_week(db, "this", 6) or await get_slots_for_week(db, "next", 6)
                if alt_slots:
                    slot_strings = [s["display"] for s in alt_slots]
                    book_urls = [make_book_url(str(lead.id), i) for i in range(len(alt_slots))]
                    lead.offered_slots_json = json.dumps(alt_slots)
                    lead.replied_at = datetime.now(timezone.utc)
                    send_v2_slots_email(
                        lead.email, lead.business_name, slot_strings,
                        book_urls=book_urls, body_text=reply_text,
                        contact_name=lead.contact_name,
                    )
                logger.info("same_company_slot_conflict", email=lead.email, colleague=colleague.email)
                return f"Same company slot conflict — sent different options (colleague: {colleague_name})"
    except Exception as exc:
        logger.warning("same_company_check_error", error=str(exc))

    # ── Check 4: Redis atomic slot lock (prevents 100-concurrent-click race) ──
    slot_locked = acquire_slot_lock(date_str, time_str, lead.email)
    if not slot_locked:
        return await _send_alternatives("redis_slot_lock_taken_concurrent_booking")

    # ── Check 5: Zoho real-time booking (source of truth) ──────────────────────
    zoho = ZohoBookingsService()
    try:
        booking_id, meeting_link = await asyncio.to_thread(
            zoho.create_booking,
            name=lead.contact_name or lead.business_name,
            email=lead.email,
            date_str=date_str,
            time_str=time_str,
        )
    except Exception as zoho_exc:
        # Zoho API completely unreachable (#19)
        release_slot_lock(date_str, time_str)
        from app.services.email_service import send_zoho_down_alert
        send_zoho_down_alert(lead.contact_name or lead.business_name, lead.email)
        logger.error("zoho_api_down", email=lead.email, error=str(zoho_exc))
        return "Zoho API down — lead notified to reply with preferred time"

    if booking_id is None:
        release_slot_lock(date_str, time_str)
        return await _send_alternatives("zoho_booking_rejected_slot_taken")

    lead.status = LeadStatus.BOOKED
    lead.replied_at = datetime.now(timezone.utc)
    lead.booked_at = datetime.now(timezone.utc)
    lead.selected_slot = display_str
    lead.booking_id = booking_id
    lead.zoho_meeting_link = meeting_link

    # Sync ZohoSlot row in DB
    try:
        hour = int(time_str.split(":")[0])
        slot_date = datetime.strptime(date_str, "%d-%b-%Y").date()
        slot_dt_ist = datetime(slot_date.year, slot_date.month, slot_date.day, hour, 0, tzinfo=IST)
        slot_dt_utc = slot_dt_ist.astimezone(timezone.utc)
        slot_row = (await db.execute(
            select(ZohoSlot).where(ZohoSlot.slot_time == slot_dt_utc)
        )).scalar_one_or_none()
        if slot_row:
            slot_row.status = SlotStatus.BOOKED
            slot_row.booked_email = lead.email
    except Exception as exc:
        logger.warning("zoho_slot_db_sync_failed", error=str(exc))

    send_booking_confirmation_to_lead(
        lead.email, lead.business_name, display_str,
        meeting_link=meeting_link, contact_name=lead.contact_name,
    )
    send_organizer_booking_notification(
        lead.business_name, lead.email, display_str, meeting_link=meeting_link
    )
    logger.info("booking_complete", email=lead.email, booking_id=booking_id)
    log_pipeline("BOOKING_COMPLETED", company=lead.business_name, email=lead.email,
                 detail=f"Slot: {display_str} | Booking ID: {booking_id}")

    # Email team with one-click "Start Onboarding" link (valid 30 days)
    try:
        from app.services.onboarding_email import notify_team_booking_start_onboarding
        notify_team_booking_start_onboarding(
            lead_name=lead.contact_name or lead.business_name,
            company_name=lead.business_name,
            lead_email=lead.email,
            lead_id=str(lead.id),
            slot_display=display_str,
        )
    except Exception as exc:
        logger.warning("start_onboarding_email_failed", email=lead.email, error=str(exc))

    # Sync booking to Zoho CRM — update Lead status and add full booking note
    try:
        from app.services.zoho_crm import find_lead_by_email, update_lead, add_note
        _crm_id = find_lead_by_email(lead.email)
        if _crm_id:
            _booking_upd: dict = {
                "Lead_Status": "VC Booking Stage",
                "Description": f"Meeting booked: {display_str}\nBooking ID: {booking_id}",
            }
            if lead.phone_number:
                _booking_upd["Phone"] = lead.phone_number
            update_lead(_crm_id, _booking_upd)
            add_note(_crm_id, f"Meeting Booked — {lead.business_name}",
                     f"Slot: {display_str}\nBooking ID: {booking_id}\nMeeting Link: {meeting_link or '—'}\n"
                     f"Contact: {lead.contact_name or '—'} | Company: {lead.business_name}")
        else:
            # Lead not in CRM yet — create it now
            from app.services.zoho_crm import upsert_lead
            upsert_lead(
                email=lead.email,
                contact_name=lead.contact_name or lead.business_name,
                company_name=lead.business_name,
                phone=lead.phone_number or "",
                summary=f"Meeting booked: {display_str}",
            )
    except Exception as exc:
        logger.warning("crm_booking_sync_failed", email=lead.email, error=str(exc))

    return f"Booked (ID: {booking_id})"


async def _try_specific_date_reply(db, lead, body: str) -> str | None:
    """Extract specific date/time from reply text. Returns result string or None."""
    from datetime import date as _date
    from zoneinfo import ZoneInfo

    IST = ZoneInfo("Asia/Kolkata")
    SLOT_START, SLOT_END = 9, 19

    today_str = datetime.now(timezone.utc).strftime("%A, %d %B %Y")
    extracted = extract_slot_from_reply(body, today_str)
    if not extracted:
        return None

    try:
        parsed_date = _date.fromisoformat(extracted["date"])
        time_str = extracted.get("time", "")
        if not time_str:
            return None
        requested_hour = int(time_str.split(":")[0])
        date_str = parsed_date.strftime("%d-%b-%Y")
    except (KeyError, ValueError, TypeError):
        return None

    zoho = ZohoBookingsService()
    available_items = await asyncio.to_thread(zoho.fetch_available_slots, date_str)
    if not available_items:
        return None

    from app.services.availability_v2 import _get_booked_slots, _parse_zoho_slot_hour
    booked_iso, booked_display = await _get_booked_slots(db)

    available_hours: set[int] = set()
    for item in available_items:
        try:
            h = _parse_zoho_slot_hour(item)  # handles "05:00 PM" → 17 correctly
            if h is None:
                continue
            slot_dt = datetime(parsed_date.year, parsed_date.month, parsed_date.day, h, 0, tzinfo=IST)
            if slot_dt.isoformat() in booked_iso or slot_dt.strftime("%A, %b %d at %I:%M %p") in booked_display:
                continue
            available_hours.add(h)
        except Exception:
            pass

    if requested_hour in available_hours:
        display = datetime(
            parsed_date.year, parsed_date.month, parsed_date.day,
            requested_hour, 0, tzinfo=IST,
        ).strftime("%A, %b %d at %I:%M %p")
        logger.info("specific_date_exact_match", email=lead.email, slot=display)
        return await _do_booking(db, lead, date_str, time_str, display)

    # Requested time taken — send available alternatives for that day
    alt_slots = []
    for hour in sorted(available_hours):
        if SLOT_START <= hour <= SLOT_END:
            slot_dt = datetime(parsed_date.year, parsed_date.month, parsed_date.day, hour, 0, tzinfo=IST)
            alt_slots.append({
                "display": slot_dt.strftime("%A, %b %d at %I:%M %p"),
                "date_str": date_str,
                "time_str": f"{hour:02d}:00",
                "iso": slot_dt.isoformat(),
            })

    if not alt_slots:
        return None

    slot_strings = [s["display"] for s in alt_slots]
    book_urls = [make_book_url(str(lead.id), i) for i in range(len(alt_slots))]
    lead.offered_slots_json = json.dumps(alt_slots)
    lead.replied_at = datetime.now(timezone.utc)

    send_v2_slots_email(
        lead.email, lead.business_name, slot_strings,
        book_urls=book_urls,
        body_text=None,
        contact_name=lead.contact_name,
    )
    logger.info("specific_date_alternatives_sent", email=lead.email, date=date_str, count=len(alt_slots))
    return f"Sent {len(alt_slots)} slots for {date_str}"


# ---------------------------------------------------------------------------
# Process reply → stage routing → booking
# ---------------------------------------------------------------------------

def _send_graceful_no_slots(lead) -> None:
    """Send a polite 'when are you available?' email when no slots can be offered right now."""
    from app.services.email_service import _send_html_email, _signature

    greeting_name = (lead.contact_name or lead.business_name or "").split()[0].capitalize()
    subject = f"Re: Partnership Opportunity — Jane Aerospace"

    body_lines = [
        f"Hi {greeting_name},",
        "",
        "Thank you so much for your response — I truly appreciate you taking the time to get back to me.",
        "I wanted to reach out personally to let you know that our current time slots are being finalised and I want to make sure I find a time that works perfectly for you rather than offering something that may not be convenient.",
        "Could you please let me know your general availability? Even a rough idea — such as which days of the week work best for you, or whether mornings or afternoons are more suitable — would be very helpful.",
        "I will then personally check our schedule and come back to you with a confirmed time straight away. I want to make sure this conversation is as convenient as possible for you.",
        "Please feel free to reply directly to this email with your preferred times and I will take care of the rest.",
        "Looking forward to hearing from you and to a great conversation.",
    ]

    paras_html = "".join(
        f'<p style="margin:0 0 18px 0;font-family:Arial,sans-serif;font-size:15px;color:#111111;line-height:1.75;">{p}</p>'
        for p in body_lines if p != ""
    )
    html = f"""<!DOCTYPE html>
<html>
<head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"></head>
<body style="margin:0;padding:0;background:#ffffff;">
  <div style="max-width:600px;margin:40px auto;padding:0 24px 60px;">
    {paras_html}
    {_signature()}
  </div>
</body>
</html>"""

    _send_html_email(lead.email, subject, html)
    logger.info("graceful_no_slots_email_sent", email=lead.email)


async def _process_reply_v2(db, reply: dict) -> str:
    from zoneinfo import ZoneInfo
    IST = ZoneInfo("Asia/Kolkata")

    from_addr = reply.get("from_addr")
    body = reply.get("body")
    if not from_addr or not body:
        return "Missing email or body"

    # Look for SENT or REPLIED lead (REPLIED = has responded but not yet booked)
    lead = (
        await db.execute(
            select(LeadV2).where(
                LeadV2.email == from_addr,
                LeadV2.status.in_([LeadStatus.SENT, LeadStatus.REPLIED]),
            )
        )
    ).scalar_one_or_none()

    # If BOOKED and the meeting has already passed, reset to SENT for rescheduling
    if not lead:
        booked_lead = (
            await db.execute(
                select(LeadV2).where(
                    LeadV2.email == from_addr,
                    LeadV2.status == LeadStatus.BOOKED,
                )
            )
        ).scalar_one_or_none()
        if booked_lead:
            slot_passed = False
            if booked_lead.selected_slot:
                try:
                    import dateutil.parser
                    slot_dt = dateutil.parser.parse(booked_lead.selected_slot)
                    if slot_dt.tzinfo is None:
                        slot_dt = slot_dt.replace(tzinfo=IST)
                    slot_passed = slot_dt < datetime.now(IST)
                except Exception:
                    slot_passed = (
                        booked_lead.booked_at is not None
                        and (datetime.now(timezone.utc) - booked_lead.booked_at).total_seconds() > 86400
                    )
            if slot_passed:
                booked_lead.status = LeadStatus.SENT
                booked_lead.offered_slots_json = None
                booked_lead.follow_up_count = 0
                booked_lead.reminder_sent_at = None
                lead = booked_lead
                logger.info("booked_lead_reset_for_reschedule", email=from_addr)

    if not lead:
        logger.info("no_matching_sent_lead", email=from_addr)
        return "No matching lead"

    # Skip leads in onboarding — their emails are KYC/NDA replies, not booking replies
    ob_exists = (await db.execute(
        select(OnboardingRecord.lead_id).where(OnboardingRecord.lead_id == lead.id)
    )).scalar_one_or_none()
    if ob_exists:
        logger.info("reply_skipped_lead_in_onboarding", email=from_addr)
        return "Lead is in onboarding — reply ignored for booking"

    # Skip opted-out leads silently
    if getattr(lead, "opted_out", False):
        logger.info("reply_skipped_opted_out", email=from_addr)
        return "Lead opted out — reply ignored"

    # ── Opted-out domain check (#36): same company, new email address ─────────
    try:
        lead_domain = from_addr.split("@")[1].lower() if "@" in from_addr else ""
        if lead_domain and lead_domain not in ("gmail.com", "yahoo.com", "hotmail.com",
                                                "outlook.com", "rediffmail.com"):
            opted_out_same_domain = (await db.execute(
                select(LeadV2).where(
                    LeadV2.opted_out == True,
                    LeadV2.email.like(f"%@{lead_domain}"),
                    LeadV2.email != from_addr,
                )
            )).scalar_one_or_none()
            if opted_out_same_domain:
                lead.escalated_to_human = True
                logger.warning("opted_out_domain_re_engagement",
                               email=from_addr, domain=lead_domain,
                               opted_out_email=opted_out_same_domain.email)
                return "Same domain as opted-out lead — flagged for human decision"
    except Exception:
        pass

    # ── Re-engagement personalisation (#37): reply after 2+ months ───────────
    if lead.replied_at is not None:
        months_silent = (datetime.now(timezone.utc) - lead.replied_at).days / 30
        if months_silent >= 2:
            lead.is_repeat_lead = True
            logger.info("re_engagement_detected", email=from_addr, months_silent=round(months_silent, 1))

    # ── After-hours queuing (#11): queue reply if outside 9am-9pm IST ────────
    from app.services.email_service import is_after_hours
    if is_after_hours():
        existing_pending = lead.pending_reply_json
        pending_list = json.loads(existing_pending) if existing_pending else []
        pending_list.append({"body": body, "received_at": datetime.now(timezone.utc).isoformat()})
        lead.pending_reply_json = json.dumps(pending_list)
        await db.flush()
        logger.info("reply_queued_after_hours", email=from_addr)
        return "Reply queued — after business hours"

    # ── CC detection (#9): extract CC'd addresses ─────────────────────────────
    cc_raw = reply.get("cc", "")
    if cc_raw:
        existing_cc = lead.cc_emails or ""
        all_cc = set(filter(None, [e.strip() for e in existing_cc.split(",")]))
        new_cc = set(filter(None, [e.strip() for e in cc_raw.split(",")]))
        all_cc.update(new_cc)
        lead.cc_emails = ",".join(all_cc)

    # ── Forward detection (#4): detect "on behalf of" forwarded replies ───────
    forward_keywords = ["---------- forwarded", "begin forwarded", "fwd:", "fw:", "forwarded message"]
    if any(kw in body.lower() for kw in forward_keywords):
        lead.booked_via_forward = True

    # ── Bounce / DSN detection (#1, #2, #16) ─────────────────────────────────
    subject_hint = reply.get("subject", "")
    bounce_info = classify_bounce(subject_hint, body)
    if bounce_info["type"] == "hard_bounce":
        lead.status = LeadStatus.INVALID_EMAIL
        lead.bounce_count = (lead.bounce_count or 0) + 1
        lead.last_bounced_at = datetime.now(timezone.utc)
        logger.warning("hard_bounce_detected", email=from_addr)
        return "Hard bounce — lead marked INVALID_EMAIL, all sends stopped"

    if bounce_info["type"] == "soft_bounce":
        lead.soft_bounce_count = (lead.soft_bounce_count or 0) + 1
        lead.last_bounced_at = datetime.now(timezone.utc)
        delay_hours = soft_bounce_retry_delay(lead.soft_bounce_count)
        logger.info("soft_bounce_detected", email=from_addr, retry_in_hours=delay_hours)
        return f"Soft bounce #{lead.soft_bounce_count} — will retry in {delay_hours}h"

    if bounce_info["type"] == "job_change":
        lead.status = LeadStatus.JOB_CHANGED
        lead.new_contact_from_job_change = (
            f"{bounce_info.get('new_contact_name', '')} <{bounce_info.get('new_contact_email', '')}>"
        )
        new_email = bounce_info.get("new_contact_email")
        new_name = bounce_info.get("new_contact_name")
        if new_email:
            existing_new = (
                await db.execute(select(LeadV2).where(LeadV2.email == new_email))
            ).scalar_one_or_none()
            if not existing_new:
                db.add(LeadV2(
                    email=new_email,
                    contact_name=new_name or lead.contact_name,
                    business_name=lead.business_name,
                    summary=lead.summary,
                    designation=lead.designation,
                    location=lead.location,
                    status=LeadStatus.NEW,
                ))
                logger.info("job_change_new_lead_created", old_email=from_addr, new_email=new_email)
        return f"Job change — new lead created for {new_email}"

    # ── Emoji-only reply detection (#15): flag for human ─────────────────────
    stripped_body = body.strip()
    non_emoji_chars = re.sub(r'[\U00010000-\U0010ffff\U00002600-\U000027BF⌀-⏿⭐❤]', '', stripped_body, flags=re.UNICODE)
    if stripped_body and not non_emoji_chars.strip():
        lead.escalated_to_human = True
        logger.info("emoji_only_reply_flagged_human", email=from_addr)
        return "Emoji-only reply — flagged for human review"

    # ── Invalid email guard ───────────────────────────────────────────────────
    if lead.status == LeadStatus.INVALID_EMAIL:
        logger.info("reply_skipped_invalid_email", email=from_addr)
        return "Lead has INVALID_EMAIL status — ignoring reply"

    # ── Thread summarization (#45): build context for AI every 5 messages ────
    from app.services.thread_summarizer import build_context_for_ai, should_summarize, summarize_thread
    reply_count = getattr(lead, "reply_count", None) or 0
    reply_count += 1
    thread_summary = getattr(lead, "thread_summary", None)
    if should_summarize(reply_count):
        thread_messages = json.loads(lead.pending_reply_json or "[]")
        thread_summary = summarize_thread(thread_messages, thread_summary)
        if hasattr(lead, "thread_summary"):
            lead.thread_summary = thread_summary
    enriched_body = build_context_for_ai(body, json.loads(lead.pending_reply_json or "[]"), thread_summary)

    # ── A/B reply tracking — record segment + send-time reply ────────────────
    if lead.ab_variant:
        seg = classify_segment(lead.designation, lead.location)
        record_variant_reply_segment(lead.ab_variant, seg)
    if lead.send_time_variant:
        record_send_time_reply(lead.send_time_variant)

    # CRM — log reply received
    try:
        from app.services.zoho_crm import find_lead_by_email, update_lead, add_note
        _crm_id = find_lead_by_email(lead.email)
        if _crm_id:
            update_lead(_crm_id, {"Lead_Status": "Contacted"})
            add_note(_crm_id, f"Reply Received — {lead.business_name}",
                     f"Lead replied to outreach email.\nSnippet: {body[:300]}")
    except Exception as _re:
        logger.warning("crm_reply_sync_failed", email=lead.email, error=str(_re))

    # Priority 1: If Stage 2 (has offered slots), try matching them simple-first
    selected_display = None
    if lead.offered_slots_json:
        slot_infos, slot_strings = _load_slot_infos(lead)
        selected_display = _match_slot_simple(body, slot_strings)
        if selected_display:
            logger.info("slot_matched_simple", email=from_addr, slot=selected_display)
            matched_info = next((s for s in slot_infos if s["display"] == selected_display), None)
            if matched_info and matched_info.get("date_str") and matched_info.get("time_str"):
                date_str = matched_info["date_str"]
                time_str = matched_info["time_str"]
            else:
                try:
                    import dateutil.parser
                    parsed = dateutil.parser.parse(selected_display)
                    date_str = parsed.strftime("%d-%b-%Y")
                    time_str = parsed.strftime("%H:%M")
                except Exception:
                    date_str = "N/A"
                    time_str = "N/A"
            return await _do_booking(db, lead, date_str, time_str, selected_display)

    # Priority 2: Use unified AI analysis for intent classification and extraction
    today_str = datetime.now(timezone.utc).strftime("%A, %d %B %Y")
    analysis = analyze_reply_intent(enriched_body, today_str)
    logger.info("reply_analysis_result", email=from_addr, analysis=analysis, reply_body=body)

    from app.services.email_service import _send_html_email, _signature

    def _simple_reply(subject: str, paragraphs: list[str]) -> None:
        paras_html = "".join(
            f'<p style="margin:0 0 18px 0;font-family:Arial,sans-serif;font-size:15px;color:#111111;line-height:1.75;">{p}</p>'
            for p in paragraphs if p
        )
        _send_html_email(lead.email, subject, f"""<!DOCTYPE html>
<html><head><meta charset="utf-8"></head>
<body style="margin:0;padding:0;background:#ffffff;">
  <div style="max-width:600px;margin:40px auto;padding:0 24px 60px;">
    {paras_html}{_signature()}
  </div>
</body></html>""")

    greeting_name = (lead.contact_name or lead.business_name or "").split()[0].capitalize()

    # ── angry_negative (#12): escalate, no AI reply ────────────────────────────
    if analysis["intent"] == "angry_negative":
        lead.escalated_to_human = True
        lead.priority_flag = True
        from app.services.email_service import send_escalation_alert
        send_escalation_alert(
            lead_name=lead.contact_name or lead.business_name,
            lead_email=lead.email,
            designation=lead.designation,
            reason="Angry/negative reply received — human intervention required",
            reply_body=body[:500],
        )
        lead.replied_at = datetime.now(timezone.utc)
        logger.info("angry_reply_escalated", email=from_addr)
        return "Angry reply — escalated to human, no AI response sent"

    # ── assistant_reply (#10): reply on behalf of the actual decision maker ───
    if analysis["intent"] == "assistant_reply":
        on_behalf_of = analysis.get("on_behalf_of") or "your manager"
        reply_body = generate_assistant_reply(lead.contact_name, lead.business_name, on_behalf_of)
        _simple_reply("Re: Partnership Opportunity — Jane Aerospace", [
            f"Hi {greeting_name},", reply_body,
        ])
        lead.status = LeadStatus.REPLIED
        lead.replied_at = datetime.now(timezone.utc)
        logger.info("assistant_reply_sent", email=from_addr, on_behalf_of=on_behalf_of)
        return "Assistant reply sent"

    # ── job_change (#16): mark old lead, extract new contact, create new lead ─
    if analysis["intent"] == "job_change":
        new_name = analysis.get("new_contact_name")
        new_email = analysis.get("new_contact_email")
        lead.status = LeadStatus.JOB_CHANGED
        lead.new_contact_from_job_change = f"{new_name or ''} <{new_email or ''}>"
        if new_email:
            existing_new = (
                await db.execute(select(LeadV2).where(LeadV2.email == new_email))
            ).scalar_one_or_none()
            if not existing_new:
                db.add(LeadV2(
                    email=new_email,
                    contact_name=new_name or lead.contact_name,
                    business_name=lead.business_name,
                    summary=lead.summary,
                    designation=lead.designation,
                    location=lead.location,
                    status=LeadStatus.NEW,
                ))
                logger.info("job_change_intent_new_lead", old_email=from_addr, new_email=new_email)
        return f"Job change noted — new lead queued for {new_email}"

    # ── nda_request (#46): send NDA info, flag for human follow-up ────────────
    if analysis["intent"] == "nda_request":
        reply_body = generate_nda_reply(lead.contact_name, lead.business_name)
        _simple_reply("Re: Partnership Opportunity — Jane Aerospace", [
            f"Hi {greeting_name},", reply_body,
        ])
        lead.escalated_to_human = True
        lead.status = LeadStatus.REPLIED
        lead.replied_at = datetime.now(timezone.utc)
        logger.info("nda_request_handled", email=from_addr)
        return "NDA reply sent, flagged for human"

    # ── case_study_request (#47): send case study template email ──────────────
    if analysis["intent"] == "case_study_request":
        reply_body = generate_case_study_reply(lead.contact_name, lead.business_name)
        _simple_reply("Re: Partnership Opportunity — Jane Aerospace", [
            f"Hi {greeting_name},", reply_body,
        ])
        lead.status = LeadStatus.REPLIED
        lead.replied_at = datetime.now(timezone.utc)
        if lead.ab_variant:
            record_variant_reply(lead.ab_variant)
        logger.info("case_study_reply_sent", email=from_addr)
        return "Case study reply sent"

    # ── existing_vendor (#48): complement, don't compete ─────────────────────
    if analysis["intent"] == "existing_vendor":
        reply_body = generate_existing_vendor_reply(lead.contact_name, lead.business_name)
        _simple_reply("Re: Partnership Opportunity — Jane Aerospace", [
            f"Hi {greeting_name},", reply_body,
        ])
        lead.status = LeadStatus.REPLIED
        lead.replied_at = datetime.now(timezone.utc)
        if lead.ab_variant:
            record_variant_reply(lead.ab_variant)
        logger.info("existing_vendor_reply_sent", email=from_addr)
        return "Existing vendor reply sent"

    # ── deadline_mention (#50): high priority + deadline reply ────────────────
    if analysis["intent"] == "deadline_mention":
        deadline_text = analysis.get("deadline_text") or ""
        lead.priority_flag = True
        lead.priority_deadline = deadline_text
        reply_body = generate_deadline_reply(lead.contact_name, lead.business_name, deadline_text)
        _simple_reply("Re: Partnership Opportunity — Jane Aerospace", [
            f"Hi {greeting_name},", reply_body,
        ])
        lead.status = LeadStatus.REPLIED
        lead.replied_at = datetime.now(timezone.utc)
        if lead.ab_variant:
            record_variant_reply(lead.ab_variant)
        logger.info("deadline_reply_sent", email=from_addr, deadline=deadline_text)
        return f"Deadline reply sent — priority flagged ({deadline_text})"

    # ── non-English reply (#6): detect language, reply in same language ───────
    reply_language = analysis.get("language")
    if reply_language and reply_language.lower() not in ("english", "en", "", None):
        lead.reply_language = reply_language
        question_text = analysis.get("question_text") or body[:300]
        reply_body = generate_multilingual_reply(
            lead.contact_name, lead.business_name, question_text, reply_language
        )
        _simple_reply("Re: Partnership Opportunity — Jane Aerospace", [reply_body])
        lead.status = LeadStatus.REPLIED
        lead.replied_at = datetime.now(timezone.utc)
        logger.info("multilingual_reply_sent", email=from_addr, language=reply_language)
        return f"Multilingual reply sent ({reply_language})"

    # ── high_intent nudge (#3): send follow-up nudge for very positive replies ─
    if analysis["intent"] == "high_intent":
        reply_body = generate_high_intent_nudge(lead.contact_name, lead.business_name)
        _simple_reply("Re: Partnership Opportunity — Jane Aerospace", [
            f"Hi {greeting_name},", reply_body,
        ])
        lead.status = LeadStatus.REPLIED
        lead.replied_at = datetime.now(timezone.utc)
        if lead.ab_variant:
            record_variant_reply(lead.ab_variant)
        logger.info("high_intent_nudge_sent", email=from_addr)
        return "High-intent nudge sent"

    # ── scheduled_followup (#34): confirm a specific follow-up date ───────────
    if analysis["intent"] == "scheduled_followup":
        followup_date = analysis.get("followup_date") or ""
        if followup_date:
            try:
                lead.scheduled_followup_at = datetime.strptime(followup_date, "%Y-%m-%d").replace(
                    tzinfo=timezone.utc
                )
            except ValueError:
                pass
        reply_body = generate_scheduled_followup_confirm(
            lead.contact_name, lead.business_name, followup_date
        )
        _simple_reply("Re: Partnership Opportunity — Jane Aerospace", [
            f"Hi {greeting_name},", reply_body,
        ])
        lead.status = LeadStatus.REPLIED
        lead.replied_at = datetime.now(timezone.utc)
        logger.info("scheduled_followup_confirmed", email=from_addr, date=followup_date)
        return f"Scheduled follow-up confirmed for {followup_date}"

    # ── reschedule: 30-min undo window ─────────────────────────────────────────
    if analysis["intent"] == "reschedule":
        now_utc = datetime.now(timezone.utc)
        booked_within_30_min = (
            lead.status == LeadStatus.BOOKED
            and lead.booked_at is not None
            and (now_utc - lead.booked_at).total_seconds() <= 1800
        )
        if booked_within_30_min and lead.booking_id:
            zoho_cancel = ZohoBookingsService()
            cancelled = await asyncio.to_thread(zoho_cancel.cancel_booking, lead.booking_id)
            if cancelled:
                # Best-effort Redis lock release — parse date/time from stored slot display
                try:
                    import dateutil.parser as _dup
                    from zoneinfo import ZoneInfo as _ZI
                    _IST = _ZI("Asia/Kolkata")
                    _slot_dt = _dup.parse(lead.selected_slot or "").replace(tzinfo=_IST)
                    release_slot_lock(_slot_dt.strftime("%d-%b-%Y"), _slot_dt.strftime("%H:%M"))
                except Exception:
                    pass  # lock auto-expires in 10 min — acceptable
                lead.status = LeadStatus.SENT
                lead.booking_id = None
                lead.selected_slot = None
                lead.booked_at = None
                lead.offered_slots_json = None
                lead.reschedule_count = getattr(lead, "reschedule_count", 0) + 1
                lead.replied_at = now_utc
                _simple_reply("Re: Your Booking — Jane Aerospace", [
                    f"Hi {greeting_name},",
                    "No worries at all — I've cancelled that booking for you.",
                    "Here are fresh slots. Click any to lock in the right time:",
                ])
                from app.services.availability_v2 import get_slots_for_week
                alt = await get_slots_for_week(db, "this", 6) or await get_slots_for_week(db, "next", 6)
                if alt:
                    book_urls = [make_book_url(str(lead.id), i) for i in range(len(alt))]
                    lead.offered_slots_json = json.dumps(alt)
                    send_v2_slots_email(lead.email, lead.business_name,
                                        [s["display"] for s in alt],
                                        book_urls=book_urls, body_text=None,
                                        contact_name=lead.contact_name)
                logger.info("reschedule_undo_done", email=from_addr, cancelled=cancelled)
                return "Reschedule: booking cancelled within 30 min, new slots sent"
            else:
                _simple_reply("Re: Your Booking — Jane Aerospace", [
                    f"Hi {greeting_name},",
                    "I tried to cancel the booking but it looks like it may have already been processed on our end.",
                    "Let me sort this out manually and get back to you within the hour.",
                ])
                logger.warning("reschedule_cancel_failed", email=from_addr)
                return "Reschedule: Zoho cancel failed — manual intervention needed"
        else:
            # Beyond 30-min window
            _simple_reply("Re: Your Booking — Jane Aerospace", [
                f"Hi {greeting_name},",
                "I'm afraid I can no longer cancel the booking automatically as more than 30 minutes have passed.",
                "Please reply and I'll personally sort this out — just let me know and I'll make it happen.",
            ])
            lead.replied_at = datetime.now(timezone.utc)
            logger.info("reschedule_beyond_window", email=from_addr)
            return "Reschedule: beyond 30-min window — manual intervention email sent"

    # ── out_of_office ──────────────────────────────────────────────────────────
    if analysis["intent"] == "out_of_office":
        return_date_str = analysis.get("return_date")
        if return_date_str:
            try:
                from datetime import date as _date_type
                ooo_date = datetime.strptime(return_date_str, "%Y-%m-%d")
                lead.ooo_until = ooo_date.replace(tzinfo=timezone.utc)
            except ValueError:
                pass
        lead.replied_at = datetime.now(timezone.utc)
        logger.info("reply_ooo", email=from_addr, return_date=return_date_str)
        return f"OOO noted — will resume after {return_date_str or 'return'}"

    # ── question ───────────────────────────────────────────────────────────────
    if analysis["intent"] == "question":
        question_text = analysis.get("question_text") or body[:300]
        reply_body = generate_question_reply(lead.contact_name, lead.business_name, question_text)
        _simple_reply("Re: Partnership Opportunity — Jane Aerospace", [
            f"Hi {greeting_name},", reply_body,
        ])
        lead.status = LeadStatus.REPLIED
        lead.replied_at = datetime.now(timezone.utc)
        if lead.ab_variant:
            record_variant_reply(lead.ab_variant)
        logger.info("reply_question_answered", email=from_addr)
        return "Question replied"

    # ── callback_request (#14) ─────────────────────────────────────────────────
    if analysis["intent"] == "callback_request":
        phone = analysis.get("phone_number")
        # Extract phone from body if AI didn't parse it
        if not phone:
            phone_match = re.search(r'[\+\d][\d\s\-\(\)]{7,14}\d', body)
            if phone_match:
                phone = phone_match.group(0).strip()
        if phone:
            lead.phone_number = phone
            try:
                from app.services.zoho_crm import find_lead_by_email, update_lead
                _pid = find_lead_by_email(lead.email)
                if _pid:
                    update_lead(_pid, {"Phone": phone})
            except Exception:
                pass
        display_phone = phone or "the number you provided"
        _simple_reply("Re: Partnership Opportunity — Jane Aerospace", [
            f"Hi {greeting_name},",
            f"Got it — I'll give you a call at {display_phone} shortly.",
            "If there's a better time for the call, just reply and let me know.",
        ])
        lead.status = LeadStatus.REPLIED
        lead.replied_at = datetime.now(timezone.utc)
        if lead.ab_variant:
            record_variant_reply(lead.ab_variant)
        logger.info("reply_callback_ack", email=from_addr, phone=display_phone)
        return "Callback acknowledged"

    # ── pricing ────────────────────────────────────────────────────────────────
    if analysis["intent"] == "pricing":
        reply_body = generate_pricing_reply(lead.contact_name, lead.business_name)
        _simple_reply("Re: Partnership Opportunity — Jane Aerospace", [
            f"Hi {greeting_name},", reply_body,
        ])
        lead.status = LeadStatus.REPLIED
        lead.replied_at = datetime.now(timezone.utc)
        if lead.ab_variant:
            record_variant_reply(lead.ab_variant)
        logger.info("reply_pricing", email=from_addr)
        return "Pricing reply sent"

    # ── not_decision_maker ─────────────────────────────────────────────────────
    if analysis["intent"] == "not_decision_maker":
        referred_to = analysis.get("referred_to")
        reply_body = generate_not_decision_maker_reply(lead.contact_name, lead.business_name, referred_to)
        _simple_reply("Re: Partnership Opportunity — Jane Aerospace", [
            f"Hi {greeting_name},", reply_body,
        ])
        lead.status = LeadStatus.REPLIED
        lead.replied_at = datetime.now(timezone.utc)
        logger.info("reply_not_dm", email=from_addr, referred_to=referred_to)
        return "Not-DM reply sent"

    # ── decline ────────────────────────────────────────────────────────────────
    if analysis["intent"] == "decline":
        # Check for hard unsubscribe keywords — mark opted_out
        hard_unsub = any(w in body.lower() for w in ["unsubscribe", "remove me", "do not contact"])
        if hard_unsub:
            lead.opted_out = True
            lead.status = LeadStatus.REPLIED
            lead.replied_at = datetime.now(timezone.utc)
            _simple_reply("Re: Jane Aerospace", [
                f"Hi {greeting_name},",
                "Understood — I've removed you from our outreach list immediately.",
                "Apologies for any inconvenience, and all the best.",
            ])
            logger.info("reply_opted_out", email=from_addr)
            return "Lead opted out — unsubscribed"
        # Soft decline — send warm "when works?" reply
        _simple_reply("Re: Partnership Opportunity — Jane Aerospace", [
            f"Hi {greeting_name},",
            "Thank you for letting me know — I completely understand, schedules get busy.",
            "Would it be possible to let me know when a better time might be? Even a rough window would help.",
            "No pressure at all — happy to follow up whenever suits you.",
        ])
        lead.status = LeadStatus.REPLIED
        lead.replied_at = datetime.now(timezone.utc)
        logger.info("reply_handled_decline", email=from_addr)
        return "Lead declined — sent 'when are you available?' reply"

    if analysis["intent"] == "book" and analysis["date"] and analysis["time"]:
        date_str = datetime.strptime(analysis["date"], "%Y-%m-%d").strftime("%d-%b-%Y")
        time_str = analysis["time"]

        # Parse date and time into IST display string
        try:
            parsed_dt = datetime.strptime(f"{analysis['date']} {analysis['time']}", "%Y-%m-%d %H:%M").replace(tzinfo=IST)
            display_str = parsed_dt.strftime("%A, %b %d at %I:%M %p")
        except Exception:
            display_str = f"{date_str} at {time_str}"
            try:
                parsed_dt = datetime.strptime(f"{analysis['date']} 09:00", "%Y-%m-%d %H:%M").replace(tzinfo=IST)
            except Exception:
                parsed_dt = datetime.now(IST)

        # Check if Zoho has availability for this slot
        zoho = ZohoBookingsService()
        available_items = await asyncio.to_thread(zoho.fetch_available_slots, date_str)

        # Filter out booked slots
        from app.services.availability_v2 import _get_booked_slots, _parse_zoho_slot_hour
        booked_iso, booked_display = await _get_booked_slots(db)

        available_hours = set()
        if available_items:
            for item in available_items:
                try:
                    h = _parse_zoho_slot_hour(item)  # handles "05:00 PM" → 17 correctly
                    if h is None:
                        continue
                    slot_dt = datetime(parsed_dt.year, parsed_dt.month, parsed_dt.day, h, 0, tzinfo=IST)
                    if slot_dt.isoformat() in booked_iso or slot_dt.strftime("%A, %b %d at %I:%M %p") in booked_display:
                        continue
                    available_hours.add(h)
                except Exception:
                    pass
        elif not zoho.service_id:
            # If Zoho is mock environment, assume business hours are available
            available_hours = set(range(9, 20))

        requested_hour = parsed_dt.hour
        if requested_hour in available_hours:
            logger.info("specific_date_exact_match_via_analysis", email=lead.email, slot=display_str)
            return await _do_booking(db, lead, date_str, time_str, display_str)

        # Requested time is taken — send available alternatives for that day
        alt_slots = []
        for hour in sorted(available_hours):
            if 9 <= hour <= 19:
                slot_dt = datetime(parsed_dt.year, parsed_dt.month, parsed_dt.day, hour, 0, tzinfo=IST)
                alt_slots.append({
                    "display": slot_dt.strftime("%A, %b %d at %I:%M %p"),
                    "date_str": date_str,
                    "time_str": f"{hour:02d}:00",
                    "iso": slot_dt.isoformat(),
                })

        # Apply time-of-day filter (e.g. "June 11 after 13" → only show 13:00+)
        min_hour = _extract_min_hour(body)
        if min_hour is not None and alt_slots:
            alt_slots = [s for s in alt_slots if int(s["time_str"].split(":")[0]) >= min_hour]
            logger.info("date_alternatives_filtered_by_min_hour", email=lead.email, min_hour=min_hour, remaining=len(alt_slots))

        if alt_slots:
            slot_strings = [s["display"] for s in alt_slots]
            book_urls = [make_book_url(str(lead.id), i) for i in range(len(alt_slots))]
            lead.offered_slots_json = json.dumps(alt_slots)
            lead.replied_at = datetime.now(timezone.utc)
            send_v2_slots_email(
                lead.email, lead.business_name, slot_strings,
                book_urls=book_urls,
                body_text=None,
                contact_name=lead.contact_name,
            )
            logger.info("specific_date_alternatives_sent", email=lead.email, date=date_str, count=len(alt_slots))
            return f"Sent {len(alt_slots)} alternatives for {date_str}"

        # If no alternatives for that day, fall back to listing slots starting from that date
        analysis["intent"] = "list_slots"
        analysis["after_date"] = analysis["date"]

    if analysis["intent"] == "list_slots":
        week = analysis.get("week")
        after_date_str = analysis.get("after_date")
        specific_date_str = analysis.get("specific_date")

        after_date = None
        if after_date_str:
            try:
                after_date = datetime.strptime(after_date_str, "%Y-%m-%d").date()
            except ValueError:
                pass

        from app.services.availability_v2 import (
            get_slots_for_week, get_available_slots,
            _parse_zoho_slot_hour, _apply_db_guard, _build_slot_info, IST as _IST_AV,
        )

        slot_infos = []

        # ── Specific day request ("I'm free Monday", "Monday works") ─────────────
        if specific_date_str:
            try:
                from datetime import date as _date_type
                specific_date = datetime.strptime(specific_date_str, "%Y-%m-%d").date()
                date_fmt = specific_date.strftime("%d-%b-%Y")
                logger.info("fetching_slots_for_specific_day", email=lead.email, date=date_fmt)

                zoho = ZohoBookingsService()
                zoho_items = await asyncio.to_thread(zoho.fetch_available_slots, date_fmt)
                if zoho_items:
                    now_ist_ts = datetime.now(_IST_AV)
                    raw_day_slots = []
                    for item in zoho_items:
                        h = _parse_zoho_slot_hour(item)
                        if h is None or h < 9 or h > 18:
                            continue
                        slot_dt = datetime(specific_date.year, specific_date.month, specific_date.day, h, 0, tzinfo=_IST_AV)
                        if slot_dt <= now_ist_ts:
                            continue
                        raw_day_slots.append(_build_slot_info(specific_date, h))

                    # Apply DB guard (removes booked + held)
                    if raw_day_slots:
                        slot_infos = await _apply_db_guard(db, raw_day_slots)

                if not slot_infos:
                    # No Zoho slots that specific day — try the next few days starting from it
                    logger.info("no_slots_for_specific_day_falling_back", email=lead.email, date=date_fmt)
                    slot_infos = await get_available_slots(db, n=6, start_date=specific_date)

                if not slot_infos:
                    # Still nothing — fall back to this week / next week pool
                    logger.info("specific_day_fallback_to_week", email=lead.email)
                    slot_infos = await get_slots_for_week(db, "this", 6)
                if not slot_infos:
                    slot_infos = await get_slots_for_week(db, "next", 6)

            except Exception as exc:
                logger.warning("specific_day_fetch_error", email=lead.email, error=str(exc))
                slot_infos = []

        elif after_date:
            logger.info("fetching_slots_after_date", email=lead.email, after_date=after_date)
            slot_infos = await get_available_slots(db, n=6, start_date=after_date)
        else:
            if not week:
                week = "next"
            logger.info("fetching_slots_for_week", email=lead.email, week=week)
            slot_infos = await get_slots_for_week(db, week, n=6)
            if not slot_infos:
                other = "next" if week == "this" else "this"
                slot_infos = await get_slots_for_week(db, other, n=6)

        if not slot_infos:
            logger.warning("no_slots_available_for_filter", email=lead.email)
            _send_graceful_no_slots(lead)
            lead.replied_at = datetime.now(timezone.utc)
            return "No slots — sent graceful week-selection email"

        # Apply time-of-day preference (e.g. "after 12", "afternoon", "after 13")
        min_hour = _extract_min_hour(body)
        if min_hour is not None:
            filtered = [s for s in slot_infos if int(s["time_str"].split(":")[0]) >= min_hour]
            logger.info("slots_filtered_by_min_hour", email=lead.email, min_hour=min_hour, remaining=len(filtered))
            if filtered:
                slot_infos = filtered
            else:
                # No slots after the time filter — try a broader date range without time limit
                logger.warning("no_slots_after_min_hour_filter_broadening", email=lead.email, min_hour=min_hour)
                from app.services.availability_v2 import get_available_slots
                broad = await get_available_slots(db, n=6, lookahead_days=14)
                broad_filtered = [s for s in broad if int(s["time_str"].split(":")[0]) >= min_hour]
                if broad_filtered:
                    slot_infos = broad_filtered
                    logger.info("broadened_search_found_slots", email=lead.email, count=len(slot_infos))
                else:
                    _send_graceful_no_slots(lead)
                    lead.replied_at = datetime.now(timezone.utc)
                    return "No slots after time filter — sent graceful email"

        slot_strings = [s["display"] for s in slot_infos]
        book_urls = [make_book_url(str(lead.id), i) for i in range(len(slot_infos))]

        body_text_for_email = (
            f"Here are the available times after {min_hour:02d}:00 — click any to confirm instantly:"
            if min_hour else None
        )
        send_v2_slots_email(
            lead.email, lead.business_name, slot_strings,
            book_urls=book_urls,
            body_text=body_text_for_email,
            contact_name=lead.contact_name,
        )

        lead.offered_slots_json = json.dumps(slot_infos)
        lead.replied_at = datetime.now(timezone.utc)
        # Bust cache so next lead gets a fresh Zoho fetch
        from app.services.availability_v2 import invalidate_week_cache
        invalidate_week_cache()
        logger.info("slots_sent_for_filter", email=lead.email, count=len(slot_infos), min_hour=min_hour)
        return f"Sent {len(slot_infos)} slots"

    # Default/Unclear positive intent fallback
    logger.info("reply_unclear_sending_general_slots", email=from_addr)
    from app.services.availability_v2 import get_slots_for_week, invalidate_week_cache
    week = "this"
    slot_infos = await get_slots_for_week(db, week, n=6)
    if not slot_infos:
        week = "next"
        slot_infos = await get_slots_for_week(db, week, n=6)

    if not slot_infos:
        logger.warning("no_general_slots_available", email=lead.email)
        _send_graceful_no_slots(lead)
        lead.replied_at = datetime.now(timezone.utc)
        return "No slots — sent graceful week-selection email"

    # Apply time-of-day preference (e.g. "after 12", "afternoon", "PM", "after 13")
    min_hour = _extract_min_hour(body)
    if min_hour is not None:
        filtered = [s for s in slot_infos if int(s["time_str"].split(":")[0]) >= min_hour]
        logger.info("slots_filtered_by_min_hour", email=lead.email, min_hour=min_hour, remaining=len(filtered))
        if filtered:
            slot_infos = filtered
        else:
            _send_graceful_no_slots(lead)
            lead.replied_at = datetime.now(timezone.utc)
            return "No slots after time filter — sent graceful email"

    slot_strings = [s["display"] for s in slot_infos]
    book_urls = [make_book_url(str(lead.id), i) for i in range(len(slot_infos))]

    body_text_for_email = (
        f"Here are the available times after {min_hour:02d}:00 — click any to confirm instantly:"
        if min_hour else
        "Thank you for getting back — here are the next available times. Click any to confirm your spot:"
    )
    send_v2_slots_email(
        lead.email, lead.business_name, slot_strings,
        book_urls=book_urls,
        body_text=body_text_for_email,
        contact_name=lead.contact_name,
    )

    lead.offered_slots_json = json.dumps(slot_infos)
    lead.replied_at = datetime.now(timezone.utc)
    # Bust cache so next lead gets a fresh Zoho fetch
    invalidate_week_cache()
    logger.info("general_slots_sent", email=lead.email, count=len(slot_infos), min_hour=min_hour)
    return f"Sent {len(slot_infos)} general slots"


@shared_task(bind=True, max_retries=3, default_retry_delay=60)
def process_reply_v2(self, reply: dict):
    logger.info("running_process_reply_v2", email=reply.get("from_addr"))
    try:
        return run_async(_process_reply_v2, reply)
    except Exception as exc:
        # The IMAP poller already marked this message-id "seen", so without a
        # retry a transient failure (LLM timeout, Zoho hiccup, DB blip) would
        # drop the reply permanently. Retry so it actually gets processed.
        logger.warning("process_reply_v2_failed_retrying", email=reply.get("from_addr"),
                       attempt=self.request.retries + 1, error=str(exc))
        raise self.retry(exc=exc)


@shared_task
def check_inbox_replies_v2():
    logger.info("running_check_inbox_replies_v2")
    from app.workers.email_tasks import poll_imap
    return poll_imap()


# ---------------------------------------------------------------------------
# Master Pipeline Tracker → Google Sheets
# Exports every lead (all statuses) + onboarding columns to "Pipeline" worksheet
# Runs every 60 seconds automatically
# ---------------------------------------------------------------------------

_PIPELINE_HEADERS = [
    "Email", "Contact Name", "Company", "Lead Status",
    "Email Sent At", "Replied At", "Booked At", "Meeting Slot",
    "Onboarding Started", "KYC Pending", "KYC Status", "KYC Detail",
    "NDA Pending", "NDA Status", "NDA Detail",
    "Agreement Pending", "Agreement Status", "Agreement Detail",
    "Stage Score (0-11)", "Human Action Needed", "Last Updated",
]


def _yn(condition: bool) -> str:
    return "YES" if condition else "NO"


def _fmt_dt(dt_val) -> str:
    if not dt_val:
        return ""
    try:
        from zoneinfo import ZoneInfo
        ist = ZoneInfo("Asia/Kolkata")
        return dt_val.astimezone(ist).strftime("%d %b %Y %H:%M IST")
    except Exception:
        return str(dt_val)


async def _export_pipeline_to_sheets(db) -> str:
    from app.core.config import settings
    if not settings.GOOGLE_SHEETS_SPREADSHEET_ID or not settings.GOOGLE_SHEETS_CREDENTIALS_JSON:
        return "Google Sheets not configured"

    from sqlalchemy.future import select as _select
    from app.db.models import OnboardingRecord

    # Fetch all leads
    leads = (await db.execute(_select(LeadV2).order_by(LeadV2.created_at.desc()))).scalars().all()

    # Fetch all onboarding records keyed by lead_id
    ob_records = (await db.execute(_select(OnboardingRecord))).scalars().all()
    ob_map = {str(r.lead_id): r for r in ob_records}

    rows = []
    for lead in leads:
        ob = ob_map.get(str(lead.id))

        # KYC
        kyc_status = ob.kyc_status if ob else ""
        kyc_pending = _yn(ob is not None and kyc_status not in ("APPROVED",) and kyc_status != "")
        kyc_detail = (ob.kyc_status_display or "") if ob else ""

        # NDA
        nda_status = ob.nda_status if ob else ""
        nda_pending = _yn(ob is not None and nda_status not in ("APPROVED", "PROCEED_NEXT", "") and nda_status != "")
        nda_detail = (ob.nda_status_display or "") if ob else ""

        # Agreement
        ag_status = ob.agreement_status if ob else ""
        ag_pending = _yn(ob is not None and ag_status not in ("APPROVED", "PROCEED_NEXT", "") and ag_status != "")
        ag_detail = (ob.agreement_status_display or "") if ob else ""

        # Stage score
        stage_score = ""
        human_action = ""
        if ob:
            from app.api.v1.onboarding_endpoints import _stage_score, _pending_action
            stage_score = str(_stage_score(ob))
            pa = _pending_action(ob)
            if pa["type"] not in ("waiting", "complete"):
                human_action = pa["label"]
            elif pa["type"] == "complete":
                human_action = "COMPLETE"

        rows.append([
            lead.email or "",
            lead.contact_name or "",
            lead.business_name or "",
            (lead.status or "").upper(),
            _fmt_dt(lead.sent_at),
            _fmt_dt(lead.replied_at),
            _fmt_dt(lead.booked_at) if hasattr(lead, "booked_at") else "",
            lead.selected_slot or "",
            _yn(ob is not None),
            kyc_pending,
            kyc_status or "",
            kyc_detail,
            nda_pending,
            nda_status or "",
            nda_detail,
            ag_pending,
            ag_status or "",
            ag_detail,
            stage_score,
            human_action,
            _fmt_dt(datetime.now(timezone.utc)),
        ])

    # Write to Google Sheets
    try:
        import json as _json
        import gspread
        from google.oauth2.service_account import Credentials

        creds_dict = _json.loads(settings.GOOGLE_SHEETS_CREDENTIALS_JSON)
        scopes = ["https://spreadsheets.google.com/feeds", "https://www.googleapis.com/auth/drive"]
        creds = Credentials.from_service_account_info(creds_dict, scopes=scopes)
        gc = gspread.authorize(creds)
        sh = gc.open_by_key(settings.GOOGLE_SHEETS_SPREADSHEET_ID)

        # Get or create Pipeline worksheet
        try:
            ws = sh.worksheet("Pipeline")
        except Exception:
            ws = sh.add_worksheet(title="Pipeline", rows=2000, cols=len(_PIPELINE_HEADERS))

        # Full overwrite: clear + rewrite headers + all rows
        ws.clear()
        ws.append_row(_PIPELINE_HEADERS)
        if rows:
            ws.append_rows(rows)

        logger.info("pipeline_sheet_exported", total_rows=len(rows))
        return f"Pipeline exported: {len(rows)} leads"
    except Exception as exc:
        logger.warning("pipeline_sheet_export_failed", error=str(exc))
        # ── CSV fallback (#44): write locally so data is never lost ──────────
        try:
            fallback_path = "/tmp/pipeline_fallback.csv"
            with open(fallback_path, "w", newline="", encoding="utf-8") as f:
                import csv as _csv
                writer = _csv.writer(f)
                writer.writerow(_PIPELINE_HEADERS)
                writer.writerows(rows)
            logger.info("pipeline_csv_fallback_written", path=fallback_path, rows=len(rows))
            return f"Sheets failed ({exc}) — written to {fallback_path}"
        except Exception as csv_exc:
            logger.error("pipeline_csv_fallback_failed", error=str(csv_exc))
            return f"Export failed: {exc}"


@shared_task
def export_pipeline_to_sheets():
    logger.info("running_export_pipeline_to_sheets")
    return run_async(_export_pipeline_to_sheets)


# ---------------------------------------------------------------------------
# OOO resumption — re-activate leads whose ooo_until has passed
# Runs daily (configured in celery beat schedule)
# ---------------------------------------------------------------------------

async def _resume_ooo_leads(db) -> str:
    now = datetime.now(timezone.utc)
    result = await db.execute(
        select(LeadV2).where(
            LeadV2.ooo_until.isnot(None),
            LeadV2.ooo_until <= now,
            LeadV2.status == LeadStatus.SENT,
        )
    )
    leads = result.scalars().all()
    if not leads:
        return "No OOO leads to resume"

    resumed = 0
    for lead in leads:
        lead.ooo_until = None
        # Send fresh week-selection email
        outreach = generate_outreach(
            business_name=lead.business_name,
            contact_name=lead.contact_name,
            designation=getattr(lead, "designation", None),
            location=getattr(lead, "location", None),
            summary=lead.summary,
        )
        this_url = make_week_url(str(lead.id), "this")
        next_url = make_week_url(str(lead.id), "next")
        ok = send_week_selection_email(
            lead.email, lead.business_name,
            this_week_url=this_url,
            next_week_url=next_url,
            body_text=outreach["body"],
            contact_name=lead.contact_name,
            subject_override=outreach["subject"],
        )
        if ok:
            lead.ab_variant = outreach["variant"]
            lead.ab_subject_variant = outreach["subject_variant"]
            record_variant_sent(outreach["variant"])
            resumed += 1
            logger.info("ooo_lead_resumed", email=lead.email)
        else:
            logger.warning("ooo_resume_email_failed", email=lead.email)

    return f"OOO resumed: {resumed}/{len(leads)}"


@shared_task
def resume_ooo_leads():
    logger.info("running_resume_ooo_leads")
    return run_async(_resume_ooo_leads)


# ---------------------------------------------------------------------------
# Process after-hours queued replies (#11)
# Runs every 10 minutes — during business hours only
# ---------------------------------------------------------------------------

async def _process_delayed_replies(db) -> str:
    from app.services.email_service import is_after_hours
    if is_after_hours():
        return "Still after hours — deferred"

    result = await db.execute(
        select(LeadV2).where(
            LeadV2.pending_reply_json.isnot(None),
            LeadV2.status.in_([LeadStatus.SENT, LeadStatus.REPLIED]),
        )
    )
    leads = result.scalars().all()
    processed = 0
    for lead in leads:
        pending = json.loads(lead.pending_reply_json or "[]")
        if not pending:
            lead.pending_reply_json = None
            continue
        for queued in pending:
            reply_dict = {
                "from_addr": lead.email,
                "body": queued.get("body", ""),
                "subject": "",
                "cc": lead.cc_emails or "",
            }
            await _process_reply_v2(db, reply_dict)
            processed += 1
        lead.pending_reply_json = None
    return f"Processed {processed} queued after-hours replies"


@shared_task
def process_delayed_replies():
    logger.info("running_process_delayed_replies")
    return run_async(_process_delayed_replies)


# ---------------------------------------------------------------------------
# Email open nudge (#3): nudge leads who opened but haven't replied
# Runs every 30 minutes
# ---------------------------------------------------------------------------

async def _check_open_nudges(db) -> str:
    from app.services.email_tracker import get_open_count
    cutoff = datetime.now(timezone.utc) - timedelta(hours=2)

    result = await db.execute(
        select(LeadV2).where(
            LeadV2.status == LeadStatus.SENT,
            LeadV2.email_open_count > 0,
            LeadV2.open_nudge_sent == False,
            LeadV2.last_opened_at <= cutoff,
        )
    )
    leads = result.scalars().all()
    nudged = 0
    from app.services.email_service import _send_html_email, _signature
    for lead in leads:
        greeting_name = (lead.contact_name or lead.business_name or "").split()[0].capitalize()
        body_text = generate_high_intent_nudge(lead.contact_name, lead.business_name)
        paras_html = "".join(
            f'<p style="margin:0 0 18px 0;font-family:Arial,sans-serif;font-size:15px;color:#111111;line-height:1.75;">{p}</p>'
            for p in [f"Hi {greeting_name},", body_text] if p
        )
        html = f"""<!DOCTYPE html>
<html><head><meta charset="utf-8"></head>
<body style="margin:0;padding:0;background:#ffffff;">
  <div style="max-width:600px;margin:40px auto;padding:0 24px 60px;">
    {paras_html}{_signature()}
  </div>
</body></html>"""
        ok = _send_html_email(lead.email, "Re: Partnership Opportunity — Jane Aerospace", html)
        if ok:
            lead.open_nudge_sent = True
            nudged += 1
            logger.info("open_nudge_sent", email=lead.email)
    return f"Open nudges sent: {nudged}"


@shared_task
def check_open_nudges():
    logger.info("running_check_open_nudges")
    return run_async(_check_open_nudges)


# ---------------------------------------------------------------------------
# Pending booking nudge (#17): nudge leads who clicked but didn't confirm
# Runs every 5 minutes
# ---------------------------------------------------------------------------

async def _check_pending_bookings(db) -> str:
    nudge_cutoff = datetime.now(timezone.utc) - timedelta(minutes=20)
    expire_cutoff = datetime.now(timezone.utc) - timedelta(minutes=30)

    result = await db.execute(
        select(LeadV2).where(
            LeadV2.pending_booking_slot_json.isnot(None),
            LeadV2.status.in_([LeadStatus.SENT, LeadStatus.REPLIED]),
        )
    )
    leads = result.scalars().all()
    nudged = expired = 0

    for lead in leads:
        if not lead.pending_booking_at:
            continue

        if lead.pending_booking_at <= expire_cutoff:
            # Lock expired — clear pending state
            lead.pending_booking_slot_json = None
            lead.pending_booking_at = None
            lead.pending_nudge_sent = False
            expired += 1
            logger.info("pending_booking_expired", email=lead.email)
            continue

        if not lead.pending_nudge_sent and lead.pending_booking_at <= nudge_cutoff:
            slot_data = json.loads(lead.pending_booking_slot_json or "{}")
            slot_display = slot_data.get("display", "your selected slot")
            nudge_text = generate_pending_booking_nudge(
                lead.contact_name, lead.business_name, slot_display
            )
            from app.services.email_service import _send_html_email, _signature
            greeting_name = (lead.contact_name or lead.business_name or "").split()[0].capitalize()
            paras_html = "".join(
                f'<p style="margin:0 0 18px 0;font-family:Arial,sans-serif;font-size:15px;color:#111111;line-height:1.75;">{p}</p>'
                for p in [f"Hi {greeting_name},", nudge_text] if p
            )
            html = f"""<!DOCTYPE html>
<html><head><meta charset="utf-8"></head>
<body style="margin:0;padding:0;background:#ffffff;">
  <div style="max-width:600px;margin:40px auto;padding:0 24px 60px;">
    {paras_html}{_signature()}
  </div>
</body></html>"""
            ok = _send_html_email(lead.email, "Re: Your Booking — Jane Aerospace", html)
            if ok:
                lead.pending_nudge_sent = True
                nudged += 1
                logger.info("pending_booking_nudge_sent", email=lead.email, slot=slot_display)

    return f"Pending booking nudges: {nudged} sent, {expired} expired"


@shared_task
def check_pending_bookings():
    logger.info("running_check_pending_bookings")
    return run_async(_check_pending_bookings)


# ---------------------------------------------------------------------------
# Scheduled follow-ups (#34): send emails to leads on their chosen date
# Runs every hour
# ---------------------------------------------------------------------------

async def _send_scheduled_followups(db) -> str:
    now = datetime.now(timezone.utc)
    result = await db.execute(
        select(LeadV2).where(
            LeadV2.scheduled_followup_at.isnot(None),
            LeadV2.scheduled_followup_at <= now,
            LeadV2.status.in_([LeadStatus.SENT, LeadStatus.REPLIED]),
        )
    )
    leads = result.scalars().all()
    sent = 0
    for lead in leads:
        outreach = generate_outreach(
            business_name=lead.business_name,
            contact_name=lead.contact_name,
            designation=getattr(lead, "designation", None),
            location=getattr(lead, "location", None),
            summary=lead.summary,
        )
        this_url = make_week_url(str(lead.id), "this")
        next_url = make_week_url(str(lead.id), "next")
        ok = send_week_selection_email(
            lead.email, lead.business_name,
            this_week_url=this_url,
            next_week_url=next_url,
            body_text=outreach["body"],
            contact_name=lead.contact_name,
            subject_override=outreach["subject"],
            lead_id=str(lead.id),
        )
        if ok:
            lead.scheduled_followup_at = None
            lead.ab_variant = outreach["variant"]
            record_variant_sent(outreach["variant"])
            sent += 1
            logger.info("scheduled_followup_sent", email=lead.email)
    return f"Scheduled follow-ups sent: {sent}"


@shared_task
def send_scheduled_followups():
    logger.info("running_send_scheduled_followups")
    return run_async(_send_scheduled_followups)


# ---------------------------------------------------------------------------
# No-show checker (#27): detect missed meetings, send re-engage email
# Runs every 30 minutes
# ---------------------------------------------------------------------------

async def _check_no_shows(db) -> str:
    from zoneinfo import ZoneInfo
    IST = ZoneInfo("Asia/Kolkata")

    now_ist = datetime.now(IST)
    result = await db.execute(
        select(LeadV2).where(
            LeadV2.status == LeadStatus.BOOKED,
            LeadV2.selected_slot.isnot(None),
        )
    )
    leads = result.scalars().all()
    no_shows = 0

    for lead in leads:
        try:
            import dateutil.parser
            slot_dt = dateutil.parser.parse(lead.selected_slot)
            if slot_dt.tzinfo is None:
                slot_dt = slot_dt.replace(tzinfo=IST)
            # 30 minutes after meeting start = no-show window
            if slot_dt + timedelta(minutes=30) > now_ist:
                continue
        except Exception:
            continue

        lead.no_show_count = (lead.no_show_count or 0) + 1
        lead.status = LeadStatus.SENT
        lead.selected_slot = None
        lead.booking_id = None
        lead.booked_at = None

        reply_text = generate_no_show_reply(lead.contact_name, lead.business_name, lead.selected_slot or "")
        from app.services.email_service import _send_html_email, _signature
        greeting_name = (lead.contact_name or lead.business_name or "").split()[0].capitalize()
        paras_html = "".join(
            f'<p style="margin:0 0 18px 0;font-family:Arial,sans-serif;font-size:15px;color:#111111;line-height:1.75;">{p}</p>'
            for p in [f"Hi {greeting_name},", reply_text] if p
        )
        html = f"""<!DOCTYPE html>
<html><head><meta charset="utf-8"></head>
<body style="margin:0;padding:0;background:#ffffff;">
  <div style="max-width:600px;margin:40px auto;padding:0 24px 60px;">
    {paras_html}{_signature()}
  </div>
</body></html>"""
        _send_html_email(lead.email, "Re: Missed Meeting — Jane Aerospace", html)
        no_shows += 1
        logger.info("no_show_handled", email=lead.email, count=lead.no_show_count)

    return f"No-shows handled: {no_shows}"


@shared_task
def check_no_shows():
    logger.info("running_check_no_shows")
    return run_async(_check_no_shows)


# ---------------------------------------------------------------------------
# Soft bounce retrier (#2): retry sending to soft-bounced leads after delay
# Runs every 6 hours
# ---------------------------------------------------------------------------

async def _retry_soft_bounces(db) -> str:
    now = datetime.now(timezone.utc)
    result = await db.execute(
        select(LeadV2).where(
            LeadV2.soft_bounce_count > 0,
            LeadV2.soft_bounce_count < 3,
            LeadV2.last_bounced_at.isnot(None),
            LeadV2.status == LeadStatus.SENT,
        )
    )
    leads = result.scalars().all()
    retried = 0

    for lead in leads:
        delay_hours = soft_bounce_retry_delay(lead.soft_bounce_count)
        retry_after = lead.last_bounced_at + timedelta(hours=delay_hours)
        if now < retry_after:
            continue

        outreach = generate_outreach(
            business_name=lead.business_name,
            contact_name=lead.contact_name,
            designation=getattr(lead, "designation", None),
            location=getattr(lead, "location", None),
            summary=lead.summary,
        )
        this_url = make_week_url(str(lead.id), "this")
        next_url = make_week_url(str(lead.id), "next")
        ok = send_week_selection_email(
            lead.email, lead.business_name,
            this_week_url=this_url,
            next_week_url=next_url,
            body_text=outreach["body"],
            contact_name=lead.contact_name,
            subject_override=outreach["subject"],
            lead_id=str(lead.id),
        )
        if ok:
            retried += 1
            logger.info("soft_bounce_retried", email=lead.email, bounce_count=lead.soft_bounce_count)

    return f"Soft bounce retries sent: {retried}"


@shared_task
def retry_soft_bounces():
    logger.info("running_retry_soft_bounces")
    return run_async(_retry_soft_bounces)
