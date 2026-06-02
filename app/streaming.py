"""Token streaming over Redis pub/sub.

Worker (consumer) publishes tokens to `wave:stream:{message_id}`; the API connection
subscribes and forwards frames to the WebSocket. The API subscribes *before* enqueuing
so no early token is missed.
"""

import json
from collections.abc import AsyncIterator

from redis.asyncio import Redis


def _channel(message_id: str) -> str:
    return f"wave:stream:{message_id}"


class StreamBus:
    def __init__(self, redis: Redis):
        self._redis = redis

    async def publish_token(self, message_id: str, token: str) -> None:
        await self._redis.publish(_channel(message_id), json.dumps({"t": "token", "v": token}))

    async def publish_done(self, message_id: str, mood: str | None) -> None:
        await self._redis.publish(_channel(message_id), json.dumps({"t": "done", "mood": mood}))

    async def publish_error(self, message_id: str, reason: str) -> None:
        await self._redis.publish(_channel(message_id), json.dumps({"t": "error", "reason": reason}))

    async def subscribe(self, message_id: str) -> "Subscription":
        pubsub = self._redis.pubsub()
        await pubsub.subscribe(_channel(message_id))
        return Subscription(pubsub)


class Subscription:
    def __init__(self, pubsub):
        self._pubsub = pubsub

    async def frames(self) -> AsyncIterator[dict]:
        """Yield frames until a terminal (`done`/`error`) frame, then stop."""
        async for msg in self._pubsub.listen():
            if msg["type"] != "message":
                continue
            frame = json.loads(msg["data"])
            yield frame
            if frame["t"] in ("done", "error"):
                return

    async def aclose(self) -> None:
        await self._pubsub.aclose()
