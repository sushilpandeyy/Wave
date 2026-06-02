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

    async def complete(
        self, messages: list[Message], *, model_quality: str = "standard", json: bool = True
    ) -> str:
        """One-shot (non-streamed) completion — used by the reflection pipeline."""
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

    async def complete(
        self, messages: list[Message], *, model_quality: str = "standard", json: bool = True
    ) -> str:
        kwargs: dict = {}
        if json:
            kwargs["response_format"] = {"type": "json_object"}
        resp = await self._client.chat.completions.create(
            model=self._model.get(model_quality, settings.model_standard),
            messages=messages,
            **kwargs,
        )
        return resp.choices[0].message.content or ""


# --- Mock fallback ----------------------------------------------------------------
_TOKEN_DELAY = {"premium": 0.01, "standard": 0.02, "fast": 0.03}
# First line is the META control line the worker expects (mood + safety flag); then reply.
_MOCK_REPLY = (
    "META|mood=neutral|flag=none\n"
    "hey, I'm here — tell me what's going on?"
)
# Canned reflection JSON so the pipeline works offline (no key).
_MOCK_REFLECTION = (
    '{"traits": {"warmth": 0.85, "humor": 0.7, "openness": 0.8, "formality": 0.2, '
    '"playfulness": 0.7, "supportiveness": 0.85}, '
    '"summary": "Enjoys easy evening chats; opened up about a stressful week at work.", '
    '"title": "a long day, decompressing"}'
)


class MockCompletionClient:
    async def stream(
        self, messages: list[Message], *, model_quality: str
    ) -> AsyncIterator[str]:
        delay = _TOKEN_DELAY.get(model_quality, 0.02)
        for word in _MOCK_REPLY.split(" "):
            await asyncio.sleep(delay)
            yield word + " "

    async def complete(
        self, messages: list[Message], *, model_quality: str = "standard", json: bool = True
    ) -> str:
        return _MOCK_REFLECTION


def get_llm() -> CompletionClient:
    return GPTClient() if settings.openai_api_key else MockCompletionClient()
