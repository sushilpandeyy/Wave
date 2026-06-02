"""Shared async Redis client.

One client per process; redis-py manages an internal connection pool, so every
coroutine reuses pooled connections (no per-call connect overhead).
"""

from redis.asyncio import Redis

from app.config import settings

_client: Redis | None = None


def get_redis() -> Redis:
    """Return the process-wide Redis client (created lazily)."""
    global _client
    if _client is None:
        _client = Redis.from_url(
            settings.redis_url, decode_responses=True
        )
    return _client


async def close_redis() -> None:
    global _client
    if _client is not None:
        await _client.aclose()
        _client = None
