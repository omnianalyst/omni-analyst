"""Phase F: meta-calibration -- the system grades its own judgment.

The conviction gate (Phase D) calibrates the system's PREDICTIONS: "calls at
0.74 confidence have hit 73% of the time." This loop calibrates the system's
REASONING: "when the macro regime said risk_on, did the market actually rise?"
and "when the scanner ranked XLK highest, did XLK actually lead?"

A regime_assessment is correct if the broad market moved in the assessed
direction over the following window: risk_on -> market up, risk_off -> market
down. A sector_score is correct if the sector outperformed the cross-sectional
median over the same window. The results land in ``meta_resolution``; the
meta-hit-rate feeds back into the sector scanner as a weight multiplier on
sectors the system has historically judged correctly.

This is the product's most novel capability: a system that calibrates its
JUDGMENT, not just its predictions. A regime call is a stronger kind of
falsifiability than a price target -- it says "the world works this way," and
the market is the oracle that scores it.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from uuid import UUID

from omni.autonomous.reading import price_closes

logger = logging.getLogger("omni.autonomous.meta")

_META_HORIZON_DAYS = 30


@dataclass(frozen=True)
class MetaReport:
    regimes_resolved: int = 0
    sectors_resolved: int = 0
    already_resolved: int = 0


async def _index_return(pool, *, entity_id, event_date, horizon):
    """The return on the broad-market proxy (SPY) over [event_date, +horizon]."""
    closes = await price_closes(pool, entity_id=entity_id, limit=500)
    if len(closes) < 2:
        return None

    rows = await pool.fetch(
        "SELECT value, event_date FROM claim "
        "WHERE entity_id = $1 AND claim_type = 'price_snapshot' "
        "AND event_date >= $2 AND event_date <= $3 "
        "ORDER BY event_date ASC",
        entity_id,
        event_date,
        event_date + horizon,
    )
    if len(rows) < 2:
        return None
    vals = []
    for r in rows:
        v = r["value"]
        if isinstance(v, (str, bytes)):
            v = json.loads(v)
        if isinstance(v, dict) and v.get("close") is not None:
            vals.append(float(v["close"]))
    if len(vals) < 2 or vals[0] == 0:
        return None
    return vals[-1] / vals[0] - 1.0


_DUE_REGIMES = """
SELECT c.id, c.entity_id, c.value, c.event_date
FROM claim c
WHERE c.claim_type = 'regime_assessment' AND c.superseded_by IS NULL
  AND c.audience_user_id IS NULL
  AND NOT EXISTS (SELECT 1 FROM meta_resolution m WHERE m.claim_id = c.id)
  AND c.event_date <= $1
ORDER BY c.event_date
"""

_DUE_SECTORS = """
SELECT c.id, c.entity_id, c.value, c.event_date
FROM claim c
WHERE c.claim_type = 'sector_score' AND c.superseded_by IS NULL
  AND NOT EXISTS (SELECT 1 FROM meta_resolution m WHERE m.claim_id = c.id)
  AND c.event_date <= $1
ORDER BY c.event_date
"""

_INSERT_META = """
INSERT INTO meta_resolution (claim_id, claim_type, correct, actual_outcome, evidence)
VALUES ($1, $2, $3, $4, $5::jsonb)
ON CONFLICT (claim_id) DO NOTHING
"""


async def resolve_meta(pool, *, horizon_days: int = _META_HORIZON_DAYS) -> MetaReport:
    """Score regime and sector assessments against the market that followed.

    For each regime_assessment older than ``horizon_days``: reads the SPY return
    over the window; risk_on is correct if the market rose, risk_off if it fell.
    For each sector_score: reads the ETF return over the window; a top-half
    sector (rs_percentile >= 0.5) is correct if it outperformed zero (the
    zero-return floor -- a sector that scored high but declined was wrong).

    Idempotent: meta_resolution PKs on claim_id, so re-running scores only
    claims that are newly due.
    """
    now = datetime.now(UTC)
    cutoff = now - timedelta(days=horizon_days)
    horizon = timedelta(days=horizon_days)

    spy_id = await pool.fetchval(
        "SELECT id FROM entity WHERE kind = 'index' AND symbol = 'SPY'"
    )

    regimes_n = 0
    if spy_id is not None:
        due_regimes = await pool.fetch(_DUE_REGIMES, cutoff)
        for r in due_regimes:
            rv = r["value"]
            if isinstance(rv, (str, bytes)):
                rv = json.loads(rv)
            risk = rv.get("risk_regime", "transition")
            if risk == "transition":
                continue

            mkt_ret = await _index_return(
                pool, entity_id=spy_id, event_date=r["event_date"], horizon=horizon
            )
            if mkt_ret is None:
                continue

            if risk == "risk_on":
                correct = mkt_ret > 0
            else:
                correct = mkt_ret < 0

            await pool.execute(
                _INSERT_META,
                r["id"],
                "regime_assessment",
                correct,
                f"market_return={mkt_ret:.4f}",
                json.dumps({"risk_regime": risk, "market_return": round(mkt_ret, 6)}),
            )
            regimes_n += 1

    sectors_n = 0
    due_sectors = await pool.fetch(_DUE_SECTORS, cutoff)
    for s in due_sectors:
        sv = s["value"]
        if isinstance(sv, (str, bytes)):
            sv = json.loads(sv)
        rs = float(sv.get("rs_percentile", 0))
        etf_ret = await _index_return(
            pool, entity_id=s["entity_id"], event_date=s["event_date"], horizon=horizon
        )
        if etf_ret is None:
            continue

        is_top = rs >= 0.5
        if is_top:
            correct = etf_ret > 0
            outcome = f"top_sector_return={etf_ret:.4f}"
        else:
            correct = etf_ret < 0
            outcome = f"bottom_sector_return={etf_ret:.4f}"

        await pool.execute(
            _INSERT_META,
            s["id"],
            "sector_score",
            correct,
            outcome,
            json.dumps({"rs_percentile": rs, "sector_return": round(etf_ret, 6)}),
        )
        sectors_n += 1

    if regimes_n or sectors_n:
        logger.info(
            "meta-calibration: %d regimes, %d sectors resolved",
            regimes_n, sectors_n,
        )
    return MetaReport(regimes_resolved=regimes_n, sectors_resolved=sectors_n)


async def meta_hit_rate(pool, *, claim_type: str) -> float | None:
    """The system's hit rate on its own assessments of this type.

    Returns the fraction correct, or None if fewer than 5 resolved (too few to
    be meaningful -- the same floor the conviction gate uses, though smaller
    because regime/sector assessments are rarer than stock predictions).
    """
    row = await pool.fetchrow(
        "SELECT count(*)::int AS n, count(*) FILTER (WHERE correct)::int AS hits "
        "FROM meta_resolution WHERE claim_type = $1",
        claim_type,
    )
    if row is None or row["n"] < 5:
        return None
    return row["hits"] / row["n"]
