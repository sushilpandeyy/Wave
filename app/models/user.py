import uuid
from datetime import datetime

from sqlalchemy import DateTime, String, func
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.postgres import Base


class User(Base):
    """Users: id, name, profile, details, created."""

    __tablename__ = "users"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    name: Mapped[str] = mapped_column(String(120))
    # Profile JSON; holds the subscription tier, e.g. {"tier": "premium++"}.
    profile: Mapped[dict] = mapped_column(JSONB, default=dict)
    # Structured metadata (tier, locale, flags, etc.).
    details: Mapped[dict] = mapped_column(JSONB, default=dict)
    created: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )

    personalities: Mapped[list["Personality"]] = relationship(  # noqa: F821
        back_populates="user", cascade="all, delete-orphan"
    )
    sessions: Mapped[list["Session"]] = relationship(  # noqa: F821
        back_populates="user", cascade="all, delete-orphan"
    )
