"""Non-blocking analytics pipeline.

`track()` is the only thing on the hot path: it builds a small dict and does an in-memory
`put_nowait` — no await, sub-millisecond. A background task batches the queue and flushes to
Redis (per-minute counters, timing aggregates, and a capped raw-event stream) off the critical
path. Under pressure the queue sheds *verbose* events first and keeps critical ones; on shutdown
it drains and does a final flush so nothing in-flight is lost.
"""

import asyncio
import json
import time

from app import obs
from app.config import settings
from app.redis import get_redis

# Events we keep even when the queue is under pressure (lifecycle / signals).
_CRITICAL = {
    "received", "delivered", "completed", "error", "timeout",
    "shed", "rate_limited", "safety", "crisis",
}


class AnalyticsPipeline:
    def __init__(self) -> None:
        self._q: asyncio.Queue = asyncio.Queue(maxsize=settings.analytics_queue_max)
        self._watermark = int(settings.analytics_queue_max * settings.analytics_watermark)
        self._task: asyncio.Task | None = None
        self._running = False
        self.dropped = 0

    # --- hot path (must stay < 1ms, never await) ---------------------------------
    def track(self, event: str, *, critical: bool = False, **fields) -> None:
        keep = critical or event in _CRITICAL
        if not keep and self._q.qsize() >= self._watermark:
            self.dropped += 1  # shed verbose telemetry first
            return
        try:
            self._q.put_nowait({"event": event, "ts": time.time(), **fields})
        except asyncio.QueueFull:
            self.dropped += 1

    # --- background flusher ------------------------------------------------------
    def start(self) -> None:
        if self._task is None:
            self._running = True
            self._task = asyncio.create_task(self._run())

    async def _run(self) -> None:
        while True:
            batch = await self._collect()
            if batch:
                await self._flush(batch)
            elif not self._running:
                return

    async def _collect(self) -> list[dict]:
        try:
            first = await asyncio.wait_for(
                self._q.get(), timeout=settings.analytics_flush_ms / 1000.0
            )
        except asyncio.TimeoutError:
            return []
        batch = [first]
        while len(batch) < settings.analytics_batch:
            try:
                batch.append(self._q.get_nowait())
            except asyncio.QueueEmpty:
                break
        return batch

    async def _flush(self, batch: list[dict]) -> None:
        minute = int(time.time() // 60)
        pipe = get_redis().pipeline()
        for e in batch:
            ev, tier = e["event"], e.get("tier")
            pipe.incr(f"wave:metrics:{ev}:{minute}")
            pipe.expire(f"wave:metrics:{ev}:{minute}", 3600)
            if tier:
                pipe.incr(f"wave:metrics:{ev}:{tier}:{minute}")
                pipe.expire(f"wave:metrics:{ev}:{tier}:{minute}", 3600)
            if "ms" in e:  # timing aggregate -> averages without storing every sample
                key = f"wave:timing:{e.get('op', ev)}:{minute}"
                pipe.hincrbyfloat(key, "sum", float(e["ms"]))
                pipe.hincrby(key, "count", 1)
                pipe.expire(key, 3600)
            pipe.xadd(
                "wave:events", {"e": json.dumps(e, default=str)},
                maxlen=settings.events_stream_max, approximate=True,
            )
        try:
            await pipe.execute()
        except Exception:
            obs.warning("analytics_flush_failed", n=len(batch))

    async def aclose(self) -> None:
        """Stop intake, drain remaining events within a bounded time, final flush."""
        self._running = False
        if self._task is None:
            return
        try:
            await asyncio.wait_for(self._task, timeout=settings.shutdown_drain_s)
        except asyncio.TimeoutError:
            obs.warning("analytics_drain_timeout", remaining=self._q.qsize())
            self._task.cancel()
        self._task = None

    def stats(self) -> dict:
        return {"analytics_queued": self._q.qsize(), "analytics_dropped": self.dropped}


_pipeline = AnalyticsPipeline()


def start_analytics() -> None:
    _pipeline.start()


async def aclose_analytics() -> None:
    await _pipeline.aclose()


def track(event: str, *, critical: bool = False, **fields) -> None:
    """Fire-and-forget metric. Sub-ms, never blocks."""
    _pipeline.track(event, critical=critical, **fields)


def event(name: str, *, critical: bool = False, **fields) -> None:
    """A traced milestone: one structured log line + one analytics counter."""
    obs.info(name, **fields)
    _pipeline.track(name, critical=critical, **fields)


def stats() -> dict:
    return _pipeline.stats()
