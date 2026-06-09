"""SQLAlchemy 2.0 ORM models.

Tables: users, organizers, availability_rules, holidays, meetings (templates),
leads, bookings, reservations, email_threads, email_messages, reminders,
audit_logs, system_settings.

Concurrency-safety notes:
  * `bookings` has a partial unique index ensuring at most one *active* booking
    per (organizer, slot_start) — the database is the final arbiter against
    double booking even if the Redis lock layer is bypassed.
  * `reservations` carries an idempotency_key so retried email replies don't
    create duplicate holds.
"""
from __future__ import annotations

import uuid
from datetime import date, datetime, time

from sqlalchemy import (
    Boolean,
    Date,
    DateTime,
    Enum,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    Time,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID as PGUUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import (
    Base,
    BookingState,
    CompanyType,
    DocumentStatus,
    EmailDirection,
    EmailIntent,
    KYCStatus,
    LeadStatus,
    ReservationStatus,
    SlotStatus,
    TimestampMixin,
    UserRole,
    UUIDMixin,
)

_val = lambda x: [e.value for e in x]  # noqa: E731  store enum values not names


class User(UUIDMixin, TimestampMixin, Base):
    __tablename__ = "users"

    email: Mapped[str] = mapped_column(String(320), unique=True, index=True)
    full_name: Mapped[str] = mapped_column(String(255))
    hashed_password: Mapped[str] = mapped_column(String(255))
    role: Mapped[UserRole] = mapped_column(
        Enum(UserRole, name="user_role", values_callable=_val), default=UserRole.AGENT
    )
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)

    organizer: Mapped["Organizer | None"] = relationship(
        back_populates="user", uselist=False
    )


class Organizer(UUIDMixin, TimestampMixin, Base):
    """A person whose calendar gets booked (the sales rep / interviewer)."""

    __tablename__ = "organizers"

    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), unique=True
    )
    display_name: Mapped[str] = mapped_column(String(255))
    timezone: Mapped[str] = mapped_column(String(64), default="UTC")
    default_meeting_minutes: Mapped[int] = mapped_column(Integer, default=30)
    buffer_minutes: Mapped[int] = mapped_column(Integer, default=0)
    # How many days ahead slots may be offered.
    booking_horizon_days: Mapped[int] = mapped_column(Integer, default=14)
    meeting_link: Mapped[str | None] = mapped_column(String(512))
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)

    user: Mapped[User] = relationship(back_populates="organizer")
    availability_rules: Mapped[list["AvailabilityRule"]] = relationship(
        back_populates="organizer", cascade="all, delete-orphan"
    )
    holidays: Mapped[list["Holiday"]] = relationship(
        back_populates="organizer", cascade="all, delete-orphan"
    )
    bookings: Mapped[list["Booking"]] = relationship(back_populates="organizer")


class AvailabilityRule(UUIDMixin, TimestampMixin, Base):
    """Recurring weekly working window for an organizer, stored in their tz."""

    __tablename__ = "availability_rules"

    organizer_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("organizers.id", ondelete="CASCADE"), index=True
    )
    weekday: Mapped[int] = mapped_column(Integer)  # 0=Mon .. 6=Sun
    start_time: Mapped[time] = mapped_column(Time)
    end_time: Mapped[time] = mapped_column(Time)

    organizer: Mapped[Organizer] = relationship(back_populates="availability_rules")

    __table_args__ = (
        Index("ix_avail_org_weekday", "organizer_id", "weekday"),
    )


class Holiday(UUIDMixin, TimestampMixin, Base):
    __tablename__ = "holidays"

    organizer_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("organizers.id", ondelete="CASCADE"), index=True
    )
    day: Mapped[date] = mapped_column(Date)
    label: Mapped[str | None] = mapped_column(String(255))

    organizer: Mapped[Organizer] = relationship(back_populates="holidays")

    __table_args__ = (
        UniqueConstraint("organizer_id", "day", name="uq_holiday_org_day"),
    )


class Meeting(UUIDMixin, TimestampMixin, Base):
    """A meeting *template / type* (e.g. '30-min discovery call')."""

    __tablename__ = "meetings"

    organizer_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("organizers.id", ondelete="CASCADE"), index=True
    )
    title: Mapped[str] = mapped_column(String(255))
    duration_minutes: Mapped[int] = mapped_column(Integer, default=30)
    location: Mapped[str | None] = mapped_column(String(512))  # video link / address
    description: Mapped[str | None] = mapped_column(Text)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)


class Lead(UUIDMixin, TimestampMixin, Base):
    __tablename__ = "leads"

    email: Mapped[str] = mapped_column(String(320), index=True)
    name: Mapped[str] = mapped_column(String(255))
    timezone: Mapped[str | None] = mapped_column(String(64))  # detected later
    metadata_: Mapped[dict] = mapped_column("metadata", JSONB, default=dict)


class Booking(UUIDMixin, TimestampMixin, Base):
    __tablename__ = "bookings"

    lead_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("leads.id", ondelete="CASCADE"), index=True
    )
    organizer_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("organizers.id", ondelete="CASCADE"), index=True
    )
    meeting_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("meetings.id", ondelete="SET NULL")
    )
    state: Mapped[BookingState] = mapped_column(
        Enum(BookingState, name="booking_state", values_callable=_val),
        default=BookingState.LEAD_CREATED,
        index=True,
    )
    # Stored in UTC; rendered to the lead's tz in emails.
    slot_start: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    slot_end: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    offered_slots: Mapped[list] = mapped_column(JSONB, default=list)
    cancel_reason: Mapped[str | None] = mapped_column(Text)
    booking_link: Mapped[str | None] = mapped_column(String(512))

    lead: Mapped[Lead] = relationship()
    organizer: Mapped[Organizer] = relationship(back_populates="bookings")
    thread: Mapped["EmailThread | None"] = relationship(
        back_populates="booking", uselist=False
    )

    __table_args__ = (
        # At most one ACTIVE booking per organizer + start time. This is the
        # hard guarantee against double booking (defense in depth vs Redis).
        Index(
            "uq_active_booking_per_slot",
            "organizer_id",
            "slot_start",
            unique=True,
            postgresql_where=text(
                "state in ('slot_reserved','booking_confirmed',"
                "'reminder_sent','meeting_completed')"
            ),
        ),
    )


class Reservation(UUIDMixin, TimestampMixin, Base):
    """A temporary hold on a slot (persistent mirror of the Redis lock)."""

    __tablename__ = "reservations"

    booking_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("bookings.id", ondelete="CASCADE"), index=True
    )
    organizer_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("organizers.id", ondelete="CASCADE"), index=True
    )
    slot_start: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    slot_end: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    status: Mapped[ReservationStatus] = mapped_column(
        Enum(ReservationStatus, name="reservation_status", values_callable=_val),
        default=ReservationStatus.HELD,
    )
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    idempotency_key: Mapped[str] = mapped_column(String(128), unique=True)


class EmailThread(UUIDMixin, TimestampMixin, Base):
    __tablename__ = "email_threads"

    booking_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("bookings.id", ondelete="CASCADE"), unique=True
    )
    # RFC 5322 message id of the first outbound message; used for References.
    root_message_id: Mapped[str | None] = mapped_column(String(512), index=True)
    subject: Mapped[str | None] = mapped_column(String(998))
    # Signed token embedded in reply-to addr to authenticate inbound replies.
    reply_token: Mapped[str] = mapped_column(String(128), unique=True, index=True)

    booking: Mapped[Booking] = relationship(back_populates="thread")
    messages: Mapped[list["EmailMessage"]] = relationship(
        back_populates="thread", cascade="all, delete-orphan"
    )


class EmailMessage(UUIDMixin, TimestampMixin, Base):
    __tablename__ = "email_messages"

    thread_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("email_threads.id", ondelete="CASCADE"), index=True
    )
    direction: Mapped[EmailDirection] = mapped_column(
        Enum(EmailDirection, name="email_direction", values_callable=_val)
    )
    message_id: Mapped[str | None] = mapped_column(String(512), index=True)
    in_reply_to: Mapped[str | None] = mapped_column(String(512))
    from_addr: Mapped[str] = mapped_column(String(320))
    to_addr: Mapped[str] = mapped_column(String(320))
    subject: Mapped[str | None] = mapped_column(String(998))
    body_text: Mapped[str | None] = mapped_column(Text)
    # LLM classification result attached to inbound messages.
    intent: Mapped[EmailIntent | None] = mapped_column(
        Enum(EmailIntent, name="email_intent", values_callable=_val)
    )
    intent_payload: Mapped[dict] = mapped_column(JSONB, default=dict)
    processed: Mapped[bool] = mapped_column(Boolean, default=False)

    thread: Mapped[EmailThread] = relationship(back_populates="messages")

    __table_args__ = (
        # De-dupe IMAP polling: a provider message id is unique per thread.
        UniqueConstraint("thread_id", "message_id", name="uq_thread_message"),
    )


class Reminder(UUIDMixin, TimestampMixin, Base):
    __tablename__ = "reminders"

    booking_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("bookings.id", ondelete="CASCADE"), index=True
    )
    send_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    sent: Mapped[bool] = mapped_column(Boolean, default=False)
    kind: Mapped[str] = mapped_column(String(64), default="reminder")


class AuditLog(UUIDMixin, TimestampMixin, Base):
    __tablename__ = "audit_logs"

    actor: Mapped[str] = mapped_column(String(320))  # user email / "system"
    action: Mapped[str] = mapped_column(String(128), index=True)
    entity_type: Mapped[str] = mapped_column(String(64))
    entity_id: Mapped[str | None] = mapped_column(String(64))
    data: Mapped[dict] = mapped_column(JSONB, default=dict)


class SystemSetting(UUIDMixin, TimestampMixin, Base):
    __tablename__ = "system_settings"

    key: Mapped[str] = mapped_column(String(128), unique=True, index=True)
    value: Mapped[dict] = mapped_column(JSONB, default=dict)


class LeadV2(UUIDMixin, TimestampMixin, Base):
    __tablename__ = "leads_v2"

    business_name: Mapped[str] = mapped_column(String(255))
    contact_name: Mapped[str | None] = mapped_column(String(255))
    email: Mapped[str] = mapped_column(String(320), unique=True, index=True)
    status: Mapped[LeadStatus] = mapped_column(
        Enum(LeadStatus, name="lead_status_v2", values_callable=_val),
        default=LeadStatus.NEW,
        index=True,
    )
    sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    replied_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    booked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    selected_slot: Mapped[str | None] = mapped_column(String(128))
    booking_id: Mapped[str | None] = mapped_column(String(255))
    offered_slots_json: Mapped[str | None] = mapped_column(Text)
    zoho_meeting_link: Mapped[str | None] = mapped_column(String(512))
    reminder_sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    follow_up_count: Mapped[int] = mapped_column(Integer, default=0)
    summary: Mapped[str | None] = mapped_column(Text)
    location: Mapped[str | None] = mapped_column(String(255))
    designation: Mapped[str | None] = mapped_column(String(255))
    ab_variant: Mapped[str | None] = mapped_column(String(2))        # A / B / C
    ab_subject_variant: Mapped[str | None] = mapped_column(String(2)) # S1 / S2 / S3
    opted_out: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    email_bounced: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    ooo_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    reschedule_count: Mapped[int] = mapped_column(Integer, default=0)

    # Bounce tracking (#1, #2)
    bounce_count: Mapped[int] = mapped_column(Integer, default=0)
    soft_bounce_count: Mapped[int] = mapped_column(Integer, default=0)
    last_bounced_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    # Email open tracking (#3)
    email_open_count: Mapped[int] = mapped_column(Integer, default=0)
    last_opened_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    open_nudge_sent: Mapped[bool] = mapped_column(Boolean, default=False)

    # A/B send-time variant (#A/B)
    send_time_variant: Mapped[str | None] = mapped_column(String(16))  # morning/afternoon/evening

    # Priority flagging (#38, #50)
    priority_flag: Mapped[bool] = mapped_column(Boolean, default=False)
    priority_deadline: Mapped[str | None] = mapped_column(String(255))
    escalated_to_human: Mapped[bool] = mapped_column(Boolean, default=False)

    # Lead properties (#5 shared inbox, #4 forward, #6 language)
    is_shared_inbox: Mapped[bool] = mapped_column(Boolean, default=False)
    reply_language: Mapped[str | None] = mapped_column(String(32))
    cc_emails: Mapped[str | None] = mapped_column(Text)      # JSON list of CC'd addresses
    booked_via_forward: Mapped[bool] = mapped_column(Boolean, default=False)

    # Job change tracking (#16)
    new_contact_from_job_change: Mapped[str | None] = mapped_column(String(512))

    # Scheduled follow-up (#34)
    scheduled_followup_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    # No-show tracking (#27)
    no_show_count: Mapped[int] = mapped_column(Integer, default=0)

    # Pending slot confirmation (#17 — slot clicked, awaiting user confirm)
    pending_booking_slot_json: Mapped[str | None] = mapped_column(Text)
    pending_booking_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    pending_nudge_sent: Mapped[bool] = mapped_column(Boolean, default=False)

    # After-hours reply queue (#11)
    pending_reply_json: Mapped[str | None] = mapped_column(Text)

    # Phone number (#14 — extracted from "call me" replies)
    phone_number: Mapped[str | None] = mapped_column(String(32))

    # Re-engagement flag (#25, #37)
    is_repeat_lead: Mapped[bool] = mapped_column(Boolean, default=False)


class ZohoSlot(UUIDMixin, TimestampMixin, Base):
    __tablename__ = "zoho_slots"

    zoho_slot_id: Mapped[str] = mapped_column(String(255), unique=True, index=True)
    slot_time: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    status: Mapped[SlotStatus] = mapped_column(
        Enum(SlotStatus, name="slot_status_v2", values_callable=_val),
        default=SlotStatus.AVAILABLE,
    )
    booked_email: Mapped[str | None] = mapped_column(String(320))


class AvailableDateV2(UUIDMixin, TimestampMixin, Base):
    __tablename__ = "available_dates_v2"

    slot_datetime: Mapped[datetime] = mapped_column(DateTime(timezone=True), unique=True, index=True)
    is_available: Mapped[bool] = mapped_column(Boolean, default=True)


# ---------------------------------------------------------------------------
# Onboarding models
# ---------------------------------------------------------------------------

class OnboardingRecord(UUIDMixin, TimestampMixin, Base):
    """One record per lead — tracks the full KYC → NDA → Agreement pipeline."""
    __tablename__ = "onboarding_records"

    lead_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("leads_v2.id", ondelete="CASCADE"),
        unique=True, nullable=False, index=True,
    )
    company_type: Mapped[str | None] = mapped_column(
        Enum(CompanyType, name="company_type_enum", values_callable=_val), nullable=True
    )

    # --- KYC ---
    kyc_status: Mapped[str] = mapped_column(
        Enum(KYCStatus, name="kyc_status_enum", values_callable=_val),
        default=KYCStatus.PENDING, nullable=False,
    )
    kyc_status_display: Mapped[str | None] = mapped_column(String(512))
    kyc_form_token: Mapped[str | None] = mapped_column(String(128))
    kyc_form_sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    kyc_submitted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    kyc_approved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    kyc_followup_count: Mapped[int] = mapped_column(Integer, default=0)
    kyc_last_followup_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    # --- NDA ---
    nda_status: Mapped[str] = mapped_column(
        Enum(DocumentStatus, name="nda_status_enum", values_callable=_val),
        default=DocumentStatus.PENDING, nullable=False,
    )
    nda_status_display: Mapped[str | None] = mapped_column(String(512))
    nda_draft_content: Mapped[str | None] = mapped_column(Text)
    nda_draft_zoho_file_id: Mapped[str | None] = mapped_column(String(255))
    nda_signed_zoho_file_id: Mapped[str | None] = mapped_column(String(255))
    nda_sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    nda_signed_received_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    nda_approved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    nda_followup_count: Mapped[int] = mapped_column(Integer, default=0)
    nda_last_followup_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    nda_draft_revision: Mapped[int] = mapped_column(Integer, default=0)
    nda_team_notes: Mapped[str | None] = mapped_column(Text)

    # --- Customer Agreement ---
    agreement_status: Mapped[str] = mapped_column(
        Enum(DocumentStatus, name="agreement_status_enum", values_callable=_val),
        default=DocumentStatus.PENDING, nullable=False,
    )
    agreement_status_display: Mapped[str | None] = mapped_column(String(512))
    agreement_draft_content: Mapped[str | None] = mapped_column(Text)
    agreement_draft_zoho_file_id: Mapped[str | None] = mapped_column(String(255))
    agreement_signed_zoho_file_id: Mapped[str | None] = mapped_column(String(255))
    agreement_sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    agreement_signed_received_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    agreement_approved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    agreement_followup_count: Mapped[int] = mapped_column(Integer, default=0)
    agreement_last_followup_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    agreement_draft_revision: Mapped[int] = mapped_column(Integer, default=0)
    agreement_team_notes: Mapped[str | None] = mapped_column(Text)

    # relationships
    lead: Mapped["LeadV2"] = relationship("LeadV2", foreign_keys=[lead_id])
    kyc_submissions: Mapped[list["KYCSubmission"]] = relationship(
        "KYCSubmission", back_populates="onboarding", cascade="all, delete-orphan"
    )


class KYCSubmission(UUIDMixin, TimestampMixin, Base):
    """Stores each attempt a lead makes at submitting the KYC form."""
    __tablename__ = "kyc_submissions"

    onboarding_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("onboarding_records.id", ondelete="CASCADE"),
        nullable=False, index=True,
    )
    attempt_number: Mapped[int] = mapped_column(Integer, default=1)

    # Form fields (mandatory for all)
    company_type: Mapped[str] = mapped_column(
        Enum(CompanyType, name="kyc_company_type_enum", values_callable=_val), nullable=False
    )
    company_name: Mapped[str] = mapped_column(String(255), nullable=False)
    contact_name: Mapped[str] = mapped_column(String(255), nullable=False)
    contact_number: Mapped[str] = mapped_column(String(50), nullable=False)

    # File references in Zoho WorkDrive (file IDs)
    gst_certificate_zoho_id: Mapped[str | None] = mapped_column(String(255))   # Indian only
    gst_certificate_filename: Mapped[str | None] = mapped_column(String(255))
    incorporation_zoho_id: Mapped[str | None] = mapped_column(String(255))
    incorporation_filename: Mapped[str | None] = mapped_column(String(255))

    # Team review
    reviewer_notes: Mapped[str | None] = mapped_column(Text)
    reviewed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    onboarding: Mapped["OnboardingRecord"] = relationship("OnboardingRecord", back_populates="kyc_submissions")

