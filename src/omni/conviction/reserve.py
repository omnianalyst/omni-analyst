"""Exchange-reserve producer: net labelled on-chain flow -> a directional call.

Coins moving ONTO exchanges are supply arriving at the venue where they can be
sold; coins moving OFF are supply leaving circulation. This producer measures
the NET exchange flow over a recent window relative to the same addresses' own
history, and asserts a directional call when the recent flow regime is
abnormally one-sided.

The signal requires labelled addresses (``ingest/labels.py``): an unlabelled
flow is not an exchange flow and is never counted as one. Flows are read
through the visibility rule (``coverage/visibility.py``), never by direct
``claim`` access -- the licence boundary. Only addresses confirmed as
exchange-category in the label store contribute; whale and unlabelled flows
are discarded, and an exchange-to-exchange transfer (both sides labelled) is
net-neutral and also skipped.

The z-score is the model's core statistic. The trailing ``window`` labelled
flows are split into baseline (older half) and signal (recent half). The
z-score compares the signal half's mean signed flow (+ for inflow, - for
outflow) to the baseline half's, normalized by the baseline's standard
deviation. It is the reserve analogue of ``trend.py``'s trend strength and
``carry.py``'s persistence: a measure of how far the current regime is from
its own history, never an absolute threshold.

**The barriers are model-grounded, not invented.**

- **Direction** is ``down`` when z > 0 (net inflow above baseline -- supply
  arriving, bearish), ``up`` when z < 0 (net outflow -- supply leaving,
  bullish).
- **The invalidation barrier** is the price move -- measured in units of
  realized volatility equal to the flow z-score -- beyond which the abnormal
  flow has been fully absorbed. A z-score of N means the net exchange flow is
  N standard deviations from its historical norm; the corresponding
  invalidation is N realized-volatility units from entry, because that is the
  price distance the model associates with an N-sigma flow surprise. Price
  reaching this level means the flow-implied pressure has been offset. This
  is the reserve analogue of ``carry.py``'s "the price move that erases
  expected carry" and ``trend.py``'s "the MA is the stop": a level the model
  itself identifies, not a round number or a fixed percentage.
- **The target barrier** is a volatility-scaled move in the trade's direction,
  exactly as ``trend.py`` and ``carry.py`` size their target with realized vol.

Abstention is honest, not failure: fewer than ``window`` labelled exchange
flows (thin coverage -- "not a call from seven addresses"), a flat baseline
(zero std -- cannot normalize), a z-score at or near zero (no abnormal flow --
no signal), a non-finite flow amount or z-score, zero/non-finite realized vol,
no price to anchor entry, or barriers that fail to straddle entry -- each
returns ``None`` rather than a manufactured call.
"""

from __future__ import annotations

import json
import math
from datetime import UTC, datetime, timedelta
from uuid import UUID

import numpy as np

from omni.conviction.ledger import record_prediction
from omni.conviction.predict import _first_passage_confidence
from omni.conviction.trend import _realized_vol
from omni.coverage.visibility import visible_claims_cte
from omni.ingest.labels import CATEGORY_EXCHANGE, lookup_many

_CAPABILITY = "flow.exchange_reserve"
METHOD = "flow.exchange_reserve"

DEFAULT_LOOKBACK_DAYS = 60
DEFAULT_WINDOW = 40
DEFAULT_TARGET_K = 2.0
DEFAULT_PRICE_WINDOW = 20

# How many raw onchain_flow rows to pull before the Python-side label filter.
# Generous: the label filter discards whales and unlabelled flows, so we
# over-read to ensure enough labelled flows survive.
_MAX_FLOWS_READ = 500

# One idiom (abs(x) <= atol, never ==), three scale-consistent tolerances.
# Vol is a price-space quantity (stdev x close, matching trend.py); the flow
# std is an ETH-space quantity (the spread of individual flow amounts); z is
# dimensionless. A single number cannot serve all three guards.
_ZERO_VOL_ATOL = 1e-9
_ZERO_FLOW_STD_ATOL = 1e-9
_ZERO_Z_ATOL = 1e-12


async def _flow_window(
    pool, *, entity_id: UUID, audience: UUID | None, as_of: datetime,
    lookback_days: int,
) -> list:
    """The trailing on-chain flow claims visible to the audience, newest-first.

    Point-in-time: a flow filed after ``as_of`` is invisible. Ordered DESC so
    the LIMIT keeps the most recent rows; the caller reverses to oldest-first.
    """
    cutoff = as_of - timedelta(days=lookback_days)
    return await pool.fetch(
        f"""
        WITH visible AS (
        {visible_claims_cte("$3")}
        )
        SELECT c.value, c.event_date
        FROM visible c
        WHERE c.entity_id = $1
          AND c.claim_type = 'onchain_flow'
          AND c.knowledge_date <= $2
          AND c.event_date >= $4
        ORDER BY c.event_date DESC
        LIMIT $5
        """,
        entity_id,
        as_of,
        audience,
        cutoff,
        _MAX_FLOWS_READ,
    )


async def _labelled_exchange_flows(
    pool, rows: list, *, chain: str = "eth",
) -> list[tuple[datetime, float]]:
    """Filter flow rows to exchange-labelled addresses.

    Returns ``(event_date, signed_amount)`` oldest-first, where
    ``signed_amount`` is ``+amount`` for inflow (coins arriving at an exchange)
    and ``-amount`` for outflow (coins leaving an exchange). A flow where
    neither address is exchange-labelled is discarded (an unlabelled flow is
    not an exchange flow); one where both are labelled is net-neutral and
    skipped.

    A non-finite amount (``NaN``/``inf``) from the value is KEPT -- it must
    poison the finiteness check into abstaining rather than be silently
    dropped, the exact failure mode AGENTS.md's float section names.
    """
    parsed: list[tuple[datetime, float, str, str]] = []
    addresses: set[str] = set()
    for r in rows:
        raw = r["value"]
        if isinstance(raw, (str, bytes)):
            raw = json.loads(raw)
        if not isinstance(raw, dict):
            continue
        amount = raw.get("amount_eth")
        from_addr = raw.get("from")
        to_addr = raw.get("to")
        if amount is None or from_addr is None or to_addr is None:
            continue
        try:
            amount = float(amount)
        except (TypeError, ValueError):
            continue
        parsed.append((r["event_date"], amount, from_addr, to_addr))
        addresses.add(from_addr)
        addresses.add(to_addr)

    if not parsed:
        return []

    labels = await lookup_many(pool, chain, addresses)
    exchange_addrs = {
        a for a, lbl in labels.items() if lbl.category == CATEGORY_EXCHANGE
    }
    if not exchange_addrs:
        return []

    result: list[tuple[datetime, float]] = []
    for event_date, amount, from_addr, to_addr in parsed:
        from_exchange = from_addr in exchange_addrs
        to_exchange = to_addr in exchange_addrs
        if from_exchange and to_exchange:
            continue
        if to_exchange:
            result.append((event_date, amount))
        elif from_exchange:
            result.append((event_date, -amount))
    result.reverse()  # oldest-first (rows were DESC)
    return result


async def _price_window(
    pool, *, entity_id: UUID, audience: UUID | None, as_of: datetime, limit: int
) -> list[float]:
    """The trailing daily closes knowable as-of ``as_of``, oldest-first.

    Mirrors ``trend._price_window`` / ``carry._price_window``; CoinGecko
    snapshots carry ``price``, Polygon bars carry ``close``.
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


def reserve_call(
    *,
    entry: float,
    vol: float,
    z: float,
    target_k: float = DEFAULT_TARGET_K,
) -> tuple[str, float, float, float] | None:
    """A falsifiable exchange-reserve call, or ``None`` when none is honest.

    Returns ``(direction, upper, lower, confidence)`` with barriers that
    genuinely straddle entry. ``direction`` is ``down`` when z > 0 (net inflow
    -- supply arriving, bearish), ``up`` when z < 0 (net outflow -- supply
    leaving, bullish).

    The **invalidation** barrier is |z| realized-volatility units from entry
    on the against-side: for a down call that is a rise to
    ``entry + |z| * vol``; for an up call a fall to ``entry - |z| * vol``.
    The **target** is a vol-scaled move in the trade's direction. Confidence
    is the driftless first-passage probability of hitting the target before
    the flow signal is invalidated, mirroring ``trend.trend_call`` and
    ``carry.carry_call``.

    ``None`` when: realized vol is non-positive/non-finite, the z-score is
    non-finite or ~zero (no abnormal flow), or the constructed barriers fail
    the straddle ``upper > entry > lower``.
    """
    if not math.isfinite(vol) or vol <= _ZERO_VOL_ATOL:
        return None
    if not math.isfinite(z) or abs(z) <= _ZERO_Z_ATOL:
        return None

    z_abs = abs(z)
    if z > 0.0:
        direction = "down"
        upper = entry + z_abs * vol
        lower = entry - target_k * vol
    else:
        direction = "up"
        upper = entry + target_k * vol
        lower = entry - z_abs * vol

    if not (upper > entry > lower):
        return None
    confidence = _first_passage_confidence(direction, entry, upper, lower)
    return direction, upper, lower, confidence


async def produce_reserve_prediction_from_coverage(
    pool,
    *,
    entity_id: UUID,
    audience_user_id: UUID | None,
    horizon_ends_at: datetime,
    method: str = METHOD,
    as_of: datetime | None = None,
    created_at: datetime | None = None,
    lookback_days: int = DEFAULT_LOOKBACK_DAYS,
    window: int = DEFAULT_WINDOW,
    target_k: float = DEFAULT_TARGET_K,
    price_window: int = DEFAULT_PRICE_WINDOW,
) -> UUID | None:
    """Produce an exchange-reserve directional prediction from coverage.

    Reads the trailing ``window`` exchange-labelled flow claims visible to the
    audience as-of ``as_of`` (default now), splits them into baseline (older
    half) and signal (recent half), computes a z-score of the signal regime
    against the baseline, and records a directional call whose invalidation is
    the z-sigma flow-absorption level and whose target is vol-scaled. Returns
    the new prediction id, or ``None`` when coverage is insufficient or the
    regime is not abnormal: fewer than ``window`` labelled exchange flows, a
    flat baseline, a z-score at or near zero, a non-finite flow or z, zero or
    non-finite vol, no price to anchor entry, or barriers that fail to
    straddle. Abstention is the honest outcome, never a manufactured call.
    """
    if as_of is None:
        as_of = datetime.now(UTC)

    rows = await _flow_window(
        pool,
        entity_id=entity_id,
        audience=audience_user_id,
        as_of=as_of,
        lookback_days=lookback_days,
    )
    signed_flows = await _labelled_exchange_flows(pool, rows)
    if len(signed_flows) < window:
        return None

    trailing = signed_flows[-window:]
    half = window // 2
    baseline = np.array([a for _, a in trailing[:half]], dtype=float)
    signal = np.array([a for _, a in trailing[half:]], dtype=float)

    if not np.all(np.isfinite(baseline)):
        return None
    if not np.all(np.isfinite(signal)):
        return None

    baseline_std = float(np.std(baseline))
    if baseline_std <= _ZERO_FLOW_STD_ATOL:
        return None

    baseline_mean = float(np.mean(baseline))
    signal_mean = float(np.mean(signal))
    z = (signal_mean - baseline_mean) / baseline_std
    if not math.isfinite(z) or abs(z) <= _ZERO_Z_ATOL:
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

    call = reserve_call(entry=entry, vol=vol, z=z, target_k=target_k)
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
            "model": "exchange_reserve",
            "window": window,
            "target_k": target_k,
            "z_score": z,
            "baseline_mean": baseline_mean,
            "baseline_std": baseline_std,
            "signal_mean": signal_mean,
            "realized_vol": vol,
            "entry": entry,
            "confidence_model": "driftless_first_passage",
        },
    )
