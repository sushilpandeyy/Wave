import uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, String, func
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.postgres import Base


class Session(Base):
    """Session: id, userid, session, created.

    A conversation session. `session` holds the external/session identifier
    (token or client-supplied key) that chats are grouped under.
    """

    __tablename__ = "sessions"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    userid: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), index=True
    )
    session: Mapped[str] = mapped_column(String(255), index=True)
    created: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )

    user: Mapped["User"] = relationship(back_populates="sessions")  # noqa: F821
    chats: Mapped[list["Chat"]] = relationship(  # noqa: F821
        back_populates="session_obj",
        cascade="all, delete-orphan",
        order_by="Chat.created",
    )
