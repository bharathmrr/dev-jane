"""Email-related Celery tasks."""
from __future__ import annotations

import asyncio
import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.logging import get_logger
from app.db.base import BookingState, EmailDirection
from app.db.models import Booking, EmailMessage, EmailThread, Lead, Organizer
from app.services import booking as booking_svc
from app.services.email_processor import OutboundAction, process_inbound
from app.services.notifications import smtp, templates
from app.workers.celery_app import celery_app
from app.workers.runtime import run_async

log = get_logger(__name__)


# --- Outbound offer ---
async def _send_offer(db: AsyncSession, booking_id: str) -> None:
    booking = await db.get(Booking, uuid.UUID(booking_id))
    if not booking:
        return
    lead = await db.get(Lead, booking.lead_id)
    org = await db.get(Organizer, booking.organizer_id)
    thread = (
        await db.execute(
            select(EmailThread).where(EmailThread.booking_id == booking.id)
        )
    ).scalar_one()

    payloads = await booking_svc.prepare_offer(db, booking, limit=5)
    body = templates.render_offer(lead.name, org.display_name, payloads)

    message_id = await asyncio.to_thread(
        smtp.send_email,
        to_addr=lead.email,
        subject=thread.subject or "Available meeting times",
        body=body,
        reply_token=thread.reply_token,
    )
    if not thread.root_message_id:
        thread.root_message_id = message_id

    db.add(
        EmailMessage(
            thread_id=thread.id,
            direction=EmailDirection.OUTBOUND,
            message_id=message_id,
            from_addr=org.display_name,
            to_addr=lead.email,
            subject=thread.subject,
            body_text=body,
        )
    )
    if booking.state == BookingState.LEAD_CREATED:
        await booking_svc.transition(db, booking, BookingState.EMAIL_SENT)
    await booking_svc.transition(db, booking, BookingState.WAITING_FOR_REPLY)


@celery_app.task(bind=True, max_retries=3, default_retry_delay=30)
def send_offer_email(self, booking_id: str) -> None:
    try:
        run_async(_send_offer, booking_id)
    except Exception as exc:  # pragma: no cover
        log.error("send_offer_failed", booking_id=booking_id, error=str(exc))
        raise self.retry(exc=exc)


# --- Send an OutboundAction returned by the processor ---
async def _send_action(db: AsyncSession, action_dict: dict, thread_id: str) -> None:
    thread = await db.get(EmailThread, uuid.UUID(thread_id))
    message_id = await asyncio.to_thread(
        smtp.send_email,
        to_addr=action_dict["to_addr"],
        subject=action_dict["subject"],
        body=action_dict["body"],
        reply_token=action_dict["reply_token"],
        in_reply_to=action_dict.get("in_reply_to"),
    )
    db.add(
        EmailMessage(
            thread_id=thread.id,
            direction=EmailDirection.OUTBOUND,
            message_id=message_id,
            from_addr=action_dict["reply_token"],
            to_addr=action_dict["to_addr"],
            subject=action_dict["subject"],
            body_text=action_dict["body"],
        )
    )


# --- Process a persisted inbound message ---
async def _process(db: AsyncSession, message_id: str) -> dict | None:
    msg = await db.get(EmailMessage, uuid.UUID(message_id))
    if not msg or msg.processed:
        return None
    action: OutboundAction | None = await process_inbound(db, msg)
    if action is None:
        return None
    return {
        "to_addr": action.to_addr,
        "subject": action.subject,
        "body": action.body,
        "reply_token": action.reply_token,
        "in_reply_to": action.in_reply_to,
        "thread_id": str(msg.thread_id),
    }


@celery_app.task(bind=True, max_retries=3, default_retry_delay=15)
def process_inbound_message(self, message_id: str) -> None:
    try:
        action = run_async(_process, message_id)
        if action:
            run_async(_send_action, action, action["thread_id"])
    except Exception as exc:  # pragma: no cover
        log.error("process_inbound_failed", message_id=message_id, error=str(exc))
        raise self.retry(exc=exc)


# --- IMAP polling (alternative to webhook) ---
async def _persist_reply(db: AsyncSession, reply: dict) -> str | None:
    booking_id = reply.get("thread_token")
    if not booking_id:
        log.info("imap_reply_no_valid_token")
        return None
    thread = (
        await db.execute(
            select(EmailThread).where(
                EmailThread.booking_id == uuid.UUID(booking_id)
            )
        )
    ).scalar_one_or_none()
    if not thread:
        return None
    # De-dupe on (thread, message_id).
    existing = (
        await db.execute(
            select(EmailMessage).where(
                EmailMessage.thread_id == thread.id,
                EmailMessage.message_id == reply["message_id"],
            )
        )
    ).scalar_one_or_none()
    if existing:
        return None
    msg = EmailMessage(
        thread_id=thread.id,
        direction=EmailDirection.INBOUND,
        message_id=reply["message_id"],
        in_reply_to=reply.get("in_reply_to"),
        from_addr=reply["from_addr"],
        to_addr="",
        subject=reply.get("subject"),
        body_text=reply.get("body"),
    )
    db.add(msg)
    await db.flush()
    return str(msg.id)


@celery_app.task(bind=True, max_retries=3, default_retry_delay=15)
def poll_imap(self) -> int:
    import redis as _redis
    from app.services.notifications.imap import fetch_replies
    from app.core.config import settings

    # Redis client for message-id de-duplication (7-day TTL)
    try:
        _r = _redis.from_url(str(settings.CELERY_BROKER_URL), decode_responses=True)
    except Exception:
        _r = None

    def _already_processed(message_id: str) -> bool:
        if not message_id or _r is None:
            return False
        key = f"imap:seen:{message_id}"
        # SET NX returns True if key was newly set (not seen before)
        return not _r.set(key, "1", nx=True, ex=86400 * 7)

    # Senders we never want to process — bounces and automations only
    _SKIP_PREFIXES = ("mailer-daemon@", "no-reply@", "noreply@", "postmaster@",
                      "donotreply@", "bounce@", "daemon@")

    def _should_skip(from_addr: str) -> bool:
        fa = (from_addr or "").lower()
        if not fa:
            return True
        return any(fa.startswith(p) for p in _SKIP_PREFIXES)

    try:
        replies = fetch_replies()
    except Exception as exc:
        err = str(exc)
        if "name resolution" in err or "gaierror" in err or "timeout" in err.lower() or "Connection" in err:
            log.warning("imap_dns_failure_retrying", error=err)
            raise self.retry(exc=exc)
        raise
    queued = 0
    for reply in replies:
        from_addr = reply.get("from_addr", "")
        body = (reply.get("body") or "").strip()

        # Skip bounce/automation emails and emails with empty body
        if _should_skip(from_addr) or not body:
            continue

        msg_id = reply.get("message_id", "")
        if _already_processed(msg_id):
            continue  # skip — already handled in a previous poll cycle

        if reply.get("thread_token"):
            db_msg_id = run_async(_persist_reply, reply)
            if db_msg_id:
                process_inbound_message.delay(db_msg_id)
                queued += 1
        else:
            from app.workers.v2_tasks import process_reply_v2
            process_reply_v2.delay(reply)
            queued += 1
    return queued
