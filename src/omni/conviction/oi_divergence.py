"""Open-interest divergence producer: OI vs price direction -> a reversion call.

OI divergence is the classic crowded-side signal. When open interest is built up
while price goes the other way, the new positions are being opened against a move
that is not following through -- one side is crowded, and a crowded side is
squeeze-prone. The signal is the DIVERGENCE between the two series' directions,
so both are required and agreement is explicitly not a signal:

- OI rising + price falling -> new shorts piling into a downtrend that stalls ->
  crowded short -> squeeze risk -> direction ``up`` (reversion).
- OI falling + price rising -> short covering lifting price with no new
  commitment -> once covering exhausts the rally fades -> direction ``down``
  (reversion).

In both cases the call is mean-reverting: it bets the recent price leg unwinds.
``open_interest`` claims (``contracts`` field, the base-asset position count --
``ingest/derivatives.py``) and ``price_snapshot`` claims flow independently; this
producer reads both through the audience-scoped visibility rule, the same shape
``carry.py`` and ``trend.py`` use.

The barriers are model-grounded, not invented (mirroring ``trend.py`` / ``carry.py``):

- **The invalidation barrier** is the level at which the divergent price leg has
  extended by its own magnitude AGAINST the reversion call. The model observes a
  price leg of size ``|entry - window_start|`` over the window; the call is dead
  when price travels that same distance past entry against the call, i.e. at
  ``2 * entry - window_start``. That is the price at which the divergence has
  doubled rather than reverted -- the honest "this squeeze is not happening"
  level, derived entirely from the leg the model itself measured. It is the
  divergence analogue of ``trend.py``'s "the MA is the invalidation" and
  ``carry.py``'s "the move that erases carry": a level the model identifies,
  never a round number or fixed percentage. WHAT IT MEANS: price has gone one
  full divergent leg further against the call.
- **The target barrier** is a volatility-scaled move in the call's direction
  (``entry +/- target_k * realized vol``), sized exactly as ``trend.py`` sizes
  its target.

Confidence is the driftless first-passage probability of hitting the target
before the invalidation, monotonic in the divergence-leg-to-vol ratio -- a call
on a large divergent leg reads more confident than one on a thin leg, the spread
the conviction gate's calibration needs.

Abstention is honest, not failure: fewer than ``window`` OI or price points, a
non-finite value in either series, either series flat within tolerance (no
directional move to diverge from), OI and price AGREEING (same sign -- the move
is confirmed, not divergent), zero/non-finite realized vol, or a constructed
barrier triple that fails the straddle -- each returns ``None`` rather than a
manufactured call.
"""

from __future__ import annotations

import json
import math
from datetime import UTC, datetime
from uuid import UUID

import numpy as np

from omni.conviction.ledger import record_prediction
from omni.conviction.predict import _first_passage_confidence
from omni.conviction.trend import _price_window, _realized_vol
from omni.coverage.visibility import visible_claims_cte

_CAPABILITY = "oi.divergence"
METHOD = "oi.divergence"

DEFAULT_WINDOW = 10
DEFAULT_TARGET_K = 2.0

# Two scale-consistent tolerances, one idiom each (mirroring carry.py). Vol is a
# price-space quantity (stdev x close) -- reused from trend.py so the whole
# codebase agrees on what counts as flat vol. The flat-move guard is relative:
# |net move| <= rtol * mean(|series|), same units as the series, so it means the
# same thing for OI (~1e4-1e5 contracts) and price (~1e0-1e5). 1e-9 is far below
# any real directional move and fires only on a true flat or float dust.
_ZERO_VOL_ATOL = 1e-9
_FLAT_RTOL = 1e-9


def _is_flat_move(series: np.ndarray) -> bool:
    """True when the series has no directional first-to-last move.

    Scale-consistent: compares the net move to the series' own mean absolute
    value (same units), not to a round number or a scale-squared variance. NaN
    and inf are refused explicitly -- every comparison against NaN is False, so a
    range check written as a comparison would pass NaN straight through and the
    caller would get a "non-flat" verdict computed from poison. Here a non-finite
    net or scale returns True (flat), so the producer abstains.
    """
    net = float(series[-1] - series[0])
    scale = float(np.mean(np.abs(series)))
    if not (math.isfinite(net) and math.isfinite(scale) and scale > 0.0):
        return True
    return abs(net) <= _FLAT_RTOL * scale


def oi_divergence_call(
    *,
    entry: float,
    window_start: float,
    vol: float,
    oi_move: float,
    price_move: float,
    target_k: float = DEFAULT_TARGET_K,
) -> tuple[str, float, float, float] | None:
    """A falsifiable OI-divergence reversion call, or ``None`` when none is honest.

    Returns ``(direction, upper, lower, confidence)`` with barriers that genuinely
    straddle entry. ``direction`` is ``up`` when price fell while OI rose (crowded
    short -> squeeze), ``down`` when price rose while OI fell (covering rally ->
    fade).

    The **invalidation** is ``2 * entry - window_start`` -- the price at which the
    divergent leg (``|entry - window_start|``) has extended by its own size
    against the call. The **target** is a vol-scaled move in the call's direction.
    Confidence is the driftless first-passage probability of hitting the target
    before the invalidation, mirroring ``trend.trend_call`` / ``carry.carry_call``.

    Assumes the caller has already refused NaN/inf and flat series (scale-aware
    checks need the full series, done in the producer). This function still guards
    the call-structure invariants from the passed scalars: non-finite inputs,
    zero/non-finite vol, OI and price AGREEING in sign (not a divergence), a zero
    move (flat), and a barrier triple that fails ``upper > entry > lower``.

    ``None`` when any of those fire -- abstention, never a manufactured call.
    """
    if not (
        math.isfinite(entry)
        and math.isfinite(window_start)
        and math.isfinite(oi_move)
        and math.isfinite(price_move)
    ):
        return None
    if not math.isfinite(vol) or vol <= _ZERO_VOL_ATOL:
        return None
    # Agreement is not divergence. A zero move is flat (the producer's scale-aware
    # guard should have caught it; defend here too -- comparing to 0.0 is exact).
    if oi_move == 0.0 or price_move == 0.0:
        return None
    if (oi_move > 0.0) == (price_move > 0.0):
        return None

    invalidation = 2.0 * entry - window_start
    if price_move < 0.0:
        # price fell, OI rose -> crowded short -> squeeze -> up
        direction = "up"
        upper = entry + target_k * vol
        lower = invalidation
    else:
        # price rose, OI fell -> covering rally fades -> down
        direction = "down"
        upper = invalidation
        lower = entry - target_k * vol

    if not (upper > entry > lower):
        return None
    confidence = _first_passage_confidence(direction, entry, upper, lower)
    return direction, upper, lower, confidence


async def _oi_window(
    pool, *, entity_id: UUID, audience: UUID | None, as_of: datetime, limit: int
) -> list[tuple[datetime, float]]:
    """The trailing ``limit`` OI samples visible to the audience as-of ``as_of``,
    oldest-first, as ``(event_date, contracts)``.

    Reads the ``contracts`` field (base-asset position count), NOT ``notional``:
    notional entangles OI with price, and an OI-vs-price divergence read on a
    price-contaminated OI series would compare price to price. Point-in-time: an
    OI sample filed after ``as_of`` is invisible. A parsed non-finite value
    (``NaN``/``inf`` from a malformed decimal) is KEPT -- it must poison the
    producer's finiteness check into abstaining rather than be silently dropped,
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
          AND c.claim_type = 'open_interest'
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
        contracts = raw.get("contracts")
        if contracts is None:
            continue
        try:
            value = float(contracts)
        except (TypeError, ValueError):
            continue
        points.append((r["event_date"], value))
    points.reverse()  # oldest-first
    return points


async def produce_oi_divergence_prediction_from_coverage(
    pool,
    *,
    entity_id: UUID,
    audience_user_id: UUID | None,
    horizon_ends_at: datetime,
    method: str = METHOD,
    as_of: datetime | None = None,
    created_at: datetime | None = None,
    window: int = DEFAULT_WINDOW,
    target_k: float = DEFAULT_TARGET_K,
) -> UUID | None:
    """Produce an OI-divergence reversion prediction from coverage.

    Reads the trailing ``window`` ``open_interest`` and ``price_snapshot`` claims
    visible to the audience as-of ``as_of`` (default now), detects a divergence
    between the two series' net directions, and records a reversion call whose
    invalidation is the doubled divergent leg and whose target is vol-scaled.
    Returns the new prediction id, or ``None`` when coverage is insufficient or
    the regime is not a divergence: fewer than ``window`` OI or price points, a
    non-finite value in either series, either series flat within tolerance, OI and
    price agreeing (same direction), zero/non-finite realized vol, or a barrier
    triple that fails the straddle. Abstention is the honest outcome, never a
    manufactured call.

    ``as_of`` / ``created_at`` are accepted because the scheduler dispatches every
    producer with ``as_of=now`` (``scheduler/worker.py``); they default to now for
    a live call and let a backtest replay fix the point-in-time read window and
    entry stamp.
    """
    if as_of is None:
        as_of = datetime.now(UTC)

    oi = await _oi_window(
        pool,
        entity_id=entity_id,
        audience=audience_user_id,
        as_of=as_of,
        limit=window,
    )
    if len(oi) < window:
        return None

    closes = await _price_window(
        pool,
        entity_id=entity_id,
        audience=audience_user_id,
        as_of=as_of,
        limit=window,
    )
    if len(closes) < window:
        return None

    oi_vals = np.array([p[1] for p in oi], dtype=float)
    price_vals = np.array(closes, dtype=float)
    if not np.all(np.isfinite(oi_vals)) or not np.all(np.isfinite(price_vals)):
        return None
    if _is_flat_move(oi_vals) or _is_flat_move(price_vals):
        return None

    entry = float(closes[-1])
    window_start = float(closes[0])
    vol = _realized_vol(closes)
    oi_move = float(oi_vals[-1] - oi_vals[0])
    price_move = entry - window_start

    call = oi_divergence_call(
        entry=entry,
        window_start=window_start,
        vol=vol,
        oi_move=oi_move,
        price_move=price_move,
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
            "model": "oi_divergence",
            "window": window,
            "target_k": target_k,
            "oi_move": oi_move,
            "price_move": price_move,
            "price_leg": abs(window_start - entry),
            "window_start_close": window_start,
            "invalidation_level": 2.0 * entry - window_start,
            "realized_vol": vol,
            "entry": entry,
            "confidence_model": "driftless_first_passage",
        },
    )
