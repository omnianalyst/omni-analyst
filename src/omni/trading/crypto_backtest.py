"""Backtesting a crypto strategy against the calendar and the costs it will meet.

`capabilities/backtest.py` is the equity backtester: daily bars, a session
close, a signal lagged one bar. Crypto breaks three of its assumptions, and
every one of them breaks in the direction that flatters the result.

**There is no session close.** No overnight gap, no weekend, no holiday. A
harness that measures a hold in business days silently discards roughly 28% of
the time a crypto position is actually exposed -- and, worse, discards exactly
the periods over which funding kept accruing. Hold time here is wall-clock, and
`test_crypto_backtest.py` pins a weekend hold against a mid-week hold of the
same duration to prove it.

**Funding accrues while a perpetual is held, and it is signed.** A positive
funding rate means longs pay shorts, so a short collecting positive funding
*earns* while holding. The funding-carry producer's entire thesis is being on
the receiving side. A harness that clamped funding to a cost would price that
strategy as its own opposite, and it would read as unprofitable precisely when
it works. `funding_collected_bps` is therefore reported separately and
positively when the book received funding.

**Cost is a property of the venue, not of the strategy.** The same trade set is
worth funding at 2bps a side and worthless at 75bps. So a result is only ever
stated against one `Capabilities`, and the venue comparison is the caller's to
make by running the same trades twice.

Every cost is computed by `venue/costs.py` -- `round_trip_cost`, which charges
both legs and folds in `carry_cost`. Nothing here re-derives a fee, a spread or
a funding accrual. If the cost model is wrong, one place is wrong.

**Why this sits in `omni.trading`.** It reads the venue cost model, and
`tests/test_trading_isolation.py` forbids `omni.capabilities` from importing
`omni.venue` -- the one-way rule that keeps a fill from ever reaching a
calibration bucket. `omni.trading` is the tier permitted to see both, so the
crypto harness lives here rather than beside its equity sibling.

**What is deliberately not modelled**, because modelling it badly would
understate cost: borrow on a margin short (refused outright), a funding rate
that varies across the hold (the supplied rate is the per-settlement rate and
is applied to every settlement in the hold), and market impact (the paper venue
owns that, against recorded bars).
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal
from enum import Enum

from omni.conviction.gate import MIN_RESOLVED_FOR_CALIBRATION
from omni.ingest.protocol import Unavailable
from omni.venue.costs import BPS, round_trip_cost
from omni.venue.protocol import Capabilities, MarketType, Side, TradeIntent

# Below this many closed trades no hit rate is reported. Tied to the conviction
# gate's own floor rather than restated, so the harness and the gate cannot
# drift apart about what "too few outcomes to say" means.
MIN_TRADES_FOR_RATE = MIN_RESOLVED_FOR_CALIBRATION

# The cost model prices a `TradeIntent`; the harness has closed trades. This
# name identifies the synthetic intent built to carry a trade's size and side
# into `round_trip_cost`. It is never routed anywhere.
_COSTING_VENUE = "backtest"


class Barrier(str, Enum):
    """Which barrier ended the trade.

    `VERTICAL` is the horizon expiring with neither price barrier touched. It is
    a real outcome and not a hit: counting a timeout as a win is how a hit rate
    inflates itself on trades that never reached their thesis.
    """

    UPPER = "upper"
    LOWER = "lower"
    VERTICAL = "vertical"


def _finite(name: str, value: Decimal | float) -> Decimal:
    """Coerce to `Decimal` and refuse NaN/inf explicitly.

    The refusal has to come before any ordering comparison. `Decimal("NaN") < 0`
    raises `InvalidOperation` and `Decimal("NaN") == 0` is False, so a validity
    check written as a comparison either dies with the wrong error or waves the
    value through into a confidently-labelled result computed from NaN.
    """
    candidate = value if isinstance(value, Decimal) else Decimal(str(value))
    if not candidate.is_finite():
        raise ValueError(
            f"{name} is not finite ({value!r}); a backtest statistic computed "
            f"from it would be a number describing nothing"
        )
    return candidate


@dataclass(frozen=True)
class ClosedTrade:
    """One resolved triple-barrier outcome, as it actually closed.

    `method` travels with the trade so a result cannot pool two producers: an
    expectancy averaged across a winning method and a losing one describes
    neither, and it is the average that opens the gate.
    """

    method: str
    symbol: str
    side: Side
    market_type: MarketType
    quantity: Decimal
    entry_price: Decimal
    exit_price: Decimal
    opened_at: datetime
    closed_at: datetime
    barrier: Barrier

    def __post_init__(self) -> None:
        for name in ("quantity", "entry_price", "exit_price"):
            object.__setattr__(self, name, _finite(name, getattr(self, name)))
        if self.quantity <= 0:
            raise ValueError(
                f"quantity must be positive, got {self.quantity}; direction is "
                f"carried by `side`, not by the sign of the size"
            )
        if self.entry_price <= 0 or self.exit_price <= 0:
            raise ValueError(
                f"prices must be positive, got entry={self.entry_price} "
                f"exit={self.exit_price}"
            )
        if self.closed_at < self.opened_at:
            raise ValueError(
                f"{self.symbol} closed at {self.closed_at} before it opened at "
                f"{self.opened_at}; a negative hold would credit funding the "
                f"position never held"
            )

    @property
    def hold(self) -> timedelta:
        """Wall-clock time held. No session close, no weekend, no calendar."""
        return self.closed_at - self.opened_at

    @property
    def gross_bps(self) -> Decimal:
        """Return before any cost, in bps of the trade's own entry notional."""
        move = self.exit_price - self.entry_price
        if self.side is Side.SELL:
            move = -move
        return move / self.entry_price * BPS

    @property
    def is_hit(self) -> bool:
        """Did the trade reach the barrier its direction was betting on."""
        target = Barrier.UPPER if self.side is Side.BUY else Barrier.LOWER
        return self.barrier is target


@dataclass(frozen=True)
class BacktestResult:
    """What one method earned on one venue, and what it cost to earn it.

    The bps aggregates are sums over trades, each trade contributing bps of its
    own entry notional. That is the equal-weight-per-trade convention an
    expectancy is stated in; a notional-weighted total would answer a different
    question and one large trade would decide it.
    """

    method: str
    n_trades: int
    hits: int
    gross_bps: Decimal
    cost_bps: Decimal
    net_bps: Decimal
    max_drawdown_bps: Decimal
    funding_collected_bps: Decimal
    min_trades_for_rate: int = MIN_TRADES_FOR_RATE

    @property
    def hit_rate(self) -> Decimal | None:
        """Realised hit rate, or None when there are too few trades to say.

        None is not zero and it is not 0.5. A rate computed from three outcomes
        is indistinguishable from noise, and reporting it as a number is how a
        gate gets opened on nothing.
        """
        if self.n_trades < self.min_trades_for_rate:
            return None
        return Decimal(self.hits) / Decimal(self.n_trades)

    @property
    def expectancy_bps(self) -> Decimal:
        """Net bps per trade -- the number GATE A is asking for."""
        if self.n_trades <= 0:
            raise Unavailable(
                "no closed trades; expectancy is unknown, not zero"
            )
        return self.net_bps / Decimal(self.n_trades)


def _costing_intent(trade: ClosedTrade) -> TradeIntent:
    return TradeIntent(
        venue=_COSTING_VENUE,
        symbol=trade.symbol,
        side=trade.side,
        market_type=trade.market_type,
        quantity=trade.quantity,
        reference_price=trade.entry_price,
    )


def _require_tradable(trade: ClosedTrade, capabilities: Capabilities) -> None:
    if not capabilities.supports(trade.market_type):
        raise Unavailable(
            f"venue does not support {trade.market_type.value}; a result for "
            f"trades it could never place is a number for an ineligible strategy"
        )
    if trade.side is Side.SELL and not capabilities.shorting:
        raise Unavailable(
            f"venue does not support shorting; {trade.symbol} short is ineligible"
        )
    if trade.side is Side.SELL and trade.market_type is MarketType.MARGIN:
        raise Unavailable(
            "borrow cost on a margin short is not modelled here; costing one "
            "without it would understate exactly the trades being validated"
        )


def _funding(
    trade: ClosedTrade,
    funding_rates: Mapping[str, Decimal] | Decimal | None,
    settlement_hours: int,
) -> tuple[Decimal, int]:
    """The per-settlement rate and how many settlements the hold crossed.

    Settlements are `floor(hold / settlement_hours)` of wall-clock time. The
    floor division is done on `timedelta` so no float ever touches the count.
    """
    if funding_rates is None or trade.market_type is not MarketType.PERPETUAL:
        return Decimal(0), 0

    if isinstance(funding_rates, Mapping):
        if trade.symbol not in funding_rates:
            raise Unavailable(
                f"no funding rate for perpetual {trade.symbol}; costing the "
                f"hold at zero would silently drop the carry leg"
            )
        rate = _finite("funding_rate", funding_rates[trade.symbol])
    else:
        rate = _finite("funding_rate", funding_rates)

    return rate, trade.hold // timedelta(hours=settlement_hours)


def run_backtest(
    trades: Sequence[ClosedTrade],
    *,
    capabilities: Capabilities,
    spread_bps: Decimal,
    funding_rates: Mapping[str, Decimal] | Decimal | None = None,
    settlement_hours: int = 8,
    min_trades_for_rate: int = MIN_TRADES_FOR_RATE,
) -> BacktestResult:
    """Expectancy of one method on one venue, net of that venue's costs.

    `funding_rates` is the per-settlement funding rate as a fraction (0.0001 is
    1bp per settlement), either one rate for every symbol or a mapping per
    symbol. `None` means funding is not modelled at all -- which is honest for
    a spot-only set and a material overstatement for a perpetual one, so the
    caller says so explicitly rather than getting it by omission.

    Raises `Unavailable` when a number cannot be produced honestly: no trades,
    a perpetual with no funding rate supplied, or a venue that cannot place the
    trades at all.
    """
    if not trades:
        raise Unavailable(
            "no closed trades; an expectancy over an empty book is unknown, "
            "not zero"
        )
    if settlement_hours <= 0:
        raise ValueError(f"settlement_hours must be positive, got {settlement_hours}")
    if min_trades_for_rate < 1:
        raise ValueError(
            f"min_trades_for_rate must be at least 1, got {min_trades_for_rate}"
        )

    spread = _finite("spread_bps", spread_bps)
    if spread < 0:
        raise ValueError(f"spread_bps must not be negative, got {spread}")

    methods = {trade.method for trade in trades}
    if len(methods) != 1:
        raise ValueError(
            f"a backtest states one method's expectancy; got {sorted(methods)}. "
            f"Pooling averages a losing method into a winning one and the "
            f"result describes neither"
        )

    gross_total = Decimal(0)
    cost_total = Decimal(0)
    funding_collected = Decimal(0)
    cumulative = Decimal(0)
    peak = Decimal(0)
    max_drawdown = Decimal(0)
    hits = 0

    for trade in trades:
        _require_tradable(trade, capabilities)
        funding_rate, funding_periods = _funding(
            trade, funding_rates, settlement_hours
        )
        cost = round_trip_cost(
            _costing_intent(trade),
            capabilities,
            spread_bps=spread,
            funding_rate=funding_rate,
            funding_periods=funding_periods,
        )

        gross_total += trade.gross_bps
        cost_total += cost.total_bps
        # `funding_bps` is a cost, so a credit is negative there and positive
        # here. A short receiving funding must read as collection, not expense.
        funding_collected -= cost.funding_bps

        cumulative += trade.gross_bps - cost.total_bps
        peak = max(peak, cumulative)
        max_drawdown = max(max_drawdown, peak - cumulative)
        if trade.is_hit:
            hits += 1

    return BacktestResult(
        method=methods.pop(),
        n_trades=len(trades),
        hits=hits,
        gross_bps=gross_total,
        cost_bps=cost_total,
        net_bps=gross_total - cost_total,
        max_drawdown_bps=max_drawdown,
        funding_collected_bps=funding_collected,
        min_trades_for_rate=min_trades_for_rate,
    )
