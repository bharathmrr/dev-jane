from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.responses import HTMLResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.security import (
    create_access_token,
    create_refresh_token,
    verify_password,
)
from app.db.models import Organizer, User
from app.db.session import get_db
from app.schemas import LoginRequest, Token, UserOut
from app.core.deps import get_current_user

router = APIRouter(prefix="/auth", tags=["auth"])


@router.get("/zoho/callback", response_class=HTMLResponse)
async def zoho_oauth_callback(request: Request) -> HTMLResponse:
    """Temporary endpoint to capture Zoho OAuth authorization code."""
    code = request.query_params.get("code", "")
    error = request.query_params.get("error", "")
    if error:
        return HTMLResponse(f"<h2>Error: {error}</h2>")
    return HTMLResponse(f"""
    <html><body style="font-family:monospace;padding:40px;background:#111;color:#0f0">
    <h2 style="color:#fff">Zoho OAuth Code</h2>
    <p>Copy this code and paste it to Claude:</p>
    <div style="background:#222;padding:20px;border-radius:8px;font-size:14px;word-break:break-all;color:#0f0">
    {code}
    </div>
    <button onclick="navigator.clipboard.writeText('{code}')"
            style="margin-top:20px;padding:10px 20px;background:#0070f3;color:#fff;border:none;border-radius:6px;cursor:pointer;font-size:14px">
    Copy Code
    </button>
    </body></html>
    """)


@router.post("/login", response_model=Token)
async def login(payload: LoginRequest, db: AsyncSession = Depends(get_db)) -> Token:
    user = (
        await db.execute(select(User).where(User.email == payload.email))
    ).scalar_one_or_none()
    if not user or not verify_password(payload.password, user.hashed_password):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid credentials"
        )
    return Token(
        access_token=create_access_token(str(user.id), user.role.value),
        refresh_token=create_refresh_token(str(user.id)),
    )


@router.get("/me", response_model=UserOut)
async def me(
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> UserOut:
    organizer_id = None
    # Populate organizer_id so front-end knows which organizer this user owns
    org = (
        await db.execute(select(Organizer).where(Organizer.user_id == user.id))
    ).scalar_one_or_none()
    if org:
        organizer_id = org.id
    return UserOut(
        id=user.id,
        email=user.email,
        full_name=user.full_name,
        role=user.role,
        is_active=user.is_active,
        organizer_id=organizer_id,
    )
