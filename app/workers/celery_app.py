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
    },
    task_time_limit=120,
    task_soft_time_limit=90,
    timezone="UTC",
)

celery_app.conf.beat_schedule = {
    "poll-imap": {
        "task": "app.workers.email_tasks.poll_imap",
        "schedule": settings.IMAP_POLL_INTERVAL_SECONDS,
    },
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
}

# Ensure task modules are imported when the worker boots.
celery_app.autodiscover_tasks(
    ["app.workers.email_tasks", "app.workers.reminder_tasks", "app.workers.reservation_tasks"]
)
