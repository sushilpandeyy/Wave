"""Producer-side orchestration for a single chat turn.

Resolves tier, enforces the rate limit, screens input, persists the user's
message, enqueues the job, and forwards streamed reply tokens back to the
caller. The worker (consumer side) does the completion and persists the
assistant reply. `handle` is an async generator yielding WS-ready frames.
"""

import time
from collections.abc import AsyncIterator

from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.core.tiers import policy_for, resolve_tier
from app.models.chat import Chat, MessageType
from app.models.session import Session
from app.models.user import User
from app.services.analytics import AnalyticsService
from app.services.rate_limit import RateLimiter
from app.services.router import MessageRouter
from app.services.safety import SafetyAction, SafetyService
from app.services.streaming import StreamBus

# Warm, non-technical copy for degraded paths.
RATE_LIMIT_NOTICE = (
    "You're sending messages a little quickly — give me a breath and try again "
    "in a moment."
)


class ChatService:
    def __init__(self, redis: Redis, session_factory: async_sessionmaker):
        self._session_factory = session_factory
        self._rate_limiter = RateLimiter(redis)
        self._router = MessageRouter(redis)
        self._safety = SafetyService()
        self._analytics = AnalyticsService(redis)
        self._stream = StreamBus(redis)

    async def handle(
        self, *, user: User, session: Session, text: str
    ) -> AsyncIterator[dict]:
        policy = policy_for(resolve_tier(user.profile))
        now = time.time()

        # 1) Rate limit — degrade gracefully, never error.
        rl = await self._rate_limiter.check(str(user.id), policy, now)
        if not rl.allowed:
            await self._analytics.track("rate_limited", tier=policy.tier.value)
            yield {"type": "notice", "message": RATE_LIMIT_NOTICE}
            return

        # 2) Persist the user's message.
        async with self._session_factory() as db:
            chat = Chat(
                sessionid=session.id,
                userid=user.id,
                messagetype=MessageType.USER,
                message=text,
            )
            db.add(chat)
            await db.commit()
            await db.refresh(chat)
            message_id = str(chat.id)

        # 3) Safety-screen the input.
        verdict = await self._safety.screen_input(text)
        if verdict.action != SafetyAction.ALLOW:
            await self._analytics.track("safety_block_in", tier=policy.tier.value)
            yield {"type": "notice", "message": verdict.user_message}
            return

        # 4) Subscribe BEFORE enqueuing so no early tokens are dropped.
        subscription = await self._stream.subscribe(message_id)
        try:
            await self._router.enqueue(
                message_id,
                policy,
                payload_extra={
                    "session_id": str(session.id),
                    "user_id": str(user.id),
                    "text": text,
                },
            )
            await self._analytics.track("message_enqueued", tier=policy.tier.value)

            # 5) Forward streamed frames to the caller.
            async for frame in subscription.frames():
                if frame["t"] == "token":
                    yield {"type": "token", "value": frame["v"]}
                elif frame["t"] == "done":
                    yield {"type": "done", "mood": frame.get("mood")}
                elif frame["t"] == "error":
                    yield {
                        "type": "notice",
                        "message": "Something hiccupped on my end — mind trying again?",
                    }
        finally:
            await subscription.aclose()
