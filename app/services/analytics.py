"""Analytics: fast in-flight counters in Redis.

Hot per-minute counters power live dashboards (messages/min, per-tier volume,
blocks, rate-limit hits). Durable roll-ups can later be derived from the Chats
table or shipped to a warehouse; we keep the runtime path lightweight here.
"""

import time

from redis.asyncio import Redis


class AnalyticsService:
    def __init__(self, redis: Redis):
        self._redis = redis

    async def track(
        self,
        event_type: str,
        *,
        tier: str | None = None,
    ) -> None:
        minute = int(time.time() // 60)
        pipe = self._redis.pipeline()
        pipe.incr(f"wave:metrics:{event_type}:{minute}")
        pipe.expire(f"wave:metrics:{event_type}:{minute}", 3600)
        if tier:
            pipe.incr(f"wave:metrics:{event_type}:{tier}:{minute}")
            pipe.expire(f"wave:metrics:{event_type}:{tier}:{minute}", 3600)
        await pipe.execute()
