"""Mock streaming completion client.

Stands in for a real provider so we can load-test the balancer without API cost.
Implements the same shape a real client would (`async def stream`), so swapping in
a real provider later is a one-class change. Per-token latency varies by model
quality, which is what makes higher tiers feel faster.
"""

import asyncio
from collections.abc import AsyncIterator

Message = dict[str, str]  # {"role": ..., "content": ...}

# Per-token delay (seconds) by model class. "premium" is snappiest.
_TOKEN_DELAY = {"premium": 0.01, "standard": 0.02, "fast": 0.03}

_REPLY = (
    "I hear you — that sounds like a lot to carry. "
    "I'm right here with you, so tell me more whenever you're ready."
)


class MockCompletionClient:
    async def stream(
        self, messages: list[Message], *, model_quality: str
    ) -> AsyncIterator[str]:
        delay = _TOKEN_DELAY.get(model_quality, 0.02)
        for word in _REPLY.split(" "):
            await asyncio.sleep(delay)
            yield word + " "
