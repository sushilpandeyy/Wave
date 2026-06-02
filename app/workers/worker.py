"""A single pull-based worker coroutine.

Loop: heartbeat -> blocking-pull a job (high lane first) -> load persona +
history -> stream a completion -> publish tokens -> screen output -> persist the
assistant reply. Many of these run concurrently as asyncio tasks in one process.
"""

import asyncio
import time

from redis.asyncio import Redis
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.core.config import settings
from app.core.logging import get_logger
from app.core.tiers import QueueLane, Tier, policy_for
from app.models.chat import Chat, MessageType
from app.models.personality import Personality
from app.services.analytics import AnalyticsService
from app.services.llm import CompletionClient
from app.services.prompt import build_prompt
from app.services.router import MessageRouter
from app.services.safety import SafetyAction, SafetyService
from app.services.streaming import StreamBus

log = get_logger("worker")

_LANES = [QueueLane.HIGH, QueueLane.LOW]  # priority order


def _detect_mood(text: str) -> str:
    """Placeholder mood tag. Swap for a real classifier later."""
    lowered = text.lower()
    if any(w in lowered for w in ("sad", "lonely", "tired", "hurt")):
        return "tender"
    if any(w in lowered for w in ("happy", "great", "excited", "love")):
        return "upbeat"
    return "neutral"


class Worker:
    def __init__(
        self,
        worker_id: str,
        redis: Redis,
        session_factory: async_sessionmaker,
        llm: CompletionClient,
    ):
        self.worker_id = worker_id
        self._redis = redis
        self._session_factory = session_factory
        self._llm = llm
        self._router = MessageRouter(redis)
        self._safety = SafetyService()
        self._analytics = AnalyticsService(redis)
        self._stream = StreamBus(redis)
        self.last_active = time.time()

    async def run(self) -> None:
        from app.services.registry import WorkerRegistry

        registry = WorkerRegistry(self._redis)
        log.info("worker.start", worker_id=self.worker_id)
        try:
            while True:
                await registry.heartbeat(self.worker_id)
                job = await self._router.dequeue_blocking(
                    _LANES, timeout=settings.worker_poll_timeout_s
                )
                if job is None:
                    continue  # idle tick — loop and re-heartbeat
                self.last_active = time.time()
                await self._process(job)
        except asyncio.CancelledError:
            log.info("worker.stop", worker_id=self.worker_id)
            await registry.remove(self.worker_id)
            raise

    async def _process(self, job: dict) -> None:
        message_id = job["message_id"]
        policy = policy_for(Tier(job["tier"]))
        try:
            persona, history = await self._load_context(
                job["session_id"], job["user_id"], message_id, policy.max_context_messages
            )
            messages = build_prompt(
                personality=persona,
                history=history,
                user_message=job["text"],
                policy=policy,
            )

            reply = await asyncio.wait_for(
                self._stream_reply(message_id, messages, policy.model_quality),
                timeout=policy.request_timeout_s,
            )

            # Screen the generated reply before finalizing.
            verdict = await self._safety.screen_output(reply)
            if verdict.action != SafetyAction.ALLOW:
                reply = verdict.user_message or reply
                await self._analytics.track("safety_block_out", tier=policy.tier.value)

            mood = _detect_mood(job["text"])
            await self._persist_reply(job["session_id"], job["user_id"], reply, mood)
            await self._stream.publish_done(message_id, mood=mood)
            await self._analytics.track("message_completed", tier=policy.tier.value)
        except asyncio.TimeoutError:
            await self._analytics.track("timeout", tier=policy.tier.value)
            await self._stream.publish_error(message_id, "timeout")
            log.warning("worker.timeout", worker_id=self.worker_id, message_id=message_id)
        except Exception as exc:  # noqa: BLE001 — keep the worker alive
            await self._stream.publish_error(message_id, "internal_error")
            log.error("worker.error", worker_id=self.worker_id, error=str(exc))

    async def _stream_reply(
        self, message_id: str, messages: list[dict], model_quality: str
    ) -> str:
        """Stream tokens to the bus and return the assembled reply."""
        chunks: list[str] = []
        async for token in self._llm.stream(
            messages, model_quality=model_quality, timeout=0
        ):
            chunks.append(token)
            await self._stream.publish_token(message_id, token)
        return "".join(chunks)

    async def _load_context(
        self, session_id: str, user_id: str, message_id: str, limit: int
    ) -> tuple[Personality | None, list[Chat]]:
        async with self._session_factory() as db:
            persona = (
                await db.execute(
                    select(Personality)
                    .where(Personality.userid == user_id)
                    .order_by(Personality.version.desc())
                    .limit(1)
                )
            ).scalar_one_or_none()

            rows = (
                await db.execute(
                    select(Chat)
                    .where(Chat.sessionid == session_id, Chat.id != message_id)
                    .order_by(Chat.created.desc())
                    .limit(limit)
                )
            ).scalars().all()
            history = list(reversed(rows))  # back to chronological order
            return persona, history

    async def _persist_reply(
        self, session_id: str, user_id: str, reply: str, mood: str
    ) -> None:
        async with self._session_factory() as db:
            db.add(
                Chat(
                    sessionid=session_id,
                    userid=user_id,
                    messagetype=MessageType.ASSISTANT,
                    mood=mood,
                    message=reply,
                )
            )
            await db.commit()
