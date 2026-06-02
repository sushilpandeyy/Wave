"""Create all tables and indexes. For local dev/testing.

Usage:
    python -m scripts.init_db
"""

import asyncio

import app.models  # noqa: F401 — register models on Base.metadata
from app.db import Base, engine


async def main() -> None:
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    print("Tables and indexes created.")


if __name__ == "__main__":
    asyncio.run(main())
