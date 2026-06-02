"""Seed one demo user per tier (+ a session each) for manual testing.

Usage:
    python -m scripts.seed
Prints the user ids and session keys to use against the WebSocket endpoint.
"""

import asyncio

from sqlalchemy import select

from app.db.postgres import Base, SessionLocal, engine
from app.models.personality import Personality
from app.models.session import Session
from app.models.user import User

DEMO_USERS = [
    ("Ava (premium++)", "premium++"),
    ("Ben (premium)", "premium"),
    ("Cleo (free)", "free"),
]


async def main() -> None:
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    async with SessionLocal() as db:
        for name, tier in DEMO_USERS:
            existing = (
                await db.execute(select(User).where(User.name == name))
            ).scalar_one_or_none()
            if existing:
                user = existing
            else:
                user = User(name=name, profile={"tier": tier}, details={})
                db.add(user)
                await db.flush()
                db.add(
                    Personality(
                        userid=user.id,
                        traits={"warmth": 0.8, "humor": 0.5},
                        context="Met through the Wave app; enjoys evening chats.",
                        version=1,
                    )
                )
                db.add(Session(userid=user.id, session="demo"))
            await db.commit()
            await db.refresh(user)
            print(f"{tier:>10}  user_id={user.id}  session=demo")


if __name__ == "__main__":
    asyncio.run(main())
