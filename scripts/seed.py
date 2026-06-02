"""Seed one demo user per tier (+ personality + active session) for manual testing.

Usage:  python -m scripts.seed
Prints a user_id per tier to use against ws://.../ws/chat?user_id=<id>.
"""

import asyncio

from sqlalchemy import select

import app.models  # noqa: F401 — register models
from app.db import SessionLocal
from app.models import Personality, Session, Tier, User

DEMO = [
    ("Ava", Tier.PREMIUM_PLUS),
    ("Ben", Tier.PREMIUM),
    ("Cleo", Tier.FREE),
]


async def main() -> None:
    async with SessionLocal() as db:
        for name, tier in DEMO:
            user = (
                await db.execute(select(User).where(User.display_name == name))
            ).scalar_one_or_none()
            if user is None:
                user = User(display_name=name, tier=tier)
                db.add(user)
                await db.flush()
                db.add(Personality(user_id=user.id, traits={"warmth": 0.8, "humor": 0.5}))
                db.add(Session(user_id=user.id, status="active"))
                await db.commit()
                await db.refresh(user)
            print(f"{tier.value:>10}  user_id={user.id}")


if __name__ == "__main__":
    asyncio.run(main())
