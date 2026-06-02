"""The tier-aware load balancer.

Enqueue side only — workers pull. The whole admit-or-shed decision plus the push is
one atomic Redis round-trip (a Lua script), so the request hot path never races and
never makes a second call. Dequeue is a single blocking BZPOPMIN.
"""

import json
import time

from redis.asyncio import Redis

from app.models import Tier
from app.tiers import LANE

QUEUE_PREFIX = "wave:q"
PRESSURE_KEY = "wave:pressure"


def queue_key(lane: str) -> str:
    return f"{QUEUE_PREFIX}:{lane}"


# Atomic admit-and-enqueue. Reads live pressure, sheds free first, else pushes.
# KEYS[1] = queue key
# ARGV: 1=member(json) 2=score 3=lane 4=free_hard_cap
_ADMIT_LUA = """
local pressure = tonumber(redis.call('GET', KEYS[2]) or '0')
if ARGV[3] == 'free' then
  if pressure >= 3 then return 'rejected' end
  if redis.call('ZCARD', KEYS[1]) >= tonumber(ARGV[4]) then return 'rejected' end
end
redis.call('ZADD', KEYS[1], ARGV[2], ARGV[1])
return 'ok'
"""


class LoadBalancer:
    def __init__(self, redis: Redis, free_hard_cap: int):
        self._redis = redis
        self._free_hard_cap = free_hard_cap
        self._admit = redis.register_script(_ADMIT_LUA)

    async def admit(
        self, *, message_id: str, user_id: str, session_id: str, tier: Tier, text: str
    ) -> bool:
        """Admit + enqueue in one round-trip. Returns False if load-shed."""
        lane = LANE[tier]
        payload = json.dumps(
            {
                "message_id": message_id,
                "user_id": user_id,
                "session_id": session_id,
                "tier": tier.value,
                "text": text,
                "enqueued_at": time.time(),  # for queue-wait tracing in the worker
            }
        )
        result = await self._admit(
            keys=[queue_key(lane), PRESSURE_KEY],
            args=[payload, time.time(), lane, self._free_hard_cap],
        )
        return result == "ok"

    async def dequeue_blocking(self, lanes: list[str], timeout: float) -> dict | None:
        """Pop the oldest job from the first non-empty lane in `lanes`.

        BZPOPMIN scans keys left-to-right, so the lane order encodes priority. Blocks
        up to `timeout` (no busy polling); returns None on timeout so the worker can
        re-heartbeat and loop.
        """
        keys = [queue_key(lane) for lane in lanes]
        result = await self._redis.bzpopmin(keys, timeout=timeout)
        if result is None:
            return None
        _key, member, _score = result
        return json.loads(member)

    async def lane_depth(self, lane: str) -> int:
        return await self._redis.zcard(queue_key(lane))

    async def snapshot(self) -> dict:
        """Depths of all lanes + current pressure, in one pipeline."""
        pipe = self._redis.pipeline()
        pipe.zcard(queue_key("ent"))
        pipe.zcard(queue_key("prem"))
        pipe.zcard(queue_key("free"))
        pipe.get(PRESSURE_KEY)
        ent, prem, free, pressure = await pipe.execute()
        return {
            "depth": {"ent": ent, "prem": prem, "free": free},
            "backlog": ent + prem + free,
            "pressure": int(pressure or 0),
        }
