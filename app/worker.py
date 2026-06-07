"""Pull-based worker + the worker-container entrypoint.

Each worker is an asyncio coroutine: pipelined heartbeat-and-pressure read, one blocking
BZPOPMIN, then process. Safety + mood come from the model's META control line.

Observability: every job sets the correlation id (= message_id) so its logs line up with the
api's; milestones (`dequeued`, `first_token`, `completed`) plus `timed()` spans record the
queue-wait / TTFT / generation breakdown of the round-trip. Shutdown is signal-driven and
flushes analytics + logs (no data loss).

Run a worker container with:  python -m app.worker
"""

import asyncio
import os
import random
import signal
import time
import uuid

from redis.asyncio import Redis

from app import obs
from app.analytics import aclose_analytics, event, start_analytics
from app.balancer import PRESSURE_KEY, LoadBalancer
from app.config import settings
from app.db import SessionLocal
from app.health import WORKERS_KEY, HealthRegistry
from app.llm import CompletionClient, get_llm
from app.models import MessageRole, Tier
from app.pool import WorkerSupervisor
from app.prompt import build_prompt
from app.queries import add_message, get_personality, recent_messages
from app.redis import close_redis, get_redis
from app.streaming import StreamBus
from app.tiers import LANES_BY_PRIORITY, degrade
from app.voice import say

_FLAGS = {"none", "jailbreak", "nsfw", "boundary", "crisis"}


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


def _parse_meta(line: str) -> tuple[str, str]:
    """Parse `META|mood=<m>|flag=<f>`. Tolerant: returns (neutral, none) if malformed."""
    mood, flag = "neutral", "none"
    if line.startswith("META|"):
        for part in line.split("|")[1:]:
            key, _, val = part.partition("=")
            val = val.strip()
            if key == "mood" and val:
                mood = val
            elif key == "flag" and val in _FLAGS:
                flag = val
    return mood, flag


class Worker:
    def __init__(
        self,
        worker_id: str,
        worker_class: str,
        redis: Redis,
        llm: CompletionClient,
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

        obs.set_corr(message_id)
        obs.bind(tier=tier.value, user_id=job["user_id"], pool=self._class, op="generate")
        queue_wait_ms = round((time.time() - job.get("enqueued_at", time.time())) * 1000, 1)
        event("dequeued", tier=tier.value, pool=self._class, queue_wait_ms=queue_wait_ms)

        await self._health.job_started()
        t0 = time.time()
        ok = True
        try:
            async with obs.timed("load_context", tier=tier.value):
                async with SessionLocal() as db:
                    persona = await get_personality(db, job["user_id"])
                    history = await recent_messages(
                        db, job["session_id"], eff.max_context, exclude_id=message_id
                    )
            messages = build_prompt(
                personality=persona, history=history, user_message=job["text"]
            )

            async with obs.timed("generate", tier=tier.value):
                mood, reply = await self._stream_reply(message_id, messages, eff.model_quality)

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
            event("completed", tier=tier.value, mood=mood)
        except asyncio.CancelledError:
            raise
        except Exception:
            ok = False
            event("error", tier=tier.value)
            obs.exception("worker.error")
            await self._stream.publish_error(message_id, "internal_error")
        finally:
            await self._health.job_finished(latency_s=time.time() - t0, ok=ok)
            obs.clear_context()

    async def _stream_reply(
        self, message_id: str, messages: list[dict], model_quality: str
    ) -> tuple[str, str]:
        """Stream the model reply, peeling off the META control line first.

        Returns (mood, persisted_reply). A safety flag short-circuits to a voice line.
        """
        t_start = time.perf_counter()
        first = False

        async def emit(tok: str) -> None:
            nonlocal first
            if not first:
                event("first_token", ttft_ms=round((time.perf_counter() - t_start) * 1000, 1))
                first = True
            await self._stream.publish_token(message_id, tok)

        buffer = ""
        meta_done = False
        mood, flag = "neutral", "none"
        body: list[str] = []

        async for token in self._llm.stream(messages, model_quality=model_quality):
            if not meta_done:
                buffer += token
                # Only keep withholding while the buffer could still be the META control
                # line. If the model skipped it (common for short replies), start streaming
                # immediately instead of waiting for a newline that never comes.
                looks_like_meta = buffer.startswith("META|") or "META|".startswith(buffer)
                if not looks_like_meta:
                    meta_done = True
                    body.append(buffer)
                    await emit(buffer)
                    continue
                if "\n" not in buffer:
                    continue
                first_line, _, rest = buffer.partition("\n")
                mood, flag = _parse_meta(first_line)
                meta_done = True
                if flag != "none":
                    break  # unsafe — discard the body, send Wave's line instead
                if rest:
                    body.append(rest)
                    await emit(rest)
                continue
            body.append(token)
            await emit(token)

        if flag != "none":
            reply = say(flag)  # jailbreak/nsfw/boundary deflect, crisis = caring
            await emit(reply)
            return mood, reply

        # body holds everything already emitted (in both the META and no-META paths).
        # An empty body means the model sent only an (unterminated) control line.
        reply = "".join(body).strip()
        if not reply:
            reply = say("error")
            await emit(reply)
        return mood, reply


async def main() -> None:
    obs.start_logging()
    start_analytics()
    redis = get_redis()
    llm = get_llm()
    container_id = os.getenv("HOSTNAME") or f"worker-{uuid.uuid4().hex[:8]}"

    async def make_worker(worker_id: str, worker_class: str):
        await Worker(worker_id, worker_class, redis, llm).run()

    supervisor = WorkerSupervisor(redis, container_id, make_worker)
    sup_task = asyncio.create_task(supervisor.run())

    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            loop.add_signal_handler(sig, stop.set)
        except NotImplementedError:  # not supported on some platforms
            pass

    obs.info("worker.start", container=container_id)
    await stop.wait()

    # Graceful shutdown: stop workers, then flush analytics + logs (no data loss).
    obs.info("worker.stop", container=container_id)
    sup_task.cancel()
    await supervisor.shutdown()
    await aclose_analytics()
    obs.stop_logging()
    await close_redis()


if __name__ == "__main__":
    asyncio.run(main())
