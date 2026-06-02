"""Routing: decide which queue lane / worker pool a message goes to.

Messages are pushed onto a per-lane Redis sorted set used as a priority queue
(score = priority, lower served first). Workers pop from their assigned lanes.
"""

import json
import time

from redis.asyncio import Redis

from app.core.config import settings
from app.core.tiers import QueueLane, TierPolicy


def lane_key(lane: QueueLane) -> str:
    return f"{settings.queue_prefix}:{lane.value}"


class MessageRouter:
    def __init__(self, redis: Redis):
        self._redis = redis

    async def enqueue(
        self,
        message_id: str,
        policy: TierPolicy,
        payload_extra: dict | None = None,
    ) -> QueueLane:
        """Place a message on its tier's lane with a priority score.

        Score blends tier priority with arrival time so higher tiers jump the
        queue but no message starves indefinitely.
        """
        score = policy.priority * 1e12 + time.time()
        payload = {
            "message_id": message_id,
            "tier": policy.tier.value,
            "timeout_s": policy.request_timeout_s,
            "model_quality": policy.model_quality,
            "max_context": policy.max_context_messages,
        }
        if payload_extra:
            payload.update(payload_extra)
        await self._redis.zadd(lane_key(policy.lane), {json.dumps(payload): score})
        return policy.lane

    async def dequeue(self, lane: QueueLane) -> dict | None:
        """Atomically pop the highest-priority job from a lane (non-blocking)."""
        result = await self._redis.zpopmin(lane_key(lane), count=1)
        if not result:
            return None
        payload, _score = result[0]
        return json.loads(payload)

    async def dequeue_blocking(
        self, lanes: list[QueueLane], timeout: float
    ) -> dict | None:
        """Block until a job is available, honoring lane order (priority).

        BZPOPMIN scans the given keys left-to-right and pops from the first
        non-empty one, so passing [HIGH, LOW] drains premium traffic first.
        Returns None on timeout so the worker can re-heartbeat and loop.
        """
        keys = [lane_key(lane) for lane in lanes]
        result = await self._redis.bzpopmin(keys, timeout=timeout)
        if result is None:
            return None
        _key, payload, _score = result
        return json.loads(payload)

    async def lane_depth(self, lane: QueueLane) -> int:
        """Number of jobs waiting in a lane."""
        return await self._redis.zcard(lane_key(lane))

    async def oldest_age(self, lane: QueueLane, now: float) -> float:
        """Age (seconds) of the oldest waiting job in a lane, 0 if empty.

        Score = priority * 1e12 + arrival_ts, and arrival_ts < 1e12, so the
        arrival timestamp is recoverable as (score mod 1e12).
        """
        items = await self._redis.zrange(lane_key(lane), 0, 0, withscores=True)
        if not items:
            return 0.0
        _payload, score = items[0]
        arrival_ts = float(score) % 1e12
        return max(0.0, now - arrival_ts)
