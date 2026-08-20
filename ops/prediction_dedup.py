"""One-time cleanup: delete the redundant backfill re-walk grids from prediction.

WHAT HAPPENED
    The backfill skip marker compared each entity's earliest prediction against
    an unreachable cutoff (730-day lookback vs Polygon's 2y cap means the first
    producible stamp always sits AFTER the cutoff), so every scheduler restart
    between 2026-08-14 and 2026-08-19 re-walked the universe and appended a
    fresh replay grid at shifted timestamps. Fixed for the future by 49f13f0
    (cutoff + window margin; the 08-20 boot processed 7 / skipped 497). This
    script removes what already accumulated: 70 runs, ~1.77M backfill-marked
    rows, against one legitimate grid of ~50K per pass.

WHY RUN_ID MAKES THIS EXACT
    Every backfill row carries provenance.assumptions.backfill.run_id, so each
    re-walk is identifiable -- no fuzzy timestamp or barrier heuristics. Per
    (entity_id, method, audience) the KEPT grid is the single run with the most
    rows for that entity (ties: lowest min id) -- the most complete replay, and
    a prefix-truncated earlier run can never outrank a fuller later one. Every
    backfill row from every OTHER run for that entity is deleted, except rows a
    finding references (finding.prediction_id FK): provenance beats tidiness, a
    referenced row survives even as a duplicate.

    Live (unmarked) predictions are never touched. Calibration bucket RATES
    are unaffected by the delete -- n and hits scale together -- but the
    inflated n corrects after the refresh, which this script runs.

Run (read-only by default):
    python ops/prediction_dedup.py            # census + victim count, no writes
    python ops/prediction_dedup.py --live     # delete in batches, refresh MVs,
                                              # ANALYZE; add --vacuum-full to
                                              # also reclaim disk (brief exclusive
                                              # lock; safe on the idle box)
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys
from collections.abc import Sequence

logger = logging.getLogger("ops.prediction_dedup")

_IS_BACKFILL = "provenance->'assumptions' ? 'backfill'"
_RUN_ID = "provenance->'assumptions'->'backfill'->>'run_id'"

_CENSUS = f"""
SELECT count(*) AS backfill_rows,
       count(DISTINCT {_RUN_ID}) AS runs
FROM prediction
WHERE {_IS_BACKFILL}
"""

# The victims: backfill rows belonging to a run that is not the kept run for
# their (entity, method, audience), minus anything a finding points at.
_VICTIMS = f"""
SELECT p.id
FROM prediction p
JOIN (
  SELECT DISTINCT ON (entity_id, method, audience_user_id)
         entity_id, method, audience_user_id, run_id
  FROM (
    SELECT entity_id, method, audience_user_id, {_RUN_ID} AS run_id,
           count(*) AS n
    FROM prediction
    WHERE {_IS_BACKFILL}
    GROUP BY 1, 2, 3, 4
  ) per_run
  ORDER BY entity_id, method, audience_user_id, n DESC, run_id
) keep ON keep.entity_id = p.entity_id
      AND keep.method = p.method
      AND keep.audience_user_id IS NOT DISTINCT FROM p.audience_user_id
WHERE {_IS_BACKFILL}
  AND p.{_RUN_ID} IS DISTINCT FROM keep.run_id
  AND NOT EXISTS (SELECT 1 FROM finding f WHERE f.prediction_id = p.id)
"""


async def main(argv: Sequence[str] | None = None) -> int:
    from omni.config import settings
    from omni.db import connect

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")

    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--live", action="store_true", help="execute the delete")
    parser.add_argument(
        "--vacuum-full", action="store_true",
        help="after the delete, VACUUM FULL prediction to reclaim disk "
             "(exclusive lock for the rewrite; run only on a quiet box)",
    )
    parser.add_argument("--batch", type=int, default=50_000)
    args = parser.parse_args(argv)

    client = await connect(settings.database_url)
    try:
        async with client.pool.acquire() as pool:
            return await _run(pool, args)
    finally:
        await client.close()


async def _run(pool, args) -> int:
    size_before = await pool.fetchval(
        "SELECT pg_size_pretty(pg_total_relation_size('prediction'))"
    )
    rows, runs = await pool.fetchrow(_CENSUS)
    logger.info(
        "prediction table %s; backfill rows %d across %d runs",
        size_before, rows, runs,
    )

    await pool.execute(
        "CREATE TEMP TABLE dedup_victims (id uuid primary key) "
        "ON COMMIT PRESERVE ROWS"
    )
    await pool.execute(
        f"INSERT INTO dedup_victims {_VICTIMS}"
    )
    victims = await pool.fetchval("SELECT count(*) FROM dedup_victims")
    total = await pool.fetchval("SELECT count(*) FROM prediction")
    logger.info(
        "victims: %d of %d rows (%.1f%%); kept after cleanup: %d",
        victims, total, 100.0 * victims / max(total, 1), total - victims,
    )

    if not args.live:
        logger.info("dry run: nothing deleted")
        return 0

    deleted = 0
    while True:
        # Consume the temp table as we go: the CTE deletes the batch's ids
        # from dedup_victims first, then the prediction rows. Without the
        # consumption the next batch would re-select the same already-
        # deleted ids, match nothing, and exit the loop early.
        batch = await pool.fetch(
            "WITH consumed AS ("
            "  DELETE FROM dedup_victims"
            "  WHERE id IN (SELECT id FROM dedup_victims LIMIT $1)"
            "  RETURNING id"
            ") DELETE FROM prediction p"
            "  USING consumed"
            "  WHERE p.id = consumed.id RETURNING 1",
            args.batch,
        )
        deleted += len(batch)
        if len(batch) < args.batch:
            break
        logger.info("deleted %d so far", deleted)
    logger.info("deleted %d rows", deleted)

    for view in ("calibration_bucket", "finding_payoff"):
        await pool.execute(
            f"REFRESH MATERIALIZED VIEW CONCURRENTLY {view}"
        )
        logger.info("refreshed %s", view)
    await pool.execute("ANALYZE prediction")
    logger.info("analyzed prediction")

    if args.vacuum_full:
        await pool.execute("VACUUM FULL prediction")
        logger.info("vacuum full prediction")

    size_after = await pool.fetchval(
        "SELECT pg_size_pretty(pg_total_relation_size('prediction'))"
    )
    remaining = await pool.fetchval("SELECT count(*) FROM prediction")
    logger.info(
        "prediction now %d rows, %s (was %s)",
        remaining, size_after, size_before,
    )
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
