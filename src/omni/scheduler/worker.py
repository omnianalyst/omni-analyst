"""The background. Without this, coverage only moves when someone asks.

Four loops, deliberately separate because they have different cost profiles:

**Sweep** -- cheap, global, frequent. Recompute demand minus coverage and record
the gaps. Touches no external API, so it can run often.

**Fill** -- expensive, per-gap, budgeted. Lease a ranked gap, call a capability,
write back a claim or an honest refusal. Every iteration may cost a paid API
call, so it runs under a hard ceiling.

**Resolve** -- cheap, coverage-only. Resolve predictions whose horizons elapsed,
each against its own audience's visible prices.

**Predict** -- cheap, coverage-only. Make a directional call for each
demanded entity, routing it to the producers registered for its kind,
deduped to one pending call per (entity, method, audience). Produces nothing
until an audience supplies a price key (BYOK) -- the correct outcome, not a
failure.

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

from omni.alerts.rules import evaluate
from omni.capability.registry import Registry
from omni.conviction.disconfirm import gather_evidence
from omni.conviction.gate import Candidate, assess
from omni.conviction.ledger import resolve_due_predictions
from omni.conviction.producers import producers_for
from omni.conviction.publish import load_calibration, record
from omni.coverage.gaps import detect_gaps, persist_gaps
from omni.fill.pipeline import drain

logger = logging.getLogger(__name__)

# A loop is "degraded" once it has failed this many times in a row. The point is
# to surface a chronically-failing loop (every cycle raises) above the per-error
# exception log, so an unattended operator notices the loop is stuck rather than
# just flaky. Below this, a transient failure stays a single exception line.
_DEGRADED_THRESHOLD = 3


async def record_loop_health(
    pool,
    *,
    loop_name: str,
    ok: bool,
    error: str | None = None,
    expected_interval_seconds: float | None = None,
) -> int:
    """Record one loop iteration's outcome into the loop_health state row.

    A success stamps last_success_at and resets consecutive_failures; a failure
    stamps last_failure_at, captures the error and increments consecutive
    failures. Returns the resulting consecutive_failures count. When that count
    reaches ``_DEGRADED_THRESHOLD`` a WARNING is logged -- the one push signal
    that a chronically-failing loop is stuck, since the loops themselves only
    log per-error tracebacks that are easy to miss in volume.

    Cancellation is not a failure and is never recorded here; callers re-raise
    ``CancelledError`` before reaching this path.
    """
    if ok:
        row = await pool.fetchrow(
            """
            INSERT INTO loop_health
                (loop_name, last_success_at, last_failure_at,
                 consecutive_failures, last_error, expected_interval_seconds)
            VALUES ($1, now(), NULL, 0, NULL, $2)
            ON CONFLICT (loop_name) DO UPDATE SET
                last_success_at           = now(),
                consecutive_failures      = 0,
                last_error                = NULL,
                expected_interval_seconds = EXCLUDED.expected_interval_seconds,
                updated_at                = now()
            RETURNING consecutive_failures
            """,
            loop_name,
            expected_interval_seconds,
        )
        return int(row["consecutive_failures"])

    row = await pool.fetchrow(
        """
        INSERT INTO loop_health
            (loop_name, last_success_at, last_failure_at,
             consecutive_failures, last_error, expected_interval_seconds)
        VALUES ($1, NULL, now(), 1, $2, $3)
        ON CONFLICT (loop_name) DO UPDATE SET
            last_failure_at           = now(),
            consecutive_failures      = loop_health.consecutive_failures + 1,
            last_error                = EXCLUDED.last_error,
            expected_interval_seconds = EXCLUDED.expected_interval_seconds,
            updated_at                = now()
        RETURNING consecutive_failures
        """,
        loop_name,
        error,
        expected_interval_seconds,
    )
    consecutive = int(row["consecutive_failures"])
    if consecutive >= _DEGRADED_THRESHOLD:
        logger.warning(
            "loop '%s' degraded: %d consecutive failures (last: %s)",
            loop_name,
            consecutive,
            error,
        )
    return consecutive


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
    #: Surface reads the coverage store + calibration (no external API), cheap
    #: like resolve. Its own interval because it answers a different question:
    #: "which predictions now clear the calibrated threshold and should be
    #: surfaced as findings". Rate-limited by conviction, never by schedule.
    surface_interval: float = 300.0
    #: The conviction gate's bar (see config.target_hit_rate). Surfaces only
    #: confidence buckets historically right at least this often; raising it
    #: yields fewer, higher-conviction calls and silences weak methods.
    target_hit_rate: float = 0.6
    #: Alerts read only the coverage store (no external API), cheap like
    #: resolve. Each active alert is evaluated against its owner's audience, so
    #: a watched condition fires the moment coverage satisfies it rather than
    #: waiting for a human to poll. Without this loop /alerts/{id}/firings is
    #: always empty -- the feature read as live but was inert.
    alerts_interval: float = 60.0
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
    surfaced: int = 0
    alerts_fired: int = 0


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
    """Make a directional call for each demanded entity, routed by kind.

    The producers applicable to an entity come from its kind (see
    ``conviction.producers``): a kind with no registered producer gets nothing
    and that is correct, not an error. Each producer is demand-driven (the
    system predicts for entities under active attention), per-audience, and
    deduped -- one pending call per (entity, method, audience), so a second
    producer for a kind cannot double-write or flood the ledger by re-firing
    each cycle. DCF needs fundamentals + a BYO price; trend needs a price
    window. Either may abstain honestly (no key, short coverage, or the model
    asserts no honest barrier) -- abstention is the correct outcome when
    coverage is insufficient, never a manufactured prediction.
    """
    now = datetime.now(UTC)
    horizon = now + timedelta(days=horizon_days)
    rows = await pool.fetch(
        """
        SELECT DISTINCT d.entity_id, d.requested_by, e.kind
        FROM demand d JOIN entity e ON e.id = d.entity_id
        WHERE d.active
        """
    )
    produced = 0
    abstained = 0
    for r in rows:
        entity_id: UUID = r["entity_id"]
        audience: UUID | None = r["requested_by"]
        for producer in producers_for(r["kind"]):
            pending = await pool.fetchval(
                "SELECT 1 FROM prediction "
                "WHERE entity_id = $1 AND method = $2 "
                "AND audience_user_id IS NOT DISTINCT FROM $3 "
                "AND outcome = 'pending' LIMIT 1",
                entity_id,
                producer.method,
                audience,
            )
            if pending:
                continue
            try:
                pid = await producer.produce(
                    pool,
                    entity_id=entity_id,
                    audience_user_id=audience,
                    as_of=now,
                    horizon_ends_at=horizon,
                )
            except Exception:  # noqa: BLE001 - a producer raising is an honest refusal (abstain, not error)
                # A producer raising is an honest refusal (e.g. DCF on negative
                # FCF, or incomplete coverage); count it as abstain, not error.
                pid = None
            if pid is None:
                abstained += 1
            else:
                produced += 1
    return produced, abstained


async def surface_once(pool, *, target_hit_rate: float = 0.6) -> int:
    """Assess recent predictions through the conviction gate; record findings.

    For each prediction that has no recorded finding yet (the latest per entity +
    method + audience), load that method's calibration, build a Candidate, assess
    it, and publish the verdict -- surfaced OR refused. Refusals are recorded too
    because the denominator behind a published hit rate must be visible (a product
    that stores only what it surfaced can claim any hit rate it likes).

    Idempotent: a prediction with an existing finding is never re-assessed, so the
    loop re-running costs nothing and never duplicates a finding. Returns the
    number of findings recorded this pass.
    """
    rows = await pool.fetch(
        """
        SELECT DISTINCT ON (p.entity_id, p.method, p.audience_user_id)
               p.id, p.entity_id, p.method, p.confidence, p.audience_user_id,
               p.direction, p.created_at
        FROM prediction p
        WHERE p.method IS NOT NULL
          AND NOT EXISTS (SELECT 1 FROM finding f WHERE f.prediction_id = p.id)
        ORDER BY p.entity_id, p.method, p.audience_user_id, p.created_at DESC
        """
    )
    n = 0
    for r in rows:
        audience = r["audience_user_id"]
        # claim_type is a label here -- the calibration view groups by method,
        # not claim_type, so load_calibration stamps whatever we pass; the
        # Candidate must carry the same label for assess's filter to match.
        label = r["method"]
        buckets = await load_calibration(
            pool, claim_type=label, method=r["method"], audience=audience
        )
        # Point-in-time as-of the call, not now: evidence gathered from prices
        # the call could not have seen would be scoring it with hindsight.
        evidence = await gather_evidence(
            pool,
            entity_id=r["entity_id"],
            method=r["method"],
            direction=r["direction"],
            audience=audience,
            as_of=r["created_at"],
        )
        candidate = Candidate(
            claim_type=label,
            method=r["method"],
            confidence=float(r["confidence"]),
            supporting=evidence.supporting,
            disconfirming=evidence.disconfirming,
            searched_for_disconfirming=evidence.searched,
            search_supported=evidence.supported,
            falsifiable=True,
        )
        verdict = assess(candidate, buckets, target_hit_rate=target_hit_rate)
        await record(
            pool, verdict, entity_id=r["entity_id"],
            audience_user_id=audience, prediction_id=r["id"],
        )
        n += 1
    return n


_ACTIVE_ALERTS = "SELECT id, user_id, entity_id, claim_type, condition FROM alert WHERE active"


async def evaluate_alerts_once(pool) -> int:
    """Evaluate every active alert against current coverage, recording new
    firings. Returns the count of new firings.

    Each alert is evaluated against its owner's audience -- ``evaluate`` reads
    through ``visible_claims`` scoped to ``user_id`` -- so an alert never sees a
    claim its owner may not. A single failing alert is logged and skipped; one
    bad condition must not stop the others from firing.
    """
    alerts = await pool.fetch(_ACTIVE_ALERTS)
    fired = 0
    for a in alerts:
        try:
            new = await evaluate(pool, a, audience=a["user_id"])
            fired += len(new)
        except Exception:
            logger.exception("alert %s evaluation failed", a["id"])
    return fired


class Scheduler:
    """Runs the loops until stopped. One instance per process."""

    def __init__(self, pool, registry: Registry, config: SchedulerConfig | None = None):
        self._pool = pool
        self._registry = registry
        self._config = config or SchedulerConfig()
        self._tasks: list[asyncio.Task] = []
        self._running = False
        self.stats = Stats()

    async def _do(
        self,
        loop_name: str,
        interval: float,
        fn,
        *args,
        on_result=None,
        **kwargs,
    ):
        """Run one loop iteration, recording its outcome to loop_health.

        Success and failure are both recorded; the underlying exception is
        re-raised so the loop's own ``except`` keeps logging the full traceback
        and applying its per-loop discipline (e.g. ``continue`` vs sleep).
        ``asyncio.CancelledError`` is never recorded as a failure -- shutdown is
        not a fault.

        ``on_result`` (if given) is called synchronously with the result BEFORE
        the health-record await. The scheduler's stats counters are updated this
        way so they tick the instant the work is done, not one DB-write later --
        otherwise there is a window where the work is visible in the store but
        ``stats`` has not advanced, which the resolve-loop test polls for.
        """
        try:
            result = await fn(*args, **kwargs)
        except asyncio.CancelledError:
            raise
        except BaseException as exc:
            await record_loop_health(
                self._pool,
                loop_name=loop_name,
                ok=False,
                error=f"{type(exc).__name__}: {exc}",
                expected_interval_seconds=interval,
            )
            raise
        if on_result is not None:
            on_result(result)
        await record_loop_health(
            self._pool,
            loop_name=loop_name,
            ok=True,
            expected_interval_seconds=interval,
        )
        return result

    async def start(self) -> None:
        self._running = True
        # Sweep once before the fill workers exist. Otherwise they start
        # against an empty queue, find nothing, and sleep out the whole poll
        # interval while work appears milliseconds later.
        try:
            n = await self._do(
                "sweep", self._config.sweep_interval, sweep_once, self._pool
            )
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
            n = await self._do(
                "resolve", self._config.resolve_interval, resolve_once, self._pool
            )
            self.stats.resolved += n
        except Exception:
            logger.exception("initial resolve failed")
        self._tasks.append(asyncio.create_task(self._resolve_loop()))
        # Predict once before the loop starts, for the same reason: otherwise a
        # demanded entity with complete coverage waits a full interval for its
        # first directional call.
        try:
            produced, abstained = await self._do(
                "predict",
                self._config.predict_interval,
                predict_once,
                self._pool,
                horizon_days=self._config.predict_horizon_days,
            )
            self.stats.predicted += produced
            self.stats.predict_abstained += abstained
        except Exception:
            logger.exception("initial predict failed")
        self._tasks.append(asyncio.create_task(self._predict_loop()))
        # Surface once before the loop starts: otherwise a prediction that
        # already clears the calibrated threshold waits a full interval to become
        # a finding.
        try:
            n = await self._do(
                "surface", self._config.surface_interval, surface_once, self._pool,
                target_hit_rate=self._config.target_hit_rate,
            )
            self.stats.surfaced += n
        except Exception:
            logger.exception("initial surface failed")
        self._tasks.append(asyncio.create_task(self._surface_loop()))
        # Alerts once before the loop starts: a watched condition already met by
        # current coverage fires immediately rather than after a full interval.
        try:
            n = await self._do(
                "alerts",
                self._config.alerts_interval,
                evaluate_alerts_once,
                self._pool,
            )
            self.stats.alerts_fired += n
        except Exception:
            logger.exception("initial alerts evaluation failed")
        self._tasks.append(asyncio.create_task(self._alerts_loop()))

    async def stop(self) -> None:
        self._running = False
        for task in self._tasks:
            task.cancel()
        for task in self._tasks:
            try:
                await task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001,S110 - shutdown: never let one task's error block teardown
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
                n = await self._do(
                    "sweep", self._config.sweep_interval, sweep_once, self._pool
                )
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
                results = await self._do(
                    "fill",
                    self._config.fill_interval,
                    fill_once,
                    self._pool,
                    self._registry,
                    self._config,
                    worker_id=worker_id,
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
                n = await self._do(
                    "resolve", self._config.resolve_interval, resolve_once, self._pool
                )
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
                produced, abstained = await self._do(
                    "predict",
                    self._config.predict_interval,
                    predict_once,
                    self._pool,
                    horizon_days=self._config.predict_horizon_days,
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

    async def _surface_loop(self) -> None:
        # start() already did one; wait before repeating.
        try:
            await asyncio.sleep(self._config.surface_interval)
        except asyncio.CancelledError:
            return
        while self._running:
            try:
                n = await self._do(
                    "surface", self._config.surface_interval, surface_once, self._pool,
                target_hit_rate=self._config.target_hit_rate,
                )
                self.stats.surfaced += n
                if n:
                    logger.info("surface recorded %d findings", n)
            except asyncio.CancelledError:
                break
            except Exception:
                # Same discipline: a failed pass must not kill the loop, or
                # surfacing silently stops while the system looks healthy.
                logger.exception("surface cycle failed")
            try:
                await asyncio.sleep(self._config.surface_interval)
            except asyncio.CancelledError:
                break

    async def _alerts_loop(self) -> None:
        # start() already did one; wait before repeating. Same discipline as the
        # other coverage-only loops: a failed pass is logged, not fatal.
        try:
            await asyncio.sleep(self._config.alerts_interval)
        except asyncio.CancelledError:
            return
        while self._running:
            try:
                n = await self._do(
                    "alerts",
                    self._config.alerts_interval,
                    evaluate_alerts_once,
                    self._pool,
                )
                self.stats.alerts_fired += n
                if n:
                    logger.info("alerts fired %d new", n)
            except asyncio.CancelledError:
                break
            except Exception:
                logger.exception("alerts cycle failed")
            try:
                await asyncio.sleep(self._config.alerts_interval)
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
