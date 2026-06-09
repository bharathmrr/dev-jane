"""Run async service code from synchronous Celery tasks.

Each task call gets a completely fresh engine + session bound to its own
event loop. This avoids the asyncpg "Future attached to a different loop"
error that occurs when a module-level engine is reused across asyncio.run()
calls (each call creates a new event loop, making old connections stale).
"""
from __future__ import annotations

import asyncio
from typing import Awaitable, Callable, TypeVar

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.core.config import settings

T = TypeVar("T")

_db_url = str(settings.DATABASE_URL)
_connect_args: dict = {}
if "-pooler." in _db_url or "pgbouncer" in _db_url.lower():
    _connect_args["statement_cache_size"] = 0


def run_async(coro_factory: Callable[..., Awaitable[T]], *args, **kwargs) -> T:
    async def _wrapped() -> T:
        engine = create_async_engine(
            _db_url,
            pool_size=2,
            max_overflow=0,
            pool_timeout=30,
            pool_pre_ping=True,
            echo=settings.DEBUG,
            connect_args=_connect_args,
        )
        session_factory = async_sessionmaker(
            engine, class_=AsyncSession, expire_on_commit=False, autoflush=False
        )
        try:
            async with session_factory() as db:
                try:
                    result = await coro_factory(db, *args, **kwargs)
                    await db.commit()
                    return result
                except Exception:
                    await db.rollback()
                    raise
        finally:
            await engine.dispose()

    return asyncio.run(_wrapped())
