"""Security primitives: password hashing, JWT, and HMAC reply tokens.

The HMAC reply token is embedded in outbound reply-to addresses / message
bodies so we can authenticate inbound replies and bind them to a thread
without trusting the From header (anti-spoofing).
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any

import jwt
from passlib.context import CryptContext

from app.core.config import settings

_pwd = CryptContext(schemes=["argon2"], deprecated="auto")


# --- Passwords ---
def hash_password(raw: str) -> str:
    return _pwd.hash(raw)


def verify_password(raw: str, hashed: str) -> bool:
    return _pwd.verify(raw, hashed)


# --- JWT ---
def create_access_token(
    subject: str, role: str, extra: dict[str, Any] | None = None
) -> str:
    now = datetime.now(timezone.utc)
    payload: dict[str, Any] = {
        "sub": subject,
        "role": role,
        "type": "access",
        "iat": now,
        "exp": now + timedelta(minutes=settings.ACCESS_TOKEN_TTL_MINUTES),
        "jti": str(uuid.uuid4()),
    }
    if extra:
        payload.update(extra)
    return jwt.encode(payload, settings.JWT_SECRET_KEY, algorithm=settings.JWT_ALGORITHM)


def create_refresh_token(subject: str) -> str:
    now = datetime.now(timezone.utc)
    payload = {
        "sub": subject,
        "type": "refresh",
        "iat": now,
        "exp": now + timedelta(days=settings.REFRESH_TOKEN_TTL_DAYS),
        "jti": str(uuid.uuid4()),
    }
    return jwt.encode(payload, settings.JWT_SECRET_KEY, algorithm=settings.JWT_ALGORITHM)


def decode_token(token: str) -> dict[str, Any]:
    """Raises jwt.PyJWTError subclasses on invalid/expired tokens."""
    return jwt.decode(
        token, settings.JWT_SECRET_KEY, algorithms=[settings.JWT_ALGORITHM]
    )


# --- HMAC reply tokens (anti-spoofing for inbound email) ---
def sign_thread_token(thread_id: str) -> str:
    """Return a short URL/email-safe token binding a thread id to our secret."""
    mac = hmac.new(
        settings.EMAIL_HMAC_SECRET.encode(), thread_id.encode(), hashlib.sha256
    ).digest()[:12]
    sig = base64.urlsafe_b64encode(mac).decode().rstrip("=")
    return f"{thread_id}.{sig}"


def verify_thread_token(token: str) -> str | None:
    """Return the thread id if the token is authentic, else None."""
    try:
        thread_id, _ = token.rsplit(".", 1)
    except ValueError:
        return None
    expected = sign_thread_token(thread_id)
    return thread_id if hmac.compare_digest(expected, token) else None
