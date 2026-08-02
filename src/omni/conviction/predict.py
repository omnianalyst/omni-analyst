"""The predict path: turn a directional analysis into a durable prediction.

The conviction apparatus's missing producer (D13 / HANDOFF §6.5): nothing in
``src/`` wrote a ``prediction`` row. ``dcf_directional`` makes the honest
directional call (base DCF target + stressed barrier on the invalidation side);
this persists it as a falsifiable prediction via ``record_prediction``.

Coverage assembly is deliberately NOT here. ``dcf_valuation`` takes a nested
``fundamentals`` dict (cash_flow / balance_sheet / income_statement) that
``ArgumentSpec`` cannot assemble from scalar claims, and EDGAR's
``DEFAULT_CONCEPTS`` does not yet collect what the DCF needs (operating cash
flow, capex, shares, debt; market_cap/beta are market data, not fundamentals).
So the caller supplies the fundamentals dict; the producer is the conviction
half of the wiring. Wiring claim-assembly is the follow-up (see COMPLETION_PLAN
§5.1.2) and is orthogonal to proving the gate closes.

Confidence is the **driftless first-passage hitting probability** -- the chance
the target barrier is hit before the invalidation barrier under a symmetric
random walk. It is a geometric, closed-form read of the barrier distances
(P(hit target) = (start - opposite) / (upper - lower)). It deliberately ignores
the DCF's directional drift, so it is a conservative lower bound on the model's
belief, not a calibrated probability. This is the honest non-arbitrary way to
populate the NOT NULL ``confidence`` column without inventing a number; a
drift-aware / volatility-based probability model is the named follow-up.
"""

from __future__ import annotations

import json
from datetime import datetime
from uuid import UUID

from omni.capabilities.fundamentals import dcf_directional
from omni.conviction.ledger import record_prediction
from omni.coverage.fundamentals import assemble_fundamentals
from omni.coverage.visibility import visible_claims_cte
from omni.ingest.protocol import Unavailable

_CAPABILITY = "fundamentals.dcf_valuation"


def _first_passage_confidence(
    direction: str, entry: float, upper: float, lower: float
) -> float:
    """P(this call's target barrier is hit before the other), driftless walk.

    Up-call target is the upper barrier; down-call target is the lower. The
    formula is the classic gambler's-ruin first-passage probability for a
    driftless symmetric process, which is a function of the barrier distances
    alone. Clamped to [0, 1] for the boundary cases (entry at a barrier).
    """
    spread = upper - lower
    if not (spread > 0):
        # A straddling triple always has positive spread; this guards a caller
        # that bypassed dcf_directional's straddle check. Refuse rather than
        # emit a probability from a non-positive denominator.
        raise Unavailable("non-positive barrier spread; no confidence read")
    if direction == "up":
        return max(0.0, min(1.0, (entry - lower) / spread))
    return max(0.0, min(1.0, (upper - entry) / spread))


async def produce_dcf_prediction(
    pool,
    *,
    entity_id: UUID,
    audience_user_id: UUID | None,
    fundamentals: dict,
    current_price: float,
    horizon_ends_at: datetime,
    input_claim_ids: tuple[str, ...] = (),
    created_at: datetime | None = None,
    growth_rate: float | None = None,
    terminal_growth_rate: float = 0.03,
    discount_rate: float | None = None,
    years: int = 5,
) -> UUID | None:
    """Run the directional DCF and record its prediction.

    Returns the new prediction id, or ``None`` when the analysis abstains (no
    honest directional call, no straddling stressed barrier, missing
    fundamentals). Abstention is the honest outcome, never a manufactured
    prediction.

    The prediction's audience is the caller's (``audience_user_id``): ``None``
    is a shared prediction that resolves on the shared network; a user id is a
    private prediction that resolves on that audience's visible prices. Either
    way (post-019) the outcome lands in the calibration bucket of the same
    audience.
    """
    try:
        call = await dcf_directional(
            fundamentals, current_price,
            growth_rate=growth_rate,
            terminal_growth_rate=terminal_growth_rate,
            discount_rate=discount_rate,
            years=years,
        )
    except Unavailable:
        return None

    confidence = _first_passage_confidence(
        call["direction"],
        call["entry_price"],
        call["upper_barrier"],
        call["lower_barrier"],
    )
    return await record_prediction(
        pool,
        entity_id=entity_id,
        capability=_CAPABILITY,
        direction=call["direction"],
        confidence=confidence,
        entry_price=call["entry_price"],
        upper_barrier=call["upper_barrier"],
        lower_barrier=call["lower_barrier"],
        horizon_ends_at=horizon_ends_at,
        input_claim_ids=input_claim_ids,
        assumptions={
            "model": "dcf",
            "fair_value_base": call["fair_value_base"],
            "fair_value_stressed": call["fair_value_stressed"],
            "scenario": call["scenario"],
            "base_assumptions": call["base_assumptions"],
            "scenario_assumptions": call["scenario_assumptions"],
            "confidence_model": "driftless_first_passage",
        },
        audience_user_id=audience_user_id,
        created_at=created_at,
    )


async def _latest_price(
    pool, *, entity_id: UUID, audience: UUID | None, as_of: datetime
) -> tuple[float, str] | None:
    """The most recent knowable ``price_snapshot`` value as of ``as_of``.

    Returns ``(price, claim_id)`` or ``None`` when no price is visible to the
    audience by ``as_of``. CoinGecko snapshots carry ``{"price": p}``; Polygon
    bars carry ``{"close": c, ...}``. Point-in-time: a price filed after
    ``as_of`` is invisible, so the entry is the price that actually existed.
    """
    rows = await pool.fetch(
        f"""
        WITH visible AS (
        {visible_claims_cte("$2")}
        )
        SELECT c.id, c.value, c.knowledge_date, c.event_date
        FROM visible c
        WHERE c.entity_id = $1
          AND c.claim_type = 'price_snapshot'
          AND c.knowledge_date <= $3
        ORDER BY c.knowledge_date DESC, c.event_date DESC
        LIMIT 1
        """,
        entity_id,
        audience,
        as_of,
    )
    if not rows:
        return None
    raw = rows[0]["value"]
    if isinstance(raw, (str, bytes)):
        raw = json.loads(raw)
    if not isinstance(raw, dict):
        return None
    scalar = raw.get("price")
    if scalar is None:
        scalar = raw.get("close")
    if scalar is None:
        return None
    try:
        return float(scalar), str(rows[0]["id"])
    except (TypeError, ValueError):
        return None


async def produce_dcf_prediction_from_coverage(
    pool,
    *,
    entity_id: UUID,
    audience_user_id: UUID | None,
    as_of: datetime,
    horizon_ends_at: datetime,
    created_at: datetime | None = None,
    growth_rate: float | None = None,
    terminal_growth_rate: float = 0.03,
    discount_rate: float | None = None,
    years: int = 5,
) -> UUID | None:
    """Produce a DCF prediction from coverage alone (the live entry point).

    Reads the latest price visible to the audience, assembles the fundamentals
    dict from EDGAR claims (point-in-time as of ``as_of``), and records the
    directional call. Returns the new prediction id, or ``None`` when coverage
    is insufficient -- no visible price, incomplete fundamentals, or the model
    abstains (no direction / no straddling barrier). Abstention is honest, never
    a manufactured prediction.

    This is the producer a scheduler loop calls per demanded entity. It produces
    nothing without a visible ``price_snapshot`` (which, byo-only for equities,
    means nothing until the audience supplies a key) -- the correct outcome for a
    BYOK deployment, not a failure.
    """
    priced = await _latest_price(
        pool, entity_id=entity_id, audience=audience_user_id, as_of=as_of
    )
    if priced is None:
        return None
    current_price, price_claim_id = priced

    try:
        fundamentals = await assemble_fundamentals(
            pool, entity_id=entity_id, as_of=as_of,
            current_price=current_price, audience=audience_user_id,
        )
    except Unavailable:
        return None

    return await produce_dcf_prediction(
        pool,
        entity_id=entity_id,
        audience_user_id=audience_user_id,
        fundamentals=fundamentals,
        current_price=current_price,
        horizon_ends_at=horizon_ends_at,
        created_at=created_at,
        input_claim_ids=(price_claim_id,),
        growth_rate=growth_rate,
        terminal_growth_rate=terminal_growth_rate,
        discount_rate=discount_rate,
        years=years,
    )
