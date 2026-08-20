"""Phase D: the cold-start backfill.

AUTONOMOUS_PLAN.md Gap 2: a fresh system has no calibration, so the conviction
gate refuses everything, and 90 days of pending predictions pass before any
finding surfaces. The fix is backfill -- replay the trend producer at historical
timestamps against the price coverage Polygon's 2-year default window already
provides, then let the resolver score those predictions against the same path.
The result is instant calibration: the gate has resolved history on day one.

The mechanism is proven (HANDOFF.md 0a: ``record_prediction(created_at=...)`` +
the resolver-on-historical-prices is a first-class point-in-time backfill path).
This module is the orchestration: iterate the universe, step backward in weekly
increments, call the existing producer at each timestamp. No new maths, no new
API calls -- the producer reads existing coverage, and the resolver scores
against it.

Idempotent: a re-run finds the earliest prediction per entity and skips
entities that already have history, so the backfill is safe to call on every
boot (it no-ops after the first successful run).

Every prediction written here is **marked as replayed in its own provenance**.
`trading/policy.py` counts live resolved predictions as those whose provenance
carries no backfill marker, and GATE C opens the scale phase on that count
alone -- so an unmarked backfill row is indistinguishable from a call the live
scheduler risked something on, and thirty of them manufactured overnight would
open the gate the marker exists to hold shut. The marker is what makes the two
distinguishable by reading the row; nothing else in the row is a reliable
witness (a live call and a replayed one differ only in `created_at`, which is a
timestamp heuristic, not a statement of origin).
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

from omni.conviction.ledger import resolve_due_predictions
from omni.conviction.trend import produce_trend_prediction_from_coverage

logger = logging.getLogger("omni.autonomous.backfill")

_TREND_METHOD = "trend.sma"
_DEFAULT_LOOKBACK_DAYS = 730
_DEFAULT_INTERVAL_DAYS = 7
_DEFAULT_HORIZON_DAYS = 90
_DEFAULT_WINDOW = 50
_DEFAULT_TARGET_K = 2.0


@dataclass(frozen=True)
class BackfillReport:
    entities_processed: int = 0
    predictions_written: int = 0
    predictions_resolved: int = 0
    entities_skipped: int = 0


_OLDEST_PREDICTION = """
SELECT MIN(created_at) FROM prediction
WHERE entity_id = $1 AND method = $2
"""

_ENTITIES_WITH_PRICES = """
SELECT DISTINCT c.entity_id
FROM claim c JOIN entity e ON e.id = c.entity_id
WHERE c.claim_type = 'price_snapshot' AND e.kind = 'company'
"""

# The marker goes under `assumptions` because that is the only part of the
# provenance envelope a caller controls: `record_prediction` builds the
# envelope itself and takes `assumptions` alone from its caller. It is written
# by UPDATE rather than passed down because the trend producer composes its own
# assumptions and does not forward any -- and this module may not change it.
# The merge (`||`) preserves those, so the model parameters the call was made
# under stay readable alongside the marker.
_STAMP_BACKFILL = """
UPDATE prediction
SET provenance = jsonb_set(
        provenance,
        '{assumptions}',
        COALESCE(provenance -> 'assumptions', '{}'::jsonb) || $1::jsonb,
        true
    )
WHERE id = $2
"""


async def backfill_trend_predictions(
    pool,
    *,
    lookback_days: int = _DEFAULT_LOOKBACK_DAYS,
    interval_days: int = _DEFAULT_INTERVAL_DAYS,
    horizon_days: int = _DEFAULT_HORIZON_DAYS,
    window: int = _DEFAULT_WINDOW,
    target_k: float = _DEFAULT_TARGET_K,
    method_suffix: str = "",
    audience_user_id=None,
    entity_ids: list[UUID] | None = None,
) -> BackfillReport:
    """Replay the trend producer across history for instant calibration.

    For each company entity with price coverage, steps backward from now in
    ``interval_days`` increments over ``lookback_days``, calling the existing
    trend producer at each timestamp with ``created_at`` set to that point in
    time. The producer reads the price window visible as-of that timestamp
    (point-in-time -- it cannot peek at future prices). After writing, the
    resolver scores predictions whose horizons have elapsed against the actual
    price path that followed.

    ``window`` and ``target_k`` are passed to the producer, controlling the SMA
    lookback and the vol-scaled target barrier. ``method_suffix`` is appended to
    the prediction's method name (e.g. ``".w100"`` -> ``"trend.sma.w100"``) so
    each parameter variant gets its own calibration bucket. The conviction gate
    then surfaces whichever variant has the best resolved hit rate -- the A/B
    test is self-judging.

    Idempotent: an entity whose earliest prediction for this method is already
    older than the lookback window is skipped, so a re-run after a successful
    backfill is a no-op for that entity.

    Every prediction written carries `provenance.assumptions.backfill` naming
    this run and the replayed decision time, so `trading/policy.py` can exclude
    it from the live count that opens GATE C. Backfilled predictions still
    resolve and still calibrate -- the marker withholds capital, not evidence.
    """
    method = _TREND_METHOD + method_suffix

    if entity_ids is None:
        rows = await pool.fetch(_ENTITIES_WITH_PRICES)
        entity_ids = [r["entity_id"] for r in rows]

    now = datetime.now(UTC)
    cutoff = now - timedelta(days=lookback_days)
    horizon = timedelta(days=horizon_days)
    run_id = str(uuid4())

    processed = 0
    written = 0
    skipped = 0

    for entity_id in entity_ids:
        oldest = await pool.fetchval(_OLDEST_PREDICTION, entity_id, method)
        if oldest is not None and oldest <= cutoff:
            skipped += 1
            continue

        entity_written = 0
        ts = cutoff
        while ts < now - horizon:
            try:
                # Write and stamp in one transaction: a row committed without
                # its marker reads as live forever, and the failure that left
                # it unmarked is exactly the one nobody notices.
                async with pool.acquire() as conn, conn.transaction():
                    pid = await produce_trend_prediction_from_coverage(
                        conn,
                        entity_id=entity_id,
                        audience_user_id=audience_user_id,
                        as_of=ts,
                        horizon_ends_at=ts + horizon,
                        created_at=ts,
                        window=window,
                        target_k=target_k,
                    )
                    if pid is not None:
                        if method_suffix:
                            await conn.execute(
                                "UPDATE prediction SET method = $1 WHERE id = $2",
                                method, pid,
                            )
                        await conn.execute(
                            _STAMP_BACKFILL,
                            json.dumps({
                                "backfill": {
                                    "run_id": run_id,
                                    "cutoff": cutoff.isoformat(),
                                    "as_of": ts.isoformat(),
                                }
                            }),
                            pid,
                        )
                if pid is not None:
                    entity_written += 1
            except Exception:
                logger.exception(
                    "backfill abstain at entity=%s ts=%s", entity_id, ts.isoformat()
                )
            ts += timedelta(days=interval_days)

        if entity_written > 0:
            processed += 1
            written += entity_written
        else:
            skipped += 1

    resolved = await resolve_due_predictions(pool)

    # A backfill resolves in bulk; force the statistics refresh rather than
    # waiting out the throttle, so the buckets reflect the new history
    # immediately.
    from omni.conviction.stats_refresh import refresh_statistics

    await refresh_statistics(pool)

    logger.info(
        "backfill [%s]: %d entities, %d predictions, %d resolved, %d skipped",
        method, processed, written, resolved, skipped,
    )
    return BackfillReport(
        entities_processed=processed,
        predictions_written=written,
        predictions_resolved=resolved,
        entities_skipped=skipped,
    )


async def backfill_parameter_sweep(
    pool,
    *,
    windows=(20, 50, 100),
    lookback_days: int = _DEFAULT_LOOKBACK_DAYS,
    interval_days: int = _DEFAULT_INTERVAL_DAYS,
    horizon_days: int = _DEFAULT_HORIZON_DAYS,
    audience_user_id=None,
) -> list[tuple[int, BackfillReport]]:
    """A/B test: backfill at multiple window sizes, each in its own calibration bucket.

    Each window size gets a distinct method name (``trend.sma.w20``,
    ``trend.sma.w50``, etc.) so the calibration_bucket view separates them. The
    conviction gate then surfaces whichever has the best resolved hit rate at
    high confidence -- the system self-selects the winner. Returns the report
    for each variant so the operator can compare.

    Usage::

        reports = await backfill_parameter_sweep(pool, windows=(20, 50, 100, 200))

    After resolution, compare calibration::

        SELECT method, count(*) FILTER (WHERE outcome <> 'pending') AS resolved,
               count(*) FILTER (WHERE outcome <> 'pending' AND
                 ((direction='up' AND outcome='upper') OR
                  (direction='down' AND outcome='lower'))) AS hits
        FROM prediction WHERE method LIKE 'trend.sma.w%'
        GROUP BY method ORDER BY method;
    """
    reports = []
    for w in windows:
        r = await backfill_trend_predictions(
            pool,
            lookback_days=lookback_days,
            interval_days=interval_days,
            horizon_days=horizon_days,
            window=w,
            method_suffix=f".w{w}",
            audience_user_id=audience_user_id,
        )
        reports.append((w, r))
    return reports
