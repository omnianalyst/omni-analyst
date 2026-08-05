"""The autonomous layer's periodic loops.

Runs alongside the existing five-loop scheduler (sweep/fill/predict/resolve/
surface). The existing scheduler is demand-driven -- it moves when a user asks.
This runner is the opposite: it scans proactively, creating the demand the
existing scheduler then closes. The two coexist without modification to the
existing Scheduler class; this runner is started and stopped independently in
``scheduler/__main__``.

Startup order is load-bearing: macro regime -> sector scan -> autonomous demand
-> (existing chain) -> synthesis -> meta. Each autonomous loop depends on the
claim the previous one wrote. Running them all once at startup means a fresh
deployment has the full deduction chain ready before the first periodic tick.

Loop 6 (macro)     -- daily; reads FRED signals, writes regime_assessment.
Loop 7 (sector)    -- twice daily; scores ETFs, writes sector_score.
Loop 8 (demand)    -- hourly; creates autonomous demand for standout sectors.
Synthesis          -- every 5 min; enriches surfaced findings with the chain.
Meta-calibration   -- daily; grades regime/sector calls against the market.
Backfill           -- once at startup; instant calibration from price history.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass

logger = logging.getLogger("omni.autonomous.runner")


@dataclass
class AutonomousConfig:
    macro_interval: float = 86_400.0          # daily (FRED updates monthly)
    sector_interval: float = 43_200.0         # twice daily
    demand_interval: float = 3_600.0          # hourly
    synthesis_interval: float = 300.0         # every 5 min, after surface
    meta_interval: float = 86_400.0           # daily
    backfill_lookback_days: int = 730
    backfill_enabled: bool = True             # set False to skip backfill


class AutonomousRunner:
    """Runs the autonomous loops until stopped. One instance per process."""

    def __init__(self, pool, config: AutonomousConfig | None = None):
        self._pool = pool
        self._config = config or AutonomousConfig()
        self._tasks: list[asyncio.Task] = []
        self._running = False
        self._operator_user_id = None

    async def start(self) -> None:
        self._running = True

        # Resolve the operator: the first user on a single-operator deployment.
        # The fill pipeline needs this to attribute byo_only Polygon fetches --
        # autonomous demand with requested_by=NULL cannot fill price gaps.
        self._operator_user_id = await self._pool.fetchval(
            "SELECT id FROM users ORDER BY created_at LIMIT 1"
        )
        if self._operator_user_id is None:
            logger.warning(
                "autonomous runner: no operator user found; byo_only demand "
                "(Polygon prices) will be unfillable until a user exists"
            )

        if self._config.backfill_enabled:
            try:
                from omni.autonomous.backfill import backfill_trend_predictions
                await backfill_trend_predictions(
                    self._pool,
                    lookback_days=self._config.backfill_lookback_days,
                )
            except Exception:
                logger.exception("autonomous backfill failed at startup")

        await self._run_all()

        self._tasks.append(asyncio.create_task(self._macro_loop()))
        self._tasks.append(asyncio.create_task(self._sector_loop()))
        self._tasks.append(asyncio.create_task(self._demand_loop()))
        self._tasks.append(asyncio.create_task(self._synthesis_loop()))
        self._tasks.append(asyncio.create_task(self._meta_loop()))
        logger.info("autonomous runner started: 5 loops")

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

    async def _run_all(self) -> None:
        """Run every autonomous loop once. Errors are contained per-loop."""
        for name, fn in self._loops():
            try:
                await fn()
            except Exception:
                logger.exception("autonomous %s failed at startup", name)

    def _loops(self):
        from omni.autonomous.demand import create_autonomous_demand
        from omni.autonomous.macro import assess_macro_regime
        from omni.autonomous.meta import resolve_meta
        from omni.autonomous.sector import scan_sectors
        from omni.autonomous.synthesis import enrich_findings

        return [
            ("macro", lambda: assess_macro_regime(self._pool)),
            ("sector", lambda: scan_sectors(self._pool)),
            ("demand", lambda: create_autonomous_demand(
                self._pool, operator_user_id=self._operator_user_id)),
            ("synthesis", lambda: enrich_findings(self._pool)),
            ("meta", lambda: resolve_meta(self._pool)),
        ]

    async def _loop(self, name: str, fn, interval: float) -> None:
        try:
            await asyncio.sleep(interval)
        except asyncio.CancelledError:
            return
        while self._running:
            try:
                await fn()
            except asyncio.CancelledError:
                break
            except Exception:
                logger.exception("autonomous %s loop failed", name)
            try:
                await asyncio.sleep(interval)
            except asyncio.CancelledError:
                break

    async def _macro_loop(self) -> None:
        from omni.autonomous.macro import assess_macro_regime
        await self._loop(
            "macro",
            lambda: assess_macro_regime(self._pool),
            self._config.macro_interval,
        )

    async def _sector_loop(self) -> None:
        from omni.autonomous.sector import scan_sectors
        await self._loop(
            "sector",
            lambda: scan_sectors(self._pool),
            self._config.sector_interval,
        )

    async def _demand_loop(self) -> None:
        from omni.autonomous.demand import create_autonomous_demand
        await self._loop(
            "demand",
            lambda: create_autonomous_demand(
                self._pool, operator_user_id=self._operator_user_id),
            self._config.demand_interval,
        )

    async def _synthesis_loop(self) -> None:
        from omni.autonomous.synthesis import enrich_findings
        await self._loop(
            "synthesis",
            lambda: enrich_findings(self._pool),
            self._config.synthesis_interval,
        )

    async def _meta_loop(self) -> None:
        from omni.autonomous.meta import resolve_meta
        await self._loop(
            "meta",
            lambda: resolve_meta(self._pool),
            self._config.meta_interval,
        )
