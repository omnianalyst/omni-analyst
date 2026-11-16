"""Trend-following directional producer -- the class-A analysis DCF could not be
on real growth equities.

DCF abstains on growth (it values it below even the bull-case market) and
refuses on value (fragile EDGAR concept coverage -- a different missing concept
per filer). Trend-following generates directional calls on exactly that
universe: it reads ``price_snapshot`` claims (Polygon, clean, split-adjusted,
already flowing) and asserts "the trend continues" -- a genuine, falsifiable
price assertion the triple-barrier schema can score.

The barriers are model-grounded, not invented:
- the **invalidation** barrier is the moving average itself. Price crossing the
  MA the call was predicated on IS the trend breaking, so the MA is the honest
  stop. It is not a synthesized level.
- the **target** barrier is a volatility-scaled move (entry +/- k * realized vol).

Because the invalidation is the MA, the first-passage confidence -- P(hit the
target before the MA breaks -- is monotonic in how far price is from the MA in
vol units (the trend strength). A call entering right at the MA reads ~0; one
several vols into the trend reads high. That spread is what the conviction
gate's calibration needs to discriminate on, unlike symmetric barriers (which
collapse to a useless constant 0.5).

Abstention is honest, not failure: insufficient price history, a flat series
(zero vol -> no honest barrier), or price exactly on the MA (no trend to call)
each return ``None`` rather than a manufactured call.
"""

from __future__ import annotations

import json
import math
from datetime import datetime
from uuid import UUID

import numpy as np

from omni.conviction.ledger import record_prediction
from omni.conviction.predict import _first_passage_confidence
from omni.coverage.visibility import visible_claims_cte

_CAPABILITY = "trend.sma"
METHOD = "trend.sma"

# A daily stdev below this is a flat series -- barriers at entry +/- k*vol would
# collapse onto the entry. Scale-free guard on the vol itself (a price-space
# quantity), the codebase idiom.
_ZERO_VOL_ATOL = 1e-9

# The floor of resolved outcomes before a method's own measured continuation
# ratio may set its target geometry. Below this the producer uses the
# conservative default -- mirrors the gate's sample floor: a continuation ratio
# from a handful of resolutions is a lottery number wearing a ratio's clothes.
_CONTINUATION_FLOOR = 10


async def measured_continuation_ratio(
    pool,
    *,
    method: str = METHOD,
    audience: UUID | None = None,
    as_of: datetime | None = None,
) -> float | None:
    """The method's REALIZED move per unit of risk, over its resolved calls.

    For each surfaced, resolved, directional call: how far price actually went
    in the call's direction by the time it closed (the barrier it hit, or the
    recorded exit price on an expiry), divided by the distance it risked. The
    mean of that is what the method has delivered, and it is the only source
    the target multiple may come from.

    It was previously `avg_payoff_pct / avg_risk_pct` -- the barrier distances
    the calls were WRITTEN with. No outcome entered it: the "measured" ratio was
    the producer's own previous geometry read back, and once active it fixed
    every call's confidence at `1 / (1 + ratio)`.

    ``as_of`` makes the read point-in-time: only calls resolved (and surfaced)
    by then count. A replay passes its historical decision time, so a backfilled
    call cannot set its target from outcomes that had not happened yet. With no
    ``as_of`` -- a live call -- the materialized ``finding_payoff`` view is read,
    which is the same quantity as of the last statistics refresh.

    None below the sample floor, and None when the realized mean is not
    positive: a method that has not delivered continuation has no continuation
    ratio, and a zero or negative target distance is not a target.
    """
    if as_of is None:
        row = await pool.fetchrow(
            """
            SELECT SUM(realized_n) AS n,
                   SUM(avg_realized_ratio * realized_n)
                       / NULLIF(SUM(realized_n), 0) AS ratio
            FROM finding_payoff
            WHERE method = $1
              AND (audience_user_id IS NULL OR audience_user_id IS NOT DISTINCT FROM $2)
            """,
            method,
            audience,
        )
    else:
        row = await pool.fetchrow(
            """
            WITH directional AS (
                SELECT p.entry_price,
                       CASE p.direction WHEN 'up'
                            THEN p.entry_price - p.lower_barrier
                            ELSE p.upper_barrier - p.entry_price END AS risk_distance,
                       CASE p.direction WHEN 'up' THEN 1 ELSE -1 END AS side_sign,
                       CASE p.outcome
                            WHEN 'upper' THEN p.upper_barrier
                            WHEN 'lower' THEN p.lower_barrier
                            WHEN 'expiry' THEN p.exit_price END AS measured_exit
                FROM finding f
                JOIN prediction p ON p.id = f.prediction_id
                WHERE f.status = 'surfaced'
                  AND f.method = $1
                  AND (f.audience_user_id IS NULL
                       OR f.audience_user_id IS NOT DISTINCT FROM $2)
                  AND p.outcome <> 'pending'
                  AND p.resolved_at <= $3
                  AND f.created_at <= $3
                  AND p.direction IN ('up', 'down')
                  AND p.entry_price > 0 AND p.lower_barrier > 0
                  AND p.lower_barrier < p.entry_price
                  AND p.upper_barrier > p.entry_price
            )
            SELECT count(*) AS n,
                   avg(side_sign * (measured_exit - entry_price) / risk_distance)
                       AS ratio
            FROM directional
            WHERE measured_exit IS NOT NULL
            """,
            method,
            audience,
            as_of,
        )
    if row is None or not row["n"] or row["n"] < _CONTINUATION_FLOOR:
        return None
    if row["ratio"] is None:
        return None
    ratio = float(row["ratio"])
    if not math.isfinite(ratio) or ratio <= 0.0:
        return None
    return ratio


def _realized_vol(closes: list[float]) -> float:
    """The per-period stdev of log returns, in price units (x entry).

    Numpy's default ddof=0 (population) is intentional and consistent: this is a
    descriptor of the observed window, not an unbiased estimator of an
    underlying process. Guarded by the caller for the near-zero (flat) case.
    """
    if len(closes) < 2:
        return 0.0
    rets = np.diff(np.log(np.asarray(closes, dtype=float)))
    if not np.all(np.isfinite(rets)):
        return 0.0
    return float(np.std(rets)) * closes[-1]


def trend_call(
    entry: float,
    sma: float,
    vol: float,
    *,
    target_k: float = 2.0,
    continuation_ratio: float | None = None,
) -> tuple[str, float, float, float] | None:
    """A falsifiable trend call, or ``None`` when none is honest.

    Returns ``(direction, upper, lower, confidence)`` where the barriers
    genuinely straddle the entry and ``confidence`` is the driftless
    first-passage probability of hitting the target before the MA invalidates.

    The invalidation barrier is the MA (price crossing the moving average the
    call was predicated on IS the trend breaking). The target is expressed in
    the same unit -- a multiple of the stop distance:

    * with a measured ``continuation_ratio`` (the method's realized
      payoff/risk distance ratio over resolved calls, from finding_payoff),
      the target is that ratio of the stop distance: the geometry encodes the
      continuation this method has actually delivered, and the payoff view
      re-measures it after the fact. A closed loop, no hand-chosen constant.
    * without one (under the sample floor), the conservative vol-scaled
      ``target_k`` fallback applies until the ledger can speak.

    ``None`` when: vol is non-positive/non-finite (a flat series -- barriers
    would collapse onto the entry), price sits exactly on the MA (no trend to
    call), or the constructed barriers fail the straddle (upper > entry > lower).
    """
    if not math.isfinite(vol) or vol <= _ZERO_VOL_ATOL:
        return None
    if entry == sma:
        return None
    stop_distance = abs(entry - sma)
    move = (
        continuation_ratio * stop_distance
        if continuation_ratio is not None
        else target_k * vol
    )
    if entry > sma:
        direction, lower, upper = "up", sma, entry + move
    else:
        direction, upper, lower = "down", sma, entry - move
    if not (upper > entry > lower):
        return None
    confidence = _first_passage_confidence(direction, entry, upper, lower)
    return direction, upper, lower, confidence


async def _price_window_with_venue(
    pool, *, entity_id: UUID, audience: UUID | None, as_of: datetime, limit: int
) -> tuple[list[float], str | None]:
    """The trailing closes of ONE venue knowable as-of ``as_of``, oldest-first,
    and that venue's key.

    Point-in-time: a price filed after ``as_of`` is invisible, so a backtest
    cannot peek. Polygon bars carry ``close``; CoinGecko snapshots carry
    ``price``.

    **One venue, one row per timestamp.** An entity priced by two venues has two
    interleaved series, and a flat ``ORDER BY event_date LIMIT n`` over both is
    neither: the "50-bar" SMA spans 25 bars, and the realized vol is mostly the
    venues' basis bouncing between consecutive rows. A restated bar is the same
    problem in one venue. So the window is pinned to the venue of the newest
    visible print (keyed as ``basis._venue_price_series`` keys it), and
    ``DISTINCT ON (event_date)`` keeps the most recently knowable version of
    each bar.
    """
    rows = await pool.fetch(
        f"""
        WITH visible AS (
        {visible_claims_cte("$3")}
        ),
        priced AS (
            SELECT c.value, c.event_date, c.knowledge_date,
                   COALESCE(c.value->>'venue', c.source) AS venue_key
            FROM visible c
            WHERE c.entity_id = $1
              AND c.claim_type = 'price_snapshot'
              AND c.knowledge_date <= $2
        ),
        newest AS (
            SELECT venue_key FROM priced
            ORDER BY event_date DESC, knowledge_date DESC, venue_key
            LIMIT 1
        )
        SELECT DISTINCT ON (p.event_date) p.value, p.venue_key
        FROM priced p
        JOIN newest n ON n.venue_key IS NOT DISTINCT FROM p.venue_key
        ORDER BY p.event_date DESC, p.knowledge_date DESC
        LIMIT $4
        """,
        entity_id,
        as_of,
        audience,
        limit,
    )
    closes: list[float] = []
    venue: str | None = None
    for r in rows:
        venue = r["venue_key"]
        raw = r["value"]
        if isinstance(raw, (str, bytes)):
            raw = json.loads(raw)
        if not isinstance(raw, dict):
            continue
        scalar = raw.get("close")
        if scalar is None:
            scalar = raw.get("price")
        if scalar is None:
            continue
        try:
            closes.append(float(scalar))
        except (TypeError, ValueError):
            continue
    closes.reverse()  # oldest-first
    return closes, venue


async def _price_window(
    pool, *, entity_id: UUID, audience: UUID | None, as_of: datetime, limit: int
) -> list[float]:
    """The trailing closes of one venue, oldest-first. See
    `_price_window_with_venue`, which this is without the venue key."""
    closes, _venue = await _price_window_with_venue(
        pool, entity_id=entity_id, audience=audience, as_of=as_of, limit=limit
    )
    return closes


async def produce_trend_prediction_from_coverage(
    pool,
    *,
    entity_id: UUID,
    audience_user_id: UUID | None,
    as_of: datetime,
    horizon_ends_at: datetime,
    created_at: datetime | None = None,
    window: int = 50,
    target_k: float = 2.0,
) -> UUID | None:
    """Produce a trend-following directional prediction from price coverage.

    Reads the trailing ``window`` daily closes visible to the audience as-of
    ``as_of``, derives an SMA and a realized-vol, and records a directional call
    whose invalidation is the MA and whose target is vol-scaled. Returns the new
    prediction id, or ``None`` when coverage is insufficient (fewer than
    ``window`` closes), the series is flat, or price sits on the MA. Abstention
    is the honest outcome, never a manufactured call.
    """
    closes, price_venue = await _price_window_with_venue(
        pool, entity_id=entity_id, audience=audience_user_id, as_of=as_of,
        limit=window,
    )
    if len(closes) < window:
        return None

    entry = closes[-1]
    sma = float(np.mean(closes))
    vol = _realized_vol(closes)
    # The target multiple comes from the method's own resolved record when it
    # has one (finding_payoff, above the sample floor); the conservative
    # vol-scaled default otherwise. Never a hand-chosen constant.
    #
    # A replay stamps `created_at` with its historical decision time; then the
    # ratio is read point-in-time as of that decision, so a backfilled call's
    # target is never set from outcomes that had not happened yet. A live call
    # (no `created_at`) reads the materialized statistics.
    ratio = await measured_continuation_ratio(
        pool,
        method=METHOD,
        audience=audience_user_id,
        as_of=as_of if created_at is not None else None,
    )
    call = trend_call(entry, sma, vol, target_k=target_k, continuation_ratio=ratio)
    if call is None:
        return None
    direction, upper, lower, confidence = call

    return await record_prediction(
        pool,
        entity_id=entity_id,
        capability=_CAPABILITY,
        method=METHOD,
        direction=direction,
        confidence=confidence,
        entry_price=entry,
        upper_barrier=upper,
        lower_barrier=lower,
        horizon_ends_at=horizon_ends_at,
        audience_user_id=audience_user_id,
        created_at=created_at,
        assumptions={
            "model": "trend_sma",
            "window": window,
            "target_k": target_k,
            "continuation_ratio": ratio,
            "sma": sma,
            "realized_vol": vol,
            "confidence_model": "driftless_first_passage",
            # The venue the window (and so the entry) was read from. The
            # resolver scores the call on this venue's path alone.
            **({"price_venue": price_venue} if price_venue is not None else {}),
        },
    )
