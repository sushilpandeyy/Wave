import uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Integer, Text, func
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.postgres import Base


class Personality(Base):
    """Personality: id, userid, traits, context, version, created.

    Versioned per user so the companion's persona can evolve over time while
    older versions remain auditable.
    """

    __tablename__ = "personalities"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    userid: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), index=True
    )
    # Structured trait map, e.g. {"warmth": 0.8, "humor": 0.6}.
    traits: Mapped[dict] = mapped_column(JSONB, default=dict)
    # Accumulated long-term context / memory the persona draws on.
    context: Mapped[str | None] = mapped_column(Text, nullable=True)
    version: Mapped[int] = mapped_column(Integer, default=1)
    created: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )

    user: Mapped["User"] = relationship(back_populates="personalities")  # noqa: F821
