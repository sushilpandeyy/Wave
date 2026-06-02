"""Autoscaler (control plane) and WorkerSupervisor (data plane).

Three pools, matching the architecture diagram:
- **priority** — enterprise-only reserve (fixed floor); nothing else can touch it.
- **standard** — always-warm general workers (fixed floor).
- **overflow** — elastic general capacity the autoscaler ramps up under load and back
  down when idle, capped by the W_MAX budget.

The Autoscaler (the `manager` service) turns every routing input — system load, pool
health, and latency — into the *single* `pressure` level that admission and degradation
already read, plus a circuit-breaker flag. So health and latency genuinely steer routing
without a parallel decision path. The WorkerSupervisor runs in each `worker` container and
keeps its fair share of each pool's coroutines alive.
"""

import asyncio
import math
import time

from redis.asyncio import Redis

from app.balancer import LoadBalancer
from app.config import settings
from app.health import HealthRegistry

TARGET_KEY = "wave:pool:target"     # HASH priority, standard, overflow
PRESSURE_KEY = "wave:pressure"
CIRCUIT_KEY = "wave:circuit"        # "1" while a pool is unhealthy
CONTAINERS_KEY = "wave:containers"  # ZSET container_id -> last_seen

POOLS = ("priority", "standard", "overflow")


def _level(ratio: float) -> int:
    """Map any "how far over the line" ratio to a 0..3 pressure level."""
    if ratio >= settings.pressure_l3:
        return 3
    if ratio >= settings.pressure_l2:
        return 2
    if ratio >= settings.pressure_l1:
        return 1
    return 0


def _ramp(current: int, target: int, step: int) -> int:
    """Move `current` toward `target` by at most `step` — gentle scaling, no thrash."""
    if target > current:
        return min(target, current + step)
    return max(target, current - step)


class Autoscaler:
    def __init__(self, redis: Redis):
        self._redis = redis
        self._lb = LoadBalancer(redis, settings.free_hard_cap)
        self._health = HealthRegistry(redis)

    async def run(self) -> None:
        while True:
            try:
                await self._tick()
            except Exception:  # control loop must never die
                pass
            await asyncio.sleep(settings.monitor_interval_s)

    async def _tick(self) -> None:
        await self._health.prune_stale(settings.heartbeat_ttl_s)
        load = await self._lb.snapshot()
        health = await self._health.snapshot(settings.heartbeat_ttl_s)
        shared_backlog = load["depth"]["prem"] + load["depth"]["free"]

        # Circuit breaker: an erroring pool shouldn't be fed more load or more workers.
        circuit = health["error_rate"] >= settings.error_circuit

        # Fixed floors; overflow is the only elastic dial, ramped gently within budget.
        priority, standard = settings.enterprise_floor, settings.standard_floor
        demand_total = min(
            settings.w_max,
            priority + standard + math.ceil(load["backlog"] / settings.backlog_per_worker),
        )
        demand_overflow = max(0, demand_total - priority - standard)
        current = int(await self._redis.hget(TARGET_KEY, "overflow") or 0)
        if circuit:
            demand_overflow = min(demand_overflow, current)  # freeze growth while unhealthy
        overflow = max(0, _ramp(current, demand_overflow, settings.ramp_step))

        # Pressure = worst of system load, latency, and the breaker.
        general_capacity = standard + overflow
        pressure = max(
            _level(shared_backlog / max(1, general_capacity)),
            _level(health["latency_ewma_s"] / settings.latency_target_s),
        )
        if circuit:
            pressure = 3

        pipe = self._redis.pipeline()
        pipe.hset(TARGET_KEY, mapping={"priority": priority, "standard": standard, "overflow": overflow})
        pipe.set(PRESSURE_KEY, pressure)
        pipe.set(CIRCUIT_KEY, 1 if circuit else 0)
        await pipe.execute()


class WorkerSupervisor:
    """Keeps this container's share of each pool's worker coroutines running."""

    def __init__(self, redis: Redis, container_id: str, make_worker):
        self._redis = redis
        self._id = container_id
        self._make_worker = make_worker  # (worker_id, pool) -> coroutine
        self._tasks: dict[str, asyncio.Task] = {}

    async def run(self) -> None:
        while True:
            try:
                await self._reconcile()
            except Exception:
                pass
            await asyncio.sleep(settings.monitor_interval_s)

    def _share(self, total: int, num: int, index: int) -> int:
        """Deterministic split so per-container shares sum exactly to `total`."""
        base, rem = divmod(total, num)
        return base + (1 if index < rem else 0)

    async def _reconcile(self) -> None:
        now = time.time()
        await self._redis.zadd(CONTAINERS_KEY, {self._id: now})
        await self._redis.zremrangebyscore(CONTAINERS_KEY, 0, now - settings.heartbeat_ttl_s)
        ids = sorted(
            await self._redis.zrangebyscore(
                CONTAINERS_KEY, now - settings.heartbeat_ttl_s, "+inf"
            )
        )
        num = max(1, len(ids))
        index = ids.index(self._id) if self._id in ids else 0

        target = await self._redis.hgetall(TARGET_KEY)
        want = {p: self._share(int(target.get(p, 0)), num, index) for p in POOLS}

        # Respect the per-container ceiling — trim elastic pools first, never priority.
        budget = settings.workers_per_container
        for pool in ("overflow", "standard"):
            over = sum(want.values()) - budget
            if over > 0:
                want[pool] = max(0, want[pool] - over)

        for pool in POOLS:
            self._scale(pool, want[pool])

    def _scale(self, pool: str, want: int) -> None:
        # Drop finished tasks, then size the pool to `want`.
        for wid in list(self._tasks):
            if self._tasks[wid].done():
                self._tasks.pop(wid, None)
        running = [wid for wid in self._tasks if wid.startswith(pool + ":")]

        while len(running) < want:
            wid = f"{pool}:{self._id}:{len(self._tasks)}:{time.time_ns()}"
            self._tasks[wid] = asyncio.create_task(self._make_worker(wid, pool))
            running.append(wid)
        while len(running) > want:
            self._tasks.pop(running.pop()).cancel()

    async def shutdown(self) -> None:
        for task in self._tasks.values():
            task.cancel()
        await asyncio.gather(*self._tasks.values(), return_exceptions=True)
        await self._redis.zrem(CONTAINERS_KEY, self._id)
