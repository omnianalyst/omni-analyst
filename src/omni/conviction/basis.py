"""Basis (cross-venue) producer: price dislocation between venues -> a directional convergence call.

``basis.crossvenue`` reads ``price_snapshot`` claims from two or more venues for
one asset -- the cross-venue dislocation ``exchanges.py`` makes possible and
CoinGecko's aggregate feed cannot represent -- and asserts "the spread
converges". This is the basis trade: short the rich venue, long the cheap one,
collect the spread as it closes. ``trend.py`` bets a price continues;
``carry.py`` bets funding persists; this bets a spread reverts. Each is a
distinct edge with a distinct invalidation.

**The edge is the spread converging, not either leg moving.** So the barriers
are spread-grounded, mirroring how ``trend.sma`` grounds its invalidation in the
moving average and ``carry.funding`` in the carry-erasing move:

- **The TARGET barrier is full convergence**: the other venue's price. That is
  the level the anchored leg reaches when the spread has closed entirely. It is
  not a synthesized level -- it is literally the definition of the edge
  resolving. It is the basis analogue of ``trend.py``'s "the MA is the
  invalidation" (here, convergence is the target).
- **The INVALIDATION barrier is the spread widening past the level where
  convergence stops being the base case.** That level is ``widening_k`` standard
  deviations of the historical spread series from the entry: when the
  dislocation has grown to a multiple of its own normal dispersion, mean
  reversion is no longer the base case and the regime has changed. The standard
  deviation is the model's own measure of how the spread normally moves, so the
  barrier is model-identified, never a round number or fixed percentage.

Which leg the call is anchored on (and thus whether direction is ``up`` or
``down``) is a reporting convention, not an economic choice -- the basis trade
is symmetric. A perpetual is the natural leveraged leg of a basis trade, so when
the latest ``funding_rate`` identifies one venue as the perp the call anchors
there (a perp at premium -> short it -> direction ``down``; a perp at discount ->
long it -> direction ``up``). With no perp identifiable the anchor is the
deterministically-first venue by key, so the recorded direction is stable across
replays. ``funding_rate`` selects the leg; it never gates the call -- a
venue-venue spread (no perp) is still a basis.

Abstention is honest, not failure: fewer than two venues with a current price
(one venue is a price, not a basis), fewer than two paired spread observations
(no dispersion to set the invalidation from), a zero or non-finite spread
dispersion (lockstep venues -- no honest widening level), a zero or non-finite
current dislocation (venues already agree -- nothing to converge), or a
non-finite input price each return ``None`` rather than a manufactured call.
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

_CAPABILITY = "basis.crossvenue"
METHOD = "basis.crossvenue"

DEFAULT_WINDOW = 20
DEFAULT_WIDENING_K = 2.0

# The minimum number of historical spread observations needed to estimate the
# spread's dispersion. Below this there is no honest widening level: np.std of a
# single point is 0.0, which would route every thin-window case through the
# zero-dispersion abstention rather than naming the real reason (insufficient
# history).
MIN_SPREAD_POINTS = 2

# One idiom (abs(x) <= atol, never ==), one scale-consistent tolerance. Both
# the current dislocation and the spread dispersion are price-space quantities
# (a price difference and the stdev of price differences), so a single tolerance
# serves both guards. Spreads/vols from a constant series land at ~1e-16 (float
# dust); any genuine dispersion on any real asset is far above 1e-9, so the
# guard fires only on a true zero or float noise -- never on a real signal.
_ZERO_PRICE_ATOL = 1e-9


def _price_of(value: dict) -> float | None:
    """The scalar price in a ``price_snapshot`` value, or ``None``.

    ccxt bars carry ``close``; CoinGecko snapshots carry ``price``. A
    successfully parsed non-finite value (``NaN``/``inf``) is KEPT and returned
    -- it must poison the spread dispersion / dislocation check into abstaining
    rather than be silently dropped, which is the exact failure mode AGENTS.md's
    float section names (every comparison against NaN is False, so a range check
    written as a comparison passes NaN straight through).
    """
    scalar = value.get("close")
    if scalar is None:
        scalar = value.get("price")
    if scalar is None:
        return None
    try:
        return float(scalar)
    except (TypeError, ValueError):
        return None


def basis_call(
    *,
    entry: float,
    target: float,
    spread_vol: float,
    widening_k: float = DEFAULT_WIDENING_K,
) -> tuple[str, float, float, float] | None:
    """A falsifiable basis-convergence call, or ``None`` when none is honest.

    Returns ``(direction, upper, lower, confidence)`` with barriers that
    genuinely straddle entry. ``entry`` is the anchored venue's current price;
    ``target`` is the other venue's current price (the convergence level);
    ``spread_vol`` is the standard deviation of the historical spread series.

    The **target** barrier is full convergence -- the other venue's price. The
    **invalidation** barrier is the spread widening ``widening_k`` spread-vols
    from entry -- the level where mean reversion stops being the base case.
    Confidence is the driftless first-passage probability of reaching the
    convergence target before the spread widens to the invalidation, mirroring
    ``trend.trend_call`` and ``carry.carry_call``.

    Direction is ``down`` when the anchor is rich (entry > target -- the anchor
    falls to converge) and ``up`` when the anchor is cheap (entry < target --
    the anchor rises to converge).

    ``None`` when: any input is non-finite, the spread dispersion is zero or
    near-zero (lockstep venues -- no honest widening level), the current
    dislocation is zero or near-zero (venues already agree -- nothing to
    converge), or the constructed barriers fail the straddle
    ``upper > entry > lower``.
    """
    if not all(math.isfinite(x) for x in (entry, target, spread_vol)):
        return None
    if spread_vol <= _ZERO_PRICE_ATOL:
        return None
    current_spread = entry - target
    if abs(current_spread) <= _ZERO_PRICE_ATOL:
        return None

    widening = widening_k * spread_vol
    if current_spread > 0.0:
        # Anchor rich -> short it -> it falls to converge.
        direction, upper, lower = "down", entry + widening, target
    else:
        # Anchor cheap -> long it -> it rises to converge.
        direction, upper, lower = "up", target, entry - widening

    if not (upper > entry > lower):
        return None
    confidence = _first_passage_confidence(direction, entry, upper, lower)
    return direction, upper, lower, confidence


async def _venue_price_series(
    pool, *, entity_id: UUID, audience: UUID | None, as_of: datetime, limit: int
) -> dict[str, list[tuple[datetime, float]]]:
    """The trailing ``limit`` daily prices per venue, oldest-first.

    Venues are identified by ``COALESCE(value->>'venue', source)``: ccxt bars
    carry a ``venue`` (the exchange name, per ``exchanges.py``); CoinGecko
    snapshots do not, so their source name stands in. A per-venue window
    (``ROW_NUMBER`` partitioned by venue) guarantees each venue contributes its
    own trailing ``limit`` prints even when one venue prints far more frequently
    than another -- a flat ``LIMIT`` would let the verbose venue starve the
    quiet one.

    Point-in-time: a price filed after ``as_of`` is invisible. A non-dict or
    price-less value is skipped. A non-finite price is KEPT (see ``_price_of``).
    """
    rows = await pool.fetch(
        f"""
        WITH visible AS (
        {visible_claims_cte("$3")}
        ),
        ranked AS (
            SELECT c.value, c.event_date,
                   COALESCE(c.value->>'venue', c.source) AS venue_key,
                   ROW_NUMBER() OVER (
                       PARTITION BY COALESCE(c.value->>'venue', c.source)
                       ORDER BY c.event_date DESC
                   ) AS rn
            FROM visible c
            WHERE c.entity_id = $1
              AND c.claim_type = 'price_snapshot'
              AND c.knowledge_date <= $2
        )
        SELECT value, event_date, venue_key
        FROM ranked
        WHERE rn <= $4
        """,
        entity_id,
        as_of,
        audience,
        limit,
    )
    by_venue: dict[str, list[tuple[datetime, float]]] = {}
    for r in rows:
        raw = r["value"]
        if isinstance(raw, (str, bytes)):
            raw = json.loads(raw)
        if not isinstance(raw, dict):
            continue
        price = _price_of(raw)
        if price is None:
            continue
        by_venue.setdefault(r["venue_key"], []).append((r["event_date"], price))
    for points in by_venue.values():
        points.reverse()  # oldest-first
    return by_venue


async def _latest_funding(
    pool, *, entity_id: UUID, audience: UUID | None, as_of: datetime
) -> dict | None:
    """The most recent ``funding_rate`` value visible to the audience as-of.

    Used only to identify which venue is the perpetual (the leveraged leg a
    basis trade anchors on). The rate itself is recorded verbatim as context;
    it never gates the call -- basis is a spread signal, and blending a funding
    stream into the barriers would duplicate ``carry.funding``'s edge under
    another name.
    """
    rows = await pool.fetch(
        f"""
        WITH visible AS (
        {visible_claims_cte("$3")}
        )
        SELECT c.value
        FROM visible c
        WHERE c.entity_id = $1
          AND c.claim_type = 'funding_rate'
          AND c.knowledge_date <= $2
        ORDER BY c.event_date DESC
        LIMIT 1
        """,
        entity_id,
        as_of,
        audience,
    )
    if not rows:
        return None
    raw = rows[0]["value"]
    if isinstance(raw, (str, bytes)):
        raw = json.loads(raw)
    return raw if isinstance(raw, dict) else None


def _pick_anchor(
    venues: list[str], funding: dict | None
) -> str:
    """The venue the convergence call is anchored on.

    A perpetual is the natural leveraged leg of a basis trade, so when the
    latest funding rate names a venue that is among the price venues the anchor
    is that perp. Otherwise the anchor is the deterministically-first venue by
    key, so the recorded direction is stable across replays. The choice is a
    reporting convention: the basis trade is symmetric, and either leg could
    express the convergence view.
    """
    if funding is not None:
        fvenue = funding.get("venue")
        if isinstance(fvenue, str) and fvenue in venues:
            return fvenue
    return min(venues)


async def produce_basis_prediction_from_coverage(
    pool,
    *,
    entity_id: UUID,
    audience_user_id: UUID | None,
    horizon_ends_at: datetime,
    method: str = METHOD,
    as_of: datetime | None = None,
    created_at: datetime | None = None,
    window: int = DEFAULT_WINDOW,
    widening_k: float = DEFAULT_WIDENING_K,
) -> UUID | None:
    """Produce a cross-venue basis prediction from coverage.

    Reads the trailing ``window`` prices per venue visible to the audience
    as-of ``as_of`` (default now), anchors on the perpetual venue when funding
    coverage identifies one (else the deterministically-first venue), and
    records a directional call whose target is full convergence to the most
    dislocated other venue and whose invalidation is the spread widening
    ``widening_k`` of its own historical dispersion. Returns the new prediction
    id, or ``None`` when coverage is insufficient or the regime is not a
    tradeable basis: fewer than two venues with a current price (one venue is a
    price, not a basis), fewer than ``MIN_SPREAD_POINTS`` paired spread
    observations, a zero or non-finite spread dispersion, a zero or non-finite
    current dislocation, or a non-finite input price. Abstention is the honest
    outcome, never a manufactured call.
    """
    if as_of is None:
        as_of = datetime.now(UTC)

    series = await _venue_price_series(
        pool,
        entity_id=entity_id,
        audience=audience_user_id,
        as_of=as_of,
        limit=window,
    )
    latest = {v: pts[-1] for v, pts in series.items() if pts}
    if len(latest) < 2:
        return None

    funding = await _latest_funding(
        pool, entity_id=entity_id, audience=audience_user_id, as_of=as_of
    )
    anchor_venue = _pick_anchor(list(latest), funding)
    _anchor_date, anchor_price = latest[anchor_venue]
    if not math.isfinite(anchor_price):
        return None

    other_venue, (_other_date, other_price) = max(
        (
            (v, dp)
            for v, dp in latest.items()
            if v != anchor_venue
        ),
        key=lambda kv: abs(kv[1][1] - anchor_price),
    )
    if not math.isfinite(other_price):
        return None

    anchor_by_date = {d.date(): p for d, p in series[anchor_venue]}
    other_by_date = {d.date(): p for d, p in series[other_venue]}
    common = sorted(set(anchor_by_date) & set(other_by_date))
    spreads = [anchor_by_date[d] - other_by_date[d] for d in common]
    if len(spreads) < MIN_SPREAD_POINTS:
        return None
    spread_arr = np.array(spreads, dtype=float)
    if not np.all(np.isfinite(spread_arr)):
        return None
    spread_vol = float(np.std(spread_arr))

    call = basis_call(
        entry=anchor_price,
        target=other_price,
        spread_vol=spread_vol,
        widening_k=widening_k,
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
        entry_price=anchor_price,
        upper_barrier=upper,
        lower_barrier=lower,
        horizon_ends_at=horizon_ends_at,
        audience_user_id=audience_user_id,
        created_at=created_at,
        assumptions={
            "model": "basis_crossvenue",
            "window": window,
            "widening_k": widening_k,
            "anchor_venue": anchor_venue,
            "other_venue": other_venue,
            "anchor_price": anchor_price,
            "other_price": other_price,
            "current_spread": anchor_price - other_price,
            "spread_vol": spread_vol,
            "convergence_target": other_price,
            "latest_funding": funding,
            "confidence_model": "driftless_first_passage",
        },
    )
