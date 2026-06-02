"""FastAPI app: the producer side of the pipeline.

A WebSocket chat endpoint plus health/metrics. Per message the producer path is tight:
one rate-limit Lua op, then persist + subscribe + atomic admit. Safety + mood are decided
by the model in the worker (it emits a control flag), so there's no screening here.
Whenever something has to interrupt the chat, Wave replies in her own voice (`app/voice.py`)
— never a system error — and the NoticeGate keeps us from repeating the same kind of line.
"""

import uuid
from contextlib import asynccontextmanager

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from sqlalchemy import select

from app.balancer import LoadBalancer
from app.config import settings
from app.db import SessionLocal
from app.health import HealthRegistry
from app.models import MessageRole, User
from app.pool import CIRCUIT_KEY, TARGET_KEY
from app.queries import add_message, get_or_open_session
from app.ratelimit import RateLimiter
from app.redis import close_redis, get_redis
from app.streaming import StreamBus
from app.tiers import POLICIES
from app.voice import NoticeGate, say


@asynccontextmanager
async def lifespan(app: FastAPI):
    redis = get_redis()
    app.state.redis = redis
    app.state.lb = LoadBalancer(redis, settings.free_hard_cap)
    app.state.stream = StreamBus(redis)
    app.state.health = HealthRegistry(redis)
    app.state.limiter = RateLimiter(redis)
    app.state.gate = NoticeGate(redis)
    yield
    await close_redis()


app = FastAPI(title="Wave", lifespan=lifespan)


def _client_ip(websocket: WebSocket) -> str:
    """Real client IP — honor a proxy/LB's X-Forwarded-For, else the socket peer."""
    xff = websocket.headers.get("x-forwarded-for")
    if xff:
        return xff.split(",")[0].strip()
    return websocket.client.host if websocket.client else "unknown"


@app.get("/healthz")
async def healthz() -> dict:
    return {"status": "ok"}


@app.get("/metrics")
async def metrics() -> dict:
    lb: LoadBalancer = app.state.lb
    health: HealthRegistry = app.state.health
    snap = await lb.snapshot()
    pipe = app.state.redis.pipeline()
    pipe.hgetall(TARGET_KEY)
    pipe.get(CIRCUIT_KEY)
    target, circuit = await pipe.execute()
    return {
        "queues": snap["depth"],
        "backlog": snap["backlog"],
        "pressure": snap["pressure"],
        "circuit_open": circuit == "1",
        "pool_target": {k: int(v) for k, v in target.items()},
        **await health.snapshot(settings.heartbeat_ttl_s),
    }


@app.websocket("/ws/chat")
async def ws_chat(websocket: WebSocket) -> None:
    """`?user_id=<uuid>`. Send `{"message": "..."}`; receive token/done/notice frames."""
    await websocket.accept()

    # Coarse per-IP guard before any DB work — flood / fake-user_id defense.
    if not (await app.state.limiter.check_ip(_client_ip(websocket))).allowed:
        await websocket.send_json({"type": "notice", "message": say("overloaded")})
        await websocket.close()
        return

    user_id = websocket.query_params.get("user_id", "")

    # Resolve + cache tier once per connection — no per-message DB tier lookup.
    async with SessionLocal() as db:
        try:
            user = (
                await db.execute(select(User).where(User.id == uuid.UUID(user_id)))
            ).scalar_one_or_none()
        except (ValueError, TypeError):
            user = None
        if user is None:
            await websocket.send_json({"type": "notice", "message": "Unknown user."})
            await websocket.close()
            return
        uid, tier = str(user.id), user.tier
        policy = POLICIES[tier]
        session_id = str((await get_or_open_session(db, uid)).id)

    lb: LoadBalancer = app.state.lb
    stream: StreamBus = app.state.stream
    limiter: RateLimiter = app.state.limiter
    gate: NoticeGate = app.state.gate

    async def notice(msg: str) -> None:
        await websocket.send_json({"type": "notice", "message": msg})

    async def persist(role: MessageRole, content: str, mood: str | None = None) -> str:
        mid = str(uuid.uuid4())
        async with SessionLocal() as db:
            await add_message(
                db, message_id=mid, session_id=session_id, user_id=uid,
                tier=tier, role=role, content=content, mood=mood,
            )
        return mid

    try:
        while True:
            raw = await websocket.receive_json()
            text = (raw or {}).get("message")
            if not isinstance(text, str) or not text.strip():
                await notice("I didn't quite catch that — say it again?")
                continue

            # 1) Rate limit — one Lua op. Speak once, then stay quiet (never spam).
            rl = await limiter.check(uid, policy)
            if not rl.allowed:
                if await gate.allow(uid, "rate_limited"):
                    await notice(say("rate_limited"))
                continue
            if rl.approaching and await gate.allow(uid, "approaching"):
                await notice(say("approaching"))

            # 2) Persist, subscribe before enqueue, admit. (Safety/mood handled in worker.)
            message_id = await persist(MessageRole.USER, text)
            sub = await stream.subscribe(message_id)
            admitted = await lb.admit(
                message_id=message_id, user_id=uid, session_id=session_id,
                tier=tier, text=text,
            )
            if not admitted:
                await sub.aclose()
                if await gate.allow(uid, "overloaded"):
                    await notice(say("overloaded"))
                continue

            try:
                async for frame in sub.frames():
                    if frame["t"] == "token":
                        await websocket.send_json({"type": "token", "value": frame["v"]})
                    elif frame["t"] == "done":
                        await websocket.send_json({"type": "done", "mood": frame.get("mood")})
                    else:
                        await notice(say("error"))
            finally:
                await sub.aclose()
    except WebSocketDisconnect:
        return
