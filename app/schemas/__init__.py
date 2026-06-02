"""API request/response schemas (Pydantic v2)."""
from __future__ import annotations

import uuid
from datetime import date, datetime, time
from typing import Literal

from pydantic import BaseModel, ConfigDict, EmailStr, Field

from app.db.base import BookingState, UserRole


class Token(BaseModel):
    access_token: str
    refresh_token: str
    token_type: str = "bearer"


class LoginRequest(BaseModel):
    email: EmailStr
    password: str


class UserOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: uuid.UUID
    email: EmailStr
    full_name: str
    role: UserRole
    is_active: bool
    organizer_id: uuid.UUID | None = None  # populated for role=organizer


class LeadCreate(BaseModel):
    name: str = Field(min_length=1, max_length=255)
    email: EmailStr
    organizer_id: uuid.UUID


class SlotOut(BaseModel):
    start: datetime
    end: datetime
    label: str


class BookingOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: uuid.UUID
    state: BookingState
    organizer_id: uuid.UUID
    lead_id: uuid.UUID
    slot_start: datetime | None
    slot_end: datetime | None


class AvailabilityRuleIn(BaseModel):
    weekday: int = Field(ge=0, le=6)
    start_time: time
    end_time: time


class AvailabilityRuleOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: uuid.UUID
    weekday: int
    start_time: time
    end_time: time


class HolidayIn(BaseModel):
    day: date
    label: str | None = None


class HolidayOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: uuid.UUID
    day: date
    label: str | None


class UserInOrganizer(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: uuid.UUID
    email: str
    role: UserRole


class OrganizerOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: uuid.UUID
    display_name: str
    timezone: str
    default_meeting_minutes: int
    buffer_minutes: int
    booking_horizon_days: int
    meeting_link: str | None
    is_active: bool
    availability_rules: list[AvailabilityRuleOut]
    holidays: list[HolidayOut]
    user: UserInOrganizer | None = None


class OrganizerCreate(BaseModel):
    display_name: str
    email: EmailStr
    password: str = Field(min_length=6)
    timezone: str = "UTC"
    default_meeting_minutes: int = 30
    buffer_minutes: int = 0
    booking_horizon_days: int = 14
    meeting_link: str | None = None
    access_level: Literal["organizer", "viewer"] = "organizer"
    rules: list[AvailabilityRuleIn] = []


class OrganizerUpdate(BaseModel):
    display_name: str | None = None
    timezone: str | None = None
    default_meeting_minutes: int | None = None
    buffer_minutes: int | None = None
    booking_horizon_days: int | None = None
    meeting_link: str | None = None
    is_active: bool | None = None
    access_level: Literal["organizer", "viewer"] | None = None


class LeadInBooking(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: uuid.UUID
    name: str
    email: str
    timezone: str | None = None


class OrganizerInBooking(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: uuid.UUID
    display_name: str


class BookingListItem(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: uuid.UUID
    state: BookingState
    organizer_id: uuid.UUID
    lead_id: uuid.UUID
    slot_start: datetime | None
    slot_end: datetime | None
    booking_link: str | None = None
    lead: LeadInBooking | None = None
    organizer: OrganizerInBooking | None = None


class InboundEmailWebhook(BaseModel):
    """Payload from an email provider webhook (e.g. SendGrid/Mailgun inbound)."""

    thread_token: str | None = None
    message_id: str | None = None
    in_reply_to: str | None = None
    from_addr: EmailStr
    subject: str | None = None
    body: str


class AnalyticsSummary(BaseModel):
    total_leads: int
    total_bookings: int
    confirmed: int
    cancelled: int
    conversion_rate: float
