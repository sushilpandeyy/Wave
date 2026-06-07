"""FastAPI app: the producer side of the pipeline.

A WebSocket chat endpoint plus health/metrics. Per message the producer path is tight:
one rate-limit Lua op, then persist + subscribe + atomic admit. Safety + mood are decided
by the model in the worker (it emits a control flag), so there's no screening here.
Whenever something has to interrupt the chat, Wave replies in her own voice (`app/voice.py`)
— never a system error — and the NoticeGate keeps us from repeating the same kind of line.
"""

import time
import uuid
from contextlib import asynccontextmanager

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from pydantic import BaseModel, Field
from sqlalchemy import select

from app import obs
from app.analytics import aclose_analytics, event, start_analytics, stats, track
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
    obs.start_logging()
    start_analytics()
    redis = get_redis()
    app.state.redis = redis
    app.state.lb = LoadBalancer(redis, settings.free_hard_cap)
    app.state.stream = StreamBus(redis)
    app.state.health = HealthRegistry(redis)
    app.state.limiter = RateLimiter(redis)
    app.state.gate = NoticeGate(redis)
    obs.info("api.start")
    yield
    # Graceful shutdown: flush analytics + logs, then close connections (no data loss).
    obs.info("api.stop")
    await aclose_analytics()
    obs.stop_logging()
    await close_redis()


DESCRIPTION = """
**Wave** — the backend message-processing pipeline for the Wave AI companion.

This service is the *producer* side of the pipeline: it accepts chat messages over a
WebSocket, applies tier-aware rate limiting, persists the turn, and atomically admits it
onto the tier-aware load balancer. Worker processes consume the queues, call the model,
and stream tokens back over a Redis pub/sub bus. Safety and mood are model-driven (the
worker maps the model's control flag to Wave's voice), so there is no screening here.

### Endpoints
- **`WS /ws/chat`** — the chat stream. *WebSocket endpoints are not rendered by Swagger UI;*
  the full frame contract is documented below.
- **`GET /healthz`** — liveness probe.
- **`GET /metrics`** — live pipeline snapshot (queue depth, pressure, pools, health, analytics).

### WebSocket contract — `WS /ws/chat?user_id=<uuid>`
Connect with a `user_id` query param identifying an existing user (tier is resolved once
per connection). Then exchange JSON frames:

**Client → server**
```json
{ "message": "hey, how are you?" }
```

**Server → client** (one of):
```json
{ "type": "token",  "value": "partial text chunk" }   // streamed model tokens
{ "type": "done",   "mood": "warm" }                  // turn complete
{ "type": "notice", "message": "..." }                // in-voice notice (rate limit, overload, error)
```
Notices are always spoken in Wave's voice — never a raw system error — and an anti-spam
gate prevents repeating the same kind of notice. Unknown users and IP-flood rejections
receive a single `notice` frame, after which the socket is closed.
"""

TAGS_METADATA = [
    {"name": "chat", "description": "The WebSocket chat stream (producer path)."},
    {"name": "ops", "description": "Operational endpoints: health probes and live metrics."},
]

app = FastAPI(
    title="Wave",
    version="1.0.0",
    summary="AI companion message-processing pipeline.",
    description=DESCRIPTION,
    openapi_tags=TAGS_METADATA,
    lifespan=lifespan,
)


class HealthResponse(BaseModel):
    """Liveness probe payload."""

    status: str = Field("ok", examples=["ok"])


class MetricsResponse(BaseModel):
    """Live snapshot of the pipeline. Fields are merged from the load balancer,
    health registry, and analytics stats, so the exact key set can grow over time."""

    queues: dict[str, int] = Field(
        description="Current depth per priority queue (priority / standard / elastic).",
        examples=[{"priority": 0, "standard": 3, "elastic": 0}],
    )
    backlog: int = Field(description="Total messages waiting across all queues.", examples=[3])
    pressure: int = Field(
        description="Global pressure level 0-3 (folds load, latency, and the health circuit).",
        examples=[1],
    )
    circuit_open: bool = Field(
        description="True when the error-rate circuit breaker has tripped.", examples=[False]
    )
    pool_target: dict[str, int] = Field(
        description="Autoscaler's current target worker count per pool.",
        examples=[{"priority": 4, "standard": 8, "elastic": 0}],
    )

    model_config = {"extra": "allow"}


def _client_ip(websocket: WebSocket) -> str:
    """Real client IP — honor a proxy/LB's X-Forwarded-For, else the socket peer."""
    xff = websocket.headers.get("x-forwarded-for")
    if xff:
        return xff.split(",")[0].strip()
    return websocket.client.host if websocket.client else "unknown"


@app.get(
    "/healthz",
    tags=["ops"],
    summary="Liveness probe",
    description="Returns `{\"status\": \"ok\"}` if the API process is up. Used by the "
    "load balancer / orchestrator health checks; does not touch Redis or Postgres.",
    response_model=HealthResponse,
)
async def healthz() -> dict:
    return {"status": "ok"}


@app.get(
    "/metrics",
    tags=["ops"],
    summary="Live pipeline metrics",
    description="A point-in-time snapshot of the pipeline: queue depth and backlog, the "
    "global pressure level, the error-circuit state, per-pool worker targets, worker "
    "health, and analytics counters. Cheap to call (one LB snapshot + a small Redis pipeline).",
    response_model=MetricsResponse,
)
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
        **stats(),
    }


@app.websocket("/ws/chat", name="chat")
async def ws_chat(websocket: WebSocket) -> None:
    """Chat stream — `?user_id=<uuid>`.

    Send `{"message": "..."}` frames; receive `token` / `done` / `notice` frames.
    See the WebSocket contract in the app description for the full frame schema.
    (WebSocket routes are not rendered in `/docs`.)
    """
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

    async def persist(message_id: str, role: MessageRole, content: str) -> None:
        async with SessionLocal() as db:
            await add_message(
                db, message_id=message_id, session_id=session_id, user_id=uid,
                tier=tier, role=role, content=content,
            )

    try:
        while True:
            raw = await websocket.receive_json()
            text = (raw or {}).get("message")
            if not isinstance(text, str) or not text.strip():
                await notice("I didn't quite catch that — say it again?")
                continue

            # message_id is the correlation id for the whole turn (api + worker).
            message_id = str(uuid.uuid4())
            obs.set_corr(message_id)
            obs.bind(tier=tier.value, user_id=uid, op="chat")
            t0 = time.perf_counter()
            event("received", tier=tier.value)

            # 1) Rate limit — one Lua op. Speak once, then stay quiet (never spam).
            rl = await limiter.check(uid, policy)
            if not rl.allowed:
                event("rate_limited", tier=tier.value)
                if await gate.allow(uid, "rate_limited"):
                    await notice(say("rate_limited"))
                continue
            if rl.approaching:
                track("approaching", tier=tier.value)
                if await gate.allow(uid, "approaching"):
                    await notice(say("approaching"))

            # 2) Persist, subscribe before enqueue, admit. (Safety/mood handled in worker.)
            async with obs.timed("persist_user", tier=tier.value):
                await persist(message_id, MessageRole.USER, text)
            sub = await stream.subscribe(message_id)
            async with obs.timed("admit", tier=tier.value):
                admitted = await lb.admit(
                    message_id=message_id, user_id=uid, session_id=session_id,
                    tier=tier, text=text,
                )
            if not admitted:
                await sub.aclose()
                event("shed", tier=tier.value)
                if await gate.allow(uid, "overloaded"):
                    await notice(say("overloaded"))
                continue

            try:
                async for frame in sub.frames():
                    if frame["t"] == "token":
                        await websocket.send_json({"type": "token", "value": frame["v"]})
                    elif frame["t"] == "done":
                        await websocket.send_json({"type": "done", "mood": frame.get("mood")})
                        event(
                            "delivered", tier=tier.value,
                            roundtrip_ms=round((time.perf_counter() - t0) * 1000, 1),
                        )
                    else:
                        event("error", tier=tier.value)
                        await notice(say("error"))
            finally:
                await sub.aclose()
    except WebSocketDisconnect:
        return
