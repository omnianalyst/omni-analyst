"""The venue protocol -- what a place capital can be committed must be able to say.

`execution/broker.py` is exchange-shaped: it assumes resting orders, an order
id worth cancelling, and an account holding positions. That is true of Alpaca
and IBKR and false of a swap on Uniswap, where execution is atomic, there is no
resting order, and a "position" is just a token balance. Forcing both through
one order-shaped interface produces methods that lie -- `cancel_order` on a
venue with nothing to cancel has to either raise or silently succeed, and both
are wrong.

So a venue declares what it can do instead of being assumed to do everything.
`Capabilities` is read by the router before an intent is built, which means a
funding-carry strategy on a spot-only venue is *ineligible* rather than an
exception at 3am. A capability absent from the declaration is absent from the
plan.

Three things carry over from `ingest/protocol.py` deliberately, because they are
the properties that made the ingestion layer testable:

- **A failure raises.** `VenueUnavailable` is raised, never returned as a
  success-shaped dict. v1's TD Ameritrade branch returned an error dict from
  `initialize()` and a caller that did not check it proceeded as if connected;
  v1's `ibkr_integration.py` made the same assumption and faked fills at a
  hardcoded 150.0.
- **Money is `Decimal`.** Every price, quantity and fee. Binary floats
  accumulate error across a position's lifetime and the error lands in the P&L.
- **Value objects validate themselves.** A `TradeIntent` whose stop sits on the
  wrong side of its reference price is rejected at construction, not discovered
  when it stops out immediately. This mirrors the schema's
  `prediction_barriers_straddle_entry` check and catches an inverted
  direction-to-side mapping in the bridge.

This module declares no implementation. `paper_venue.py`, `ccxt_venue.py` and
`onchain_venue.py` provide those.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from enum import Enum
from typing import Any, Protocol, runtime_checkable
from uuid import uuid4


class VenueUnavailable(Exception):
    """A venue could not answer, quote, or execute.

    Raised, never returned. The trading loop turns this into a recorded
    refusal against the intent rather than a retry loop, so a venue outage
    reads as "did not trade, here is why" instead of a gap in the order
    ledger.
    """


class InvalidIntent(Exception):
    """An intent could not be constructed as stated.

    Distinct from `VenueUnavailable`: the venue is fine, the instruction is
    incoherent. Raised at construction so an incoherent intent cannot reach a
    venue at all.
    """


class Side(str, Enum):
    BUY = "buy"
    SELL = "sell"


class OrderKind(str, Enum):
    MARKET = "market"
    LIMIT = "limit"


class MarketType(str, Enum):
    SPOT = "spot"
    MARGIN = "margin"
    PERPETUAL = "perpetual"


@dataclass(frozen=True)
class Capabilities:
    """What a venue can actually do, declared rather than assumed.

    `maker_fee_bps` / `taker_fee_bps` live here rather than in the cost model
    because they are a property of the venue and the account tier, and the
    router needs them before it builds an intent. Everything else that varies
    per trade -- spread, gas, funding accrual, borrow -- belongs to `costs.py`,
    which computes against a specific intent.

    Venues that charge different fees for spot and perpetuals (Hyperliquid:
    4/7 bps spot vs 1.5/4.5 bps perp) populate ``perp_maker_fee_bps`` /
    ``perp_taker_fee_bps``. When ``None``, the cost model falls back to the
    spot fees. This prevents the delta-neutral carry pair -- which has two
    spot legs and two perp legs -- from being charged at the spot rate on
    every leg, a 5 bps overcharge on a 23 bps round trip that refused
    marginal trades the edge survives.
    """

    spot: bool
    margin: bool
    perpetuals: bool
    limit_orders: bool
    shorting: bool
    funding_data: bool
    maker_fee_bps: Decimal
    taker_fee_bps: Decimal
    min_notional: Decimal
    perp_maker_fee_bps: Decimal | None = None
    perp_taker_fee_bps: Decimal | None = None

    def __post_init__(self) -> None:
        if self.maker_fee_bps < 0 or self.taker_fee_bps < 0:
            raise ValueError(
                f"negative fees are not a discount to model: "
                f"maker={self.maker_fee_bps} taker={self.taker_fee_bps}"
            )
        for name in ("perp_maker_fee_bps", "perp_taker_fee_bps"):
            val = getattr(self, name)
            if val is not None and val < 0:
                raise ValueError(
                    f"negative {name} is not a discount to model: {val}"
                )
        if self.min_notional < 0:
            raise ValueError(f"min_notional must not be negative: {self.min_notional}")
        if self.perpetuals and not self.shorting:
            # A perpetual market you cannot short is not a perpetual market;
            # a strategy would select this venue for a carry leg it cannot open.
            raise ValueError("a venue offering perpetuals must support shorting")

    def supports(self, market_type: MarketType) -> bool:
        if market_type is MarketType.SPOT:
            return self.spot
        if market_type is MarketType.MARGIN:
            return self.margin
        return self.perpetuals


@dataclass(frozen=True)
class TradeIntent:
    """A sized, bounded instruction to a venue.

    Already sized: `quantity` comes from `portfolio/sizing.py`, never from the
    prediction that motivated it. A prediction states a direction and a
    confidence; how much of the portfolio that is worth is a portfolio
    question, and keeping the two apart is what stops a confident signal from
    also being a large one.

    `stop_price` and `take_profit_price` carry the prediction's lower and upper
    barriers, and `expires_at` its horizon. The venue is not required to
    support them natively -- a venue without bracket orders leaves the trading
    loop to manage the exit -- but the intent records what the analysis
    actually claimed, so an exit can be checked against it.
    """

    venue: str
    symbol: str
    side: Side
    market_type: MarketType
    quantity: Decimal
    reference_price: Decimal
    order_kind: OrderKind = OrderKind.MARKET
    limit_price: Decimal | None = None
    stop_price: Decimal | None = None
    take_profit_price: Decimal | None = None
    expires_at: datetime | None = None
    reduce_only: bool = False
    provenance: dict[str, Any] = field(default_factory=dict)
    idempotency_key: str = field(default_factory=lambda: uuid4().hex)

    def __post_init__(self) -> None:
        if self.quantity <= 0:
            raise InvalidIntent(
                f"quantity must be positive, got {self.quantity}; "
                f"direction is carried by `side`, not by the sign of the size"
            )
        if self.reference_price <= 0:
            raise InvalidIntent(
                f"reference_price must be positive, got {self.reference_price}"
            )
        if self.order_kind is OrderKind.LIMIT and self.limit_price is None:
            raise InvalidIntent("a limit order requires a limit_price")
        if self.order_kind is OrderKind.MARKET and self.limit_price is not None:
            raise InvalidIntent(
                "a market order carries no limit_price; the price it would "
                "have respected is not the price it will get"
            )
        if self.limit_price is not None and self.limit_price <= 0:
            raise InvalidIntent(f"limit_price must be positive, got {self.limit_price}")

        self._validate_barriers()

    def _validate_barriers(self) -> None:
        """A stop below and a target above -- inverted for a short.

        The failure this catches is a direction-to-side mapping that inverts:
        the bridge reads a `down` prediction, writes `Side.SELL`, and then
        assigns the prediction's `lower_barrier` to `stop_price` out of habit.
        The result is a short whose stop sits below its entry, which is a take
        profit wearing a stop's name -- it never stops the loss it was written
        to stop. The schema's `prediction_barriers_straddle_entry` constraint
        cannot catch it because both orderings straddle.
        """
        if self.side is Side.BUY:
            below, above = self.stop_price, self.take_profit_price
            below_name, above_name = "stop_price", "take_profit_price"
        else:
            below, above = self.take_profit_price, self.stop_price
            below_name, above_name = "take_profit_price", "stop_price"

        if below is not None and below >= self.reference_price:
            raise InvalidIntent(
                f"{below_name} {below} must be below reference_price "
                f"{self.reference_price} for a {self.side.value}"
            )
        if above is not None and above <= self.reference_price:
            raise InvalidIntent(
                f"{above_name} {above} must be above reference_price "
                f"{self.reference_price} for a {self.side.value}"
            )

    @property
    def notional(self) -> Decimal:
        return self.quantity * self.reference_price


@dataclass(frozen=True)
class Quote:
    """What a venue says an intent would cost, before committing to it.

    `total_cost` is the sum of the components in the quote currency, so a
    caller comparing venues compares one number without re-deriving it and
    without two callers deriving it differently.
    """

    intent: TradeIntent
    expected_price: Decimal
    fee: Decimal
    slippage: Decimal
    gas: Decimal
    as_of: datetime

    def __post_init__(self) -> None:
        if self.expected_price <= 0:
            raise ValueError(f"expected_price must be positive, got {self.expected_price}")
        for name in ("fee", "slippage", "gas"):
            if getattr(self, name) < 0:
                raise ValueError(f"{name} must not be negative, got {getattr(self, name)}")

    @property
    def total_cost(self) -> Decimal:
        return self.fee + self.slippage + self.gas

    @property
    def cost_bps(self) -> Decimal:
        """Cost as basis points of notional -- the unit strategies reason in."""
        notional = self.quantity_notional
        if notional == 0:
            raise ValueError("cannot express cost in bps of a zero notional")
        return self.total_cost / notional * Decimal(10_000)

    @property
    def quantity_notional(self) -> Decimal:
        return self.intent.quantity * self.expected_price


@dataclass(frozen=True)
class Fill:
    """What actually happened. Never constructed from what was requested.

    `filled_quantity` may be less than the intent's `quantity`; a partial fill
    is a normal outcome and the caller reconciles against it. A venue that
    cannot report the executed price does not get to report a fill -- there is
    no default here, because a fabricated average price is a fabricated P&L.
    """

    intent_id: str
    venue: str
    symbol: str
    side: Side
    filled_quantity: Decimal
    average_price: Decimal
    fee_paid: Decimal
    filled_at: datetime
    external_id: str | None = None
    raw: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.filled_quantity < 0:
            raise ValueError(f"filled_quantity must not be negative: {self.filled_quantity}")
        if self.filled_quantity > 0 and self.average_price <= 0:
            raise ValueError(
                f"a fill of {self.filled_quantity} needs a real average_price, "
                f"got {self.average_price}"
            )
        if self.fee_paid < 0:
            raise ValueError(f"fee_paid must not be negative: {self.fee_paid}")

    @property
    def is_empty(self) -> bool:
        return self.filled_quantity == 0

    @property
    def notional(self) -> Decimal:
        return self.filled_quantity * self.average_price


@dataclass(frozen=True)
class Position:
    """An open exposure at a venue.

    `quantity` is signed: negative is short. Unlike `TradeIntent`, where
    direction is carried by `side` so a size cannot be accidentally negative,
    a position has no side of its own -- it is the net of everything that
    happened, and that net has a sign.
    """

    venue: str
    symbol: str
    market_type: MarketType
    quantity: Decimal
    average_entry: Decimal
    as_of: datetime

    def __post_init__(self) -> None:
        if self.quantity != 0 and self.average_entry <= 0:
            raise ValueError(
                f"an open position of {self.quantity} needs a real average_entry, "
                f"got {self.average_entry}"
            )

    @property
    def is_short(self) -> bool:
        return self.quantity < 0

    @property
    def notional(self) -> Decimal:
        return abs(self.quantity) * self.average_entry


@dataclass(frozen=True)
class Balance:
    """What a venue *reports* it is holding for us. Never negative.

    The guard is a statement about the venue, not a convenience: an exchange
    does not report a negative available balance. `free` answers "how much may
    you spend right now", which floors at zero, and money owed appears as a
    separate borrow or margin liability that this protocol does not carry. A
    negative `free` from an adapter is therefore a sign error in the adapter,
    and admitting it would put a fabricated liability into reconciliation.

    This is **not** the same quantity as the local `cash_balance` row that
    `portfolio/state.py` materialises, whose `free` is signed by design because
    a margin buy legitimately overdraws it. The two share a name and nothing
    else, so the local side has its own type -- `portfolio.state.CashPosition`
    -- and the reconciler compares them explicitly rather than either one being
    converted into the other. A local balance that is negative and a venue
    balance that cannot be is a divergence with a cause, and it is reported as
    one.
    """

    venue: str
    asset: str
    free: Decimal
    locked: Decimal
    as_of: datetime

    def __post_init__(self) -> None:
        if self.free < 0 or self.locked < 0:
            raise ValueError(
                f"balances must not be negative: free={self.free} locked={self.locked}"
            )

    @property
    def total(self) -> Decimal:
        return self.free + self.locked


@runtime_checkable
class Venue(Protocol):
    """Somewhere capital can be committed.

    `quote` must not commit anything and must be safe to call for venues the
    router will then reject. `execute` is the only method that moves money.

    `cancel` returns False for a venue with nothing to cancel rather than
    raising, because an atomic-swap venue legitimately has no resting order --
    but it must never return True for a cancellation that did not happen.

    `symbol_for` is how an ASSET becomes something this venue can trade. A
    strategy reasons about assets -- `entity.symbol` is `BTC` -- and a venue
    trades instruments on pairs: `BTC/USDT` spot, `BTC/USDT:USDT` perpetual on
    ccxt, and something else again elsewhere. Nothing but the venue knows its
    own naming, so nothing but the venue should build it.

    It exists because the carry loop passed `MKR` straight through as a venue
    symbol and every test passed: tests construct venue symbols directly, so no
    test ever took a symbol from an entity row and handed it to a venue, which
    is the only path production has. `PaperVenue` then settled the fill in an
    asset called `MKR`, its cash never moved, and reconciliation halted the
    book. A live venue would have rejected the order outright and half-opened
    the pair on whichever leg went first (Finding 21).

    **`None` means this venue does not list that asset**, which is a normal
    answer and not an error: a caller skips the name. It is deliberately not an
    exception, because an unlisted asset on one venue is routine and a strategy
    must be able to pass over it without a try/except around every candidate --
    and deliberately not a best-guess string, because a guessed symbol is an
    order on a market nobody chose.
    """

    name: str
    capabilities: Capabilities

    def symbol_for(self, asset: str, market_type: MarketType) -> str | None: ...

    def held_symbol_aliases(
        self, asset: str, market_type: MarketType
    ) -> tuple[str, ...]:
        """Extra spellings under which a HELD position in this leg may be
        recorded. Orders are addressed by `symbol_for`; positions and fills
        can arrive under the venue's raw market id instead (Hyperliquid's
        spot fills carry `UETH/USDC` while the tradable symbol is
        `ETH/USDC` -- the same market, two spellings). A reader that maps
        only the tradable spelling will read a hedged book as four unpaired
        legs and refuse to touch it.

        Empty by default: most venues use one spelling per market, and a
        venue with no recorded divergence has nothing to add.
        """
        return ()

    async def quote(self, intent: TradeIntent) -> Quote: ...

    async def execute(self, intent: TradeIntent) -> Fill: ...

    async def positions(self) -> list[Position]: ...

    async def balances(self) -> list[Balance]: ...

    async def cancel(self, external_id: str) -> bool: ...
