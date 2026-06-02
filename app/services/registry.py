"""Worker heartbeat registry (Redis sorted set, score = last-seen epoch).

The pull-based pool doesn't use this to route work — it's the liveness +
autoscaling signal. Each worker bumps its heartbeat on every loop; the manager
prunes any worker whose heartbeat is older than `heartbeat_ttl_s`.
"""

import time

from redis.asyncio import Redis

_KEY = "wave:workers:alive"


class WorkerRegistry:
    def __init__(self, redis: Redis):
        self._redis = redis

    async def heartbeat(self, worker_id: str, now: float | None = None) -> None:
        await self._redis.zadd(_KEY, {worker_id: now or time.time()})

    async def remove(self, worker_id: str) -> None:
        await self._redis.zrem(_KEY, worker_id)

    async def prune_stale(self, ttl_s: float, now: float | None = None) -> list[str]:
        """Drop workers not seen within `ttl_s`; return the evicted ids."""
        cutoff = (now or time.time()) - ttl_s
        stale = await self._redis.zrangebyscore(_KEY, min=0, max=cutoff)
        if stale:
            await self._redis.zrem(_KEY, *stale)
        return stale

    async def alive_count(self, ttl_s: float, now: float | None = None) -> int:
        cutoff = (now or time.time()) - ttl_s
        return await self._redis.zcount(_KEY, min=cutoff, max="+inf")

    async def list_alive(self, ttl_s: float, now: float | None = None) -> list[str]:
        cutoff = (now or time.time()) - ttl_s
        return await self._redis.zrangebyscore(_KEY, min=cutoff, max="+inf")
