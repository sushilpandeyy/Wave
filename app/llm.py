"""Completion clients.

`GPTClient` streams from OpenAI; `MockCompletionClient` is the offline stand-in. Both
expose the same `stream(messages, model_quality)` async generator, so the worker doesn't
care which is in use. `get_llm()` picks the real client when an API key is configured and
falls back to the mock otherwise (dev / offline / tests).
"""

import asyncio
from collections.abc import AsyncIterator
from typing import Protocol

from app.config import settings

Message = dict[str, str]  # {"role": ..., "content": ...}


class CompletionClient(Protocol):
    def stream(self, messages: list[Message], *, model_quality: str) -> AsyncIterator[str]:
        ...


class GPTClient:
    """Streaming OpenAI chat completions, model chosen by tier quality."""

    def __init__(self) -> None:
        from openai import AsyncOpenAI  # imported lazily so the dep is optional

        self._client = AsyncOpenAI(api_key=settings.openai_api_key)
        self._model = {
            "premium": settings.model_premium,
            "standard": settings.model_standard,
            "fast": settings.model_fast,
        }

    async def stream(
        self, messages: list[Message], *, model_quality: str
    ) -> AsyncIterator[str]:
        stream = await self._client.chat.completions.create(
            model=self._model.get(model_quality, settings.model_standard),
            messages=messages,
            stream=True,
        )
        async for chunk in stream:
            token = chunk.choices[0].delta.content
            if token:
                yield token


# --- Mock fallback ----------------------------------------------------------------
_TOKEN_DELAY = {"premium": 0.01, "standard": 0.02, "fast": 0.03}
# First line is the META control line the worker expects (mood + safety flag); then reply.
_MOCK_REPLY = (
    "META|mood=neutral|flag=none\n"
    "hey, I'm here — tell me what's going on?"
)


class MockCompletionClient:
    async def stream(
        self, messages: list[Message], *, model_quality: str
    ) -> AsyncIterator[str]:
        delay = _TOKEN_DELAY.get(model_quality, 0.02)
        for word in _MOCK_REPLY.split(" "):
            await asyncio.sleep(delay)
            yield word + " "


def get_llm() -> CompletionClient:
    return GPTClient() if settings.openai_api_key else MockCompletionClient()
