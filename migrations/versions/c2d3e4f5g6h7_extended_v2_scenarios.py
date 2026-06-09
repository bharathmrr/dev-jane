"""Extended V2 scenarios — bounce tracking, open tracking, pending booking,
after-hours queue, priority flags, job change, shared inbox, language, CC.

Revision ID: c2d3e4f5g6h7
Revises: c2d3e4f5a6b7
"""
from __future__ import annotations

from alembic import op

revision = "c2d3e4f5g6h7"
down_revision = "c2d3e4f5a6b7"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Add new LeadStatus enum values (idempotent)
    op.execute("ALTER TYPE lead_status_v2 ADD VALUE IF NOT EXISTS 'INVALID_EMAIL'")
    op.execute("ALTER TYPE lead_status_v2 ADD VALUE IF NOT EXISTS 'JOB_CHANGED'")

    # All columns use IF NOT EXISTS so re-running is safe
    cols = [
        # Bounce tracking (#1, #2)
        "ALTER TABLE leads_v2 ADD COLUMN IF NOT EXISTS bounce_count INTEGER NOT NULL DEFAULT 0",
        "ALTER TABLE leads_v2 ADD COLUMN IF NOT EXISTS soft_bounce_count INTEGER NOT NULL DEFAULT 0",
        "ALTER TABLE leads_v2 ADD COLUMN IF NOT EXISTS last_bounced_at TIMESTAMPTZ",
        # Email open tracking (#3)
        "ALTER TABLE leads_v2 ADD COLUMN IF NOT EXISTS email_open_count INTEGER NOT NULL DEFAULT 0",
        "ALTER TABLE leads_v2 ADD COLUMN IF NOT EXISTS last_opened_at TIMESTAMPTZ",
        "ALTER TABLE leads_v2 ADD COLUMN IF NOT EXISTS open_nudge_sent BOOLEAN NOT NULL DEFAULT FALSE",
        # A/B send-time variant
        "ALTER TABLE leads_v2 ADD COLUMN IF NOT EXISTS send_time_variant VARCHAR(16)",
        # Priority (#38, #50)
        "ALTER TABLE leads_v2 ADD COLUMN IF NOT EXISTS priority_flag BOOLEAN NOT NULL DEFAULT FALSE",
        "ALTER TABLE leads_v2 ADD COLUMN IF NOT EXISTS priority_deadline VARCHAR(255)",
        "ALTER TABLE leads_v2 ADD COLUMN IF NOT EXISTS escalated_to_human BOOLEAN NOT NULL DEFAULT FALSE",
        # Lead properties (#4, #5, #6, #9)
        "ALTER TABLE leads_v2 ADD COLUMN IF NOT EXISTS is_shared_inbox BOOLEAN NOT NULL DEFAULT FALSE",
        "ALTER TABLE leads_v2 ADD COLUMN IF NOT EXISTS reply_language VARCHAR(32)",
        "ALTER TABLE leads_v2 ADD COLUMN IF NOT EXISTS cc_emails TEXT",
        "ALTER TABLE leads_v2 ADD COLUMN IF NOT EXISTS booked_via_forward BOOLEAN NOT NULL DEFAULT FALSE",
        # Job change (#16)
        "ALTER TABLE leads_v2 ADD COLUMN IF NOT EXISTS new_contact_from_job_change VARCHAR(512)",
        # Scheduled follow-up (#34)
        "ALTER TABLE leads_v2 ADD COLUMN IF NOT EXISTS scheduled_followup_at TIMESTAMPTZ",
        # No-show tracking (#27)
        "ALTER TABLE leads_v2 ADD COLUMN IF NOT EXISTS no_show_count INTEGER NOT NULL DEFAULT 0",
        # Pending booking (#17)
        "ALTER TABLE leads_v2 ADD COLUMN IF NOT EXISTS pending_booking_slot_json TEXT",
        "ALTER TABLE leads_v2 ADD COLUMN IF NOT EXISTS pending_booking_at TIMESTAMPTZ",
        "ALTER TABLE leads_v2 ADD COLUMN IF NOT EXISTS pending_nudge_sent BOOLEAN NOT NULL DEFAULT FALSE",
        # After-hours reply queue (#11)
        "ALTER TABLE leads_v2 ADD COLUMN IF NOT EXISTS pending_reply_json TEXT",
        # Phone number (#14)
        "ALTER TABLE leads_v2 ADD COLUMN IF NOT EXISTS phone_number VARCHAR(32)",
        # Re-engagement (#25, #37)
        "ALTER TABLE leads_v2 ADD COLUMN IF NOT EXISTS is_repeat_lead BOOLEAN NOT NULL DEFAULT FALSE",
    ]
    for sql in cols:
        op.execute(sql)


def downgrade() -> None:
    for col in [
        "is_repeat_lead", "phone_number",
        "pending_reply_json", "pending_nudge_sent", "pending_booking_at",
        "pending_booking_slot_json", "no_show_count", "scheduled_followup_at",
        "new_contact_from_job_change", "booked_via_forward", "cc_emails",
        "reply_language", "is_shared_inbox", "escalated_to_human",
        "priority_deadline", "priority_flag", "send_time_variant",
        "open_nudge_sent", "last_opened_at", "email_open_count",
        "last_bounced_at", "soft_bounce_count", "bounce_count",
    ]:
        op.execute(f"ALTER TABLE leads_v2 DROP COLUMN IF EXISTS {col}")
