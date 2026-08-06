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

from omni.autonomous.runner import AutonomousRunner
from omni.config import settings
from omni.db import connect, migrate
from omni.entities.identify import run as populate_identifiers
from omni.entities.seed import run as seed_market_universe
from omni.scheduler.worker import Scheduler, SchedulerConfig, default_registry

logging.basicConfig(
    level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s"
)
logger = logging.getLogger("omni.scheduler")


async def main() -> None:
    client = await connect(settings.database_url)
    await migrate(client)

    # Stand up the market universe before identifier population, so the company
    # entities exist by the time identify reads them. Idempotent upserts, pure
    # DB work -- running on every boot self-heals a refreshed static list the
    # same way populate_identifiers self-heals entities added since the last
    # boot. A failure here is a real defect (the seeder cannot write to its own
    # database), so unlike populate_identifiers it is not contained: the loops
    # have nothing to scan without a universe.
    await seed_market_universe(client.pool)

    # Identifier population is one idempotent HTTP request against SEC's ticker
    # map; running it on every boot is self-healing for entities added since the
    # last boot. `populate_identifiers` contains every SEC failure (no
    # User-Agent, SEC unreachable) and logs it, so a SEC outage cannot stop the
    # scheduler coming up -- the loops are the scheduler's job, CIKs are a
    # precondition it improves when it can.
    await populate_identifiers(client.pool, user_agent=settings.sec_user_agent)

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

    autonomous = AutonomousRunner(client.pool)
    await autonomous.start()

    try:
        await stopping.wait()
    finally:
        logger.info(
            "stopping: %d sweeps, %d gaps seen, %d filled, %d unfillable, %d errors",
            scheduler.stats.sweeps, scheduler.stats.gaps_detected,
            scheduler.stats.filled, scheduler.stats.unfillable,
            scheduler.stats.errored,
        )
        await autonomous.stop()
        await scheduler.stop()
        await client.close()


if __name__ == "__main__":
    asyncio.run(main())
