"""Reflection-plane entrypoint: the session reaper + reflection consumers.

Closes idle conversation sessions and evolves each user's personality from the finished
conversation — entirely in the background, off the chat path. Shuts down gracefully: stops
between jobs (no reflection cut off mid-LLM-call) and flushes analytics + logs.

Run with:  python -m app.reflector
"""

import asyncio
import signal

from app import obs
from app.analytics import aclose_analytics, start_analytics
from app.config import settings
from app.llm import get_llm
from app.redis import close_redis, get_redis
from app.reflection import SessionReaper, consume


async def main() -> None:
    obs.start_logging()
    start_analytics()
    redis = get_redis()
    llm = get_llm()
    stop = asyncio.Event()

    tasks = [asyncio.create_task(SessionReaper(redis).run(stop))]
    tasks += [
        asyncio.create_task(consume(redis, llm, stop))
        for _ in range(settings.reflect_concurrency)
    ]

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            loop.add_signal_handler(sig, stop.set)
        except NotImplementedError:
            pass

    obs.info("reflector.start", consumers=settings.reflect_concurrency)
    await stop.wait()

    # Graceful: let in-flight reflections finish (bounded), then flush.
    obs.info("reflector.stop")
    try:
        await asyncio.wait_for(asyncio.gather(*tasks, return_exceptions=True),
                               timeout=settings.shutdown_drain_s)
    except asyncio.TimeoutError:
        for t in tasks:
            t.cancel()
    await aclose_analytics()
    obs.stop_logging()
    await close_redis()


if __name__ == "__main__":
    asyncio.run(main())
