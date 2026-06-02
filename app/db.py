"""Async SQLAlchemy setup: engine, session factory, and the ORM base.

All I/O is non-blocking (asyncpg + asyncio). The DSN comes from POSTGRES_DSN.
"""

import os
from collections.abc import AsyncGenerator

from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.orm import DeclarativeBase

POSTGRES_DSN = os.getenv(
    "POSTGRES_DSN", "postgresql+asyncpg://wave:wave@localhost:5432/wave"
)


class Base(DeclarativeBase):
    """Base class for all ORM models."""


engine = create_async_engine(POSTGRES_DSN, pool_pre_ping=True)

SessionLocal = async_sessionmaker(
    bind=engine, expire_on_commit=False, class_=AsyncSession
)


async def get_session() -> AsyncGenerator[AsyncSession, None]:
    """Yield a scoped async session."""
    async with SessionLocal() as session:
        yield session
