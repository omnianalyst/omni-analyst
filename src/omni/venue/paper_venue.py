"""A venue that executes nothing, honestly.

The paper phase decides whether real capital moves, so a paper venue that fills
generously is worse than no paper venue at all -- it produces a track record
that cannot be reproduced live, and the gap only becomes visible after the
money is committed. Every rule here is chosen to fill *less* readily than
reality, never more.

Four rules, each closing a specific way a paper fill flatters itself:

**Never fill at a price that did not trade.** A fill is clamped into the bar's
own `[low, high]`. Without this, a slippage model can walk the fill price past
the extreme the market actually printed, and the position opens at a level that
never existed.

**A limit order fills only if the market reached it.** A buy limit needs
`low <= limit`, a sell limit needs `high >= limit`. The common shortcut --
filling any limit whose price is "close enough" to the close -- is how a
backtest earns the spread on every trade instead of paying it.

**Size is capped by volume.** An order larger than the bar's traded volume
cannot fill in full, and what does fill moves the price against itself. A model
without this lets a strategy scale to any size at the touch, which is the
single most common reason a backtest does not survive contact with a book.

**A rejection is a rejection.** Below `min_notional` the venue returns an empty
fill, not a fill for the minimum. Silently resizing an order is how a risk limit
gets ignored one trade at a time.

Market data is injected as a `MarketWindow` rather than read from the claim
store, so fills can be tested against recorded bars with no database -- the same
property that made the ingest adapters testable.
"""

from __future__ import annotations

from collections.abc import Collection
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from typing import Protocol, runtime_checkable

from omni.venue.protocol import (
    Balance,
    Capabilities,
    Fill,
    MarketType,
    OrderKind,
    Position,
    Quote,
    Side,
    TradeIntent,
    VenueUnavailable,
)

# What fraction of a bar's volume a single order may take. A strategy filling
# more than this is not being modelled, it is being flattered: at 100% the
# order is the entire market for that bar and the fill price is fiction.
DEFAULT_PARTICIPATION_CAP = Decimal("0.10")

# Price impact in bps per unit of participation. At the cap above, a full-size
# order pays 10bps. Deliberately modest -- the point is that impact exists and
# scales, not that this coefficient is calibrated. A venue with real book data
# should override it from measured depth.
DEFAULT_IMPACT_BPS = Decimal(100)


@dataclass(frozen=True)
class Bar:
    """One OHLCV window. The only thing a paper fill is allowed to believe."""

    symbol: str
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    volume: Decimal
    at: datetime

    def __post_init__(self) -> None:
        if self.low > self.high:
            raise ValueError(f"low {self.low} exceeds high {self.high} for {self.symbol}")
        if not (self.low <= self.open <= self.high):
            raise ValueError(f"open {self.open} outside [{self.low}, {self.high}]")
        if not (self.low <= self.close <= self.high):
            raise ValueError(f"close {self.close} outside [{self.low}, {self.high}]")
        if self.volume < 0:
            raise ValueError(f"negative volume: {self.volume}")

    def clamp(self, price: Decimal) -> Decimal:
        return min(max(price, self.low), self.high)


@runtime_checkable
class MarketWindow(Protocol):
    """The recorded market a paper fill executes against."""

    async def bar(self, symbol: str, at: datetime) -> Bar | None: ...


@dataclass
class RecordedBars:
    """A `MarketWindow` over an in-memory list. Used by tests and backtests."""

    bars: dict[str, list[Bar]] = field(default_factory=dict)

    def add(self, bar: Bar) -> None:
        self.bars.setdefault(bar.symbol, []).append(bar)

    async def bar(self, symbol: str, at: datetime) -> Bar | None:
        candidates = [b for b in self.bars.get(symbol, []) if b.at <= at]
        if not candidates:
            return None
        return max(candidates, key=lambda b: b.at)


class PaperVenue:
    """Simulated execution against recorded bars, using the real cost model.

    `spread_bps` is charged on every taker fill because the reference price is
    the bar close, which sits inside the spread. A paper venue that fills at the
    close pays nothing to cross, and a strategy trading frequently enough will
    show an edge composed entirely of that omission.
    """

    def __init__(
        self,
        market: MarketWindow,
        capabilities: Capabilities,
        *,
        name: str = "paper",
        spread_bps: Decimal = Decimal(4),
        participation_cap: Decimal = DEFAULT_PARTICIPATION_CAP,
        impact_bps: Decimal = DEFAULT_IMPACT_BPS,
        starting_balances: dict[str, Decimal] | None = None,
        quote_asset: str = "USD",
        listed: Collection[str] | None = None,
    ) -> None:
        if participation_cap <= 0 or participation_cap > 1:
            raise ValueError(
                f"participation_cap must be in (0, 1], got {participation_cap}"
            )
        if spread_bps < 0 or impact_bps < 0:
            raise ValueError("spread_bps and impact_bps must not be negative")

        self.name = name
        self.capabilities = capabilities
        self._market = market
        self._spread_bps = spread_bps
        self._participation_cap = participation_cap
        self._impact_bps = impact_bps
        if not quote_asset or not quote_asset.strip():
            raise ValueError("quote_asset must name the asset this venue settles in")
        self._quote_asset_name = quote_asset
        # `None` means every asset is listed, which is what a paper venue with no
        # stated universe should mean. An empty collection means none are, and
        # the two must not collapse: `listed=[]` is a venue that lists nothing,
        # and silently treating it as "everything" would trade a universe the
        # caller explicitly emptied.
        self._listed = None if listed is None else frozenset(listed)
        self._balances: dict[str, Decimal] = dict(starting_balances or {})
        self._positions: dict[tuple[str, MarketType], Position] = {}
        self._fills: list[Fill] = []

    @property
    def fills(self) -> list[Fill]:
        return list(self._fills)

    async def _require_bar(self, intent: TradeIntent) -> Bar:
        at = intent.expires_at or _now_from(intent)
        bar = await self._market.bar(intent.symbol, at)
        if bar is None:
            raise VenueUnavailable(
                f"no recorded bar for {intent.symbol} at or before {at}; "
                f"refusing to fill against a price that was never observed"
            )
        return bar

    def _fillable_quantity(self, intent: TradeIntent, bar: Bar) -> Decimal:
        cap = bar.volume * self._participation_cap
        return min(intent.quantity, cap)

    def _participation(self, quantity: Decimal, bar: Bar) -> Decimal:
        if bar.volume <= 0:
            return Decimal(1)
        return quantity / bar.volume

    async def quote(self, intent: TradeIntent) -> Quote:
        bar = await self._require_bar(intent)
        quantity = self._fillable_quantity(intent, bar)
        price = self._price_for(intent, bar, quantity)
        if price is None:
            raise VenueUnavailable(
                f"{intent.symbol} did not trade through {intent.limit_price} "
                f"in the window [{bar.low}, {bar.high}]"
            )
        fee = price * quantity * self.capabilities.taker_fee_bps / Decimal(10_000)
        slippage = abs(price - bar.close) * quantity
        return Quote(
            intent=intent,
            expected_price=price,
            fee=fee,
            slippage=slippage,
            gas=Decimal(0),
            as_of=bar.at,
        )

    def _price_for(
        self, intent: TradeIntent, bar: Bar, quantity: Decimal
    ) -> Decimal | None:
        """The price this intent would fill at, or None if it would not fill."""
        if intent.order_kind is OrderKind.LIMIT:
            limit = intent.limit_price
            assert limit is not None  # guaranteed by TradeIntent.__post_init__
            if intent.side is Side.BUY:
                if bar.low > limit:
                    return None
                return bar.clamp(min(limit, bar.open))
            if bar.high < limit:
                return None
            return bar.clamp(max(limit, bar.open))

        half_spread = bar.close * self._spread_bps / Decimal(20_000)
        impact = (
            bar.close
            * self._impact_bps
            * self._participation(quantity, bar)
            / Decimal(10_000)
        )
        adverse = half_spread + impact
        raw = bar.close + adverse if intent.side is Side.BUY else bar.close - adverse
        return bar.clamp(raw)

    async def execute(self, intent: TradeIntent) -> Fill:
        bar = await self._require_bar(intent)

        if intent.notional < self.capabilities.min_notional:
            return _empty_fill(
                intent,
                self.name,
                bar.at,
                reason=(
                    f"notional {intent.notional} below venue minimum "
                    f"{self.capabilities.min_notional}"
                ),
            )

        if not self.capabilities.supports(intent.market_type):
            raise VenueUnavailable(
                f"{self.name} does not support {intent.market_type.value}"
            )
        if intent.side is Side.SELL and not self.capabilities.shorting:
            existing = self._positions.get((intent.symbol, intent.market_type))
            held = existing.quantity if existing else Decimal(0)
            if held < intent.quantity:
                raise VenueUnavailable(
                    f"{self.name} cannot short: holding {held} of "
                    f"{intent.symbol}, asked to sell {intent.quantity}"
                )

        quantity = self._fillable_quantity(intent, bar)
        if quantity <= 0:
            return _empty_fill(
                intent, self.name, bar.at, reason="no volume traded in the window"
            )

        price = self._price_for(intent, bar, quantity)
        if price is None:
            return _empty_fill(
                intent,
                self.name,
                bar.at,
                reason=(
                    f"limit {intent.limit_price} not reached; bar traded "
                    f"[{bar.low}, {bar.high}]"
                ),
            )

        fee = price * quantity * self.capabilities.taker_fee_bps / Decimal(10_000)
        fill = Fill(
            intent_id=intent.idempotency_key,
            venue=self.name,
            symbol=intent.symbol,
            side=intent.side,
            filled_quantity=quantity,
            average_price=price,
            fee_paid=fee,
            filled_at=bar.at,
            external_id=f"paper-{len(self._fills)}",
            raw={
                "bar": {
                    "open": str(bar.open),
                    "high": str(bar.high),
                    "low": str(bar.low),
                    "close": str(bar.close),
                    "volume": str(bar.volume),
                },
                "requested_quantity": str(intent.quantity),
                "participation": str(self._participation(quantity, bar)),
                "partial": quantity < intent.quantity,
            },
        )
        self._apply(fill, intent.market_type)
        self._fills.append(fill)
        return fill

    def held_symbol_aliases(
        self, asset: str, market_type: MarketType
    ) -> tuple[str, ...]:
        """One spelling per market here: positions are keyed by the same
        symbol orders are addressed by, so there is no raw id to map."""
        return ()

    def spot_holding_asset(self, symbol_or_asset: str) -> str | None:
        """No token balances here: every spot holding is a position, so no
        balance needs naming as one."""
        return None

    def symbol_for(self, asset: str, market_type: MarketType) -> str | None:
        """The tradable symbol for an asset here, or `None` if it is not listed.

        A strategy holds assets -- `entity.symbol` is `BTC` -- and a venue trades
        pairs. This venue quotes everything in one asset, so `BTC` becomes
        `BTC/USD`, and both market types share a symbol because positions here
        are keyed `(symbol, market_type)` and the market type already separates
        them. A real exchange does not have that luxury: ccxt spells the
        perpetual `BTC/USDT:USDT`, which is why the resolver belongs on the
        venue rather than in the strategy composing a string.

        `None` for an asset outside a stated universe, so a caller skips the
        name rather than sizing an order on a market that does not exist. An
        asset already carrying a quote is returned unchanged, which keeps every
        caller that already speaks in venue symbols working.
        """
        if not self.capabilities.supports(market_type):
            return None
        asset = asset.strip()
        if not asset:
            return None
        if "/" in asset:
            # Already a venue symbol. Passing it through is what lets a test or
            # a caller that resolved elsewhere hand one straight in.
            return asset
        if self._listed is not None and asset not in self._listed:
            return None
        return f"{asset}/{self._quote_asset_name}"

    def _quote_asset(self, symbol: str) -> str:
        """The asset a fill is paid in. `BTC/USD` settles in USD.

        A bare ticker has no quote to find, and returning the ticker itself is
        how this venue came to hold balances in `MKR` and `APT` while its cash
        never moved (Finding 21). `symbol_for` is what stops a bare ticker
        arriving; this refuses one that does, because a fill settling in an
        asset the account never agreed to trade is worse than a loud failure.
        """
        _, sep, quote = symbol.partition("/")
        if not sep or not quote:
            raise ValueError(
                f"{symbol!r} is an asset, not a symbol this venue can settle: "
                f"it names no quote currency, so a fill against it would credit "
                f"or debit {symbol!r} itself. Resolve it with symbol_for() first"
            )
        return quote

    def credit_funding(self, symbol: str, amount: Decimal) -> None:
        """Credit a funding settlement the way an exchange would.

        A real venue does this on its own schedule: funding settles, the
        exchange moves the cash, and a caller learns about it by reading
        `balances()`. A simulated venue has no such schedule, so it must be
        told, and **not telling it is not a neutral omission** -- the book
        applies the settlement through `portfolio.state.apply_funding` and the
        venue does not, so the two diverge by exactly the carry the book earned,
        growing every cycle. A reconcile-first loop then halts on the second
        rebalance and every one after it.

        That is not a hypothetical: it is what the carry loop's own two-cycle
        test found the moment reconciliation was wired in, diverging by 1.10 on
        a 100k book, which was precisely the first cycle's accrual.

        `amount` is signed the way the book signs it -- positive is received --
        so a caller mirrors `FundingAccrual.amount` without reinterpreting it.
        Reinterpreting the sign here would make the venue and the book disagree
        about the direction of the only cash flow this strategy earns.
        """
        if not isinstance(amount, Decimal):
            raise TypeError(
                f"funding amount must be a Decimal, got {type(amount).__name__}; "
                f"a float carries a binary error into the venue's own cash"
            )
        if not amount.is_finite():
            raise ValueError(f"funding amount must be finite, got {amount}")
        asset = self._quote_asset(symbol)
        self._balances[asset] = self._balances.get(asset, Decimal(0)) + amount

    def _debit_cash(
        self, fill: Fill, market_type: MarketType, existing: Position | None
    ) -> None:
        """Move cash the way a fill actually moves it.

        `balances()` previously reported whatever the constructor was handed and
        never changed, so a paper book's cash was fiction: it diverged from the
        real figure by the full notional of the very first fill, and any caller
        reconciling cash against this venue halted immediately.

        **Spot and margin** settle in cash: a buy pays notional plus the fee, a
        sell receives notional less the fee. The fee is subtracted in BOTH
        directions -- it is a cost, not a signed flow -- which is the sign error
        worth naming, because charging it as a credit on the sell side makes a
        round trip look free.

        **A perpetual does not settle in cash.** Opening one posts margin rather
        than spending, so the only cash it costs is the fee; closing one
        realises P&L, and that realisation is the whole of the cash the contract
        ever returns. This mirrors `portfolio.state._cash_delta` exactly, and it
        has to: the two are the venue's and the book's answer to one question,
        and a reconciler compares them directly.

        That symmetry was broken. `portfolio.state` was corrected first, and
        this side kept crediting a short perpetual's full notional -- so a carry
        book and this venue disagreed on cash by exactly the perpetual notional
        after any perp trade, and a reconcile-first loop halted on every cycle
        after the first. The logic is duplicated rather than shared because
        `omni.venue` sits BELOW `omni.portfolio` and may not import from it; the
        duplication is the price of that direction, and the tests on both sides
        assert the same worked figures so a future change to one shows up as a
        failure in the other.

        The balance may go negative here. That is correct for a paper book with
        no funding model, and `Balance` refuses it, so `balances()` clamps at
        zero for reporting while `_cash` keeps the true figure. A venue never
        reports a negative available balance; that is exactly why
        `portfolio.state.CashPosition` exists as a separate type.
        """
        asset = self._quote_asset(fill.symbol)
        if market_type is MarketType.PERPETUAL:
            delta = _closed_pnl(fill, existing)
        else:
            delta = -fill.notional if fill.side is Side.BUY else fill.notional
        self._balances[asset] = (
            self._balances.get(asset, Decimal(0)) + delta - fill.fee_paid
        )

    def _apply(self, fill: Fill, market_type: MarketType) -> None:
        key = (fill.symbol, market_type)
        # Read the position BEFORE the update: a perpetual's cash flow is the
        # P&L realised against the entry it is closing, which the updated row no
        # longer carries.
        self._debit_cash(fill, market_type, self._positions.get(key))
        signed = fill.filled_quantity if fill.side is Side.BUY else -fill.filled_quantity
        existing = self._positions.get(key)

        if existing is None:
            new_quantity = signed
            new_entry = fill.average_price
        else:
            new_quantity = existing.quantity + signed
            if existing.quantity == 0 or (existing.quantity > 0) != (signed > 0):
                # Opening, flipping or reducing: a reduction keeps the original
                # entry, a flip takes the new one. Averaging across a flip would
                # invent an entry price the position never had.
                new_entry = (
                    fill.average_price
                    if new_quantity != 0 and (new_quantity > 0) != (existing.quantity > 0)
                    else existing.average_entry
                )
            else:
                total = abs(existing.quantity) + abs(signed)
                new_entry = (
                    existing.average_entry * abs(existing.quantity)
                    + fill.average_price * abs(signed)
                ) / total

        if new_quantity == 0:
            self._positions.pop(key, None)
            return

        self._positions[key] = Position(
            venue=self.name,
            symbol=fill.symbol,
            market_type=market_type,
            quantity=new_quantity,
            average_entry=new_entry,
            as_of=fill.filled_at,
        )

    async def positions(self) -> list[Position]:
        return list(self._positions.values())

    async def balances(self) -> list[Balance]:
        now = max((f.filled_at for f in self._fills), default=_EPOCH)
        return [
            Balance(
                venue=self.name,
                asset=asset,
                # Clamped: a real venue never reports a negative available
                # balance. The signed figure lives in `_balances` and reaches a
                # reconciler through `portfolio.state.CashPosition`.
                free=max(amount, Decimal(0)),
                locked=Decimal(0),
                as_of=now,
            )
            for asset, amount in self._balances.items()
        ]

    async def cancel(self, external_id: str) -> bool:
        # Paper fills are immediate; there is never a resting order to cancel.
        # Returning True would report a cancellation that did not happen.
        return False


_EPOCH = datetime.fromtimestamp(0).astimezone()


def _closed_pnl(fill: Fill, existing: Position | None) -> Decimal:
    """The P&L a perpetual fill realises against the position it lands on.

    Only the part of the fill that CLOSES realises anything. A fill that opens,
    adds to, or flips beyond an existing position realises only on the quantity
    it actually closed -- the remainder is a new position at a new entry, and
    counting it as realised would book a profit on a trade still open.

    Mirrors `portfolio.state._closed_pnl`. See `_debit_cash` for why the two are
    duplicated rather than shared.
    """
    if existing is None or existing.quantity == 0:
        return Decimal(0)
    signed = fill.filled_quantity if fill.side is Side.BUY else -fill.filled_quantity
    if (existing.quantity > 0) == (signed > 0):
        # Same direction: adding to the position closes nothing.
        return Decimal(0)
    closed = min(abs(signed), abs(existing.quantity))
    if existing.quantity > 0:
        return closed * (fill.average_price - existing.average_entry)
    return closed * (existing.average_entry - fill.average_price)


def _now_from(intent: TradeIntent) -> datetime:
    stamp = intent.provenance.get("as_of")
    if isinstance(stamp, datetime):
        return stamp
    raise VenueUnavailable(
        "a paper intent must carry provenance['as_of'] or expires_at so the "
        "fill can be dated; filling against 'now' would let a backtest see a "
        "bar that had not printed yet"
    )


def _empty_fill(
    intent: TradeIntent, venue: str, at: datetime, *, reason: str
) -> Fill:
    return Fill(
        intent_id=intent.idempotency_key,
        venue=venue,
        symbol=intent.symbol,
        side=intent.side,
        filled_quantity=Decimal(0),
        average_price=Decimal(0),
        fee_paid=Decimal(0),
        filled_at=at,
        external_id=None,
        raw={"rejected": reason, "requested_quantity": str(intent.quantity)},
    )
