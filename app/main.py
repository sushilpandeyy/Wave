"""FastAPI application entrypoint.

Wires the data stores, the worker manager, and the chat service together on
startup, and tears them down cleanly on shutdown.
"""

from contextlib import asynccontextmanager

from fastapi import FastAPI

from app.api import api_router
from app.core.config import settings
from app.core.logging import configure_logging, get_logger
from app.db.postgres import Base, SessionLocal, engine
from app.db.redis import close_redis, get_redis
from app.services.chat import ChatService
from app.services.registry import WorkerRegistry
from app.services.router import MessageRouter
from app.workers.manager import WorkerManager

log = get_logger("app")


@asynccontextmanager
async def lifespan(app: FastAPI):
    configure_logging(settings.debug)

    # Dev convenience: create tables. Use Alembic migrations in production.
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    redis = get_redis()
    app.state.redis = redis
    app.state.router = MessageRouter(redis)
    app.state.registry = WorkerRegistry(redis)
    app.state.chat_service = ChatService(redis, SessionLocal)

    manager = WorkerManager(redis, SessionLocal)
    app.state.manager = manager
    await manager.start()
    log.info("app.startup", env=settings.environment)

    try:
        yield
    finally:
        await manager.stop()
        await close_redis()
        await engine.dispose()
        log.info("app.shutdown")


app = FastAPI(title=settings.app_name, lifespan=lifespan)
app.include_router(api_router)
