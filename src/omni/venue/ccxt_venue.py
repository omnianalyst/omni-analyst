"""Live execution at a centralised exchange, through ccxt.

This is the first module here that can spend real money, so every default in it
is chosen against the caller: the safe state is the state you get by saying
nothing, and the dangerous state is the one you had to type.

**Trading is off unless it was asked for.** `mode` defaults to
`TradingMode.READ_ONLY` and `execute` refuses -- before any network call --
until a caller passes `TradingMode.LIVE` explicitly. `quote`, `balances`,
`positions` and `cancel` are always allowed: reading an account and cancelling
an order never commits capital, and a kill switch that needed an opt-in would
be a kill switch that fails closed in the wrong direction. A boolean flag would
have been enough for the type checker and not enough for the reader, because
`CCXTVenue(exchange, True)` is one careless positional away from a live order.

**Idempotency is the intent's, not ours.** `TradeIntent.idempotency_key` is sent
as ccxt's unified `clientOrderId` param -- Binance maps that onto
`newClientOrderId` in `create_order_request`, and rejects a second order
carrying the same one with `DuplicateOrderId`. That is what makes a retry after
a timeout safe, and it is why the key is never regenerated here.

**A network timeout on `create_order` is the single most dangerous event in
live trading**, because the request may have reached the matching engine and
the response may have died on the way back. This adapter never retries the
placement. It performs exactly one read -- `fetch_order` by client order id --
and answers from what the venue says:

- the venue describes a filled order: that is a real observation, return it;
- the venue says the order is open or dead: return the honest empty fill;
- the venue does not know the id, or cannot be reached: raise
  `VenueUnavailable` naming the client order id. A resubmission is the caller's
  decision, and it MUST reuse the same idempotency key.

The one thing that never happens is a fabricated `Fill`. A fill invented from
the intent writes a position that does not exist, and every size computed after
it is computed against a wrong book -- v1's `ibkr_integration.py` did exactly
this at a hardcoded 150.0.

**Minimums and precision are per symbol and come from the venue.** Measured on
Binance: spot BTC/USDT and ETH/USDT require 5 USDT of cost, while the perpetuals
require 50, 20 and 5 USDT for BTC, ETH and SOL respectively. A single constant
covering all of them either blocks valid orders or waves through orders the
exchange will reject, so the minimum is read per symbol from
`markets[symbol]['limits']['cost']['min']` and the amount is rounded with the
venue's own `amount_to_precision` before submission. For a carry book of a few
hundred dollars these are not edge cases -- they are the normal path.

`Capabilities.min_notional` is a single scalar and therefore cannot carry the
per-symbol figure. It is set to the LARGEST minimum in scope, which is the only
direction that cannot mislead a caller into sending an order the venue will
reject; `min_notional_for(symbol)` is the real answer and is what `execute`
enforces. Scoping the venue with `symbols=` narrows both.

ccxt is imported lazily inside the functions that need it, as
`ingest/exchanges.py` does, so this module imports without ccxt present and the
exception translation still resolves against the real taxonomy on the live path.
"""

from __future__ import annotations

import hashlib
from collections.abc import Sequence
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from enum import Enum
from typing import Any, NoReturn

from omni.venue.credentials import TradingCredentials, WalletCredentials
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

BPS = Decimal(10_000)

# Tradable symbol -> the native spellings the venue's own records use for a
# held position in it. Measured 2026-08-19 against the live Hyperliquid
# ledger: spot fills on ETH/USDC and SOL/USDC report as the wrapped coin
# names UETH/USDC and USOL/USDC in userFills. Extend only from a measurement.
_HELD_SYMBOL_ALIASES: dict[str, tuple[str, ...]] = {
    "ETH/USDC": ("UETH/USDC",),
    "SOL/USDC": ("USOL/USDC",),
}

# ccxt's unified key for a caller-supplied order id. Binance's
# `create_order_request` reads it (alongside `newClientOrderId`) and sends it as
# `newClientOrderId`; other venues map it to their own field. Sending the
# venue-specific key directly would break every venue that does not use it.
CLIENT_ORDER_ID_PARAM = "clientOrderId"

# Statuses that mean the order is finished and filled nothing. Reporting an
# empty fill for these is a statement of fact, not an absence of information.
TERMINAL_UNFILLED = frozenset({"canceled", "cancelled", "rejected", "expired"})

# Statuses that mean the order exists and may still fill. An empty fill here
# carries the external id so the caller can cancel or poll it -- raising would
# strip the only handle on a live order.
RESTING = frozenset({"open"})

CANCELLED = frozenset({"canceled", "cancelled", "expired"})
FILLED = frozenset({"closed", "filled"})

_MARKET_FLAG = {
    MarketType.SPOT: "spot",
    MarketType.MARGIN: "margin",
    MarketType.PERPETUAL: "swap",
}


# Levels to walk when pricing a market order. Deep enough that a retail
# size fills inside it on any venue here, shallow enough to stay one
# cheap call: a quote that pages the book is a quote nobody runs per leg.
BOOK_DEPTH = 50


class TradingMode(str, Enum):
    """Whether this adapter may place orders.

    An enum rather than a boolean so the dangerous value has to be imported and
    named at the call site. `CCXTVenue(exchange)` cannot trade; only
    `CCXTVenue(exchange, mode=TradingMode.LIVE)` can.
    """

    READ_ONLY = "read_only"
    LIVE = "live"


def _decimal(value: Any) -> Decimal | None:
    """Parse a ccxt float into `Decimal` without going through binary float.

    Returns None for anything unusable -- missing, non-numeric, NaN or inf --
    so the caller refuses rather than substituting a value. `Decimal(str(x))`
    rather than `Decimal(x)`: the latter preserves the binary error the float
    already carries, which is the error this whole tier exists to keep out of
    the P&L.
    """
    if value is None or isinstance(value, bool):
        return None
    try:
        parsed = Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError):
        return None
    if not parsed.is_finite():
        return None
    return parsed


def _text(value: Any) -> str | None:
    if value is None:
        return None
    rendered = str(value).strip()
    return rendered or None


def _moment(value: Any) -> datetime | None:
    ms = _decimal(value)
    if ms is None:
        return None
    try:
        return datetime.fromtimestamp(float(ms) / 1000, tz=UTC)
    except (OverflowError, OSError, ValueError):
        return None


def _limit(market: Any, name: str) -> Decimal | None:
    if not isinstance(market, dict):
        return None
    limits = market.get("limits")
    if not isinstance(limits, dict):
        return None
    bucket = limits.get(name)
    if not isinstance(bucket, dict):
        return None
    return _decimal(bucket.get("min"))


def _walk_book(book: Any, intent: TradeIntent) -> Decimal | None:
    """Size-weighted price of filling `intent.quantity` against `book`.

    A pure function over a book already fetched, not a second call: `quote`
    needs the top of that same book for its bid/ask, so making this fetch its
    own would double the request count of every market quote.

    Returns None when the visible levels cannot fill the size, so the caller
    falls back to the touch rather than receiving a price for a fill that would
    not happen. Inventing a worse price for the invisible remainder would be a
    cost model guessing at depth it cannot see; an order the book cannot fill is
    a refusal `execute` makes.
    """
    if not isinstance(book, dict):
        return None
    levels = book.get("asks") if intent.side is Side.BUY else book.get("bids")
    if not levels:
        return None

    remaining = intent.quantity
    spent = Decimal(0)
    for level in levels:
        price = _decimal(level[0])
        size = _decimal(level[1])
        if price is None or size is None or price <= 0 or size <= 0:
            continue
        take = min(size, remaining)
        spent += take * price
        remaining -= take
        if remaining <= 0:
            filled = intent.quantity - remaining
            return spent / filled if filled > 0 else None
    return None


def _translate(exc: BaseException, venue: str, subject: str) -> NoReturn:
    """Map ccxt's exception taxonomy onto `VenueUnavailable`.

    Same shape as `ingest/exchanges.py::_translate` and same reason: the
    trading loop records a refusal against the intent, so a venue's own
    complaint has to arrive as one exception type carrying the venue's words.
    ccxt is imported here rather than at module scope; by the time this runs a
    ccxt call has already raised, so ccxt is necessarily installed.
    """
    import ccxt

    if isinstance(exc, ccxt.BadSymbol):
        raise VenueUnavailable(f"{venue} does not list {subject}: {exc}") from exc
    if isinstance(exc, ccxt.NetworkError):
        raise VenueUnavailable(f"{venue} unavailable for {subject}: {exc}") from exc
    if isinstance(exc, ccxt.InsufficientFunds):
        raise VenueUnavailable(
            f"{venue} refused {subject} for insufficient funds: {exc}"
        ) from exc
    if isinstance(exc, ccxt.ExchangeError):
        raise VenueUnavailable(f"{venue} rejected {subject}: {exc}") from exc
    raise exc


def _is_ambiguous_placement(exc: BaseException) -> bool:
    """Did this failure leave the order's existence unknown.

    Every `NetworkError` does. A timeout is the obvious case, but a connection
    reset or a DNS failure mid-flight is the same fact: the request may have
    been executed and the acknowledgement lost. Treating only `RequestTimeout`
    as ambiguous would leave the rest to be reported as clean failures, and a
    clean failure invites a resubmission.
    """
    import ccxt

    return isinstance(exc, ccxt.NetworkError)


def _is_missing_order(exc: BaseException) -> bool:
    import ccxt

    return isinstance(exc, ccxt.OrderNotFound)


def _is_duplicate_id(exc: BaseException) -> bool:
    import ccxt

    return isinstance(exc, ccxt.DuplicateOrderId)


def _reported_fee(order: dict[str, Any]) -> tuple[Decimal | None, str | None]:
    fee = order.get("fee")
    if isinstance(fee, dict):
        cost = _decimal(fee.get("cost"))
        if cost is not None:
            return cost, _text(fee.get("currency"))
    fees = order.get("fees")
    if isinstance(fees, list):
        total = Decimal(0)
        currency: str | None = None
        seen = False
        for entry in fees:
            if not isinstance(entry, dict):
                continue
            cost = _decimal(entry.get("cost"))
            if cost is None:
                continue
            total += cost
            seen = True
            if currency is None:
                currency = _text(entry.get("currency"))
        if seen:
            return total, currency
    return None, None


def _average_price(order: dict[str, Any], filled: Decimal) -> Decimal | None:
    """The price the venue says it filled at, or None.

    Three sources in falling order of directness: the reported average, the
    executed cost divided by the executed quantity (arithmetic on the venue's
    own numbers, not a model), and finally the order's own price. None of them
    is a guess; if all three are absent the caller raises rather than pricing a
    real fill from the intent it was asked to place.
    """
    average = _decimal(order.get("average"))
    if average is not None and average > 0:
        return average
    cost = _decimal(order.get("cost"))
    if cost is not None and cost > 0 and filled > 0:
        return cost / filled
    price = _decimal(order.get("price"))
    if price is not None and price > 0:
        return price
    return None


def _derive_capabilities(
    exchange: Any, markets: Sequence[Any], venue: str
) -> Capabilities:
    """Read the venue's own declaration instead of asserting one.

    Market-type support comes from the markets in scope rather than from `has`,
    because `has['margin']` says the venue offers margin somewhere while the
    markets say whether the symbols being traded do. Fees come from ccxt's
    published schedule for the venue: that is the default tier, not this
    account's tier, so a discounted account is charged too much in the cost
    model and refuses marginal trades -- the safe direction. A venue that
    publishes no fee at all raises, because a fee of zero is a fabricated
    number and every expectancy downstream is measured against it.
    """
    has = getattr(exchange, "has", None) or {}
    entries = [m for m in markets if isinstance(m, dict)]

    spot = any(bool(m.get("spot")) for m in entries)
    margin = any(bool(m.get("margin")) for m in entries)
    swap = any(bool(m.get("swap")) for m in entries)

    trading = (getattr(exchange, "fees", None) or {}).get("trading")
    trading = trading if isinstance(trading, dict) else {}
    maker = _decimal(trading.get("maker"))
    taker = _decimal(trading.get("taker"))
    perp_maker = None
    perp_taker = None
    if maker is None or taker is None:
        # Not every venue publishes a schedule at the exchange level. Hyperliquid
        # reports `fees.trading` as all None and prices each market instead --
        # spot at 4/7 bps against perpetuals at 1.5/4.5 -- so reading only the
        # top-level default refuses a venue that in fact states its fees.
        #
        # The spot fees are the max across spot markets; perp fees the max
        # across swap markets. Keeping them separate stops a carry pair (2 spot
        # legs, 2 perp legs) from being charged at the spot rate on every leg --
        # a 5 bps overcharge that refused marginal trades the edge survives.
        spot_entries = [m for m in entries if m.get("spot")]
        perp_entries = [m for m in entries if m.get("swap")]
        spot_maker = [f for f in (_decimal(m.get("maker")) for m in spot_entries) if f is not None]
        spot_taker = [f for f in (_decimal(m.get("taker")) for m in spot_entries) if f is not None]
        perp_maker_list = [f for f in (_decimal(m.get("maker")) for m in perp_entries) if f is not None]
        perp_taker_list = [f for f in (_decimal(m.get("taker")) for m in perp_entries) if f is not None]

        maker = max(spot_maker) if spot_maker else (max(perp_maker_list) if perp_maker_list else maker)
        taker = max(spot_taker) if spot_taker else (max(perp_taker_list) if perp_taker_list else taker)

        if perp_entries and perp_maker_list:
            perp_maker = max(perp_maker_list)
        if perp_entries and perp_taker_list:
            perp_taker = max(perp_taker_list)
    if maker is None or taker is None:
        raise ValueError(
            f"{venue} publishes no usable trading fee (maker={trading.get('maker')!r} "
            f"taker={trading.get('taker')!r}) at the venue level or on any of its "
            f"{len(entries)} markets in scope; refusing to assume one, because a "
            f"fee this tier invents is a fee no strategy is measured against"
        )

    minimums = [m for m in (_limit(entry, "cost") for entry in entries) if m is not None]

    return Capabilities(
        spot=spot,
        margin=margin,
        perpetuals=swap,
        limit_orders=bool(has.get("createLimitOrder", has.get("createOrder"))),
        # A perpetual or a margin market is short-able by construction; a
        # spot-only venue is not, and `Capabilities` refuses the combination
        # that would let a carry leg be planned somewhere it cannot open.
        shorting=swap or margin,
        funding_data=bool(
            has.get("fetchFundingRate")
            or has.get("fetchFundingRates")
            or has.get("fetchFundingRateHistory")
        ),
        maker_fee_bps=maker * BPS,
        taker_fee_bps=taker * BPS,
        perp_maker_fee_bps=(perp_maker * BPS) if perp_maker is not None else None,
        perp_taker_fee_bps=(perp_taker * BPS) if perp_taker is not None else None,
        min_notional=max(minimums) if minimums else Decimal(0),
    )


def _rejected_fill(
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


def _venue_cloid(key: str) -> str:
    """Deterministic 128-bit hex cloid for Hyperliquid from any idempotency key.

    Hyperliquid requires a client order id as 0x-prefixed 128-bit hex. The
    carry loop's idempotency key is a descriptive string (portfolio:carry:
    timestamp:symbol:...) for traceability; a sha256 prefix of it is
    deterministic -- same key always maps to the same cloid, so the venue's
    duplicate-rejection still works -- and wire-valid. The 128-bit truncation
    leaves a collision space of 2^128, effectively zero for the thousands of
    orders this book will ever place.
    """
    return "0x" + hashlib.sha256(key.encode()).hexdigest()[:32]


class CCXTVenue:
    """A live centralised exchange, read-only until told otherwise.

    The exchange object is injected. `connect()` builds a real ccxt client and
    loads its markets; tests pass a fake with the same surface and never touch
    the network. Markets must already be loaded at construction, because both
    guards that stand between an intent and the matching engine -- the
    per-symbol minimum and the per-symbol precision -- live in that metadata,
    and a venue that cannot apply them has no business submitting orders.
    """

    def __init__(
        self,
        exchange: Any,
        *,
        mode: TradingMode = TradingMode.READ_ONLY,
        symbols: Sequence[str] | None = None,
        quote_asset: str | None = None,
        name: str | None = None,
        owns_exchange: bool = False,
    ) -> None:
        markets = getattr(exchange, "markets", None)
        if not isinstance(markets, dict) or not markets:
            raise ValueError(
                "CCXTVenue needs an exchange with its markets already loaded: "
                "the minimum notional and the amount precision are per symbol "
                "and are read from that metadata. Use CCXTVenue.connect(), "
                "which awaits load_markets() first"
            )
        if not isinstance(mode, TradingMode):
            raise TypeError(
                f"mode must be a TradingMode, got {mode!r}; a truthy value is "
                f"not an opt-in to trading"
            )

        self.name = name or _text(getattr(exchange, "id", None)) or "ccxt"
        self._exchange = exchange
        self._mode = mode
        self._owns_exchange = owns_exchange
        self._markets = markets
        self._quote_asset = quote_asset.strip() if quote_asset else None

        if symbols is None:
            self._symbols: list[str] | None = None
            scope: list[Any] = list(markets.values())
        else:
            missing = [symbol for symbol in symbols if symbol not in markets]
            if missing:
                raise ValueError(
                    f"{self.name} does not list {missing}; a symbol that is not "
                    f"a market has no minimum and no precision to trade against"
                )
            self._symbols = list(symbols)
            scope = [markets[symbol] for symbol in self._symbols]

        self.capabilities = _derive_capabilities(exchange, scope, self.name)
        self._order_symbols: dict[str, str] = {}

    @classmethod
    async def connect(
        cls,
        *,
        venue: str,
        api_key: str | None = None,
        api_secret: str | None = None,
        credentials: TradingCredentials | WalletCredentials | None = None,
        mode: TradingMode = TradingMode.READ_ONLY,
        symbols: Sequence[str] | None = None,
        quote_asset: str | None = None,
    ) -> CCXTVenue:
        import ccxt.async_support as ccxt_async

        try:
            exchange_cls = getattr(ccxt_async, venue)
        except AttributeError as exc:
            raise VenueUnavailable(f"ccxt has no venue named {venue!r}") from exc

        # defaultSlippage: Hyperliquid spot market orders require a price to
        # derive a max-slippage price, and reject a market order with none
        # ("market orders require price to calculate the max slippage price").
        # Setting it lets ccxt synthesise that price for any spot market order.
        # Other venues ignore the option.
        options: dict[str, Any] = {"enableRateLimit": True, "defaultSlippage": 0.05}
        # `credentials` carries its own ccxt shape, which is what lets this stay
        # generic: a key/secret venue and a wallet venue differ in what they
        # authenticate with, and neither this method nor its callers should have
        # to know which is which.
        if credentials is not None:
            if api_key or api_secret:
                raise ValueError(
                    "pass credentials or api_key/api_secret, not both; two "
                    "sources for one field is a silent precedence question"
                )
            options.update(credentials.ccxt_options())
        if api_key:
            options["apiKey"] = api_key
        if api_secret:
            options["secret"] = api_secret
        exchange = exchange_cls(options)

        try:
            await exchange.load_markets()
        except Exception as exc:
            await exchange.close()
            _translate(exc, venue, "load_markets")
            raise
        try:
            return cls(
                exchange,
                mode=mode,
                symbols=symbols,
                name=venue,
                owns_exchange=True,
                quote_asset=quote_asset,
            )
        except Exception:
            # The constructor validates -- an unpriced venue raises here, after
            # load_markets succeeded. Without this the session leaks: nothing
            # owns the exchange yet, so nobody is left to close it, and the
            # traceback arrives buried under aiohttp's unclosed-connector
            # warning.
            await exchange.close()
            raise

    @property
    def mode(self) -> TradingMode:
        return self._mode

    def _client_order_id(self, key: str) -> str:
        """The idempotency key in the venue's own client-order-id format.

        Hyperliquid requires 0x-prefixed 128-bit hex; a descriptive key with
        colons and timestamps fails its EIP-712 signing path. Other venues
        accept the key as-is.
        """
        if self.name == "hyperliquid":
            return _venue_cloid(key)
        return key

    async def aclose(self) -> None:
        if self._owns_exchange:
            await self._exchange.close()

    def symbol_for(self, asset: str, market_type: MarketType) -> str | None:
        """The symbol this exchange lists for an asset, or `None` if it lists none.

        Resolved from the exchange's OWN market metadata rather than composed
        from a template. A template would be a guess, and the guess is wrong in
        both directions here: ccxt spells the perpetual `BTC/USDT:USDT` and the
        spot market `BTC/USDT`, the quote asset differs per exchange and per
        listing, and an asset delisted yesterday still formats perfectly. The
        markets dict is the only thing that knows what is actually tradable, and
        it is already loaded.

        The market's own `base`, `quote` and type flags are matched rather than
        the symbol string parsed, because the string is a rendering of those
        fields and not their definition.

        The QUOTE ASSET is stated on the venue, never inferred. Binance lists
        BTC against ARS, AUD, BRL, USDT and more; the first implementation of
        this took whichever sorted first and returned `BTC/ARS` for spot and
        `BTC/U:U` for the perpetual -- deterministic, and stably wrong, which is
        worse than unstable because it looks reliable. Which quote a book trades
        is a real decision (liquidity, depeg risk, what the funding stream is
        actually denominated in) and belongs to the caller.

        `None` when the asset is not listed, which is routine: a 30-name universe
        against one exchange always contains names it does not carry, and a
        caller skips them. An asset already carrying a `/` is treated as a symbol
        and confirmed against the markets, so a caller that resolved elsewhere
        gets a check rather than a bypass.
        """
        asset = asset.strip()
        if not asset:
            return None
        if "/" in asset:
            market = self._markets.get(asset)
            if not isinstance(market, dict):
                return None
            return asset if self._is_market_type(market, market_type) else None

        if self._quote_asset is None:
            raise ValueError(
                f"{self.name} cannot resolve {asset!r} without a quote asset. "
                f"Binance lists BTC against ARS, AUD, BRL, USDT and a dozen "
                f"more; picking one of them here would be a stable, silent, "
                f"wrong choice of market. Construct the venue with "
                f"quote_asset='USDT' (or whichever you mean to trade)"
            )
        matches = sorted(
            symbol
            for symbol, market in self._markets.items()
            if isinstance(market, dict)
            and isinstance(symbol, str)
            and market.get("base") == asset
            and market.get("quote") == self._quote_asset
            and market.get("active") is not False
            and self._is_market_type(market, market_type)
        )
        # Sorted for stability rather than preference: with base, quote, type
        # and active all pinned, anything still matching is an equivalent
        # listing, and an arbitrary-but-stable pick is fine where an unstable
        # one would have the book holding one market today and another tomorrow.
        return matches[0] if matches else None

    def held_symbol_aliases(
        self, asset: str, market_type: MarketType
    ) -> tuple[str, ...]:
        """Spellings under which a HELD position in this leg may be recorded.

        ccxt addresses orders by unified symbol, but a venue's own fills and
        balances can speak a native name for the same market. On Hyperliquid
        the wrapped spot tokens report under their wrapped coin name -- a fill
        on ETH/USDC reports as `UETH/USDC` in `userFills` -- and a book whose
        ledger was rebuilt from venue-side fills (the 2026-08-18 repair) keys
        the spot leg by that spelling. A pairing map that recognises only the
        tradable spelling reads the hedged book as unpaired legs and halts on
        it (carry halt 2026-08-19).

        These are DECLARED per symbol rather than derived: ccxt normalises the
        wrapped name out of its market metadata entirely (baseId is a numeric
        token id), and a `U{base}` template would be a guess wearing the
        uniform of a derivation -- Hyperliquid also lists unwrapped spot
        names, so the prefix is not a rule. Each entry is a measured mapping.
        """
        resolved = self.symbol_for(asset, market_type)
        if resolved is None:
            return ()
        return _HELD_SYMBOL_ALIASES.get(resolved, ())

    def spot_holding_asset(self, symbol_or_asset: str) -> str | None:
        """Reverse of the alias map, plus the bare-asset case.

        A venue balance named `ETH` is a holding of ETH; a wrapped position
        row named `UETH/USDC` is a holding of ETH because the declared alias
        table says that spelling is ETH/USDC's held name. Everything else --
        perpetual spellings, unknown symbols -- is None: not a spot holding
        this venue can name. Like the alias table, derived only from declared,
        measured entries.
        """
        stripped = symbol_or_asset.strip()
        if not stripped:
            return None
        if "/" not in stripped:
            return stripped
        for unified, aliases in _HELD_SYMBOL_ALIASES.items():
            if stripped in aliases:
                return unified.partition("/")[0]
        market = self._markets.get(stripped)
        if isinstance(market, dict) and market.get("spot"):
            return market.get("base")
        return None

    @staticmethod
    def _is_market_type(market: dict[str, Any], market_type: MarketType) -> bool:
        """Whether a ccxt market IS the requested instrument.

        ccxt marks a perpetual as `swap` with `linear`/`inverse` set, and dated
        futures as `future`; a dated future is not a perpetual and must not
        answer for one, because it expires and a carry book does not roll.
        """
        if market_type is MarketType.PERPETUAL:
            return bool(market.get("swap"))
        if market_type is MarketType.SPOT:
            return bool(market.get("spot"))
        return bool(market.get("margin"))

    def min_notional_for(self, symbol: str) -> Decimal | None:
        """The venue's own minimum order cost for this symbol.

        None means the venue declares none for that market -- not that there is
        none to worry about, which is why the caller never substitutes zero.
        """
        return _limit(self._market(symbol), "cost")

    def _market(self, symbol: str) -> dict[str, Any]:
        market = self._markets.get(symbol)
        if not isinstance(market, dict):
            raise VenueUnavailable(f"{self.name} does not list {symbol}")
        return market

    def _require_market_type(self, market: dict[str, Any], intent: TradeIntent) -> None:
        """The symbol has to BE the market type the intent asked for.

        `BTC/USDT` and `BTC/USDT:USDT` differ by four characters and by whether
        the order opens a spot holding or a leveraged perpetual. A carry book
        holds both at once, so the pair is one transposition apart at all times,
        and the exchange will happily fill whichever one it was sent.
        """
        flag = _MARKET_FLAG[intent.market_type]
        if not market.get(flag):
            actual = _text(market.get("type")) or "unknown"
            raise VenueUnavailable(
                f"{intent.symbol} is a {actual} market at {self.name}, not "
                f"{intent.market_type.value}; refusing to trade a different "
                f"instrument from the one the intent named"
            )

    def _rounded_amount(self, symbol: str, quantity: Decimal) -> Decimal:
        """Round the size the way the venue will, before it is sent.

        An unrounded amount is rejected outright by the exchange, so this is not
        cosmetic. It rounds DOWN through ccxt's own precision rules for this
        market, which is why the minimum-notional check has to come after it:
        rounding can drop an order that was above the minimum below it.
        """
        try:
            rendered = self._exchange.amount_to_precision(symbol, str(quantity))
        except ArithmeticError as exc:
            raise VenueUnavailable(
                f"{quantity} {symbol} rounds away to nothing at {self.name}'s "
                f"amount precision: {exc}"
            ) from exc
        rounded = _decimal(rendered)
        if rounded is None:
            raise VenueUnavailable(
                f"{self.name} returned an unusable rounded amount {rendered!r} "
                f"for {quantity} {symbol}"
            )
        if rounded <= 0:
            raise VenueUnavailable(
                f"{quantity} {symbol} rounds to {rounded} at {self.name}'s "
                f"amount precision; there is no order this size to place"
            )
        return rounded

    def _rounded_price(self, symbol: str, price: Decimal) -> Decimal:
        try:
            rendered = self._exchange.price_to_precision(symbol, str(price))
        except ArithmeticError as exc:
            raise VenueUnavailable(
                f"{price} for {symbol} is not expressible at {self.name}'s "
                f"price precision: {exc}"
            ) from exc
        rounded = _decimal(rendered)
        if rounded is None or rounded <= 0:
            raise VenueUnavailable(
                f"{self.name} returned an unusable rounded price {rendered!r} "
                f"for {price} {symbol}"
            )
        return rounded

    async def quote(self, intent: TradeIntent) -> Quote:
        market = self._market(intent.symbol)
        self._require_market_type(market, intent)

        # ONE book fetch, reused for both jobs it can do: its top two levels are
        # the same bid/ask a ticker publishes, and its depth is what prices a
        # market order. Fetching a ticker and then a book doubled every quote's
        # API cost for no information -- the affordability filter makes eight
        # quotes per asset, which was 96 calls a pass against a venue that
        # rate-limits.
        #
        # The ticker remains the fallback rather than the primary, because a
        # venue can serve one and not the other: Hyperliquid publishes bid/ask
        # on its perpetuals and leaves both None on SPOT, so a quote taken from
        # the ticker alone cannot price half of any cash-and-carry pair here.
        book: dict | None = None
        ticker: dict | None = None
        try:
            book = await self._exchange.fetch_order_book(intent.symbol, limit=BOOK_DEPTH)
        except Exception:  # noqa: BLE001 - the ticker is still to be tried
            book = None

        bid = ask = None
        if isinstance(book, dict):
            bids = book.get("bids") or []
            asks = book.get("asks") or []
            bid = _decimal(bids[0][0]) if bids else None
            ask = _decimal(asks[0][0]) if asks else None

        if bid is None or ask is None or bid <= 0 or ask <= 0:
            try:
                ticker = await self._exchange.fetch_ticker(intent.symbol)
            except Exception as exc:
                _translate(exc, self.name, intent.symbol)
                raise
            if not isinstance(ticker, dict):
                raise VenueUnavailable(
                    f"{self.name} returned {type(ticker).__name__} for a "
                    f"{intent.symbol} ticker"
                )
            bid = _decimal(ticker.get("bid"))
            ask = _decimal(ticker.get("ask"))

        if bid is None or ask is None or bid <= 0 or ask <= 0:
            raise VenueUnavailable(
                f"{self.name} published no two-sided market for {intent.symbol} "
                f"in either its ticker or its order book; a cost quoted without "
                f"a spread is a cost with the spread left out"
            )
        if ask < bid:
            raise VenueUnavailable(
                f"{self.name} published a crossed book for {intent.symbol}: "
                f"bid {bid} above ask {ask}"
            )

        mid = (bid + ask) / 2
        if intent.order_kind is OrderKind.LIMIT:
            assert intent.limit_price is not None  # TradeIntent.__post_init__
            expected = intent.limit_price
        else:
            # A market order takes the touch only if the touch is big enough to
            # fill it. Quoting the top of book for any size charges a large
            # order the small order's price, and understates it by exactly the
            # amount that matters on a thin name: PURR modelled at 83 bps from
            # the touch and cost 121 bps when the book was walked, on $70.
            #
            # `_walk` returns None when the visible book cannot fill the size at
            # all, and the touch is then the honest fallback -- an unfillable
            # order is a different refusal, made by `execute`, and inventing a
            # worse price here would be a cost model guessing at depth it cannot
            # see rather than reporting what it can.
            expected = _walk_book(book, intent) or (
                ask if intent.side is Side.BUY else bid
            )

        adverse = expected - mid if intent.side is Side.BUY else mid - expected
        # A limit resting inside the mid is price improvement, not slippage.
        # `Quote` forbids a negative component, and reporting the improvement as
        # a positive cost would be worse than dropping it.
        slippage = max(Decimal(0), adverse) * intent.quantity

        # Taker on both order kinds, as `PaperVenue.quote` charges it: a limit
        # order is not guaranteed to rest, and a maker assumption understates
        # the cost of exactly the orders that cross.
        fee = expected * intent.quantity * self.capabilities.taker_fee_bps / BPS

        return Quote(
            intent=intent,
            expected_price=expected,
            fee=fee,
            slippage=slippage,
            gas=Decimal(0),
            # Whichever source actually answered. The book carries its own
            # timestamp and is the primary read now, so preferring the
            # ticker's would stamp most quotes from an endpoint that was
            # never called.
            as_of=(
                _moment((book or {}).get("timestamp"))
                or _moment((ticker or {}).get("timestamp"))
                or datetime.now(UTC)
            ),
        )

    async def execute(self, intent: TradeIntent) -> Fill:
        if self._mode is not TradingMode.LIVE:
            raise VenueUnavailable(
                f"{self.name} is in {self._mode.value} mode and will not place "
                f"orders; construct it with mode=TradingMode.LIVE to trade "
                f"real capital"
            )
        if not self.capabilities.supports(intent.market_type):
            raise VenueUnavailable(
                f"{self.name} does not support {intent.market_type.value} for "
                f"the markets in scope"
            )
        if intent.order_kind is OrderKind.LIMIT and not self.capabilities.limit_orders:
            raise VenueUnavailable(f"{self.name} does not accept limit orders")

        market = self._market(intent.symbol)
        self._require_market_type(market, intent)

        amount = self._rounded_amount(intent.symbol, intent.quantity)
        price: Decimal | None = None
        eff_kind = intent.order_kind.value
        if intent.order_kind is OrderKind.LIMIT:
            assert intent.limit_price is not None  # TradeIntent.__post_init__
            price = self._rounded_price(intent.symbol, intent.limit_price)
        else:
            # Hyperliquid (ccxt 4.5.x) rejects spot MARKET orders with "market
            # orders require price to calculate the max slippage price" and
            # ignores both the defaultSlippage option and an explicit price on
            # the market path. Its LIMIT path works. So a market intent becomes
            # an aggressive LIMIT at reference +/- 5% (the venue's own default
            # slippage): it crosses the book and fills like a market order, on
            # the working limit path, with a hard worst-price bound. The
            # pair-integrity guard still catches any pathological fill.
            slippage = Decimal("0.05")
            ref = intent.reference_price
            raw = ref * (Decimal(1) + slippage) if intent.side is Side.BUY else ref * (Decimal(1) - slippage)
            price = self._rounded_price(intent.symbol, raw)
            eff_kind = OrderKind.LIMIT.value

        notional = amount * (price if price is not None else intent.reference_price)
        minimum = self.min_notional_for(intent.symbol)
        if minimum is not None and notional < minimum:
            return _rejected_fill(
                intent,
                self.name,
                datetime.now(UTC),
                reason=(
                    f"{intent.symbol} notional {notional} is below {self.name}'s "
                    f"minimum {minimum} for that market"
                ),
            )

        params: dict[str, Any] = {
            CLIENT_ORDER_ID_PARAM: self._client_order_id(intent.idempotency_key)
        }
        if intent.reduce_only and intent.market_type is not MarketType.SPOT:
            params["reduceOnly"] = True

        try:
            order = await self._exchange.create_order(
                intent.symbol,
                eff_kind,
                intent.side.value,
                float(amount),
                price=float(price) if price is not None else None,
                params=params,
            )
        except Exception as exc:
            if _is_ambiguous_placement(exc) or _is_duplicate_id(exc):
                return await self._resolve_placement(intent, amount, exc)
            _translate(exc, self.name, intent.symbol)
            raise

        return self._fill_from_order(order, intent, submitted=amount)

    async def _resolve_placement(
        self, intent: TradeIntent, submitted: Decimal, cause: BaseException
    ) -> Fill:
        """Ask the venue what happened, once. Never resubmit.

        Reached from two places, and they are the same question: a placement
        whose response was lost, and a placement the venue refused because this
        idempotency key had already been used. In both cases an order carrying
        `intent.idempotency_key` may exist, and the only honest way to find out
        is to read it back by that id.

        Nothing here retries `create_order`. A blind retry is how one intent
        becomes two positions, and the client order id is the only thing
        standing between a timeout and a double fill.
        """
        key = intent.idempotency_key
        fetch_order = getattr(self._exchange, "fetch_order", None)
        if fetch_order is None or not (getattr(self._exchange, "has", None) or {}).get(
            "fetchOrder"
        ):
            raise VenueUnavailable(
                f"{self.name} did not answer the {intent.symbol} order carrying "
                f"client order id {key} ({cause}), and exposes no fetch_order to "
                f"establish whether it exists. Nothing was retried. A "
                f"resubmission must reuse idempotency key {key}"
            ) from cause

        try:
            order = await fetch_order(
                None,
                intent.symbol,
                {CLIENT_ORDER_ID_PARAM: self._client_order_id(key)},
            )
        except Exception as probe:
            if _is_missing_order(probe):
                raise VenueUnavailable(
                    f"{self.name} does not know client order id {key} for "
                    f"{intent.symbol} after the placement failed ({cause}), so "
                    f"the order most likely never reached the book -- but that "
                    f"is inference, not observation, and no fill is reported "
                    f"from it. Nothing was retried. A resubmission must reuse "
                    f"idempotency key {key}"
                ) from probe
            raise VenueUnavailable(
                f"{self.name} could not be asked what became of client order id "
                f"{key} for {intent.symbol} after the placement failed "
                f"({cause}): {probe}. Nothing was retried. A resubmission must "
                f"reuse idempotency key {key}"
            ) from probe

        return self._fill_from_order(
            order, intent, submitted=submitted, recovered_from=str(cause)
        )

    def _fill_from_order(
        self,
        order: Any,
        intent: TradeIntent,
        *,
        submitted: Decimal,
        recovered_from: str | None = None,
    ) -> Fill:
        """Turn the venue's order into a `Fill`, or refuse to.

        The refusal is the point. Three states are distinguishable and each has
        an honest answer:

        - a reported execution with a usable price -- a `Fill`, partial or not;
        - a finished order that filled nothing, or a live one that has not
          filled yet -- an empty `Fill`, which asserts no position and carries
          the external id so the caller can cancel or poll it;
        - anything else -- `VenueUnavailable`. An order with no readable filled
          quantity, or an execution with no readable price, is the venue
          declining to say what happened, and the one answer that must never be
          given to that is a zero-quantity fill, which reads downstream as "the
          venue declined" and licenses a resubmission against a position that
          may already exist.
        """
        key = intent.idempotency_key
        if not isinstance(order, dict):
            raise VenueUnavailable(
                f"{self.name} returned {type(order).__name__} rather than an "
                f"order for {intent.symbol} (client order id {key}); what "
                f"happened to it is unknown"
            )

        reported_status = _text(order.get("status"))
        status = reported_status.lower() if reported_status is not None else None
        external_id = _text(order.get("id")) or _text(order.get("clientOrderId"))
        at = (
            _moment(order.get("lastTradeTimestamp"))
            or _moment(order.get("timestamp"))
            or datetime.now(UTC)
        )
        if external_id is not None:
            self._order_symbols[external_id] = intent.symbol

        filled = _decimal(order.get("filled"))
        if filled is None or filled < 0:
            raise VenueUnavailable(
                f"{self.name} reported filled={order.get('filled')!r} for "
                f"{intent.symbol} (client order id {key}, external id "
                f"{external_id}, status {reported_status!r}); refusing to report "
                f"a fill the venue did not describe"
            )

        raw: dict[str, Any] = {
            "status": reported_status,
            "client_order_id": key,
            "external_id": external_id,
            "requested_quantity": str(intent.quantity),
            "submitted_quantity": str(submitted),
        }
        if recovered_from is not None:
            raw["recovered_from"] = recovered_from

        if filled > 0:
            average = _average_price(order, filled)
            if average is None:
                raise VenueUnavailable(
                    f"{self.name} filled {filled} of {intent.symbol} (client "
                    f"order id {key}, external id {external_id}) and reported no "
                    f"usable price; a fill priced from the intent is a "
                    f"fabricated P&L"
                )
            fee, currency = _reported_fee(order)
            if fee is None:
                # The venue acknowledged the execution but not its fee. The
                # position is real, so refusing the fill would lose it; the
                # charge is filled in from the venue's own published taker rate
                # and flagged, never left at zero, which would understate every
                # cost measured against this trade.
                fee = average * filled * self.capabilities.taker_fee_bps / BPS
                raw["fee_estimated"] = True
            elif fee < 0:
                # A rebate. `Fill` forbids a negative fee_paid, so the credit is
                # recorded rather than booked.
                raw["fee_rebate"] = str(-fee)
                fee = Decimal(0)
            if currency is not None:
                raw["fee_currency"] = currency
            raw["partial"] = filled < submitted
            return Fill(
                intent_id=key,
                venue=self.name,
                symbol=intent.symbol,
                side=intent.side,
                filled_quantity=filled,
                average_price=average,
                fee_paid=fee,
                filled_at=at,
                external_id=external_id,
                raw=raw,
            )

        if status in TERMINAL_UNFILLED:
            raw["rejected"] = (
                f"{self.name} returned status {reported_status} with nothing filled"
            )
            return Fill(
                intent_id=key,
                venue=self.name,
                symbol=intent.symbol,
                side=intent.side,
                filled_quantity=Decimal(0),
                average_price=Decimal(0),
                fee_paid=Decimal(0),
                filled_at=at,
                external_id=external_id,
                raw=raw,
            )

        if status in RESTING:
            raw["resting"] = True
            return Fill(
                intent_id=key,
                venue=self.name,
                symbol=intent.symbol,
                side=intent.side,
                filled_quantity=Decimal(0),
                average_price=Decimal(0),
                fee_paid=Decimal(0),
                filled_at=at,
                external_id=external_id,
                raw=raw,
            )

        raise VenueUnavailable(
            f"{self.name} reported status {reported_status!r} with nothing filled "
            f"for {intent.symbol} (client order id {key}, external id "
            f"{external_id}); whether that order is live cannot be told from "
            f"this response, so no fill is reported for it"
        )

    async def positions(self) -> list[Position]:
        if not self.capabilities.perpetuals:
            return []
        if not (getattr(self._exchange, "has", None) or {}).get("fetchPositions"):
            raise VenueUnavailable(
                f"{self.name} lists perpetual markets but exposes no "
                f"fetch_positions; open exposure cannot be read back, and "
                f"reporting none would report being flat"
            )
        try:
            reported = await self._exchange.fetch_positions(self._symbols)
        except Exception as exc:
            _translate(exc, self.name, "positions")
            raise

        positions: list[Position] = []
        for entry in reported or []:
            position = self._position_from(entry)
            if position is not None:
                positions.append(position)
        return positions

    def _position_from(self, entry: Any) -> Position | None:
        """One ccxt position, or None if it is flat.

        A position that cannot be described honestly raises rather than being
        skipped. Dropping it would under-report real exposure, and every
        reconciliation after that compares a book against a venue it has
        already been told to ignore.
        """
        if not isinstance(entry, dict):
            raise VenueUnavailable(
                f"{self.name} returned {type(entry).__name__} in its positions"
            )
        symbol = _text(entry.get("symbol"))
        if symbol is None:
            raise VenueUnavailable(f"{self.name} reported a position with no symbol")

        contracts = _decimal(entry.get("contracts"))
        if contracts is None:
            raise VenueUnavailable(
                f"{self.name} reported contracts={entry.get('contracts')!r} for "
                f"{symbol}; a position of unknown size cannot be reconciled"
            )
        if contracts.is_zero():
            return None

        side = _text(entry.get("side"))
        if side not in ("long", "short"):
            raise VenueUnavailable(
                f"{self.name} reported side={entry.get('side')!r} for a "
                f"{contracts} position in {symbol}; its direction is what makes "
                f"it a hedge or a double-up"
            )
        quantity = abs(contracts) if side == "long" else -abs(contracts)

        entry_price = _decimal(entry.get("entryPrice"))
        if entry_price is None or entry_price <= 0:
            raise VenueUnavailable(
                f"{self.name} reported entryPrice={entry.get('entryPrice')!r} "
                f"for an open {quantity} position in {symbol}; an invented "
                f"entry is an invented P&L"
            )

        market = self._markets.get(symbol)
        market_type = MarketType.PERPETUAL
        if isinstance(market, dict) and not market.get("swap") and market.get("margin"):
            market_type = MarketType.MARGIN

        return Position(
            venue=self.name,
            symbol=symbol,
            market_type=market_type,
            quantity=quantity,
            average_entry=entry_price,
            as_of=_moment(entry.get("timestamp")) or datetime.now(UTC),
        )

    async def balances(self) -> list[Balance]:
        try:
            reported = await self._exchange.fetch_balance()
        except Exception as exc:
            _translate(exc, self.name, "balances")
            raise

        if not isinstance(reported, dict):
            raise VenueUnavailable(
                f"{self.name} returned {type(reported).__name__} for balances"
            )
        totals = reported.get("total")
        if not isinstance(totals, dict):
            raise VenueUnavailable(
                f"{self.name} returned no per-asset totals in its balance"
            )
        as_of = _moment(reported.get("timestamp")) or datetime.now(UTC)

        balances: list[Balance] = []
        for asset in totals:
            holding = reported.get(asset)
            if not isinstance(holding, dict):
                raise VenueUnavailable(
                    f"{self.name} reported a total for {asset} with no free/used "
                    f"breakdown; dropping it would report holding less than the "
                    f"venue says we hold"
                )
            free = _decimal(holding.get("free"))
            locked = _decimal(holding.get("used"))
            if free is None or locked is None:
                raise VenueUnavailable(
                    f"{self.name} reported free={holding.get('free')!r} "
                    f"used={holding.get('used')!r} for {asset}; a balance that "
                    f"cannot be read is not a balance of zero"
                )
            if free < 0 or locked < 0:
                raise VenueUnavailable(
                    f"{self.name} reported a negative balance for {asset} "
                    f"(free={free} used={locked}); an exchange does not report "
                    f"one, so this is a sign error and admitting it would put a "
                    f"fabricated liability into reconciliation"
                )
            if free.is_zero() and locked.is_zero():
                continue
            balances.append(
                Balance(
                    venue=self.name,
                    asset=asset,
                    free=free,
                    locked=locked,
                    as_of=as_of,
                )
            )
        return balances

    async def cancel(self, external_id: str) -> bool:
        if not (getattr(self._exchange, "has", None) or {}).get("cancelOrder"):
            return False
        symbol = self._order_symbols.get(external_id)
        if symbol is None:
            raise VenueUnavailable(
                f"{self.name} needs the symbol to cancel {external_id} and this "
                f"adapter never placed it. Returning False would report nothing "
                f"to cancel, which is exactly what an unknown live order is not"
            )
        try:
            result = await self._exchange.cancel_order(external_id, symbol)
        except Exception as exc:
            if _is_missing_order(exc):
                # Gone already: filled, or cancelled by someone else. Nothing
                # was cancelled here, and False says so.
                return False
            _translate(exc, self.name, external_id)
            raise

        if isinstance(result, dict):
            status = _text(result.get("status"))
            if status is not None and status.lower() in FILLED:
                return False
            if status is not None and status.lower() not in CANCELLED:
                raise VenueUnavailable(
                    f"{self.name} answered the cancellation of {external_id} "
                    f"with status {status!r}; that is not a cancellation and "
                    f"reporting one would leave a live order unattended"
                )
        return True
