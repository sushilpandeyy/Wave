from redis.asyncio import ConnectionPool, Redis

from app.core.config import settings

_pool: ConnectionPool | None = None


def get_redis() -> Redis:
    """Return a Redis client backed by a shared connection pool.

    Used for rate limiting, the message queue, and hot caches.
    """
    global _pool
    if _pool is None:
        _pool = ConnectionPool.from_url(
            settings.redis_url,
            decode_responses=True,
            max_connections=50,
        )
    return Redis(connection_pool=_pool)


async def close_redis() -> None:
    global _pool
    if _pool is not None:
        await _pool.disconnect()
        _pool = None
