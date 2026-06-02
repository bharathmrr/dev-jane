from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, HTTPException, Response, status
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.core.deps import get_current_user, rate_limit, require_roles
from app.core.security import hash_password, verify_thread_token
from app.db.base import BookingState, EmailDirection, UserRole
from app.db.models import (
    AvailabilityRule,
    Booking,
    EmailMessage,
    EmailThread,
    Holiday,
    Lead,
    Organizer,
    User,
)
from app.db.session import get_db
from app.schemas import (
    AnalyticsSummary,
    AvailabilityRuleIn,
    BookingListItem,
    HolidayIn,
    HolidayOut,
    InboundEmailWebhook,
    OrganizerCreate,
    OrganizerOut,
    OrganizerUpdate,
)

router = APIRouter(tags=["email", "admin", "analytics", "health"])

_ACCESS_ROLE = {"organizer": UserRole.ORGANIZER, "viewer": UserRole.AGENT}


# --- Inbound email webhook ---
@router.post("/webhooks/email/inbound", dependencies=[Depends(rate_limit)])
async def inbound_email(
    payload: InboundEmailWebhook, db: AsyncSession = Depends(get_db)
) -> dict:
    token = payload.thread_token
    booking_id = verify_thread_token(token) if token else None
    if not booking_id:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Invalid or missing thread token")
    thread = (
        await db.execute(
            select(EmailThread).where(EmailThread.booking_id == uuid.UUID(booking_id))
        )
    ).scalar_one_or_none()
    if not thread:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Thread not found")
    msg = EmailMessage(
        thread_id=thread.id,
        direction=EmailDirection.INBOUND,
        message_id=payload.message_id,
        in_reply_to=payload.in_reply_to,
        from_addr=payload.from_addr,
        to_addr="",
        subject=payload.subject,
        body_text=payload.body,
    )
    db.add(msg)
    await db.flush()
    from app.workers.email_tasks import process_inbound_message
    process_inbound_message.delay(str(msg.id))
    return {"status": "accepted", "message_id": str(msg.id)}


# --- Current organizer profile (for organizer role) ---
@router.get("/me/organizer", response_model=OrganizerOut)
async def my_organizer_profile(
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
) -> Organizer:
    result = await db.execute(
        select(Organizer)
        .where(Organizer.user_id == user.id)
        .options(
            selectinload(Organizer.availability_rules),
            selectinload(Organizer.holidays),
            selectinload(Organizer.user),
        )
    )
    org = result.scalar_one_or_none()
    if not org:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "No organizer profile found")
    return org


# --- Admin: create organizer ---
@router.post("/admin/organizers", status_code=status.HTTP_201_CREATED, response_model=OrganizerOut)
async def create_organizer(
    payload: OrganizerCreate,
    db: AsyncSession = Depends(get_db),
    _: User = Depends(require_roles(UserRole.ADMIN)),
) -> Organizer:
    existing = (
        await db.execute(
            select(User)
            .where(func.lower(User.email) == func.lower(payload.email))
            .options(selectinload(User.organizer))
        )
    ).scalar_one_or_none()

    if existing:
        if existing.organizer:
            raise HTTPException(
                status.HTTP_409_CONFLICT,
                f"Organizer profile already exists for user {payload.email}"
            )
        # Use/promote the existing user
        role = _ACCESS_ROLE.get(payload.access_level, UserRole.ORGANIZER)
        existing.role = role
        org_user = existing
    else:
        role = _ACCESS_ROLE.get(payload.access_level, UserRole.ORGANIZER)
        org_user = User(
            id=uuid.uuid4(),
            email=payload.email,
            full_name=payload.display_name,
            hashed_password=hash_password(payload.password),
            role=role,
        )
        db.add(org_user)
        await db.flush()

    organizer = Organizer(
        id=uuid.uuid4(),
        user_id=org_user.id,
        display_name=payload.display_name,
        timezone=payload.timezone,
        default_meeting_minutes=payload.default_meeting_minutes,
        buffer_minutes=payload.buffer_minutes,
        booking_horizon_days=payload.booking_horizon_days,
        meeting_link=payload.meeting_link or None,
    )
    db.add(organizer)
    await db.flush()
    for r in payload.rules:
        db.add(AvailabilityRule(
            organizer_id=organizer.id,
            weekday=r.weekday,
            start_time=r.start_time,
            end_time=r.end_time,
        ))
    await db.flush()
    await db.refresh(organizer, ["availability_rules", "holidays", "user"])
    return organizer


# --- Admin: list organizers ---
@router.get("/admin/organizers", response_model=list[OrganizerOut])
async def list_organizers(
    search: str = "",
    db: AsyncSession = Depends(get_db),
    _: User = Depends(require_roles(UserRole.ADMIN)),
) -> list[Organizer]:
    q = select(Organizer).options(
        selectinload(Organizer.availability_rules),
        selectinload(Organizer.holidays),
        selectinload(Organizer.user),
    )
    if search:
        q = q.join(Organizer.user).where(
            User.email.ilike(f"%{search}%") | Organizer.display_name.ilike(f"%{search}%")
        )
    result = await db.execute(q)
    return list(result.scalars().all())


# --- Admin: get single organizer ---
@router.get("/admin/organizers/{organizer_id}", response_model=OrganizerOut)
async def get_organizer(
    organizer_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    _: User = Depends(require_roles(UserRole.ADMIN)),
) -> Organizer:
    result = await db.execute(
        select(Organizer)
        .where(Organizer.id == organizer_id)
        .options(
            selectinload(Organizer.availability_rules),
            selectinload(Organizer.holidays),
            selectinload(Organizer.user),
        )
    )
    org = result.scalar_one_or_none()
    if not org:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Organizer not found")
    return org


# --- Admin: update organizer ---
@router.put("/admin/organizers/{organizer_id}", response_model=OrganizerOut)
async def update_organizer(
    organizer_id: uuid.UUID,
    payload: OrganizerUpdate,
    db: AsyncSession = Depends(get_db),
    _: User = Depends(require_roles(UserRole.ADMIN)),
) -> Organizer:
    result = await db.execute(
        select(Organizer)
        .where(Organizer.id == organizer_id)
        .options(
            selectinload(Organizer.availability_rules),
            selectinload(Organizer.holidays),
            selectinload(Organizer.user),
        )
    )
    org = result.scalar_one_or_none()
    if not org:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Organizer not found")

    data = payload.model_dump(exclude_unset=True)
    access_level = data.pop("access_level", None)

    for field, value in data.items():
        if field == "meeting_link":
            value = value or None
        setattr(org, field, value)

    if access_level and org.user:
        org.user.role = _ACCESS_ROLE.get(access_level, UserRole.ORGANIZER)

    await db.flush()
    return org


# --- Admin: replace availability rules ---
@router.put("/admin/organizers/{organizer_id}/availability", response_model=OrganizerOut)
async def replace_availability(
    organizer_id: uuid.UUID,
    rules: list[AvailabilityRuleIn],
    db: AsyncSession = Depends(get_db),
    _: User = Depends(require_roles(UserRole.ADMIN)),
) -> Organizer:
    result = await db.execute(
        select(Organizer)
        .where(Organizer.id == organizer_id)
        .options(
            selectinload(Organizer.availability_rules),
            selectinload(Organizer.holidays),
            selectinload(Organizer.user),
        )
    )
    org = result.scalar_one_or_none()
    if not org:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Organizer not found")
    existing = await db.execute(
        select(AvailabilityRule).where(AvailabilityRule.organizer_id == organizer_id)
    )
    for rule in existing.scalars().all():
        await db.delete(rule)
    for r in rules:
        db.add(AvailabilityRule(
            organizer_id=organizer_id,
            weekday=r.weekday,
            start_time=r.start_time,
            end_time=r.end_time,
        ))
    await db.flush()
    await db.refresh(org, ["availability_rules", "holidays", "user"])
    return org


# --- Admin: add holiday ---
@router.post(
    "/admin/organizers/{organizer_id}/holidays",
    response_model=HolidayOut,
    status_code=status.HTTP_201_CREATED,
)
async def add_holiday(
    organizer_id: uuid.UUID,
    payload: HolidayIn,
    db: AsyncSession = Depends(get_db),
    _: User = Depends(require_roles(UserRole.ADMIN)),
) -> Holiday:
    org = await db.get(Organizer, organizer_id)
    if not org:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Organizer not found")
    holiday = Holiday(organizer_id=organizer_id, day=payload.day, label=payload.label)
    db.add(holiday)
    await db.flush()
    return holiday


# --- Admin: delete holiday ---
@router.delete(
    "/admin/organizers/{organizer_id}/holidays/{holiday_id}",
    status_code=status.HTTP_204_NO_CONTENT,
)
async def delete_holiday(
    organizer_id: uuid.UUID,
    holiday_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    _: User = Depends(require_roles(UserRole.ADMIN)),
) -> Response:
    holiday = await db.get(Holiday, holiday_id)
    if not holiday or holiday.organizer_id != organizer_id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Holiday not found")
    await db.delete(holiday)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


# --- Bookings (admin: all, organizer: own) ---
@router.get("/bookings/list", response_model=list[BookingListItem])
async def list_bookings(
    skip: int = 0,
    limit: int = 50,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
) -> list[Booking]:
    q = (
        select(Booking)
        .options(selectinload(Booking.lead), selectinload(Booking.organizer))
        .order_by(Booking.created_at.desc())
        .offset(skip)
        .limit(limit)
    )
    if user.role != UserRole.ADMIN:
        # Scope to the organizer linked to this user
        org_result = await db.execute(
            select(Organizer).where(Organizer.user_id == user.id)
        )
        org = org_result.scalar_one_or_none()
        if org:
            q = q.where(Booking.organizer_id == org.id)
        else:
            return []
    result = await db.execute(q)
    return list(result.scalars().all())


# --- Keep /admin/bookings for backwards compat ---
@router.get("/admin/bookings", response_model=list[BookingListItem])
async def admin_list_bookings(
    skip: int = 0,
    limit: int = 50,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
) -> list[Booking]:
    return await list_bookings(skip=skip, limit=limit, db=db, user=user)


# --- Analytics (admin: all, organizer: own) ---
@router.get("/analytics/summary", response_model=AnalyticsSummary)
async def analytics_summary(
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
) -> AnalyticsSummary:
    organizer_id: uuid.UUID | None = None
    if user.role != UserRole.ADMIN:
        org_result = await db.execute(
            select(Organizer).where(Organizer.user_id == user.id)
        )
        org = org_result.scalar_one_or_none()
        organizer_id = org.id if org else None

    def _booking_q(extra=None):
        q = select(func.count(Booking.id))
        if organizer_id:
            q = q.where(Booking.organizer_id == organizer_id)
        if extra is not None:
            q = q.where(extra)
        return q

    total_leads = (await db.execute(select(func.count(Lead.id)))).scalar() or 0
    total_bookings = (await db.execute(_booking_q())).scalar() or 0
    confirmed = (await db.execute(_booking_q(
        Booking.state.in_([
            BookingState.BOOKING_CONFIRMED,
            BookingState.REMINDER_SENT,
            BookingState.MEETING_COMPLETED,
        ])
    ))).scalar() or 0
    cancelled = (await db.execute(_booking_q(Booking.state == BookingState.CANCELLED))).scalar() or 0
    denom = total_bookings if organizer_id else total_leads
    rate = (confirmed / denom) if denom else 0.0
    return AnalyticsSummary(
        total_leads=total_leads,
        total_bookings=total_bookings,
        confirmed=confirmed,
        cancelled=cancelled,
        conversion_rate=round(rate, 4),
    )


# --- Health checks ---
@router.get("/health/live")
async def liveness() -> dict:
    return {"status": "ok"}


@router.get("/health/ready")
async def readiness(db: AsyncSession = Depends(get_db)) -> dict:
    from app.core.redis_client import get_redis
    await db.execute(select(1))
    await get_redis().ping()
    return {"status": "ready"}
