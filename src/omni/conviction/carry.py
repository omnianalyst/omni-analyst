"""Carry-funding producer: persistent perp funding -> a directional carry call.

`RESEARCH.md` rates funding-rate carry the best risk-adjusted crypto strategy
available to a solo operator, and ``funding_rate`` claims now flow from
``ingest/derivatives.py`` -- but nothing reads them. This producer does.

When perpetual funding is persistently positive, longs pay shorts every
settlement; a short perp position collects that stream. The directional bet is
NOT that price falls -- it is that funding stays positive long enough for the
collected carry to exceed the price risk taken. So this is a price prediction
whose EDGE comes from carry, and the barriers are price levels the triple-barrier
schema can score -- exactly the discipline ``ledger.py`` enforces.

The barriers are model-grounded, not invented (mirroring ``trend.py``):

- **Direction** is ``down`` when funding is persistently positive (a short
  collects), ``up`` when persistently negative. Persistence, not a single print:
  the trailing ``persistence`` settlements must all share one nonzero sign.
- **The invalidation barrier** is the price move that erases the carry collected
  over the horizon. Expected carry = mean funding rate x settlements remaining,
  converted to a price distance from entry. That level -- where the trade stops
  being worth holding -- is derived from the funding stream and the horizon, not
  chosen. It is the carry analogue of ``trend.py``'s "the MA is the
  invalidation".
- **The target barrier** is a volatility-scaled move in the trade's direction,
  exactly as ``trend.py`` sizes its target with realized vol.

The funding sign convention is the one ``venue/costs.py::carry_cost`` prices and
``ingest/derivatives.py`` preserves: **positive means longs pay shorts**. A short
therefore collects when funding is positive, so persistently-positive funding
yields direction ``down``. Inverting this sign prices the strategy as its own
opposite.

Abstention is honest, not failure: fewer than ``persistence`` settlements, a
sign flip or a zero inside the window, a non-finite rate, zero/non-finite
realized vol, zero/non-finite expected carry, no price to anchor entry, or a
horizon containing no settlement -- each returns ``None`` rather than a
manufactured call.
"""

from __future__ import annotations

import json
import math
from datetime import UTC, datetime
from itertools import pairwise
from uuid import UUID

import numpy as np

from omni.conviction.ledger import record_prediction
from omni.conviction.predict import _first_passage_confidence
from omni.conviction.trend import _realized_vol
from omni.coverage.visibility import visible_claims_cte

_CAPABILITY = "carry.funding"
METHOD = "carry.funding"

DEFAULT_PERSISTENCE = 8
DEFAULT_TARGET_K = 2.0
DEFAULT_PRICE_WINDOW = 20

# One idiom (abs(x) <= atol, never ==), two scale-consistent tolerances. A single
# number cannot serve both guards: vol is a price-space quantity (stdev x close,
# matching trend.py), while the expected carry is a dimensionless return. Funding
# prints are ~1e-4 per settlement, so 1e-12 is far below any real signal and
# fires only on a true zero or float dust.
_ZERO_VOL_ATOL = 1e-9
_ZERO_CARRY_ATOL = 1e-12


def _median_gap_seconds(dates: list[datetime]) -> float | None:
    if len(dates) < 2:
        return None
    gaps = sorted((b - a).total_seconds() for a, b in pairwise(dates))
    return gaps[len(gaps) // 2]


async def _funding_window(
    pool, *, entity_id: UUID, audience: UUID | None, as_of: datetime, limit: int
) -> list[tuple[datetime, float]]:
    """The trailing ``limit`` funding settlements visible to the audience as-of
    ``as_of``, oldest-first, as ``(event_date, rate)``.

    Point-in-time: a funding rate filed after ``as_of`` is invisible. The rate is
    parsed to float from the adapter's decimal-faithful string. A successfully
    parsed non-finite value (``NaN``/``inf``) is KEPT -- it must poison the
    persistence check into abstaining rather than be silently dropped, which is
    the exact failure mode AGENTS.md's float section names.
    """
    rows = await pool.fetch(
        f"""
        WITH visible AS (
        {visible_claims_cte("$3")}
        )
        SELECT c.value, c.event_date
        FROM visible c
        WHERE c.entity_id = $1
          AND c.claim_type = 'funding_rate'
          AND c.knowledge_date <= $2
        ORDER BY c.event_date DESC
        LIMIT $4
        """,
        entity_id,
        as_of,
        audience,
        limit,
    )
    points: list[tuple[datetime, float]] = []
    for r in rows:
        raw = r["value"]
        if isinstance(raw, (str, bytes)):
            raw = json.loads(raw)
        if not isinstance(raw, dict):
            continue
        rate_str = raw.get("rate")
        if rate_str is None:
            continue
        try:
            rate = float(rate_str)
        except (TypeError, ValueError):
            continue
        points.append((r["event_date"], rate))
    points.reverse()  # oldest-first
    return points


async def _price_window(
    pool, *, entity_id: UUID, audience: UUID | None, as_of: datetime, limit: int
) -> list[float]:
    """The trailing daily closes knowable as-of ``as_of``, oldest-first.

    Mirrors ``trend._price_window``; CoinGecko snapshots carry ``price``,
    Polygon bars carry ``close``.
    """
    rows = await pool.fetch(
        f"""
        WITH visible AS (
        {visible_claims_cte("$3")}
        )
        SELECT c.value
        FROM visible c
        WHERE c.entity_id = $1
          AND c.claim_type = 'price_snapshot'
          AND c.knowledge_date <= $2
        ORDER BY c.event_date DESC
        LIMIT $4
        """,
        entity_id,
        as_of,
        audience,
        limit,
    )
    closes: list[float] = []
    for r in rows:
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
    return closes


def carry_call(
    *,
    entry: float,
    vol: float,
    mean_rate: float,
    settlements_remaining: int,
    target_k: float = DEFAULT_TARGET_K,
) -> tuple[str, float, float, float] | None:
    """A falsifiable carry call, or ``None`` when none is honest.

    Returns ``(direction, upper, lower, confidence)`` with barriers that
    genuinely straddle entry. ``direction`` is ``down`` when funding is
    persistently positive (a short collects the stream), ``up`` when persistently
    negative.

    The **invalidation** barrier is the price move that erases expected carry:
    ``carry_distance = entry x |mean_rate| x settlements_remaining``. For a short
    (direction down) that is a rise to ``entry + carry_distance``; for a long
    (direction up) a fall to ``entry - carry_distance``. The **target** is a
    vol-scaled move in the trade's direction. Confidence is the driftless
    first-passage probability of hitting the target before carry is erased,
    mirroring ``trend.trend_call``.

    ``None`` when: realized vol is non-positive/non-finite (no honest target),
    expected carry is non-finite or ~zero (no edge to protect), or the
    constructed barriers fail the straddle ``upper > entry > lower``.
    """
    if not math.isfinite(vol) or vol <= _ZERO_VOL_ATOL:
        return None
    expected_carry = mean_rate * settlements_remaining
    if not math.isfinite(expected_carry) or abs(expected_carry) <= _ZERO_CARRY_ATOL:
        return None

    carry_distance = entry * abs(expected_carry)
    if mean_rate > 0.0:
        direction = "down"
        upper = entry + carry_distance
        lower = entry - target_k * vol
    else:
        direction = "up"
        upper = entry + target_k * vol
        lower = entry - carry_distance

    if not (upper > entry > lower):
        return None
    confidence = _first_passage_confidence(direction, entry, upper, lower)
    return direction, upper, lower, confidence


async def produce_carry_prediction_from_coverage(
    pool,
    *,
    entity_id: UUID,
    audience_user_id: UUID | None,
    horizon_ends_at: datetime,
    method: str = METHOD,
    as_of: datetime | None = None,
    created_at: datetime | None = None,
    persistence: int = DEFAULT_PERSISTENCE,
    price_window: int = DEFAULT_PRICE_WINDOW,
    target_k: float = DEFAULT_TARGET_K,
) -> UUID | None:
    """Produce a funding-carry directional prediction from coverage.

    Reads the trailing ``persistence`` funding settlements and ``price_window``
    daily closes visible to the audience as-of ``as_of`` (default now), and
    records a directional call whose invalidation erases expected carry and whose
    target is vol-scaled. Returns the new prediction id, or ``None`` when
    coverage is insufficient or the regime is not persistent carry: fewer than
    ``persistence`` settlements, a sign flip or a zero inside the window, a
    non-finite rate, zero/non-finite vol, zero/non-finite expected carry, no
    price to anchor entry, or a horizon containing no settlement. Abstention is
    the honest outcome, never a manufactured call.
    """
    if as_of is None:
        as_of = datetime.now(UTC)

    funding = await _funding_window(
        pool,
        entity_id=entity_id,
        audience=audience_user_id,
        as_of=as_of,
        limit=persistence,
    )
    if len(funding) < persistence:
        return None

    rates = np.array([f[1] for f in funding], dtype=float)
    if not np.all(np.isfinite(rates)):
        return None

    signs = np.sign(rates)
    if np.any(signs == 0):
        return None
    if not np.all(signs == signs[0]):
        return None

    mean_rate = float(np.mean(rates))

    cadence_s = _median_gap_seconds([f[0] for f in funding])
    if cadence_s is None or cadence_s <= 0.0:
        return None
    horizon_s = (horizon_ends_at - as_of).total_seconds()
    settlements_remaining = round(horizon_s / cadence_s)
    if settlements_remaining <= 0:
        return None

    closes = await _price_window(
        pool,
        entity_id=entity_id,
        audience=audience_user_id,
        as_of=as_of,
        limit=price_window,
    )
    if not closes:
        return None
    entry = closes[-1]
    vol = _realized_vol(closes)

    call = carry_call(
        entry=entry,
        vol=vol,
        mean_rate=mean_rate,
        settlements_remaining=settlements_remaining,
        target_k=target_k,
    )
    if call is None:
        return None
    direction, upper, lower, confidence = call

    return await record_prediction(
        pool,
        entity_id=entity_id,
        capability=_CAPABILITY,
        method=method,
        direction=direction,
        confidence=confidence,
        entry_price=entry,
        upper_barrier=upper,
        lower_barrier=lower,
        horizon_ends_at=horizon_ends_at,
        audience_user_id=audience_user_id,
        created_at=created_at,
        assumptions={
            "model": "carry_funding",
            "persistence": persistence,
            "target_k": target_k,
            "mean_rate": mean_rate,
            "settlements_remaining": settlements_remaining,
            "settlement_interval_s": cadence_s,
            "expected_carry": mean_rate * settlements_remaining,
            "realized_vol": vol,
            "entry": entry,
            "confidence_model": "driftless_first_passage",
        },
    )
