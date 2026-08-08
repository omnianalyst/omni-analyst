"""Derivatives ingestion: funding rate, open interest, liquidations.

Crypto's real edge lives in derivatives, and the coverage network could not
represent a funding rate at all before this file. Three of the planned crypto
producers (`carry.funding`, `basis.crossvenue`, `oi.divergence`) read claim
types this adapter writes; without it they have nothing to compute on.

These endpoints are public and keyless on Binance, Bybit and OKX, which makes
them cheap to reach -- but keyless is not the same as redistributable, and this
docstring previously conflated the two. `redistribution_for("binance")` resolves
to `byo_only`: the venue's terms restrict serving its market data on to third
parties whether or not a key was needed to fetch it. So these claims are pinned
to the credential owner exactly like CoinGecko prices, and do NOT accumulate as
shared network coverage. `defillama` and `etherscan` are the redistributable
crypto sources; a venue feed is not.

Nothing about the adapter changes either way. It declares
`provider_key = "binance"` and produces drafts; `ClaimDraft` has no audience
field, so the adapter cannot and does not make the redistribution decision.
That is the writer's job, resolved from the catalog, exactly as for CoinGecko
and OnChain -- which is why the mistake above was a documentation error and
never a leak.

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

**Funding history pages; open interest barely does, and the two must not be
conflated.** `GET /fapi/v1/fundingRate` serves settled funding back to contract
inception (BTC perp: 2019-09-10) at up to 1000 rows per request, but it defaults
to 100 and this adapter used to send no `startTime` and no `limit` at all -- so
one month of history per symbol was mistaken for all the history that exists,
and the strongest measured signal in this project sat unmeasured behind that
mistake. A `since` now walks the endpoint forward page by page. Open interest is
a different animal: `GET /futures/data/openInterestHist` retains only about the
last 30 days whatever window is asked for, so that series is
**forward-accumulating** -- paging it reaches the retention wall, not inception,
and deep OI history can only be obtained by running the collector over time.

The walk ends on an empty page, on a cursor that has passed the present, or on a
page whose newest timestamp fails to advance. That last condition is not
defensive decoration: a venue that silently re-serves the same window would be
paged forever, and an infinite loop over a paging endpoint presents as slow
progress rather than as a fault. Rows are deduplicated on their settlement
timestamp because consecutive pages can overlap at the boundary, and one
settlement counted twice is carry counted twice.

Bitemporal rule: a funding rate is knowable the instant it settles, and open
interest the instant it is sampled, so `knowledge_date == event_date` for every
claim here -- the same reasoning `coingecko.py` records for a crypto tick. That
is a property of the row, not of the run: a settlement paged in today from 2019
is still stamped 2019 in both fields, because the fetch clock has no bearing on
when the venue settled it.
Deriving either from `now()` would let a backtest read a rate that had not
settled yet. **No predicted or estimated next funding rate is emitted.** Binance
publishes a settled `fundingRate` history and a live `lastFundingRate` estimate;
only the settled series is an observation, and writing an estimate as a claim is
the fabrication AGENTS.md rule 2 forbids.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from typing import Any

from omni.ingest.protocol import ClaimDraft, Unavailable, get_json

logger = logging.getLogger(__name__)

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

# The venue's own per-request ceilings. Asking for more is rejected; asking for
# nothing gets the 100-row default that hid seven years of funding history.
FUNDING_PAGE_LIMIT = 1000
OPEN_INTEREST_PAGE_LIMIT = 500

# Binance returns these codes in a 200 body when it throttles a request. A body
# carrying one is the source refusing to answer, not an empty window -- so the
# parse functions raise `Unavailable` rather than producing no claims.
RATE_LIMIT_CODES = frozenset({-1003, -1015})

DerivativesFetcher = Callable[..., Awaitable[Any]]


def _event_date(ts_ms: Any) -> datetime | None:
    if ts_ms is None:
        return None
    try:
        return datetime.fromtimestamp(int(ts_ms) / 1000, tz=UTC)
    except (TypeError, ValueError, OSError):
        return None


def _row_time(row: Any, field: str) -> int | None:
    if not isinstance(row, dict):
        return None
    value = row.get(field)
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _epoch_ms(moment: datetime) -> int:
    if moment.tzinfo is None:
        raise ValueError(
            "since must be timezone-aware; a naive datetime is read as local "
            "time and would silently shift the requested window"
        )
    return int(moment.timestamp() * 1000)


def _now_ms() -> int:
    """Wall clock, used only to bound the walk. It never reaches a claim: every
    bitemporal field here comes from the venue's own settlement stamp."""
    return int(datetime.now(UTC).timestamp() * 1000)


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

    Unlike funding, this series is **forward-accumulating**: Binance retains
    roughly the last 30 days of `openInterestHist`, so paging it back reaches a
    retention wall rather than contract inception. Years of OI history exist
    only if the collector has been running for years.
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
        since: datetime | None = None,
        page_limit: int | None = None,
    ) -> None:
        self._venue = venue
        self._fetch_fn = fetch_fn
        self._session = session
        self._since_ms = None if since is None else _epoch_ms(since)
        self._page_limit = page_limit
        self.last_stop_reason: str | None = None

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

        if kind == "funding":
            payload = await self._walk(
                fetch_fn,
                key,
                time_field="fundingTime",
                page_limit=self._resolved_limit(FUNDING_PAGE_LIMIT),
            )
            return parse_funding(payload or [], symbol=symbol, venue=self._venue)
        if kind == "oi":
            payload = await self._walk(
                fetch_fn,
                key,
                time_field="timestamp",
                page_limit=self._resolved_limit(OPEN_INTEREST_PAGE_LIMIT),
            )
            return parse_open_interest(
                payload or [], symbol=symbol, venue=self._venue
            )
        payload = await fetch_fn(key)
        return parse_liquidations(
            payload or [], symbol=symbol, venue=self._venue
        )

    def _resolved_limit(self, venue_max: int) -> int:
        return venue_max if self._page_limit is None else self._page_limit

    async def _walk(
        self,
        fetch_fn: DerivativesFetcher,
        key: str,
        *,
        time_field: str,
        page_limit: int,
    ) -> Any:
        """Page forward from `since`, or take a single unpaged request.

        With no `since` the request is exactly what it always was -- no
        `startTime`, no `limit` -- so the scheduler's rolling path is unchanged
        and stays one request per key.

        With a `since` the walk ends on an empty page, on a page carrying no
        usable timestamp, on a cursor that has passed the present, or on a page
        that fails to advance the newest timestamp seen. A stalled page is not
        merged: its rows are by definition ones already walked past. Rows are
        deduplicated on `time_field` because pages can overlap at the boundary.

        A throttle body mid-walk raises rather than ending the walk quietly. A
        partial history returned as if it were complete is the defect this
        method exists to fix, and re-running from the same `since` resumes.
        """
        if self._since_ms is None:
            self.last_stop_reason = "no since requested: single request"
            return await fetch_fn(key)

        collected: list[Any] = []
        seen: set[int] = set()
        cursor = self._since_ms
        newest: int | None = None
        while True:
            payload = await fetch_fn(key, start_time=cursor, limit=page_limit)
            _raise_if_throttled(payload, self._venue)
            rows = payload if isinstance(payload, list) else []
            if not rows:
                self.last_stop_reason = (
                    f"{self._venue} returned no rows from {cursor}"
                )
                break
            stamps = [
                ts
                for ts in (_row_time(row, time_field) for row in rows)
                if ts is not None
            ]
            if not stamps:
                self.last_stop_reason = (
                    f"{self._venue} returned {len(rows)} rows from {cursor} "
                    f"with no usable {time_field}"
                )
                break
            page_newest = max(stamps)
            if newest is not None and page_newest <= newest:
                self.last_stop_reason = (
                    f"{self._venue} did not advance past {newest}: asked from "
                    f"{cursor}, newest row returned was {page_newest}"
                )
                logger.warning("%s paging stalled: %s", key, self.last_stop_reason)
                break
            for row in rows:
                ts = _row_time(row, time_field)
                if ts is None or ts in seen:
                    continue
                seen.add(ts)
                collected.append(row)
            newest = page_newest
            cursor = page_newest + 1
            if cursor > _now_ms():
                self.last_stop_reason = (
                    f"{self._venue} reached the present at {newest}"
                )
                break
        return collected

    def _default_fetcher(
        self, kind: str, symbol: str
    ) -> DerivativesFetcher:
        venue = self._venue
        if venue not in BASE_URLS:
            raise Unavailable(
                f"no base URL configured for venue {venue!r}"
            )

        if kind == "funding":
            path, base_params = FUNDING_PATH, {"symbol": symbol}
        elif kind == "oi":
            path, base_params = OPEN_INTEREST_PATH, {
                "symbol": symbol,
                "period": "5m",
                "limit": 30,
            }
        else:
            path, base_params = LIQUIDATION_PATH, {"symbol": symbol}

        session = self._session

        async def fetch(
            _key: str,
            *,
            start_time: int | None = None,
            limit: int | None = None,
        ) -> Any:
            params = dict(base_params)
            if start_time is not None:
                params["startTime"] = start_time
            if limit is not None:
                params["limit"] = limit
            if session is not None:
                return await _fetch_with_session(session, venue, path, params)
            import httpx

            async with httpx.AsyncClient(timeout=30.0) as client:
                return await _fetch_with_session(client, venue, path, params)

        return fetch
