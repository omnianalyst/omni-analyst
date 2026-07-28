"""Run the background as its own process.

    uv run python -m omni.scheduler

Separate from the API process on purpose: the sweep and fill loops should keep
running whether or not anyone is making requests, and a request spike should
not starve them.
"""

from __future__ import annotations

import asyncio
import logging
import signal

from omni.config import settings
from omni.db import connect, migrate
from omni.scheduler.worker import Scheduler, SchedulerConfig, default_registry

logging.basicConfig(
    level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s"
)
logger = logging.getLogger("omni.scheduler")


async def main() -> None:
    client = await connect(settings.database_url)
    await migrate(client)

    registry = default_registry()
    scheduler = Scheduler(client.pool, registry, SchedulerConfig())

    stopping = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, stopping.set)

    logger.info(
        "scheduler up: %d capabilities, %d fill workers, ceiling %d gaps/cycle",
        len(registry),
        scheduler._config.fill_workers,
        scheduler._config.max_gaps_per_cycle,
    )
    await scheduler.start()
    try:
        await stopping.wait()
    finally:
        logger.info(
            "stopping: %d sweeps, %d gaps seen, %d filled, %d unfillable, %d errors",
            scheduler.stats.sweeps, scheduler.stats.gaps_detected,
            scheduler.stats.filled, scheduler.stats.unfillable,
            scheduler.stats.errored,
        )
        await scheduler.stop()
        await client.close()


if __name__ == "__main__":
    asyncio.run(main())
