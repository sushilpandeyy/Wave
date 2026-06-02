"""HTTP + WebSocket routes for the chat pipeline."""

import uuid

from fastapi import APIRouter, Request, WebSocket, WebSocketDisconnect
from pydantic import ValidationError
from sqlalchemy import select

from app.core.config import settings
from app.core.logging import get_logger
from app.core.tiers import QueueLane
from app.db.postgres import SessionLocal
from app.models.session import Session
from app.models.user import User
from app.schemas.chat import InboundMessage

log = get_logger("api")
router = APIRouter()


@router.get("/healthz")
async def healthz() -> dict:
    return {"status": "ok"}


@router.get("/metrics")
async def metrics(request: Request) -> dict:
    """Lightweight live snapshot for dashboards/ops."""
    registry = request.app.state.registry
    msg_router = request.app.state.router
    return {
        "workers_alive": await registry.alive_count(settings.heartbeat_ttl_s),
        "queue_depth": {
            "high": await msg_router.lane_depth(QueueLane.HIGH),
            "low": await msg_router.lane_depth(QueueLane.LOW),
        },
    }


async def _get_or_create_session(user_id: uuid.UUID, session_key: str) -> Session:
    async with SessionLocal() as db:
        existing = (
            await db.execute(
                select(Session).where(
                    Session.userid == user_id, Session.session == session_key
                )
            )
        ).scalar_one_or_none()
        if existing:
            return existing
        sess = Session(userid=user_id, session=session_key)
        db.add(sess)
        await db.commit()
        await db.refresh(sess)
        return sess


async def _get_user(user_id: str) -> User | None:
    try:
        uid = uuid.UUID(user_id)
    except (ValueError, TypeError):
        return None
    async with SessionLocal() as db:
        return (
            await db.execute(select(User).where(User.id == uid))
        ).scalar_one_or_none()


@router.websocket("/ws/chat")
async def ws_chat(websocket: WebSocket) -> None:
    """Streaming chat. Query params: `user_id`, `session` (session key).

    Each inbound `{"message": "..."}` frame yields a stream of `token` frames
    then a `done` frame, or a `notice` frame when degraded/blocked.
    """
    await websocket.accept()
    user_id = websocket.query_params.get("user_id", "")
    session_key = websocket.query_params.get("session", "default")

    user = await _get_user(user_id)
    if user is None:
        await websocket.send_json({"type": "notice", "message": "Unknown user."})
        await websocket.close()
        return

    session = await _get_or_create_session(user.id, session_key)
    chat_service = websocket.app.state.chat_service

    try:
        while True:
            raw = await websocket.receive_json()
            try:
                inbound = InboundMessage.model_validate(raw)
            except ValidationError:
                await websocket.send_json(
                    {"type": "notice", "message": "I didn't catch that — try again?"}
                )
                continue

            async for frame in chat_service.handle(
                user=user, session=session, text=inbound.message
            ):
                await websocket.send_json(frame)
    except WebSocketDisconnect:
        log.info("ws.disconnect", user_id=user_id)
