"""Per-user, tier-aware token-bucket rate limiting.

One atomic Lua op does the whole thing — refill by elapsed time, take a token, and
report how close to empty we are — so the hot path is a single round-trip with no
races. The bucket auto-expires when idle, so dormant users cost nothing.

The *graceful* part (what we say, and not spamming) lives in the api layer via the
NoticeGate; here we only decide allow/deny + "approaching".
"""

import time
from dataclasses import dataclass
from math import ceil

from redis.asyncio import Redis

from app.config import settings
from app.tiers import TierPolicy

# KEYS[1] = bucket hash;  ARGV: 1=rpm 2=burst 3=now 4=approaching_tokens
# Returns {allowed, remaining, approaching}.
_BUCKET_LUA = """
local rpm, burst, now = tonumber(ARGV[1]), tonumber(ARGV[2]), tonumber(ARGV[3])
local b = redis.call('HMGET', KEYS[1], 'tokens', 'ts')
local tokens = tonumber(b[1]) or burst
local ts = tonumber(b[2]) or now
tokens = math.min(burst, tokens + (now - ts) * rpm / 60.0)
local allowed = 0
if tokens >= 1 then tokens = tokens - 1; allowed = 1 end
redis.call('HSET', KEYS[1], 'tokens', tokens, 'ts', now)
redis.call('EXPIRE', KEYS[1], math.ceil(burst / rpm * 60) + 60)
local remaining = math.floor(tokens)
local approaching = (remaining <= tonumber(ARGV[4])) and 1 or 0
return {allowed, remaining, approaching}
"""


@dataclass(frozen=True)
class Decision:
    allowed: bool
    remaining: int
    approaching: bool


class RateLimiter:
    def __init__(self, redis: Redis):
        self._redis = redis
        self._bucket = redis.register_script(_BUCKET_LUA)

    async def _take(self, key: str, rpm: int, burst: int, approaching_tokens: int) -> Decision:
        allowed, remaining, approaching = await self._bucket(
            keys=[key], args=[rpm, burst, time.time(), approaching_tokens]
        )
        return Decision(bool(allowed), int(remaining), bool(approaching))

    async def check(self, user_id: str, policy: TierPolicy) -> Decision:
        """Per-user, tier-aware limit — the conversational rate (with 'approaching')."""
        approaching = max(1, ceil(policy.burst * settings.approaching_frac))
        return await self._take(f"wave:rl:{user_id}", policy.rpm, policy.burst, approaching)

    async def check_ip(self, ip: str) -> Decision:
        """Coarse per-IP guard at connection accept — catches floods / fake user_ids.

        Unauthenticated edge defense; production would also do this at the GCP LB.
        """
        return await self._take(f"wave:rl:ip:{ip}", settings.ip_rpm, settings.ip_burst, 0)
