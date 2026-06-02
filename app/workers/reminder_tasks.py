"""Reminder scheduling and dispatch."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.logging import get_logger
from app.db.base import BookingState, EmailDirection
from app.db.models import Booking, EmailMessage, EmailThread, Lead, Organizer, Reminder
from app.services import booking as booking_svc
from app.services.notifications import smtp, templates
from app.services.timeutil import slot_label_from_iso
from app.workers.celery_app import celery_app
from app.workers.runtime import run_async

log = get_logger(__name__)


async def _enqueue(db: AsyncSession) -> int:
    """Create reminder rows for confirmed bookings that lack one."""
    now = datetime.now(timezone.utc)
    horizon = now + timedelta(days=2)
    bookings = (
        await db.execute(
            select(Booking).where(
                Booking.state == BookingState.BOOKING_CONFIRMED,
                Booking.slot_start.is_not(None),
                Booking.slot_start <= horizon,
                Booking.slot_start > now,
            )
        )
    ).scalars().all()
    created = 0
    for b in bookings:
        existing = (
            await db.execute(
                select(Reminder).where(Reminder.booking_id == b.id, Reminder.sent.is_(False))
            )
        ).scalar_one_or_none()
        if existing:
            continue
        send_at = b.slot_start - timedelta(minutes=settings.REMINDER_LEAD_MINUTES)
        db.add(Reminder(booking_id=b.id, send_at=send_at))
        created += 1
    return created


async def _dispatch(db: AsyncSession) -> int:
    now = datetime.now(timezone.utc)
    due = (
        await db.execute(
            select(Reminder).where(Reminder.sent.is_(False), Reminder.send_at <= now)
        )
    ).scalars().all()
    sent = 0
    for rem in due:
        booking = await db.get(Booking, rem.booking_id)
        if not booking or booking.state not in (
            BookingState.BOOKING_CONFIRMED,
            BookingState.REMINDER_SENT,
        ):
            rem.sent = True
            continue
        lead = await db.get(Lead, booking.lead_id)
        org = await db.get(Organizer, booking.organizer_id)
        thread = (
            await db.execute(
                select(EmailThread).where(EmailThread.booking_id == booking.id)
            )
        ).scalar_one()
        tz = lead.timezone or org.timezone
        label = slot_label_from_iso(booking.slot_start.isoformat(), tz)
        body = templates.render_reminder(lead.name, org.display_name, label)
        message_id = smtp.send_email(
            to_addr=lead.email,
            subject="Reminder: upcoming meeting",
            body=body,
            reply_token=thread.reply_token,
            in_reply_to=thread.root_message_id,
        )
        db.add(
            EmailMessage(
                thread_id=thread.id,
                direction=EmailDirection.OUTBOUND,
                message_id=message_id,
                from_addr=org.display_name,
                to_addr=lead.email,
                subject="Reminder: upcoming meeting",
                body_text=body,
            )
        )
        rem.sent = True
        if booking.state == BookingState.BOOKING_CONFIRMED:
            await booking_svc.transition(db, booking, BookingState.REMINDER_SENT)
        sent += 1
    if sent:
        log.info("reminders_dispatched", count=sent)
    return sent


@celery_app.task
def enqueue_upcoming_reminders() -> int:
    return run_async(_enqueue)


@celery_app.task
def dispatch_due_reminders() -> int:
    return run_async(_dispatch)
