"""The background. Without this, coverage only moves when someone asks.

Four loops, deliberately separate because they have different cost profiles:

**Sweep** -- cheap, global, frequent. Recompute demand minus coverage and record
the gaps. Touches no external API, so it can run often.

**Fill** -- expensive, per-gap, budgeted. Lease a ranked gap, call a capability,
write back a claim or an honest refusal. Every iteration may cost a paid API
call, so it runs under a hard ceiling.

**Resolve** -- cheap, coverage-only. Resolve predictions whose horizons elapsed,
each against its own audience's visible prices.

**Predict** -- cheap, coverage-only. Make a DCF directional call for each
demanded company entity with enough coverage to run one, deduped to one pending
call per (entity, method, audience). Produces nothing until an audience supplies
a price key (BYOK) -- the correct outcome, not a failure.

Collapsing them would mean either sweeping too rarely to notice staleness, or
filling without a ceiling. The gap table is the work queue itself -- it already
has lease columns and `SKIP LOCKED` claiming -- so Neutron's job queue is used
only for periodic triggering and its advisory-lock leader election, not to
carry the per-gap work. One queue, not many.
"""

from __future__ import annotations

import asyncio
import logging
import os
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

from omni.capability.registry import Registry
from omni.conviction.ledger import resolve_due_predictions
from omni.conviction.predict import produce_dcf_prediction_from_coverage
from omni.coverage.gaps import detect_gaps, persist_gaps
from omni.fill.pipeline import drain

logger = logging.getLogger(__name__)

# The method the predict loop writes/dupe-checks. Kept here as a literal rather
# than imported so the scheduler does not depend on the producer's internals;
# produce_dcf_prediction defaults its method to the capability name, which is
# this string. A second producer would carry its own constant the same way.
_DCF_METHOD = "fundamentals.dcf_valuation"


@dataclass
class SchedulerConfig:
    sweep_interval: float = 300.0
    fill_interval: float = 30.0
    #: Hard ceiling on gaps attempted per fill cycle. The reason this exists:
    #: a gap engine that can reopen gaps plus an unbounded drain is a way to
    #: spend an entire API budget in one loop iteration.
    max_gaps_per_cycle: int = 25
    fill_workers: int = 2
    #: Resolve reads only the coverage store (no external API), so it is cheap
    #: like sweep rather than budgeted like fill. Its own interval because it
    #: answers a different question: "which predictions' horizons just elapsed".
    resolve_interval: float = 60.0
    #: Predict reads the coverage store (no external API), so it is cheap like
    #: sweep/resolve. Its own interval because it answers a different question:
    #: "which demanded entities now have enough coverage to make a directional
    #: call". Produces nothing until an audience supplies a price key (BYOK) --
    #: the correct outcome, not a failure.
    predict_interval: float = 300.0
    #: How far out a DCF triple-barrier call resolves. A fair-value reversion is
    #: a long-horizon view, but resolution needs a finite window.
    predict_horizon_days: int = 90
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
    resolved: int = 0
    predicted: int = 0
    predict_abstained: int = 0


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


async def resolve_once(pool) -> int:
    """Resolve predictions whose horizons have elapsed.

    Each prediction resolves against its own audience's visible prices (read
    back from the row in ledger._resolve_one): shared predictions on the shared
    network, private ones on their owner's visible set. So the calibration
    bucket an outcome lands in always matches the audience that decided it, and
    a byo_only price series can never move a shared finding's threshold. See
    ledger.py.
    """
    return await resolve_due_predictions(pool)


async def predict_once(pool, *, horizon_days: int) -> tuple[int, int]:
    """Make a DCF directional call for each demanded company entity.

    Demand-driven (the system predicts for entities under active attention, not
    for the whole universe), per-audience (each audience's own price feeds their
    own prediction), and deduped -- one pending call per (entity, method,
    audience), so the loop cannot flood the ledger by re-firing each cycle.
    Returns ``(produced, abstained)``: abstain is the honest outcome when
    coverage is short (no visible price, incomplete fundamentals, or the model
    asserts no honest barrier), never a manufactured prediction.
    """
    now = datetime.now(UTC)
    horizon = now + timedelta(days=horizon_days)
    rows = await pool.fetch(
        """
        SELECT DISTINCT d.entity_id, d.requested_by
        FROM demand d JOIN entity e ON e.id = d.entity_id
        WHERE d.active AND e.kind = 'company'
        """
    )
    produced = 0
    abstained = 0
    for r in rows:
        entity_id: UUID = r["entity_id"]
        audience: UUID | None = r["requested_by"]
        pending = await pool.fetchval(
            "SELECT 1 FROM prediction "
            "WHERE entity_id = $1 AND method = $2 "
            "AND audience_user_id IS NOT DISTINCT FROM $3 "
            "AND outcome = 'pending' LIMIT 1",
            entity_id,
            _DCF_METHOD,
            audience,
        )
        if pending:
            continue
        pid = await produce_dcf_prediction_from_coverage(
            pool,
            entity_id=entity_id,
            audience_user_id=audience,
            as_of=now,
            horizon_ends_at=horizon,
        )
        if pid is None:
            abstained += 1
        else:
            produced += 1
    return produced, abstained


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
        # Resolve once before the loop starts, for the same reason sweep does:
        # otherwise the loop sleeps a full interval before clearing predictions
        # whose horizons already elapsed while the process was down.
        try:
            n = await resolve_once(self._pool)
            self.stats.resolved += n
        except Exception:
            logger.exception("initial resolve failed")
        self._tasks.append(asyncio.create_task(self._resolve_loop()))
        # Predict once before the loop starts, for the same reason: otherwise a
        # demanded entity with complete coverage waits a full interval for its
        # first directional call.
        try:
            produced, abstained = await predict_once(
                self._pool, horizon_days=self._config.predict_horizon_days
            )
            self.stats.predicted += produced
            self.stats.predict_abstained += abstained
        except Exception:
            logger.exception("initial predict failed")
        self._tasks.append(asyncio.create_task(self._predict_loop()))

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

    async def _resolve_loop(self) -> None:
        # start() already did one; wait before repeating.
        try:
            await asyncio.sleep(self._config.resolve_interval)
        except asyncio.CancelledError:
            return
        while self._running:
            try:
                n = await resolve_once(self._pool)
                self.stats.resolved += n
                if n:
                    logger.info("resolve closed %d predictions", n)
            except asyncio.CancelledError:
                break
            except Exception:
                # Same discipline as sweep: a failed pass must not kill the loop,
                # or resolution silently stops while the system looks healthy.
                logger.exception("resolve cycle failed")
            try:
                await asyncio.sleep(self._config.resolve_interval)
            except asyncio.CancelledError:
                break

    async def _predict_loop(self) -> None:
        # start() already did one; wait before repeating.
        try:
            await asyncio.sleep(self._config.predict_interval)
        except asyncio.CancelledError:
            return
        while self._running:
            try:
                produced, abstained = await predict_once(
                    self._pool, horizon_days=self._config.predict_horizon_days
                )
                self.stats.predicted += produced
                self.stats.predict_abstained += abstained
                if produced:
                    logger.info("predict wrote %d directional calls", produced)
            except asyncio.CancelledError:
                break
            except Exception:
                # Same discipline as the other loops: a failed pass must not
                # kill predict, or calls silently stop while the system looks
                # healthy.
                logger.exception("predict cycle failed")
            try:
                await asyncio.sleep(self._config.predict_interval)
            except asyncio.CancelledError:
                break


def default_registry() -> Registry:
    """Everything v2 can actually run: adapters, extracted analysis, derived."""
    from omni.capability.builtin import build_builtin_registry
    from omni.capability.derived import build_derived_registry
    from omni.capability.extracted import build_extracted_registry

    registry = build_builtin_registry()
    for capability in build_extracted_registry()._by_name.values():
        registry.add(capability)
    for capability in build_derived_registry()._by_name.values():
        registry.add(capability)
    return registry
