"""Pull-based worker + the worker-container entrypoint.

Each worker is an asyncio coroutine. Its loop is tight: one pipelined heartbeat-and-pressure
read, one blocking BZPOPMIN, then process.

Safety + mood are decided by the model, not regex. The model's reply begins with a control
line `META|mood=..|flag=..` (see WAVE_SYSTEM). We parse that prefix off the stream: a non-`none`
flag means we drop the reply and send Wave's in-character line (crisis = caring); otherwise we
stream the body live and record the reported mood.

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
from app.llm import CompletionClient, get_llm
from app.models import MessageRole, Tier
from app.pool import WorkerSupervisor
from app.prompt import build_prompt
from app.queries import add_message, get_personality, recent_messages
from app.redis import get_redis
from app.streaming import StreamBus
from app.tiers import LANES_BY_PRIORITY, degrade
from app.voice import say

# Flag values the model may emit; non-"none" maps 1:1 to a voice scenario.
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

        await self._health.job_started()
        t0 = time.time()
        ok = True
        try:
            async with SessionLocal() as db:
                persona = await get_personality(db, job["user_id"])
                history = await recent_messages(
                    db, job["session_id"], eff.max_context, exclude_id=message_id
                )
            messages = build_prompt(
                personality=persona, history=history, user_message=job["text"]
            )

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
        except asyncio.CancelledError:
            raise
        except Exception:
            ok = False
            await self._stream.publish_error(message_id, "internal_error")
        finally:
            await self._health.job_finished(latency_s=time.time() - t0, ok=ok)

    async def _stream_reply(
        self, message_id: str, messages: list[dict], model_quality: str
    ) -> tuple[str, str]:
        """Stream the model reply, peeling off the META control line first.

        Returns (mood, persisted_reply). A safety flag short-circuits to a voice line.
        """
        buffer = ""
        meta_done = False
        mood, flag = "neutral", "none"
        body: list[str] = []

        async for token in self._llm.stream(messages, model_quality=model_quality):
            if not meta_done:
                buffer += token
                if "\n" not in buffer:
                    continue
                first_line, _, rest = buffer.partition("\n")
                mood, flag = _parse_meta(first_line)
                meta_done = True
                if flag != "none":
                    break  # unsafe — discard the body, send Wave's line instead
                if rest:
                    body.append(rest)
                    await self._stream.publish_token(message_id, rest)
                continue
            body.append(token)
            await self._stream.publish_token(message_id, token)

        if flag != "none":
            reply = say(flag)  # jailbreak/nsfw/boundary deflect, crisis = caring
            await self._stream.publish_token(message_id, reply)
            return mood, reply

        # Tolerant fallback: model ignored the format (no META line) → it's all reply.
        reply = ("".join(body) if meta_done else buffer).strip()
        if not reply:
            reply = say("error")
            await self._stream.publish_token(message_id, reply)
        return mood, reply


async def main() -> None:
    redis = get_redis()
    llm = get_llm()
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
