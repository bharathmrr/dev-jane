"""Async SQLAlchemy 2.0 engine, session factory, and FastAPI dependency."""
from __future__ import annotations

from typing import AsyncIterator

from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from app.core.config import settings

if not settings.DATABASE_URL:
    raise RuntimeError(
        "DATABASE_URL is not set. Add it to your .env file.\n"
        "Example: DATABASE_URL=postgresql+asyncpg://user:pass@host/dbname"
    )

_db_url = str(settings.DATABASE_URL)

# Neon (and PgBouncer) poolers require statement_cache_size=0 with asyncpg.
# We detect pooler endpoints by the "-pooler." hostname pattern.
_connect_args: dict = {}
if "-pooler." in _db_url or "pgbouncer" in _db_url.lower():
    _connect_args["statement_cache_size"] = 0

engine = create_async_engine(
    _db_url,
    pool_size=settings.DB_POOL_SIZE,
    max_overflow=settings.DB_MAX_OVERFLOW,
    pool_timeout=settings.DB_POOL_TIMEOUT,
    pool_pre_ping=True,
    echo=settings.DEBUG,
    connect_args=_connect_args,
)

SessionLocal = async_sessionmaker(
    engine, class_=AsyncSession, expire_on_commit=False, autoflush=False
)


async def get_db() -> AsyncIterator[AsyncSession]:
    """FastAPI dependency yielding a session with commit/rollback handling."""
    async with SessionLocal() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise
