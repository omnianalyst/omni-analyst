"""The background. Without this, coverage only moves when someone asks.

Two loops, deliberately separate because they have opposite cost profiles:

**Sweep** — cheap, global, frequent. Recompute demand minus coverage and record
the gaps. Touches no external API, so it can run often.

**Fill** — expensive, per-gap, budgeted. Lease a ranked gap, call a capability,
write back a claim or an honest refusal. Every iteration may cost a paid API
call, so it runs under a hard ceiling.

Collapsing them would mean either sweeping too rarely to notice staleness, or
filling without a ceiling. The gap table is the work queue itself — it already
has lease columns and `SKIP LOCKED` claiming — so Neutron's job queue is used
only for periodic triggering and its advisory-lock leader election, not to
carry the per-gap work. One queue, not two.
"""

from __future__ import annotations

import asyncio
import logging
import os
from dataclasses import dataclass, field
from uuid import uuid4

from omni.capability.registry import Registry
from omni.coverage.gaps import detect_gaps, persist_gaps
from omni.fill.pipeline import drain

logger = logging.getLogger(__name__)


@dataclass
class SchedulerConfig:
    sweep_interval: float = 300.0
    fill_interval: float = 30.0
    #: Hard ceiling on gaps attempted per fill cycle. The reason this exists:
    #: a gap engine that can reopen gaps plus an unbounded drain is a way to
    #: spend an entire API budget in one loop iteration.
    max_gaps_per_cycle: int = 25
    fill_workers: int = 2
    licensed: tuple[str, ...] = ()
    worker_id: str = field(default_factory=lambda: f"omni-{os.getpid()}-{uuid4().hex[:6]}")


@dataclass
class Stats:
    sweeps: int = 0
    gaps_detected: int = 0
    cycles: int = 0
    filled: int = 0
    unfillable: int = 0
    errored: int = 0


async def sweep_once(pool) -> int:
    """Recompute gaps and record them. Returns how many are open."""
    gaps = await detect_gaps(pool)
    if not gaps:
        return 0
    return await persist_gaps(pool, gaps)


async def fill_once(
    pool, registry: Registry, config: SchedulerConfig, *, worker_id: str | None = None
) -> list:
    """Work the ranked gap queue, bounded by the cycle ceiling."""
    return await drain(
        pool,
        registry=registry,
        worker_id=worker_id or config.worker_id,
        max_gaps=config.max_gaps_per_cycle,
        licensed=config.licensed,
    )


class Scheduler:
    """Runs the loops until stopped. One instance per process."""

    def __init__(self, pool, registry: Registry, config: SchedulerConfig | None = None):
        self._pool = pool
        self._registry = registry
        self._config = config or SchedulerConfig()
        self._tasks: list[asyncio.Task] = []
        self._running = False
        self.stats = Stats()

    async def start(self) -> None:
        self._running = True
        # Sweep once before the fill workers exist. Otherwise they start
        # against an empty queue, find nothing, and sleep out the whole poll
        # interval while work appears milliseconds later.
        try:
            n = await sweep_once(self._pool)
            self.stats.sweeps += 1
            self.stats.gaps_detected += n
        except Exception:
            logger.exception("initial sweep failed")
        self._tasks.append(asyncio.create_task(self._sweep_loop()))
        for i in range(self._config.fill_workers):
            self._tasks.append(
                asyncio.create_task(self._fill_loop(f"{self._config.worker_id}-{i}"))
            )

    async def stop(self) -> None:
        self._running = False
        for task in self._tasks:
            task.cancel()
        for task in self._tasks:
            try:
                await task
            except (asyncio.CancelledError, Exception):
                pass
        self._tasks.clear()

    async def _sweep_loop(self) -> None:
        # start() already did one; wait before repeating.
        try:
            await asyncio.sleep(self._config.sweep_interval)
        except asyncio.CancelledError:
            return
        while self._running:
            try:
                n = await sweep_once(self._pool)
                self.stats.sweeps += 1
                self.stats.gaps_detected += n
                if n:
                    logger.info("sweep recorded %d gaps", n)
            except asyncio.CancelledError:
                break
            except Exception:
                # A failed sweep must not kill the loop; the next one may
                # succeed, and stopping silently would leave the system looking
                # healthy while coverage quietly stopped updating.
                logger.exception("sweep failed")
            try:
                await asyncio.sleep(self._config.sweep_interval)
            except asyncio.CancelledError:
                break

    async def _fill_loop(self, worker_id: str) -> None:
        while self._running:
            try:
                results = await fill_once(
                    self._pool, self._registry, self._config, worker_id=worker_id
                )
                self.stats.cycles += 1
                for r in results:
                    if r.outcome == "filled":
                        self.stats.filled += 1
                    elif r.outcome == "unfillable":
                        self.stats.unfillable += 1
                    else:
                        self.stats.errored += 1
                if results:
                    # Work was found, so there may be more. Sleeping here would
                    # cap throughput at max_gaps_per_cycle per interval no
                    # matter how much is queued.
                    continue
            except asyncio.CancelledError:
                break
            except Exception:
                logger.exception("fill cycle failed")
            try:
                await asyncio.sleep(self._config.fill_interval)
            except asyncio.CancelledError:
                break


def default_registry() -> Registry:
    """Everything v2 can actually run: the adapters plus extracted analysis."""
    from omni.capability.builtin import build_builtin_registry
    from omni.capability.extracted import build_extracted_registry

    registry = build_builtin_registry()
    for capability in build_extracted_registry()._by_name.values():
        registry.add(capability)
    return registry
