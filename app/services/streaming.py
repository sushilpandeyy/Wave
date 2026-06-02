"""Result streaming over Redis pub/sub.

The worker publishes reply tokens to `wave:stream:{message_id}`; the WebSocket
handler subscribes and forwards them to the client. Pub/sub decouples the two so
this keeps working unchanged once workers run as separate processes.

Ordering note: the caller must `subscribe()` *before* enqueuing the job, or
early tokens can be missed (pub/sub does not buffer for late subscribers).
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
        await self._redis.publish(
            _channel(message_id), json.dumps({"t": "token", "v": token})
        )

    async def publish_done(self, message_id: str, *, mood: str | None = None) -> None:
        await self._redis.publish(
            _channel(message_id), json.dumps({"t": "done", "mood": mood})
        )

    async def publish_error(self, message_id: str, detail: str) -> None:
        await self._redis.publish(
            _channel(message_id), json.dumps({"t": "error", "detail": detail})
        )

    async def subscribe(self, message_id: str) -> "StreamSubscription":
        """Open and register a subscription *eagerly* (before the job is queued).

        Returns a handle you then iterate; this guarantees we're listening
        before any token is published, so nothing is missed.
        """
        pubsub = self._redis.pubsub()
        await pubsub.subscribe(_channel(message_id))
        return StreamSubscription(pubsub, _channel(message_id))


class StreamSubscription:
    """An open pub/sub subscription. Iterate `frames()` then `aclose()`."""

    def __init__(self, pubsub, channel: str):
        self._pubsub = pubsub
        self._channel = channel

    async def frames(self) -> AsyncIterator[dict]:
        """Yield decoded frames until a terminal (`done`/`error`) frame."""
        async for raw in self._pubsub.listen():
            if raw.get("type") != "message":
                continue
            frame = json.loads(raw["data"])
            yield frame
            if frame["t"] in ("done", "error"):
                return

    async def aclose(self) -> None:
        await self._pubsub.unsubscribe(self._channel)
        await self._pubsub.aclose()
