"""Pull-based worker + the worker-container entrypoint.

Each worker is an asyncio coroutine. Its loop is deliberately tight: one pipelined
heartbeat-and-pressure read, one blocking BZPOPMIN, then process. No polling, no
speculative reads, metrics written once per job.

Run a worker container with:  python -m app.worker
"""

import asyncio
import os
import random
import time
import uuid

from redis.asyncio import Redis

from app.balancer import PRESSURE_KEY, LoadBalancer
from app.config import settings
from app.db import SessionLocal
from app.health import WORKERS_KEY, HealthRegistry
from app.llm import MockCompletionClient
from app.models import MessageRole, Tier
from app.pool import WorkerSupervisor
from app.prompt import build_prompt
from app.queries import add_message, get_personality, recent_messages
from app.redis import get_redis
from app.safety import SafetyScreener
from app.streaming import StreamBus
from app.tiers import LANES_BY_PRIORITY, degrade
from app.voice import say

# Stateless after compile — one shared instance per worker process.
_screener = SafetyScreener()


def _lanes(pool: str) -> list[str]:
    """Lane scan order for one pull. The priority pool serves enterprise only.

    Standard/overflow workers scan in strict priority (enterprise first), but with
    chance `fairness` the lowest lane jumps to the front so it always drains —
    graceful, never starved — while the priority pool keeps enterprise fast-tracked.
    """
    if pool == "priority":
        return LANES_BY_PRIORITY[:1]
    if random.random() < settings.fairness:
        return [LANES_BY_PRIORITY[-1], *LANES_BY_PRIORITY[:-1]]
    return LANES_BY_PRIORITY


def _detect_mood(text: str) -> str:
    low = text.lower()
    if any(w in low for w in ("sad", "lonely", "tired", "hurt", "anxious")):
        return "tender"
    if any(w in low for w in ("happy", "great", "excited", "love", "good")):
        return "upbeat"
    return "neutral"


class Worker:
    def __init__(
        self,
        worker_id: str,
        worker_class: str,
        redis: Redis,
        llm: MockCompletionClient,
    ):
        self.id = worker_id
        self._class = worker_class
        self._redis = redis
        self._lb = LoadBalancer(redis, settings.free_hard_cap)
        self._health = HealthRegistry(redis)
        self._stream = StreamBus(redis)
        self._llm = llm

    async def run(self) -> None:
        try:
            while True:
                # Heartbeat + fetch live pressure in a single round-trip.
                pipe = self._redis.pipeline()
                pipe.zadd(WORKERS_KEY, {self.id: time.time()})
                pipe.get(PRESSURE_KEY)
                _, pressure_raw = await pipe.execute()
                pressure = int(pressure_raw or 0)

                lanes = _lanes(self._class)
                job = await self._lb.dequeue_blocking(
                    lanes, timeout=settings.worker_poll_timeout_s
                )
                if job is None:
                    continue
                await self._process(job, pressure)
        except asyncio.CancelledError:
            await self._health.leave(self.id)
            raise

    async def _process(self, job: dict, pressure: int) -> None:
        message_id = job["message_id"]
        tier = Tier(job["tier"])
        eff = degrade(tier, pressure)

        await self._health.job_started()
        t0 = time.time()
        ok = True
        try:
            async with SessionLocal() as db:
                persona = await get_personality(db, job["user_id"])
                history = await recent_messages(
                    db, job["session_id"], eff.max_context, exclude_id=message_id
                )

            # Detect mood up front so it steers the reply (not just the done frame).
            mood = _detect_mood(job["text"])
            messages = build_prompt(
                personality=persona, history=history, user_message=job["text"], mood=mood
            )

            chunks: list[str] = []
            async for token in self._llm.stream(messages, model_quality=eff.model_quality):
                chunks.append(token)
                await self._stream.publish_token(message_id, token)
            reply = "".join(chunks).strip()

            # Output safety net. The mock is trusted, so this only diverges for a real
            # provider — where you'd moderate the stream incrementally rather than here.
            if not _screener.screen_output(reply).safe:
                reply = say("output_blocked")
            async with SessionLocal() as db:
                await add_message(
                    db,
                    message_id=None,
                    session_id=job["session_id"],
                    user_id=job["user_id"],
                    tier=tier,
                    role=MessageRole.ASSISTANT,
                    content=reply,
                    mood=mood,
                )
            await self._stream.publish_done(message_id, mood=mood)
        except asyncio.CancelledError:
            raise
        except Exception:
            ok = False
            await self._stream.publish_error(message_id, "internal_error")
        finally:
            await self._health.job_finished(latency_s=time.time() - t0, ok=ok)


async def main() -> None:
    redis = get_redis()
    llm = MockCompletionClient()
    container_id = os.getenv("HOSTNAME") or f"worker-{uuid.uuid4().hex[:8]}"

    async def make_worker(worker_id: str, worker_class: str):
        await Worker(worker_id, worker_class, redis, llm).run()

    supervisor = WorkerSupervisor(redis, container_id, make_worker)
    try:
        await supervisor.run()
    finally:
        await supervisor.shutdown()


if __name__ == "__main__":
    asyncio.run(main())
