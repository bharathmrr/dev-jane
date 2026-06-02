"""Run async service code from synchronous Celery tasks.

Each task gets its own event loop + async DB session. Using a dedicated loop per
invocation avoids cross-task loop reuse issues in prefork workers.
"""
from __future__ import annotations

import asyncio
from typing import Awaitable, Callable, TypeVar

from app.db.session import SessionLocal

T = TypeVar("T")


def run_async(coro_factory: Callable[..., Awaitable[T]], *args, **kwargs) -> T:
    async def _wrapped() -> T:
        async with SessionLocal() as db:
            try:
                result = await coro_factory(db, *args, **kwargs)
                await db.commit()
                return result
            except Exception:
                await db.rollback()
                raise

    return asyncio.run(_wrapped())
