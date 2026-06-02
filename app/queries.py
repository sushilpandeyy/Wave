"""Reusable query patterns over the Part 1 schema.

These are the primary queries the pipeline runs, kept in one place so both the API
(producer) and the workers (consumer) share them. Also satisfies the Part 1
"implement query patterns" requirement.
"""

from datetime import datetime

from sqlalchemy import func, select, update
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
    await db.commit()  # PK is a client-side uuid4, so no refresh round-trip needed
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
    """Insert a message and bump the session's counters in one transaction.

    Hot path (runs per user message AND per assistant reply): the counter bump is a
    single atomic UPDATE — no SELECT to load the session, no read-modify-write race —
    and we skip the post-commit refresh since callers don't read the returned row.
    """
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
    await db.execute(
        update(Session)
        .where(Session.id == session_id)
        .values(message_count=Session.message_count + 1, last_message_at=func.now())
    )
    await db.commit()
    return msg


async def claim_idle_sessions(db: AsyncSession, cutoff: datetime) -> list[tuple]:
    """Atomically close active sessions idle since `cutoff`, returning what was claimed.

    One `UPDATE … RETURNING` so each session is claimed by exactly one reflector instance.
    """
    result = await db.execute(
        update(Session)
        .where(Session.status == "active", Session.last_message_at < cutoff)
        .values(status="closed")
        .returning(Session.id, Session.user_id, Session.message_count)
    )
    rows = result.all()
    await db.commit()
    return rows


async def session_transcript(db: AsyncSession, session_id: str, limit: int) -> list[Message]:
    """The session's last `limit` messages, chronological — input to reflection."""
    rows = (
        await db.execute(
            select(Message)
            .where(Message.session_id == session_id)
            .order_by(Message.created_at.desc())
            .limit(limit)
        )
    ).scalars().all()
    return list(reversed(rows))


async def update_personality(
    db: AsyncSession, user_id: str, *, traits: dict, summary: str
) -> None:
    """Upsert the user's single personality row (updated_at bumps via onupdate)."""
    p = (
        await db.execute(select(Personality).where(Personality.user_id == user_id))
    ).scalar_one_or_none()
    if p is None:
        db.add(Personality(user_id=user_id, traits=traits, summary=summary))
    else:
        p.traits = traits
        p.summary = summary
    await db.commit()


async def set_session_title(db: AsyncSession, session_id: str, title: str) -> None:
    sess = await db.get(Session, session_id)
    if sess is not None:
        sess.title = title[:200]
        await db.commit()


async def active_users_by_tier(db: AsyncSession, since: datetime) -> dict[str, int]:
    """Active-user counts grouped by tier — index-only scan on users(tier, last_active_at)."""
    result = await db.execute(
        select(User.tier, func.count())
        .where(User.last_active_at >= since)
        .group_by(User.tier)
    )
    return {tier.value: count for tier, count in result.all()}
