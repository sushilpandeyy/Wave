"""Wave data model — four tables: users, personalities, sessions, messages.

Design notes:
- `tier` is a first-class column (read on every message, grouped on in analytics),
  not buried in JSON.
- A "session" is one conversation *episode*; a user has at most one active session.
- `messages` denormalizes `user_id` and `tier` (tier-at-send) so the hot reads and
  per-tier aggregations don't need joins.
"""

import enum
import uuid
from datetime import datetime

from sqlalchemy import (
    DateTime,
    Enum,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base


class Tier(str, enum.Enum):
    FREE = "free"
    PREMIUM = "premium"
    PREMIUM_PLUS = "premium++"


class MessageRole(str, enum.Enum):
    USER = "user"
    ASSISTANT = "assistant"
    SYSTEM = "system"


# Store the enum *values* (e.g. "free", "premium++") in Postgres, not the names.
_tier_enum = Enum(Tier, name="tier", values_callable=lambda e: [m.value for m in e])
_role_enum = Enum(
    MessageRole, name="message_role", values_callable=lambda e: [m.value for m in e]
)


class User(Base):
    """An account holder and their subscription tier."""

    __tablename__ = "users"
    __table_args__ = (
        # Aggregations by tier / "active users by tier".
        Index("ix_users_tier_active", "tier", "last_active_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    display_name: Mapped[str] = mapped_column(String(120))
    tier: Mapped[Tier] = mapped_column(
        _tier_enum, default=Tier.FREE, server_default=text("'free'")
    )
    locale: Mapped[str] = mapped_column(
        String(16), default="en", server_default="en"
    )
    timezone: Mapped[str] = mapped_column(
        String(64), default="UTC", server_default="UTC"
    )
    last_active_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    # Schemaless preferences only — nothing we query or group by.
    settings: Mapped[dict] = mapped_column(
        JSONB, default=dict, server_default=text("'{}'::jsonb")
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class Personality(Base):
    """The companion's persona for a user — one row per user, updated in place."""

    __tablename__ = "personalities"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    # Unique → exactly one personality per user.
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="CASCADE"),
        unique=True,
    )
    # Trait map, e.g. {"warmth": 0.8, "humor": 0.6}.
    traits: Mapped[dict] = mapped_column(
        JSONB, default=dict, server_default=text("'{}'::jsonb")
    )
    # Long-term memory digest the persona draws on.
    summary: Mapped[str | None] = mapped_column(Text, nullable=True)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class Session(Base):
    """One conversation episode. A user has at most one active session at a time."""

    __tablename__ = "sessions"
    __table_args__ = (
        # ≤1 active session per user AND the fast "current active session" lookup.
        Index(
            "uq_sessions_active_per_user",
            "user_id",
            unique=True,
            postgresql_where=text("status = 'active'"),
        ),
        # Session history, newest first.
        Index("ix_sessions_user_recent", "user_id", "last_message_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE")
    )
    # active | closed
    status: Mapped[str] = mapped_column(
        String(16), default="active", server_default="active"
    )
    title: Mapped[str | None] = mapped_column(String(200), nullable=True)
    message_count: Mapped[int] = mapped_column(
        Integer, default=0, server_default=text("0")
    )
    last_message_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class Message(Base):
    """A single chat message (user, assistant, or system)."""

    __tablename__ = "messages"
    __table_args__ = (
        # Recent messages for a session, newest first — the LLM-context query.
        Index("ix_messages_session_time", "session_id", "created_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    session_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("sessions.id", ondelete="CASCADE")
    )
    # Denormalized from the session for join-free user-scoped queries.
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE")
    )
    # Tier at send time — what per-tier message analytics actually wants.
    tier: Mapped[Tier] = mapped_column(_tier_enum)
    role: Mapped[MessageRole] = mapped_column(_role_enum)
    content: Mapped[str] = mapped_column(Text)
    # Detected mood; nullable until classified.
    mood: Mapped[str | None] = mapped_column(String(32), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
