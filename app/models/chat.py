import enum
import uuid
from datetime import datetime

from sqlalchemy import DateTime, Enum, ForeignKey, Index, String, Text, func
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.postgres import Base


class MessageType(str, enum.Enum):
    USER = "user"
    ASSISTANT = "assistant"
    SYSTEM = "system"


class Chat(Base):
    """Chats: id, sessionid, messagetype, mood, userid, message, created."""

    __tablename__ = "chats"
    __table_args__ = (
        Index("ix_chats_session_created", "sessionid", "created"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    sessionid: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("sessions.id", ondelete="CASCADE"), index=True
    )
    messagetype: Mapped[MessageType] = mapped_column(
        Enum(MessageType, name="message_type")
    )
    # Detected/expressed mood for this message; nullable until classified.
    mood: Mapped[str | None] = mapped_column(String(32), nullable=True)
    userid: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), index=True
    )
    message: Mapped[str] = mapped_column(Text)
    created: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )

    session_obj: Mapped["Session"] = relationship(back_populates="chats")  # noqa: F821
