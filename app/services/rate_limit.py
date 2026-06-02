"""Per-user, per-tier rate limiting using a Redis token bucket.

The bucket is implemented as an atomic Lua script so refill + consume happen
in a single round trip and stay correct under concurrency.
"""

from dataclasses import dataclass

from redis.asyncio import Redis

from app.core.tiers import TierPolicy

# KEYS[1] = bucket key
# ARGV   = rate_per_sec, burst, now (sec, float), requested_tokens
_TOKEN_BUCKET_LUA = """
local key = KEYS[1]
local rate = tonumber(ARGV[1])
local burst = tonumber(ARGV[2])
local now = tonumber(ARGV[3])
local requested = tonumber(ARGV[4])

local data = redis.call('HMGET', key, 'tokens', 'ts')
local tokens = tonumber(data[1])
local ts = tonumber(data[2])

if tokens == nil then
  tokens = burst
  ts = now
end

local elapsed = math.max(0, now - ts)
tokens = math.min(burst, tokens + elapsed * rate)

local allowed = 0
if tokens >= requested then
  tokens = tokens - requested
  allowed = 1
end

redis.call('HMSET', key, 'tokens', tokens, 'ts', now)
redis.call('EXPIRE', key, math.ceil(burst / rate) + 1)

return {allowed, tokens}
"""


@dataclass
class RateLimitResult:
    allowed: bool
    remaining: float


class RateLimiter:
    def __init__(self, redis: Redis):
        self._redis = redis
        self._script = redis.register_script(_TOKEN_BUCKET_LUA)

    async def check(
        self, user_id: str, policy: TierPolicy, now: float, cost: int = 1
    ) -> RateLimitResult:
        rate_per_sec = policy.requests_per_minute / 60.0
        key = f"wave:ratelimit:{user_id}"
        allowed, remaining = await self._script(
            keys=[key],
            args=[rate_per_sec, policy.burst, now, cost],
        )
        return RateLimitResult(allowed=bool(allowed), remaining=float(remaining))
