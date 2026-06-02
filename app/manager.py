"""Control-plane entrypoint: runs the autoscaler.

Single replica (the `manager` service). Reads backlog + worker liveness and publishes
per-class worker targets and the global pressure level. Does no request work.

Run with:  python -m app.manager
"""

import asyncio

from app.pool import Autoscaler
from app.redis import get_redis


async def main() -> None:
    await Autoscaler(get_redis()).run()


if __name__ == "__main__":
    asyncio.run(main())
