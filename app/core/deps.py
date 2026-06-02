"""Shared FastAPI dependencies: auth, RBAC, and rate limiting."""
from __future__ import annotations

import time

import jwt
from fastapi import Depends, HTTPException, Request, status
from fastapi.security import OAuth2PasswordBearer
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.redis_client import get_redis
from app.core.security import decode_token
from app.db.base import UserRole
from app.db.models import User
from app.db.session import get_db

oauth2 = OAuth2PasswordBearer(tokenUrl=f"{settings.API_V1_PREFIX}/auth/login")


async def get_current_user(
    token: str = Depends(oauth2), db: AsyncSession = Depends(get_db)
) -> User:
    cred_exc = HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Could not validate credentials",
        headers={"WWW-Authenticate": "Bearer"},
    )
    try:
        payload = decode_token(token)
        if payload.get("type") != "access":
            raise cred_exc
        user_id = payload["sub"]
    except (jwt.PyJWTError, KeyError):
        raise cred_exc

    user = (
        await db.execute(select(User).where(User.id == user_id))
    ).scalar_one_or_none()
    if user is None or not user.is_active:
        raise cred_exc
    return user


def require_roles(*roles: UserRole):
    async def _checker(user: User = Depends(get_current_user)) -> User:
        if user.role not in roles:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN, detail="Insufficient role"
            )
        return user

    return _checker


async def rate_limit(request: Request) -> None:
    """Fixed-window per-IP limiter backed by Redis INCR + EXPIRE.
    Degrades gracefully if Redis is unavailable (skips limiting rather than blocking all traffic).
    """
    try:
        ip = request.client.host if request.client else "unknown"
        window = int(time.time() // 60)
        key = f"rl:{ip}:{window}"
        r = get_redis()
        count = await r.incr(key)
        if count == 1:
            await r.expire(key, 60)
        if count > settings.RATE_LIMIT_PER_MINUTE:
            raise HTTPException(
                status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                detail="Rate limit exceeded",
            )
    except HTTPException:
        raise
    except Exception:
        pass  # Redis unavailable — allow the request through
