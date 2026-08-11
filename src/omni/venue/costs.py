"""What a trade costs, so a signal can be rejected before it loses money slowly.

The failure this module exists to prevent is not dramatic. It is a strategy
with a genuine edge, executed somewhere the edge does not survive the round
trip, losing money for months while every backtest keeps saying it should work.
A 73%-hit-rate signal targeting +3% against a -2% stop earns 165bps gross; it
clears comfortably at 20bps of friction and is worthless at 150bps. Nothing
about the signal changed. Only the venue did.

So cost is computed per intent per venue, and the router picks the venue where
the edge survives -- or refuses the trade at every venue, which is a correct
outcome and not a failure.

**Funding is signed, and that is load-bearing.** Every other component is a
cost: fees, spread, gas and borrow are paid, never received. Funding is a
transfer between longs and shorts, so it is income exactly as often as it is
expense -- and the funding-carry producer's entire thesis is being on the
receiving side. A model that clamps funding to a positive cost would price the
carry strategy as its own opposite, and the strategy would look unprofitable
precisely when it works. `CostBreakdown` therefore refuses a negative fee and
permits a negative funding, and the sign convention is asserted in the tests
rather than left to the reader.

**Convention:** a positive funding rate means longs pay shorts. This is the
convention Binance, Bybit and OKX publish, and `ingest/derivatives.py` asserts
it against a recorded fixture rather than assuming it.

Everything is basis points of notional unless the name says otherwise, because
that is the unit an expectancy is stated in and mixing the two is how a 1%
figure gets compared against a 100bps figure and silently wins.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from omni.venue.protocol import Capabilities, MarketType, Side, TradeIntent

BPS = Decimal(10_000)


@dataclass(frozen=True)
class CostBreakdown:
    """Cost of a trade in basis points of notional, by component.

    Kept as components rather than a single number because the router's
    rejection message has to say *why* a venue was too expensive -- "gas is
    160bps on a 5,000 notional" is actionable, "too expensive" is not.
    """

    fee_bps: Decimal = Decimal(0)
    spread_bps: Decimal = Decimal(0)
    gas_bps: Decimal = Decimal(0)
    borrow_bps: Decimal = Decimal(0)
    funding_bps: Decimal = Decimal(0)

    def __post_init__(self) -> None:
        for name in ("fee_bps", "spread_bps", "gas_bps", "borrow_bps"):
            if getattr(self, name) < 0:
                raise ValueError(
                    f"{name} must not be negative ({getattr(self, name)}); "
                    f"only funding_bps may be a credit"
                )

    @property
    def total_bps(self) -> Decimal:
        return (
            self.fee_bps
            + self.spread_bps
            + self.gas_bps
            + self.borrow_bps
            + self.funding_bps
        )

    def __add__(self, other: CostBreakdown) -> CostBreakdown:
        return CostBreakdown(
            fee_bps=self.fee_bps + other.fee_bps,
            spread_bps=self.spread_bps + other.spread_bps,
            gas_bps=self.gas_bps + other.gas_bps,
            borrow_bps=self.borrow_bps + other.borrow_bps,
            funding_bps=self.funding_bps + other.funding_bps,
        )

    def as_fraction(self) -> Decimal:
        """Total as a fraction of notional, for arithmetic against a return."""
        return self.total_bps / BPS


def gross_expectancy_bps(
    *,
    hit_rate: Decimal,
    target_bps: Decimal,
    stop_bps: Decimal,
) -> Decimal:
    """Expected return per trade before costs, in bps.

    `hit_rate` must be a *calibrated* rate from resolved predictions. There is
    deliberately no default: a caller without a calibrated rate has no
    expectancy to compute, and substituting 0.5 would produce a confident
    number describing nothing.

    `stop_bps` is the loss taken when the stop is hit, stated positive.
    """
    if not Decimal(0) <= hit_rate <= Decimal(1):
        raise ValueError(f"hit_rate out of range: {hit_rate}")
    if target_bps <= 0:
        raise ValueError(f"target_bps must be positive, got {target_bps}")
    if stop_bps <= 0:
        raise ValueError(
            f"stop_bps must be positive and is stated as a magnitude, "
            f"got {stop_bps}"
        )
    return hit_rate * target_bps - (Decimal(1) - hit_rate) * stop_bps


def entry_cost(
    intent: TradeIntent,
    capabilities: Capabilities,
    *,
    spread_bps: Decimal = Decimal(0),
    gas_quote: Decimal = Decimal(0),
    is_maker: bool = False,
) -> CostBreakdown:
    """Cost of getting into the position, one leg.

    `gas_quote` is an absolute amount in the quote currency, not bps, because
    that is how gas actually behaves: a fixed cost per transaction whose weight
    depends entirely on trade size. Converting it here is what makes a $40
    transaction 160bps on a $5,000 trade and 1.6bps on a $500,000 one -- the
    single most important fact about on-chain execution and the reason small
    on-chain trades cannot carry a thin edge.

    A taker crosses the spread; a maker does not. The half-spread is charged
    because the reference price is the mid.

    For venues that charge different fees for spot and perpetuals
    (Hyperliquid: 7 bps spot taker vs 4.5 bps perp taker), the perp fee is
    used when the intent targets a perpetual market. Without this, a carry
    pair's two perp legs are overcharged at the spot rate, which overstates
    the round trip by 5 bps (28 modelled vs 23 actual) and refuses marginal
    trades the edge survives.
    """
    if spread_bps < 0:
        raise ValueError(f"spread_bps must not be negative, got {spread_bps}")
    if gas_quote < 0:
        raise ValueError(f"gas_quote must not be negative, got {gas_quote}")

    notional = intent.notional
    if notional <= 0:
        raise ValueError(f"cannot cost a non-positive notional: {notional}")

    if intent.market_type is MarketType.PERPETUAL:
        if is_maker and capabilities.perp_maker_fee_bps is not None:
            fee_bps = capabilities.perp_maker_fee_bps
        elif not is_maker and capabilities.perp_taker_fee_bps is not None:
            fee_bps = capabilities.perp_taker_fee_bps
        else:
            fee_bps = capabilities.maker_fee_bps if is_maker else capabilities.taker_fee_bps
    else:
        fee_bps = capabilities.maker_fee_bps if is_maker else capabilities.taker_fee_bps
    crossed_spread_bps = Decimal(0) if is_maker else spread_bps / Decimal(2)

    return CostBreakdown(
        fee_bps=fee_bps,
        spread_bps=crossed_spread_bps,
        gas_bps=gas_quote / notional * BPS,
    )


def carry_cost(
    intent: TradeIntent,
    *,
    funding_rate: Decimal = Decimal(0),
    funding_periods: int = 0,
    borrow_rate_bps_per_period: Decimal = Decimal(0),
    borrow_periods: int = 0,
) -> CostBreakdown:
    """Cost of *holding* the position, signed for funding.

    A positive `funding_rate` means longs pay shorts. So a long accrues a cost
    and a short accrues a credit, and the credit is what the carry producer is
    selling. Getting this sign backwards prices the strategy as its own
    opposite; `test_costs.py` asserts both directions.

    Borrow applies to a short that must locate the asset -- margin shorts, not
    perpetual shorts, which are synthetic and pay funding instead. Charging
    both to a perp short would double-count the cost of being short.
    """
    if funding_periods < 0 or borrow_periods < 0:
        raise ValueError("periods must not be negative")
    if borrow_rate_bps_per_period < 0:
        raise ValueError(
            f"borrow_rate must not be negative, got {borrow_rate_bps_per_period}"
        )

    direction = Decimal(1) if intent.side is Side.BUY else Decimal(-1)
    funding_bps = funding_rate * Decimal(funding_periods) * BPS * direction

    borrow_bps = Decimal(0)
    if intent.side is Side.SELL and intent.market_type is MarketType.MARGIN:
        borrow_bps = borrow_rate_bps_per_period * Decimal(borrow_periods)

    return CostBreakdown(funding_bps=funding_bps, borrow_bps=borrow_bps)


def round_trip_cost(
    intent: TradeIntent,
    capabilities: Capabilities,
    *,
    spread_bps: Decimal = Decimal(0),
    gas_quote: Decimal = Decimal(0),
    is_maker: bool = False,
    exit_is_maker: bool | None = None,
    funding_rate: Decimal = Decimal(0),
    funding_periods: int = 0,
    borrow_rate_bps_per_period: Decimal = Decimal(0),
    borrow_periods: int = 0,
) -> CostBreakdown:
    """Entry plus exit plus carry -- what the edge is actually measured against.

    Both legs are charged. Costing only the entry is the most common way a
    backtest reports an edge that does not exist, because it halves the one
    number the edge is competing with.

    `exit_is_maker` defaults to `is_maker`. It is separable because a strategy
    frequently enters passively and exits at market when a stop is hit -- and a
    stop exit is a taker exit by definition, so a model assuming a maker exit
    understates the cost of exactly the trades that lose.
    """
    exit_maker = is_maker if exit_is_maker is None else exit_is_maker

    entry = entry_cost(
        intent,
        capabilities,
        spread_bps=spread_bps,
        gas_quote=gas_quote,
        is_maker=is_maker,
    )
    exit_leg = entry_cost(
        intent,
        capabilities,
        spread_bps=spread_bps,
        gas_quote=gas_quote,
        is_maker=exit_maker,
    )
    carry = carry_cost(
        intent,
        funding_rate=funding_rate,
        funding_periods=funding_periods,
        borrow_rate_bps_per_period=borrow_rate_bps_per_period,
        borrow_periods=borrow_periods,
    )
    return entry + exit_leg + carry


@dataclass(frozen=True)
class Viability:
    """Whether an edge survives a venue, with the arithmetic that decided it."""

    gross_bps: Decimal
    cost: CostBreakdown
    survives: bool

    @property
    def net_bps(self) -> Decimal:
        return self.gross_bps - self.cost.total_bps

    def explain(self) -> str:
        return (
            f"gross {self.gross_bps:.1f}bps - cost {self.cost.total_bps:.1f}bps "
            f"(fee {self.cost.fee_bps:.1f}, spread {self.cost.spread_bps:.1f}, "
            f"gas {self.cost.gas_bps:.1f}, borrow {self.cost.borrow_bps:.1f}, "
            f"funding {self.cost.funding_bps:+.1f}) = net {self.net_bps:.1f}bps"
        )


def survives_costs(
    *,
    gross_bps: Decimal,
    cost: CostBreakdown,
    margin_bps: Decimal = Decimal(0),
) -> Viability:
    """Does the edge clear the friction, with room to spare.

    `margin_bps` is a required buffer above break-even. Zero means "any
    positive net is enough", which is rarely true in practice -- a modelled
    cost is an estimate and a strategy sitting at +0.05bps net is inside the
    error bar of its own cost model. The router sets this; the default stays
    zero so the arithmetic here is the plain comparison and the policy lives
    with the caller that owns it.
    """
    if margin_bps < 0:
        raise ValueError(f"margin_bps must not be negative, got {margin_bps}")
    net = gross_bps - cost.total_bps
    return Viability(gross_bps=gross_bps, cost=cost, survives=net > margin_bps)
