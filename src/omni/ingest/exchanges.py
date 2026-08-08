"""Multi-venue crypto OHLCV ingestion via ccxt.

Every venue ccxt exposes is a `byo_only` source in the credential catalog
(binance already is; coinbase, kraken and the rest follow the same licence
class). An exchange's terms forbid serving its prints on to third parties, so a
claim fetched here is pinned to the credential owner and never enters shared
coverage. **The adapter does not enforce that rule** -- the writer does, from
`provider_key`. This adapter declares `provider_key = <venue>` (the venue, not
the literal "ccxt") and produces drafts; `ClaimDraft` has no audience or licence
field, so there is nowhere for an adapter to make that decision even if it
tried. Collapsing every venue onto `provider_key = "ccxt"` would hand one
venue's terms to all of them, which is why the key is per venue.

The value this adds over CoinGecko is the venue itself. CoinGecko is an
aggregate: it cannot say which exchange printed a price, so every cross-venue
strategy (basis, spread, routing) is impossible against it. A `price_snapshot`
from here carries `venue` alongside open/high/low/close/volume, and two venues'
bars for the same symbol are distinguishable -- the property cross-venue
producers depend on and the claim store already supports (two sources for one
entity is its normal case).

`event_date` is the bar's own close timestamp, and `knowledge_date` equals it.
Crypto trades continuously with no settlement lag -- the reasoning
`coingecko.py` already records -- so the bar was knowable the moment it closed.
Deriving either from `now()` would let a backtest peek at a bar that had not
closed yet.

ccxt is imported lazily inside the methods that need it, exactly as
`protocol.py::get_json` imports httpx lazily, so this module stays importable
without ccxt installed. A missing ccxt therefore raises `ImportError` from its
own call path on the live fetch path -- it never returns a success-shaped empty
result, the v1 `ibkr_integration.py` defect.
"""

from __future__ import annotations

import math
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from typing import Any

from omni.ingest.protocol import ClaimDraft, Unavailable

SOURCE = "ccxt"
CLAIM_TYPE = "price_snapshot"

# Every ccxt exception that means "the venue would not answer" maps to
# `Unavailable`. `BadSymbol` is listed separately because it is not under
# NetworkError (it sits under ExchangeError) and carries a different fact: this
# venue does not list this asset, which is not the same as the asset not having
# traded. Returning [] for it would conflate the two.
OhlcvFetcher = Callable[[str], Awaitable[list[list[Any]]]]


def _event_date(ts_ms: Any) -> datetime | None:
    if ts_ms is None:
        return None
    try:
        return datetime.fromtimestamp(ts_ms / 1000, tz=UTC)
    except (TypeError, ValueError, OSError):
        return None


def _valid_field(field: Any) -> bool:
    # None is honestly absent; NaN/inf are not real prints; a non-numeric value
    # is unparseable. A zero is none of these -- a zero-volume bar is real and
    # must pass. Never compared to zero with ==.
    if field is None:
        return False
    if not isinstance(field, (int, float)):
        return False
    return not (isinstance(field, float) and (math.isnan(field) or math.isinf(field)))


def parse_ohlcv(
    ohlcv: list[list[Any]] | None,
    *,
    symbol: str,
    venue: str,
) -> list[ClaimDraft]:
    """Flatten a ccxt ``fetch_ohlcv`` result into claim drafts.

    Each row is ``[ts_ms, open, high, low, close, volume]`` -- the close
    timestamp anchors the bitemporal pair. A row with a null or unparseable
    field is skipped, never zero-filled: zero-filling a missing close would
    invent a price. Neighbours are still emitted, so one bad bar does not erase
    the window.
    """
    drafts: list[ClaimDraft] = []
    for row in ohlcv or []:
        if not isinstance(row, (list, tuple)) or len(row) < 6:
            continue
        event_date = _event_date(row[0])
        if event_date is None:
            continue
        o, h, l, c, v = row[1], row[2], row[3], row[4], row[5]
        if not all(_valid_field(f) for f in (o, h, l, c, v)):
            continue
        drafts.append(
            ClaimDraft(
                claim_type=CLAIM_TYPE,
                key=symbol,
                value={
                    "open": o,
                    "high": h,
                    "low": l,
                    "close": c,
                    "volume": v,
                    "venue": venue,
                },
                event_date=event_date,
                knowledge_date=event_date,
                confidence=1.0,
            )
        )
    return drafts


def _translate(exc: BaseException, venue: str, symbol: str) -> None:
    """Map the ccxt exception types the work order names onto ``Unavailable``.

    ccxt is imported here, lazily, so the module imports without it installed;
    the only time this runs is when a fetch already raised, at which point ccxt
    is necessarily present on the live path. ``BadSymbol`` is matched first
    because it is not a ``NetworkError`` subclass and names the symbol.
    """
    import ccxt

    if isinstance(exc, ccxt.BadSymbol):
        raise Unavailable(f"{venue} does not list {symbol}: {exc}") from exc
    if isinstance(
        exc,
        (
            ccxt.NetworkError,
            ccxt.ExchangeNotAvailable,
            ccxt.RateLimitExceeded,
            ccxt.RequestTimeout,
        ),
    ):
        raise Unavailable(f"{venue} unavailable: {exc}") from exc
    raise exc


class CCXTAdapter:
    source = SOURCE
    provider_key: str

    def __init__(
        self,
        *,
        venue: str,
        api_key: str | None = None,
        api_secret: str | None = None,
        timeframe: str = "1d",
        fetch_fn: OhlcvFetcher | None = None,
    ) -> None:
        self.provider_key = venue
        self._venue = venue
        self._api_key = api_key
        self._api_secret = api_secret
        self._timeframe = timeframe
        self._fetch_fn = fetch_fn

    async def _default_fetch(self, symbol: str) -> list[list[Any]]:
        import ccxt.async_support as ccxt_async

        try:
            exchange_cls = getattr(ccxt_async, self._venue)
        except AttributeError as exc:
            raise Unavailable(f"ccxt has no venue named {self._venue!r}") from exc
        options: dict[str, Any] = {}
        if self._api_key:
            options["apiKey"] = self._api_key
        if self._api_secret:
            options["secret"] = self._api_secret
        exchange = exchange_cls(options)
        try:
            return await exchange.fetch_ohlcv(symbol, self._timeframe)
        finally:
            await exchange.close()

    async def fetch(self, key: str) -> list[ClaimDraft]:
        fetch_fn = self._fetch_fn if self._fetch_fn is not None else self._default_fetch
        try:
            ohlcv = await fetch_fn(key)
        except Unavailable:
            raise
        except Exception as exc:
            _translate(exc, self._venue, key)
            raise
        return parse_ohlcv(ohlcv, symbol=key, venue=self._venue)
