"""The one place analysis touches capital: a prediction becomes a sized intent.

A `prediction` row already is a trade specification -- `direction`,
`confidence`, `entry_price`, `upper_barrier`, `lower_barrier` and
`horizon_ends_at` are a side, a target, a stop and an expiry. What it is not is
a *position*: nothing in it says how much, and nothing in it says whether the
method that produced it has earned the right to hold capital at all. This
module supplies both answers from elsewhere -- eligibility from `policy.py`,
quantity from `portfolio/sizing.py` -- and refuses, by name, at every point
where the answer is no.

**The barriers swap for a short, and that swap is the whole module.** For a buy
the lower barrier is the stop and the upper is the target. For a sell they
exchange places: price rising against a short is the loss, so `upper_barrier`
is the stop and `lower_barrier` is the take profit. Assigning `lower_barrier`
to `stop_price` for a short out of habit produces a stop below entry -- a take
profit wearing a stop's name, which never stops the loss it was written to
stop, and which sizes the position off a risk leg that cannot be lost. The
schema's `prediction_barriers_straddle_entry` check cannot catch it because
both orderings straddle. `TradeIntent.__post_init__` can and does, which is why
the intent is constructed with its barriers rather than assigned them
afterwards.

**Nothing here reads the database.** Eligibility, portfolio state and limits
arrive as arguments because a bridge that fetched its own policy could be
tested only against a live ledger, and because the one-way rule is easier to
see when the module has no query in it at all. `trading/` may import
`conviction`, `portfolio` and `venue`; nothing on the analysis side may import
back. `tests/test_trading_isolation.py` enforces that by AST scan.

**Absence is refused, never filled in.** An uncalibrated method has no hit rate
and does not get a substituted one -- `sizing.py` raises on `None` and this
module refuses before it gets there, so the refusal is named rather than an
exception from two layers down. A missing portfolio state or limit set is not a
refusal at all but a caller error, and raises: a size computed against an
assumed NAV, or clamped by an assumed cap, is a fabricated number wearing a
real one's units.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from enum import Enum
from typing import Any

from omni.portfolio.risk import PortfolioSnapshot, RiskLimits, RiskVerdict, check
from omni.portfolio.sizing import DEFAULT_KELLY_FRACTION, size
from omni.trading.policy import Eligibility
from omni.venue.protocol import Capabilities, MarketType, Side, TradeIntent

ZERO = Decimal(0)


class BridgeRefusal(str, Enum):
    """Why a prediction did not become an intent. Each is a normal outcome."""

    NEUTRAL_DIRECTION = "no_directional_trade_exists_for_a_neutral_prediction"
    METHOD_INELIGIBLE = "the_method_may_not_hold_capital"
    UNCALIBRATED_HIT_RATE = "no_calibrated_hit_rate_to_size_against"
    SIZED_TO_ZERO = "the_edge_sizes_to_nothing"
    RISK_REFUSED = "the_risk_engine_refused_the_intent"
    VENUE_LACKS_CAPABILITY = "the_venue_cannot_take_this_side"
    NO_SYMBOL = "the_entity_has_no_tradeable_symbol_at_this_venue"


@dataclass(frozen=True)
class BridgeResult:
    """An intent, or a named reason there is none. Never both, never neither.

    The two incoherent states are refused at construction rather than left for
    a caller to interpret. A result carrying an intent *and* a refusal would be
    read as permission by `if result.intent:` and as a refusal by
    `if result.refusal:`, and both readings appear in ordinary code; a result
    carrying neither is a silent no-op that no operator can act on.
    """

    intent: TradeIntent | None
    refusal: BridgeRefusal | None
    detail: str
    eligibility: Eligibility | None = None
    risk: RiskVerdict | None = None

    def __post_init__(self) -> None:
        if self.intent is not None and self.refusal is not None:
            raise ValueError(
                f"an intent and a refusal cannot both hold: refused "
                f"{self.refusal.value} while carrying an intent for "
                f"{self.intent.symbol}"
            )
        if self.intent is None and self.refusal is None:
            raise ValueError(
                "a result with no intent must name the refusal that produced it"
            )

    def __bool__(self) -> bool:
        return self.intent is not None


def _barriers(side: Side, *, upper: Decimal, lower: Decimal) -> tuple[Decimal, Decimal]:
    """(stop, take_profit) for a side -- and they swap between the two.

    A long is stopped out below entry and takes profit above it. A short is the
    mirror: the upper barrier is where the trade is wrong and the lower is
    where it is right. This function exists as its own name so the swap is a
    thing that can be pointed at, tested directly, and removed only
    deliberately.
    """
    if side is Side.BUY:
        return lower, upper
    return upper, lower


def _held_quantity(
    state: PortfolioSnapshot | None, venue_name: str, symbol: str
) -> Decimal:
    """Net quantity held in this symbol at this venue, zero when unknown.

    Scoped to the venue because a long held elsewhere cannot be sold here: the
    sell would open a short at this venue regardless of what the book looks
    like in aggregate.
    """
    if state is None:
        return ZERO
    return sum(
        (
            position.quantity
            for position in state.positions
            if position.venue == venue_name and position.symbol == symbol
        ),
        ZERO,
    )


async def prediction_to_intent(
    prediction: Mapping[str, Any],
    *,
    eligibility: Eligibility,
    state: PortfolioSnapshot | None,
    limits: RiskLimits | None,
    venue_name: str,
    capabilities: Capabilities,
    symbol: str | None,
    market_type: MarketType,
    kelly_cap: Decimal = DEFAULT_KELLY_FRACTION,
    volatility: Decimal | None = None,
    vol_target: Decimal | None = None,
    correlations: Mapping[tuple[str, str], Decimal] | None = None,
    data_as_of: datetime | None = None,
    now: datetime | None = None,
    realised_pnl_today: Decimal | None = None,
    peak_nav: Decimal | None = None,
    halted: bool = False,
    reconciled: bool | None = None,
) -> BridgeResult:
    """Turn one prediction into a sized, bounded intent -- or refuse by name.

    Refusals are ordered cheapest and most fundamental first, so the reason
    reported is the one an operator would have to fix first: an entity with no
    symbol cannot trade at this venue whatever its direction; a neutral
    prediction has no side to take whatever the method's record; an ineligible
    method may not hold capital whatever the size would have been.

    `async` because the trading loop awaits every step of the path. Nothing
    here performs I/O, and nothing here may: eligibility, state and limits are
    supplied by the caller precisely so a capital decision can be reproduced
    from its inputs.

    `max_position_pct_nav` is read from `limits` rather than taken as its own
    argument. The cap that clamps the size and the cap the risk engine enforces
    are the same number, and two arguments for it is two places for it to
    drift.
    """

    def refuse(
        reason: BridgeRefusal, detail: str, *, verdict: RiskVerdict | None = None
    ) -> BridgeResult:
        return BridgeResult(
            intent=None,
            refusal=reason,
            detail=detail,
            eligibility=eligibility,
            risk=verdict,
        )

    if symbol is None or not symbol.strip():
        return refuse(
            BridgeRefusal.NO_SYMBOL,
            f"no symbol resolves this prediction to an instrument at "
            f"{venue_name}; there is nothing to send an order for",
        )

    direction = prediction["direction"]
    if direction == "neutral":
        return refuse(
            BridgeRefusal.NEUTRAL_DIRECTION,
            "a neutral prediction claims the price stays inside its barriers; "
            "there is no directional trade in that",
        )
    if direction == "up":
        side = Side.BUY
    elif direction == "down":
        side = Side.SELL
    else:
        raise ValueError(
            f"unknown prediction direction {direction!r}; the ledger records "
            f"up, down or neutral and a fourth value has no side to map to"
        )

    if not eligibility.eligible:
        reason = eligibility.reason.value if eligibility.reason else "no reason recorded"
        return refuse(
            BridgeRefusal.METHOD_INELIGIBLE,
            f"{eligibility.method}/{eligibility.entity_kind} may not hold "
            f"capital in phase {eligibility.phase.value}: {reason} "
            f"({eligibility.detail})",
        )

    if eligibility.hit_rate is None:
        return refuse(
            BridgeRefusal.UNCALIBRATED_HIT_RATE,
            f"{eligibility.method}/{eligibility.entity_kind} has no calibrated "
            f"hit rate over {eligibility.resolved_n} resolved predictions; a "
            f"substituted probability would size a position from nothing",
        )

    if side is Side.SELL and not capabilities.shorting:
        held = _held_quantity(state, venue_name, symbol)
        if held <= ZERO:
            return refuse(
                BridgeRefusal.VENUE_LACKS_CAPABILITY,
                f"{venue_name} cannot short and holds {held} {symbol}, so a "
                f"sell there is an order it cannot take",
            )

    if state is None:
        raise ValueError(
            "portfolio state is required to size against; an assumed NAV "
            "produces a size attributable to no portfolio"
        )
    if limits is None:
        raise ValueError(
            "risk limits are required: the position cap that clamps the size "
            "comes from them, and an assumed cap is a disabled one"
        )

    entry = prediction["entry_price"]
    stop, take_profit = _barriers(
        side, upper=prediction["upper_barrier"], lower=prediction["lower_barrier"]
    )

    quantity = size(
        nav=state.nav,
        hit_rate=eligibility.hit_rate,
        entry=entry,
        stop=stop,
        target=take_profit,
        kelly_cap=kelly_cap,
        max_position_pct_nav=limits.max_position_pct_nav,
        volatility=volatility,
        vol_target=vol_target,
    )
    if quantity <= ZERO:
        return refuse(
            BridgeRefusal.SIZED_TO_ZERO,
            f"a {eligibility.hit_rate:.2f} hit rate against the barriers "
            f"{stop}/{take_profit} around {entry} sizes to {quantity}; the "
            f"edge does not pay for the risk",
        )

    prediction_id = prediction.get("id")
    intent = TradeIntent(
        venue=venue_name,
        symbol=symbol,
        side=side,
        market_type=market_type,
        quantity=quantity,
        reference_price=entry,
        stop_price=stop,
        take_profit_price=take_profit,
        expires_at=prediction["horizon_ends_at"],
        provenance={
            "prediction_id": str(prediction_id) if prediction_id is not None else None,
            "method": eligibility.method,
            "direction": direction,
            "confidence": prediction.get("confidence"),
            "hit_rate": eligibility.hit_rate,
            "measured_n": eligibility.measured_n,
            "phase": eligibility.phase.value,
        },
    )

    verdict = check(
        intent,
        state,
        limits,
        correlations=correlations,
        data_as_of=data_as_of,
        now=now,
        realised_pnl_today=realised_pnl_today,
        peak_nav=peak_nav,
        halted=halted,
        reconciled=reconciled,
    )
    if not verdict:
        return refuse(
            BridgeRefusal.RISK_REFUSED,
            f"risk refused the intent: {verdict.detail}",
            verdict=verdict,
        )

    return BridgeResult(
        intent=intent,
        refusal=None,
        detail=(
            f"{eligibility.method} {direction} on {symbol}: {quantity} at "
            f"{entry}, stop {stop}, target {take_profit}, expiring "
            f"{intent.expires_at}"
        ),
        eligibility=eligibility,
        risk=verdict,
    )
