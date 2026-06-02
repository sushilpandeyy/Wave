"""Worker pool manager + autoscaler.

Owns the in-process pool of `Worker` asyncio tasks. A monitor loop watches queue
depth / oldest-job age and scales the pool between `min_workers` and the
hardcoded `max_workers` knob, retiring workers that sit idle, and prunes dead
heartbeats from the registry.
"""

import asyncio
import time
import uuid

from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.core.config import settings
from app.core.logging import get_logger
from app.core.tiers import QueueLane
from app.services.llm import CompletionClient, MockCompletionClient
from app.services.registry import WorkerRegistry
from app.services.router import MessageRouter
from app.workers.worker import Worker

log = get_logger("manager")


class WorkerManager:
    def __init__(
        self,
        redis: Redis,
        session_factory: async_sessionmaker,
        llm: CompletionClient | None = None,
    ):
        self._redis = redis
        self._session_factory = session_factory
        self._llm = llm or MockCompletionClient()
        self._router = MessageRouter(redis)
        self._registry = WorkerRegistry(redis)
        self._workers: dict[str, tuple[Worker, asyncio.Task]] = {}
        self._monitor_task: asyncio.Task | None = None

    async def start(self) -> None:
        for _ in range(settings.min_workers):
            self._spawn()
        self._monitor_task = asyncio.create_task(self._monitor_loop())
        log.info("manager.start", workers=len(self._workers))

    async def stop(self) -> None:
        if self._monitor_task:
            self._monitor_task.cancel()
        for _worker, task in self._workers.values():
            task.cancel()
        await asyncio.gather(
            *(t for _w, t in self._workers.values()), return_exceptions=True
        )
        self._workers.clear()
        log.info("manager.stop")

    def _spawn(self) -> None:
        worker_id = f"w-{uuid.uuid4().hex[:8]}"
        worker = Worker(worker_id, self._redis, self._session_factory, self._llm)
        task = asyncio.create_task(worker.run())
        self._workers[worker_id] = (worker, task)
        log.info("manager.scale_up", worker_id=worker_id, total=len(self._workers))

    def _retire_one(self) -> None:
        """Cancel the most-idle worker (above min_workers)."""
        now = time.time()
        idlest = max(self._workers.items(), key=lambda kv: now - kv[1][0].last_active)
        worker_id, (_worker, task) = idlest
        task.cancel()
        self._workers.pop(worker_id, None)
        log.info("manager.scale_down", worker_id=worker_id, total=len(self._workers))

    async def _monitor_loop(self) -> None:
        try:
            while True:
                await asyncio.sleep(settings.monitor_interval_s)
                await self._evaluate()
        except asyncio.CancelledError:
            raise

    async def _evaluate(self) -> None:
        now = time.time()
        await self._registry.prune_stale(settings.heartbeat_ttl_s, now)

        high_depth = await self._router.lane_depth(QueueLane.HIGH)
        low_depth = await self._router.lane_depth(QueueLane.LOW)
        high_age = await self._router.oldest_age(QueueLane.HIGH, now)
        backlog = high_depth + low_depth
        current = len(self._workers)

        scale_up = (
            high_depth >= settings.scale_up_queue_depth
            or high_age >= settings.scale_up_age_s
            or backlog > current  # more waiting work than workers
        )

        if scale_up and current < settings.max_workers:
            self._spawn()
            return

        # Scale down: no backlog and the idlest worker has been quiet a while.
        if backlog == 0 and current > settings.min_workers:
            idle_for = now - min(w.last_active for w, _t in self._workers.values())
            if idle_for >= settings.scale_down_idle_s:
                self._retire_one()
