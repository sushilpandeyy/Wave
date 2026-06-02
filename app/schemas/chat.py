"""WebSocket frame schemas for the chat endpoint."""

from typing import Literal

from pydantic import BaseModel, Field


class InboundMessage(BaseModel):
    """Client -> server: a user message frame."""

    message: str = Field(min_length=1, max_length=8000)


# Server -> client frames (sent as JSON dicts; modeled here for documentation).
class TokenFrame(BaseModel):
    type: Literal["token"] = "token"
    value: str


class DoneFrame(BaseModel):
    type: Literal["done"] = "done"
    mood: str | None = None


class NoticeFrame(BaseModel):
    """Friendly, non-technical message (rate limit, safety, transient error)."""

    type: Literal["notice"] = "notice"
    message: str
