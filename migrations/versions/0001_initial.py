"""initial schema baseline

Revision ID: 0001_initial
Revises:
Create Date: 2026-06-01

Creates all enum types first (each op.execute commits immediately in AUTOCOMMIT
+ transactional_ddl=False mode) so the partial index on `bookings.state` can
reference them. create_all then skips existing types and creates tables/indexes.
"""
from alembic import op
from sqlalchemy import text

from app.db.base import Base
from app.db import models  # noqa: F401  register tables on metadata

revision = "0001_initial"
down_revision = None
branch_labels = None
depends_on = None

_ENUM_TYPES = [
    (
        "booking_state",
        "lead_created,email_sent,waiting_for_reply,slot_selected,"
        "slot_reserved,booking_confirmed,reminder_sent,meeting_completed,"
        "cancelled,rescheduled",
    ),
    ("user_role", "admin,organizer,agent"),
    ("email_direction", "inbound,outbound"),
    (
        "email_intent",
        "confirm_slot,reject_slots,suggest_new_time,reschedule,"
        "cancel,general_query,unknown",
    ),
    ("reservation_status", "held,confirmed,expired,released"),
]


def upgrade() -> None:
    # Create enum types individually so each is committed before the partial
    # index on bookings (which references booking_state values) is created.
    for type_name, values_csv in _ENUM_TYPES:
        quoted = ",".join(f"'{v}'" for v in values_csv.split(","))
        op.execute(
            text(f"CREATE TYPE {type_name} AS ENUM ({quoted})")
        )

    bind = op.get_bind()
    Base.metadata.create_all(bind=bind)


def downgrade() -> None:
    bind = op.get_bind()
    Base.metadata.drop_all(bind=bind)
    for type_name, _ in _ENUM_TYPES:
        op.execute(text(f"DROP TYPE IF EXISTS {type_name}"))
