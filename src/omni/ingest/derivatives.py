"""Derivatives ingestion: funding rate, open interest, liquidations.

Crypto's real edge lives in derivatives, and the coverage network could not
represent a funding rate at all before this file. Three of the planned crypto
producers (`carry.funding`, `basis.crossvenue`, `oi.divergence`) read claim
types this adapter writes; without it they have nothing to compute on.

These endpoints are public and keyless on Binance, Bybit and OKX. That matters
for licensing: keyless public market data is `allowed`, so unlike CoinGecko's
`byo_only` prices these claims accumulate as shared network coverage rather than
being pinned to one user. The adapter declares `provider_key = "binance"` -- the
licence class of what it emits -- and produces drafts; `ClaimDraft` has no
audience field, so the adapter cannot and does not make the redistribution
decision. That is the writer's job, exactly as for CoinGecko and OnChain.

Routing follows `OnChainAdapter`: a `"<kind>:<symbol>"` key dispatches to one of
three parse paths, because one venue serves several claim types. An unknown kind
raises `Unavailable`, never returns `[]` -- an empty list reads as "nothing
happened in this window", which is a different and honest answer from "I do not
serve that kind at all".

**The funding sign convention is load-bearing, and it is the adapter's job to
preserve the venue's sign unchanged, not to normalise it.** Binance, Bybit and
OKX all publish the same convention: a *positive* funding rate means longs pay
shorts, a *negative* rate means shorts pay longs. `venue/costs.py::carry_cost`
prices funding in exactly that convention -- a short holding collects a credit
when the rate is positive. An adapter that took the absolute value, or flipped
the sign "for clarity", would invert the carry producer silently: the strategy
would look unprofitable precisely when it works. `fundingRate` arrives as a
signed decimal string and is parsed with `Decimal`, never `float`; the rate is
stored as its decimal string (float-free, precision-preserving) so hundreds of
settlements accumulate without representation error in the carry P&L. A zero
rate is a real, common observation and is emitted, never filtered as falsy.

Bitemporal rule: a funding rate is knowable the instant it settles, and open
interest the instant it is sampled, so `knowledge_date == event_date` for every
claim here -- the same reasoning `coingecko.py` records for a crypto tick.
Deriving either from `now()` would let a backtest read a rate that had not
settled yet. **No predicted or estimated next funding rate is emitted.** Binance
publishes a settled `fundingRate` history and a live `lastFundingRate` estimate;
only the settled series is an observation, and writing an estimate as a claim is
the fabrication AGENTS.md rule 2 forbids.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from typing import Any

from omni.ingest.protocol import ClaimDraft, Unavailable, get_json

SOURCE = "derivatives"
PROVIDER_KEY = "binance"

FUNDING_RATE = "funding_rate"
OPEN_INTEREST = "open_interest"
LIQUIDATION_EVENT = "liquidation_event"

BASE_URLS: dict[str, str] = {
    "binance": "https://fapi.binance.com",
}

FUNDING_PATH = "/fapi/v1/fundingRate"
OPEN_INTEREST_PATH = "/futures/data/openInterestHist"
LIQUIDATION_PATH = "/fapi/v1/forceOrders"

# Binance returns these codes in a 200 body when it throttles a request. A body
# carrying one is the source refusing to answer, not an empty window -- so the
# parse functions raise `Unavailable` rather than producing no claims.
RATE_LIMIT_CODES = frozenset({-1003, -1015})

DerivativesFetcher = Callable[[str], Awaitable[Any]]


def _event_date(ts_ms: Any) -> datetime | None:
    if ts_ms is None:
        return None
    try:
        return datetime.fromtimestamp(int(ts_ms) / 1000, tz=UTC)
    except (TypeError, ValueError, OSError):
        return None


def _decimal(value: Any) -> Decimal | None:
    """Parse a venue decimal string into ``Decimal`` without touching float.

    Returns ``None`` for a missing or unparseable value so the caller can skip
    the entry rather than substitute a default. ``str(value)`` is fed to
    ``Decimal`` so an int or float already coerced by a JSON layer still parses
    faithfully -- but the rate, quantity and notional fields arrive as decimal
    strings on every venue here, and the funding rate must never round through
    float.
    """
    if value is None:
        return None
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError):
        return None


def _raise_if_throttled(payload: Any, venue: str) -> None:
    if isinstance(payload, dict) and payload.get("code") in RATE_LIMIT_CODES:
        raise Unavailable(
            f"{venue} rate-limited the request: code {payload.get('code')} "
            f"({payload.get('msg', 'throttled')})"
        )


def parse_funding(
    payload: Any, *, symbol: str, venue: str
) -> list[ClaimDraft]:
    """Flatten a Binance ``GET /fapi/v1/fundingRate`` history into drafts.

    Each entry is one settled funding interval: ``fundingTime`` (ms, the
    settlement instant) and ``fundingRate`` (signed decimal string). The rate's
    sign is preserved unchanged -- positive means longs pay shorts -- because
    ``venue/costs.py`` relies on that exact convention and a flip inverts the
    carry strategy. ``markPrice`` is not a funding observation and is ignored.

    An entry missing its settlement time or carrying an unparseable rate is
    skipped, not emitted with a substituted zero; a zero rate that *did* parse
    is emitted, because zero funding is a real observation. A body carrying a
    Binance throttle code raises ``Unavailable``.
    """
    _raise_if_throttled(payload, venue)
    points = payload if isinstance(payload, list) else []

    drafts: list[ClaimDraft] = []
    for point in points:
        when = _event_date(point.get("fundingTime"))
        rate = _decimal(point.get("fundingRate"))
        if when is None or rate is None:
            continue
        drafts.append(
            ClaimDraft(
                claim_type=FUNDING_RATE,
                key=f"{venue}:{symbol}",
                value={
                    "rate": str(rate),
                    "symbol": symbol,
                    "venue": venue,
                },
                event_date=when,
                knowledge_date=when,
                confidence=1.0,
                unit="rate",
            )
        )
    return drafts


def parse_open_interest(
    payload: Any, *, symbol: str, venue: str
) -> list[ClaimDraft]:
    """Flatten a Binance open-interest history into drafts.

    Each entry carries ``sumOpenInterest`` (the contract quantity in the base
    asset) and, when the venue supplies it, ``sumOpenInterestValue`` (the
    notional in quote currency). These are kept as separate fields and neither
    is substituted for the other: a contract count and a dollar notional answer
    different questions, and an OI divergence producer reading the wrong one
    would compare units that do not match.

    A point missing its timestamp or its contract quantity is skipped; a missing
    notional is honestly absent (the field is omitted), never guessed from the
    contract count and a price.
    """
    _raise_if_throttled(payload, venue)
    points = payload if isinstance(payload, list) else []

    drafts: list[ClaimDraft] = []
    for point in points:
        when = _event_date(point.get("timestamp"))
        contracts = _decimal(point.get("sumOpenInterest"))
        if when is None or contracts is None:
            continue
        value: dict[str, Any] = {
            "contracts": str(contracts),
            "symbol": symbol,
            "venue": venue,
        }
        notional = _decimal(point.get("sumOpenInterestValue"))
        if notional is not None:
            value["notional"] = str(notional)
        drafts.append(
            ClaimDraft(
                claim_type=OPEN_INTEREST,
                key=f"{venue}:{symbol}",
                value=value,
                event_date=when,
                knowledge_date=when,
                confidence=1.0,
                unit="contracts",
            )
        )
    return drafts


def parse_liquidations(
    payload: Any, *, symbol: str, venue: str
) -> list[ClaimDraft]:
    """Flatten a Binance forced-order (liquidation) list into drafts.

    Each entry is one liquidated position: ``side`` (the direction of the forced
    order), ``origQty`` (its size) and ``time`` (ms). ``side`` is preserved as
    the venue publishes it -- a SELL liquidation closed a long, a BUY closed a
    short -- so a positioning producer can aggregate net forced flow without an
    adapter reinterpreting it.

    An entry missing its time or size is skipped rather than emitted with a
    substituted value.
    """
    _raise_if_throttled(payload, venue)
    points = payload if isinstance(payload, list) else []

    drafts: list[ClaimDraft] = []
    for point in points:
        when = _event_date(point.get("time"))
        size = _decimal(point.get("origQty"))
        if when is None or size is None:
            continue
        value: dict[str, Any] = {
            "side": point.get("side"),
            "size": str(size),
            "symbol": symbol,
            "venue": venue,
        }
        price = _decimal(point.get("price"))
        if price is not None:
            value["price"] = str(price)
        drafts.append(
            ClaimDraft(
                claim_type=LIQUIDATION_EVENT,
                key=str(point.get("orderId"))
                or f"{venue}:{symbol}:{when.isoformat()}:{point.get('side')}",
                value=value,
                event_date=when,
                knowledge_date=when,
                confidence=1.0,
                unit=symbol,
            )
        )
    return drafts


async def _fetch_with_session(
    session: Any,
    venue: str,
    path: str,
    params: dict[str, Any],
) -> Any:
    url = BASE_URLS[venue] + path
    response = await get_json(session, url, params=params)
    if response.status_code == 429:
        raise Unavailable(f"{venue} returned HTTP 429 for {path}")
    if response.status_code != 200:
        raise Unavailable(
            f"{venue} returned HTTP {response.status_code} for {path}"
        )
    return response.json()


class DerivativesAdapter:
    source = SOURCE
    provider_key = PROVIDER_KEY

    def __init__(
        self,
        *,
        venue: str = "binance",
        fetch_fn: DerivativesFetcher | None = None,
        session: Any = None,
    ) -> None:
        self._venue = venue
        self._fetch_fn = fetch_fn
        self._session = session

    async def fetch(self, key: str) -> list[ClaimDraft]:
        kind, sep, symbol = key.partition(":")
        if not sep or not symbol:
            raise Unavailable(
                f"derivatives key must be '<kind>:<symbol>', got {key!r}"
            )
        if kind not in ("funding", "oi", "liq"):
            raise Unavailable(f"unknown derivatives kind {kind!r}")

        fetch_fn = self._fetch_fn
        if fetch_fn is None:
            fetch_fn = self._default_fetcher(kind, symbol)

        payload = await fetch_fn(key)
        if kind == "funding":
            return parse_funding(payload or [], symbol=symbol, venue=self._venue)
        if kind == "oi":
            return parse_open_interest(
                payload or [], symbol=symbol, venue=self._venue
            )
        return parse_liquidations(
            payload or [], symbol=symbol, venue=self._venue
        )

    def _default_fetcher(
        self, kind: str, symbol: str
    ) -> DerivativesFetcher:
        venue = self._venue
        if venue not in BASE_URLS:
            raise Unavailable(
                f"no base URL configured for venue {venue!r}"
            )

        if kind == "funding":
            path, params = FUNDING_PATH, {"symbol": symbol}
        elif kind == "oi":
            path, params = OPEN_INTEREST_PATH, {
                "symbol": symbol,
                "period": "5m",
                "limit": 30,
            }
        else:
            path, params = LIQUIDATION_PATH, {"symbol": symbol}

        session = self._session

        async def fetch(_key: str) -> Any:
            if session is not None:
                return await _fetch_with_session(session, venue, path, params)
            import httpx

            async with httpx.AsyncClient(timeout=30.0) as client:
                return await _fetch_with_session(client, venue, path, params)

        return fetch
