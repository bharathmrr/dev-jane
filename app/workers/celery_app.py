"""Celery application and periodic beat schedule.

Queues:
  email      - outbound sends + inbound processing
  reminders  - reminder dispatch
  maintenance- expired-hold sweeping, IMAP polling
"""
from __future__ import annotations

from celery import Celery
from celery.schedules import crontab

from app.core.config import settings

celery_app = Celery(
    "scheduler",
    broker=str(settings.CELERY_BROKER_URL),
    backend=str(settings.CELERY_RESULT_BACKEND),
)

celery_app.conf.update(
    task_acks_late=True,
    task_reject_on_worker_lost=True,
    worker_prefetch_multiplier=1,
    task_default_queue="email",
    task_routes={
        "app.workers.reminder_tasks.*": {"queue": "reminders"},
        "app.workers.reservation_tasks.*": {"queue": "maintenance"},
        "app.workers.email_tasks.poll_imap": {"queue": "maintenance"},
        "app.workers.onboarding_tasks.*": {"queue": "onboarding"},
    },
    task_time_limit=300,
    task_soft_time_limit=270,
    timezone="UTC",
)

celery_app.conf.beat_schedule = {
    "sweep-expired-holds": {
        "task": "app.workers.reservation_tasks.sweep_expired_reservations",
        "schedule": 60.0,
    },
    "dispatch-due-reminders": {
        "task": "app.workers.reminder_tasks.dispatch_due_reminders",
        "schedule": 60.0,
    },
    "schedule-reminders": {
        "task": "app.workers.reminder_tasks.enqueue_upcoming_reminders",
        "schedule": crontab(minute="*/5"),
    },
    "v2-sync-sheets": {
        "task": "app.workers.v2_tasks.sync_google_sheet",
        "schedule": 30.0,
    },
    "v2-process-leads": {
        "task": "app.workers.v2_tasks.process_new_leads",
        "schedule": 20.0,
    },
    "v2-check-inbox": {
        "task": "app.workers.v2_tasks.check_inbox_replies_v2",
        "schedule": 20.0,
    },
    "v2-send-reminders": {
        "task": "app.workers.v2_tasks.send_v2_reminders",
        "schedule": 300.0,  # every 5 minutes
    },
    "onboarding-daily-reminders": {
        "task": "app.workers.onboarding_tasks.sweep_onboarding_reminders",
        "schedule": crontab(hour="8", minute="0"),  # every day at 8:00 UTC
    },
}

# Ensure task modules are imported when the worker boots.
celery_app.autodiscover_tasks(
    [
        "app.workers.email_tasks",
        "app.workers.reminder_tasks",
        "app.workers.reservation_tasks",
        "app.workers.v2_tasks",
        "app.workers.onboarding_tasks",
    ]
)
