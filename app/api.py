"""FastAPI app: the producer side of the pipeline.

A WebSocket chat endpoint plus health/metrics. The hot path per message is tight:
tier is resolved once per connection (cached), then each message does one DB insert,
one subscribe, and one atomic admit-and-enqueue. Streaming back is pub/sub.
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
from app.redis import close_redis, get_redis
from app.streaming import StreamBus

SHED_NOTICE = (
    "I'm a little swamped right now — give me a moment and try again shortly."
)
ERROR_NOTICE = "Something hiccupped on my end — mind trying again?"


@asynccontextmanager
async def lifespan(app: FastAPI):
    redis = get_redis()
    app.state.lb = LoadBalancer(redis, settings.free_hard_cap)
    app.state.stream = StreamBus(redis)
    app.state.health = HealthRegistry(redis)
    app.state.redis = redis
    yield
    await close_redis()


app = FastAPI(title="Wave", lifespan=lifespan)


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
        tier = user.tier
        session = await get_or_open_session(db, str(user.id))
        session_id = str(session.id)

    lb: LoadBalancer = websocket.app.state.lb
    stream: StreamBus = websocket.app.state.stream

    try:
        while True:
            raw = await websocket.receive_json()
            text = (raw or {}).get("message")
            if not isinstance(text, str) or not text.strip():
                await websocket.send_json(
                    {"type": "notice", "message": "I didn't catch that — try again?"}
                )
                continue

            message_id = str(uuid.uuid4())
            async with SessionLocal() as db:
                await add_message(
                    db,
                    message_id=message_id,
                    session_id=session_id,
                    user_id=str(user.id),
                    tier=tier,
                    role=MessageRole.USER,
                    content=text,
                )

            # Subscribe BEFORE enqueuing so no early token is dropped.
            sub = await stream.subscribe(message_id)
            admitted = await lb.admit(
                message_id=message_id,
                user_id=str(user.id),
                session_id=session_id,
                tier=tier,
                text=text,
            )
            if not admitted:
                await sub.aclose()
                await websocket.send_json({"type": "notice", "message": SHED_NOTICE})
                continue

            try:
                async for frame in sub.frames():
                    if frame["t"] == "token":
                        await websocket.send_json({"type": "token", "value": frame["v"]})
                    elif frame["t"] == "done":
                        await websocket.send_json({"type": "done", "mood": frame.get("mood")})
                    else:
                        await websocket.send_json({"type": "notice", "message": ERROR_NOTICE})
            finally:
                await sub.aclose()
    except WebSocketDisconnect:
        return
