"""Async Redis client and a safe distributed lock.

The lock is a single-instance Redlock: SET key value NX PX ttl to acquire, and
a Lua compare-and-delete to release so we never delete a lock we no longer own
(the classic race where our lock expires, another worker takes it, and we then
delete theirs). For multi-node Redis you would extend this to the full Redlock
algorithm across N independent masters.
"""
from __future__ import annotations

import uuid
from contextlib import asynccontextmanager
from typing import AsyncIterator

import redis.asyncio as redis

from app.core.config import settings
from app.core.logging import get_logger

log = get_logger(__name__)

_RELEASE_LUA = """
if redis.call("get", KEYS[1]) == ARGV[1] then
    return redis.call("del", KEYS[1])
else
    return 0
end
"""

_pool: redis.Redis | None = None


def get_redis() -> redis.Redis:
    global _pool
    if _pool is None:
        _pool = redis.from_url(
            str(settings.REDIS_URL),
            encoding="utf-8",
            decode_responses=True,
            max_connections=50,
            health_check_interval=30,
        )
    return _pool


class LockNotAcquired(Exception):
    """Raised when a distributed lock could not be obtained."""


@asynccontextmanager
async def distributed_lock(
    key: str,
    ttl_seconds: int | None = None,
    wait: bool = False,
) -> AsyncIterator[str]:
    """Acquire a distributed lock.

    Args:
        key: logical lock name (we namespace it as lock:{key}).
        ttl_seconds: auto-expiry so a crashed holder never deadlocks others.
        wait: if False, fail fast (used for slot reservation — first wins).

    Yields the lock token; raises LockNotAcquired if it cannot be taken.
    """
    r = get_redis()
    lock_key = f"lock:{key}"
    token = str(uuid.uuid4())
    ttl = (ttl_seconds or settings.LOCK_TIMEOUT_SECONDS) * 1000

    acquired = await r.set(lock_key, token, nx=True, px=ttl)
    if not acquired and wait:
        # Bounded spin; for heavy contention prefer a blocking primitive.
        import asyncio

        deadline = settings.LOCK_TIMEOUT_SECONDS
        step = 0.05
        waited = 0.0
        while waited < deadline and not acquired:
            await asyncio.sleep(step)
            waited += step
            acquired = await r.set(lock_key, token, nx=True, px=ttl)

    if not acquired:
        raise LockNotAcquired(key)

    try:
        yield token
    finally:
        try:
            await r.eval(_RELEASE_LUA, 1, lock_key, token)
        except Exception:  # pragma: no cover - release is best effort
            log.warning("lock_release_failed", key=key)
