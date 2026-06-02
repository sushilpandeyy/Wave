"""Pool health tracking in Redis.

- Liveness: each worker heartbeats into a sorted set (score = last-seen epoch);
  stale entries are pruned by score.
- Load: a single in-flight counter (incr on pickup, decr on finish).
- Quality: rolling latency EWMA + error rate, written once per finished job.

All writes are cheap and batched; nothing here is on the per-token path.
"""

import time

from redis.asyncio import Redis

WORKERS_KEY = "wave:workers"        # ZSET worker_id -> last_seen
INFLIGHT_KEY = "wave:inflight"      # INT currently-processing jobs
HEALTH_KEY = "wave:health"          # HASH latency_ewma, err_rate

_EWMA_ALPHA = 0.2                   # weight of the newest sample in the rolling gauges


class HealthRegistry:
    def __init__(self, redis: Redis):
        self._redis = redis

    async def heartbeat(self, worker_id: str) -> None:
        await self._redis.zadd(WORKERS_KEY, {worker_id: time.time()})

    async def leave(self, worker_id: str) -> None:
        await self._redis.zrem(WORKERS_KEY, worker_id)

    async def alive_count(self, ttl_s: float) -> int:
        cutoff = time.time() - ttl_s
        return await self._redis.zcount(WORKERS_KEY, cutoff, "+inf")

    async def prune_stale(self, ttl_s: float) -> int:
        cutoff = time.time() - ttl_s
        return await self._redis.zremrangebyscore(WORKERS_KEY, 0, cutoff)

    async def job_started(self) -> None:
        await self._redis.incr(INFLIGHT_KEY)

    async def job_finished(self, *, latency_s: float, ok: bool) -> None:
        """Record one completed job: decr in-flight + fold into EWMA/err-rate."""
        pipe = self._redis.pipeline()
        pipe.decr(INFLIGHT_KEY)
        # EWMA with alpha=0.2; HINCRBYFLOAT can't do EWMA atomically, so we read+write
        # only the rolled-up fields — coarse but cheap and good enough for a gauge.
        pipe.hget(HEALTH_KEY, "latency_ewma")
        pipe.hget(HEALTH_KEY, "err_rate")
        _, prev_lat, prev_err = await pipe.execute()

        a = _EWMA_ALPHA
        lat = (1 - a) * float(prev_lat or latency_s) + a * latency_s
        err = (1 - a) * float(prev_err or 0.0) + a * (0.0 if ok else 1.0)
        await self._redis.hset(HEALTH_KEY, mapping={"latency_ewma": lat, "err_rate": err})

    async def snapshot(self, ttl_s: float) -> dict:
        pipe = self._redis.pipeline()
        pipe.get(INFLIGHT_KEY)
        pipe.hgetall(HEALTH_KEY)
        inflight, health = await pipe.execute()
        alive = await self.alive_count(ttl_s)
        latency = float(health.get("latency_ewma", 0.0)) if health else 0.0
        err = float(health.get("err_rate", 0.0)) if health else 0.0
        return {
            "workers_alive": alive,
            "in_flight": int(inflight or 0),
            "latency_ewma_s": round(latency, 3),
            "error_rate": round(err, 3),
            "pool_score": round(max(0.0, 1.0 - err), 3),  # 1.0 = healthy
        }
