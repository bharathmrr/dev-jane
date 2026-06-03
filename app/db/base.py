"""Declarative base, shared mixins, and domain enums."""
from __future__ import annotations

import enum
import uuid
from datetime import datetime

from sqlalchemy import DateTime, func
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.orm import DeclarativeBase, Mapped, declared_attr, mapped_column


class Base(DeclarativeBase):
    pass


class UUIDMixin:
    id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )


class TimestampMixin:
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )


# --- Domain enums ---
class UserRole(str, enum.Enum):
    ADMIN = "admin"
    ORGANIZER = "organizer"
    AGENT = "agent"


class BookingState(str, enum.Enum):
    """The booking lifecycle state machine (see app/state_machine.py)."""

    LEAD_CREATED = "lead_created"
    EMAIL_SENT = "email_sent"
    WAITING_FOR_REPLY = "waiting_for_reply"
    SLOT_SELECTED = "slot_selected"
    SLOT_RESERVED = "slot_reserved"
    BOOKING_CONFIRMED = "booking_confirmed"
    REMINDER_SENT = "reminder_sent"
    MEETING_COMPLETED = "meeting_completed"
    CANCELLED = "cancelled"
    RESCHEDULED = "rescheduled"


class EmailDirection(str, enum.Enum):
    INBOUND = "inbound"
    OUTBOUND = "outbound"


class EmailIntent(str, enum.Enum):
    CONFIRM_SLOT = "confirm_slot"
    REJECT_SLOTS = "reject_slots"
    SUGGEST_NEW_TIME = "suggest_new_time"
    RESCHEDULE = "reschedule"
    CANCEL = "cancel"
    GENERAL_QUERY = "general_query"
    UNKNOWN = "unknown"


class ReservationStatus(str, enum.Enum):
    HELD = "held"
    CONFIRMED = "confirmed"
    EXPIRED = "expired"
    RELEASED = "released"


class LeadStatus(str, enum.Enum):
    NEW = "NEW"
    SENT = "SENT"
    REPLIED = "REPLIED"
    BOOKED = "BOOKED"
    FAILED = "FAILED"


class SlotStatus(str, enum.Enum):
    AVAILABLE = "AVAILABLE"
    BOOKED = "BOOKED"

