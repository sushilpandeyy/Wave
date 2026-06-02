"""Chat completion client.

A narrow streaming interface so the pipeline is provider-agnostic. Swap
`MockCompletionClient` for a real Anthropic-backed client later by implementing
the same `stream` coroutine — nothing else in the pipeline changes.
"""

import asyncio
from collections.abc import AsyncIterator
from typing import Protocol

# A "message" is a simple role/content dict, e.g. {"role": "user", "content": "hi"}.
Message = dict[str, str]


class CompletionClient(Protocol):
    async def stream(
        self, messages: list[Message], *, model_quality: str, timeout: float
    ) -> AsyncIterator[str]:
        """Yield reply tokens as they are generated."""
        ...


_DUMMY_REPLY = (
    "Hey, I'm really glad you reached out. I'm here with you, and I'd love to "
    "hear more about what's on your mind today."
)


class MockCompletionClient:
    """Returns a canned reply, streamed word-by-word with a small delay.

    `model_quality` only nudges the per-token delay so higher tiers feel a touch
    snappier in demos; it has no effect on content.
    """

    _DELAY_BY_QUALITY = {"premium": 0.02, "standard": 0.04, "fast": 0.06}

    async def stream(
        self, messages: list[Message], *, model_quality: str, timeout: float
    ) -> AsyncIterator[str]:
        delay = self._DELAY_BY_QUALITY.get(model_quality, 0.04)
        words = _DUMMY_REPLY.split(" ")
        for i, word in enumerate(words):
            await asyncio.sleep(delay)
            # Re-attach spaces between words so the stream reassembles cleanly.
            yield word if i == 0 else f" {word}"
