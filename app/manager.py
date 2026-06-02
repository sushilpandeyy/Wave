"""Control-plane entrypoint: runs the autoscaler.

Single replica (the `manager` service). Reads backlog + worker liveness and publishes
per-class worker targets and the global pressure level. Does no request work. Shuts down
gracefully on SIGTERM/SIGINT, flushing logs.

Run with:  python -m app.manager
"""

import asyncio
import signal

from app import obs
from app.analytics import aclose_analytics, start_analytics
from app.pool import Autoscaler
from app.redis import close_redis, get_redis


async def main() -> None:
    obs.start_logging()
    start_analytics()
    task = asyncio.create_task(Autoscaler(get_redis()).run())

    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            loop.add_signal_handler(sig, stop.set)
        except NotImplementedError:
            pass

    obs.info("manager.start")
    await stop.wait()

    obs.info("manager.stop")
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass
    await aclose_analytics()
    obs.stop_logging()
    await close_redis()


if __name__ == "__main__":
    asyncio.run(main())
