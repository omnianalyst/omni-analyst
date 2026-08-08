"""fundamentals.protocol producer: P/F mean reversion -> a directional call.

Price-to-fees (P/F) is the crypto counterpart to the equity DCF: market cap over
protocol fees, the headline valuation multiple. ``protocol_fees`` (DefiLlama,
``allowed`` -- shared coverage) and ``protocol_revenue`` flow from
``ingest/defillama.py``; ``market_cap`` rides inside the ``price_snapshot``
value dict alongside the price (CoinGecko, ``byo_only`` -- private to the
credential owner). Nothing else emits a market cap.

The directional call is mean reversion of the multiple toward that protocol's
OWN trailing history, never toward a cross-protocol average. Protocols have
structurally different fee economics (a L2's per-tx fees are unrelated to a
lending venue's interest spread), so anchoring a "fair" multiple to a peer set
is how a defensible number becomes a fabricated one. The only honest reference
is the protocol's own past.

The barriers are model-grounded, not invented -- both come from the multiple's
OWN trailing distribution, which IS the model:

- **The target** is the price at which the multiple equals its trailing mean.
  The model's assertion is "the multiple reverts to its own history"; the target
  IS that reversion level. For an overvalued protocol (current multiple above
  its mean) the target sits below entry (direction ``down``); for an undervalued
  one it sits above (direction ``up``). This inverts ``trend.py`` (whose
  model-grounded barrier is the invalidation, the MA) because momentum follows
  while reversion targets -- the model's prediction is the destination, so the
  destination is the model-grounded barrier.
- **The invalidation** is ``barrier_k`` trailing standard deviations of the
  multiple, measured from the current level in the adverse direction. Its
  MEANING: the mispricing has widened beyond the multiple's own trailing
  dispersion -- the reversion thesis is broken when the multiple moves further
  against the position than its own history says it normally wanders. It is the
  multiple-space analogue of ``trend.py``'s vol-scaled barrier, but scaled by
  the multiple's measured stdev rather than the price's. Both barriers therefore
  come from the same trailing window the mean is read from; no round number, no
  fixed percentage, no cross-protocol constant enters.

Because the target (mean) and the invalidation (k stdevs from current) are
constructed differently, the barriers are asymmetric and the driftless
first-passage confidence genuinely discriminates -- unlike a symmetric pair,
which collapses to a useless constant 0.5 the conviction gate cannot calibrate
on.

Market cap is ``byo_only`` (CoinGecko); fees are ``allowed`` (DefiLlama).
Everything is read through ``coverage/visibility.py`` scoped to the audience,
which resolves the licence boundary: the gap engine never reasons about
licences itself. An operator with no CoinGecko key has no visible market cap and
so no multiple -- that is an abstention, and the correct outcome.

Abstention is honest, not failure: fewer than ``window`` aligned
market-cap/fees days (short or misaligned history), fees or revenue zero across
the window (no denominator, no real accrual economics), a flat multiple series
(zero dispersion -- no invalidation to set), no spread between current and mean
(nothing to revert), a non-finite input (NaN/inf must poison, never propagate),
or a null market cap on every snapshot (the no-CoinGecko-key case). Each returns
``None`` rather than a manufactured call.
"""

from __future__ import annotations

import json
import math
from datetime import UTC, datetime
from uuid import UUID

import numpy as np

from omni.conviction.ledger import record_prediction
from omni.conviction.predict import _first_passage_confidence
from omni.coverage.visibility import visible_claims_cte

_CAPABILITY = "fundamentals.protocol"
METHOD = "fundamentals.protocol"

DEFAULT_WINDOW = 30
DEFAULT_BARRIER_K = 2.0

# One idiom (abs(x) <= atol / x <= atol, never ==), two scale-consistent
# tolerances. A single number cannot serve both guards: USD-valued quantities
# (fees, revenue, market cap) live at dollar scale where a cent is the floor
# beneath which a value is genuinely zero, while the multiple's stdev is a
# dimensionless ratio whose float dust lands far below 1e-9 for any realistic
# multiple. The straddle (upper > entry > lower > 0) is the final backstop for
# the magnitude cases a fixed absolute tolerance cannot reach.
_ZERO_USD_ATOL = 1e-6
_ZERO_SIGMA_ATOL = 1e-9


async def _price_marketcap_window(
    pool, *, entity_id: UUID, audience: UUID | None, as_of: datetime, limit: int
) -> list[tuple[datetime, float | None, float | None]]:
    """Trailing ``limit`` price snapshots knowable as-of ``as_of``, oldest-first.

    Each CoinGecko snapshot carries ``{"price": p, "market_cap": mc, ...}``;
    Polygon bars carry ``close``. ``market_cap`` may be ``None``:
    ``parse_market_chart`` joins the three parallel arrays on timestamp and a
    missing entry stays honestly absent rather than borrowing a neighbour. A
    ``None`` market cap is preserved here so the caller abstains on it rather
    than fabricating one (price times an assumed supply, or a forward-filled
    cap) -- the exact fabrication the work order names.
    """
    rows = await pool.fetch(
        f"""
        WITH visible AS (
        {visible_claims_cte("$3")}
        )
        SELECT c.value, c.event_date
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
    points: list[tuple[datetime, float | None, float | None]] = []
    for r in rows:
        raw = r["value"]
        if isinstance(raw, (str, bytes)):
            raw = json.loads(raw)
        if not isinstance(raw, dict):
            continue
        price = raw.get("price")
        if price is None:
            price = raw.get("close")
        mcap = raw.get("market_cap")
        try:
            price_f = float(price) if price is not None else None
        except (TypeError, ValueError):
            price_f = None
        try:
            mcap_f = float(mcap) if mcap is not None else None
        except (TypeError, ValueError):
            mcap_f = None
        points.append((r["event_date"], price_f, mcap_f))
    points.reverse()  # oldest-first
    return points


async def _scalar_window(
    pool,
    *,
    entity_id: UUID,
    audience: UUID | None,
    as_of: datetime,
    claim_type: str,
    field: str,
    limit: int,
) -> list[tuple[datetime, float]]:
    """Trailing ``limit`` scalar values knowable as-of ``as_of``, oldest-first.

    Reads a single value-dict key (``fees`` or ``revenue``) off the given claim
    type. A value that fails to parse is dropped rather than coerced.
    """
    rows = await pool.fetch(
        f"""
        WITH visible AS (
        {visible_claims_cte("$3")}
        )
        SELECT c.value, c.event_date
        FROM visible c
        WHERE c.entity_id = $1
          AND c.claim_type = $4::claim_type
          AND c.knowledge_date <= $2
        ORDER BY c.event_date DESC
        LIMIT $5
        """,
        entity_id,
        as_of,
        audience,
        claim_type,
        limit,
    )
    points: list[tuple[datetime, float]] = []
    for r in rows:
        raw = r["value"]
        if isinstance(raw, (str, bytes)):
            raw = json.loads(raw)
        if not isinstance(raw, dict):
            continue
        v = raw.get(field)
        if v is None:
            continue
        try:
            val = float(v)
        except (TypeError, ValueError):
            continue
        points.append((r["event_date"], val))
    points.reverse()  # oldest-first
    return points


def protocol_call(
    *,
    entry: float,
    current_multiple: float,
    mean_multiple: float,
    sigma_multiple: float,
    barrier_k: float = DEFAULT_BARRIER_K,
) -> tuple[str, float, float, float] | None:
    """A falsifiable P/F mean-reversion call, or ``None`` when none is honest.

    Returns ``(direction, upper, lower, confidence)`` with barriers that
    genuinely straddle a positive entry. ``direction`` is ``down`` when the
    current multiple sits above its trailing mean (overvalued -> revert down),
    ``up`` when below.

    The **target** is the price at which the multiple equals its trailing mean
    (the reversion level). The **invalidation** is ``barrier_k`` trailing stdevs
    of the multiple, measured from the current level in the adverse direction:
    the mispricing has widened beyond the multiple's own dispersion. Confidence
    is the driftless first-passage probability of reaching the target before the
    invalidation, mirroring ``trend.trend_call`` and ``carry.carry_call``.

    ``None`` when: any input is non-finite, any multiple is non-positive, the
    multiple series is flat (sigma ~ 0 -> no dispersion to set an invalidation),
    current equals the mean (nothing to revert), the invalidation would land at
    or below zero (a price can never reach a negative barrier), or the
    constructed barriers fail the straddle.
    """
    if not all(
        math.isfinite(x)
        for x in (entry, current_multiple, mean_multiple, sigma_multiple, barrier_k)
    ):
        return None
    if entry <= 0.0 or current_multiple <= 0.0 or mean_multiple <= 0.0:
        return None
    if sigma_multiple <= _ZERO_SIGMA_ATOL:
        return None
    if math.isclose(current_multiple, mean_multiple, rel_tol=_ZERO_SIGMA_ATOL):
        return None

    if current_multiple > mean_multiple:
        direction = "down"
        lower = entry * (mean_multiple / current_multiple)
        upper = entry * (1.0 + barrier_k * sigma_multiple / current_multiple)
    else:
        direction = "up"
        upper = entry * (mean_multiple / current_multiple)
        lower = entry * (1.0 - barrier_k * sigma_multiple / current_multiple)

    if not (0.0 < lower < entry < upper):
        return None
    confidence = _first_passage_confidence(direction, entry, upper, lower)
    return direction, upper, lower, confidence


async def produce_protocol_fundamentals_prediction_from_coverage(
    pool,
    *,
    entity_id: UUID,
    audience_user_id: UUID | None,
    horizon_ends_at: datetime,
    method: str = METHOD,
    as_of: datetime | None = None,
    created_at: datetime | None = None,
    window: int = DEFAULT_WINDOW,
    barrier_k: float = DEFAULT_BARRIER_K,
) -> UUID | None:
    """Produce a P/F mean-reversion directional prediction from coverage.

    Reads the trailing ``window`` daily market caps (off the same
    ``price_snapshot`` claims the price is read from), protocol fees and protocol
    revenue visible to the audience as-of ``as_of`` (default now). Builds the
    multiple series by aligning market cap and fees on calendar day -- the two
    sources publish independently and only their intersection carries a multiple
    -- then derives the trailing mean and stdev. Records a directional call whose
    target is the mean-reversion level and whose invalidation is a
    stdev-of-the-multiple adverse excursion.

    Returns the new prediction id, or ``None`` when coverage is insufficient:
    fewer than ``window`` aligned market-cap/fees days, fees or revenue zero
    across the window, a flat multiple series, no current-to-mean spread, a
    non-finite input, or no visible market cap at all (the no-CoinGecko-key
    case). Abstention is the honest outcome, never a manufactured call.
    """
    if as_of is None:
        as_of = datetime.now(UTC)

    caps = await _price_marketcap_window(
        pool, entity_id=entity_id, audience=audience_user_id, as_of=as_of, limit=window
    )
    fees = await _scalar_window(
        pool, entity_id=entity_id, audience=audience_user_id, as_of=as_of,
        claim_type="protocol_fees", field="fees", limit=window,
    )
    revenues = await _scalar_window(
        pool, entity_id=entity_id, audience=audience_user_id, as_of=as_of,
        claim_type="protocol_revenue", field="revenue", limit=window,
    )

    if not any(v > _ZERO_USD_ATOL for _, v in fees):
        return None
    if not any(v > _ZERO_USD_ATOL for _, v in revenues):
        return None

    cap_by_date: dict[object, tuple[float, float]] = {}
    for d, price, mcap in caps:
        if price is None or mcap is None:
            continue
        if not math.isfinite(price) or price <= _ZERO_USD_ATOL:
            continue
        if not math.isfinite(mcap) or mcap <= _ZERO_USD_ATOL:
            continue
        cap_by_date[d.date()] = (price, mcap)

    fees_by_date: dict[object, float] = {}
    for d, v in fees:
        if not math.isfinite(v) or v <= _ZERO_USD_ATOL:
            continue
        fees_by_date[d.date()] = v

    series: list[tuple[float, float]] = []  # (multiple, price), oldest-first
    for d, (price, mcap) in sorted(cap_by_date.items()):
        fee = fees_by_date.get(d)
        if fee is None:
            continue
        series.append((mcap / fee, price))

    if len(series) < window:
        return None

    multiples = np.array([m for m, _ in series], dtype=float)
    if not np.all(np.isfinite(multiples)):
        return None
    current_multiple = float(multiples[-1])
    entry = float(series[-1][1])
    mean_multiple = float(np.mean(multiples))
    sigma_multiple = float(np.std(multiples))

    call = protocol_call(
        entry=entry,
        current_multiple=current_multiple,
        mean_multiple=mean_multiple,
        sigma_multiple=sigma_multiple,
        barrier_k=barrier_k,
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
            "model": "protocol_pf_mean_reversion",
            "window": window,
            "barrier_k": barrier_k,
            "current_multiple": current_multiple,
            "mean_multiple": mean_multiple,
            "sigma_multiple": sigma_multiple,
            "entry": entry,
            "aligned_days": len(series),
            "confidence_model": "driftless_first_passage",
        },
    )
