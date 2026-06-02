"""Reusable query patterns over the Part 1 schema.

These are the primary queries the pipeline runs, kept in one place so both the API
(producer) and the workers (consumer) share them. Also satisfies the Part 1
"implement query patterns" requirement.
"""

from datetime import datetime

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Message, MessageRole, Personality, Session, Tier, User


async def get_or_open_session(db: AsyncSession, user_id: str) -> Session:
    """The user's current active session, opening one if none is active.

    Point lookup on the partial unique index `sessions(user_id) WHERE status='active'`.
    """
    existing = (
        await db.execute(
            select(Session).where(
                Session.user_id == user_id, Session.status == "active"
            )
        )
    ).scalar_one_or_none()
    if existing is not None:
        return existing
    session = Session(user_id=user_id, status="active")
    db.add(session)
    await db.commit()
    await db.refresh(session)
    return session


async def recent_messages(
    db: AsyncSession, session_id: str, limit: int, exclude_id: str | None = None
) -> list[Message]:
    """Most recent `limit` messages for a session, returned chronologically.

    Range scan on `messages(session_id, created_at)` — stops after `limit` rows, no sort.
    """
    if limit <= 0:
        return []
    stmt = select(Message).where(Message.session_id == session_id)
    if exclude_id is not None:
        stmt = stmt.where(Message.id != exclude_id)
    stmt = stmt.order_by(Message.created_at.desc()).limit(limit)
    rows = (await db.execute(stmt)).scalars().all()
    return list(reversed(rows))


async def get_personality(db: AsyncSession, user_id: str) -> Personality | None:
    return (
        await db.execute(
            select(Personality).where(Personality.user_id == user_id)
        )
    ).scalar_one_or_none()


async def add_message(
    db: AsyncSession,
    *,
    message_id: str | None,
    session_id: str,
    user_id: str,
    tier: Tier,
    role: MessageRole,
    content: str,
    mood: str | None = None,
) -> Message:
    """Insert a message and bump the session's counters in one transaction."""
    msg = Message(
        session_id=session_id,
        user_id=user_id,
        tier=tier,
        role=role,
        content=content,
        mood=mood,
    )
    if message_id is not None:
        msg.id = message_id
    db.add(msg)
    sess = await db.get(Session, session_id)
    if sess is not None:
        sess.message_count += 1
        sess.last_message_at = func.now()
    await db.commit()
    await db.refresh(msg)
    return msg


async def active_users_by_tier(db: AsyncSession, since: datetime) -> dict[str, int]:
    """Active-user counts grouped by tier — index-only scan on users(tier, last_active_at)."""
    result = await db.execute(
        select(User.tier, func.count())
        .where(User.last_active_at >= since)
        .group_by(User.tier)
    )
    return {tier.value: count for tier, count in result.all()}
