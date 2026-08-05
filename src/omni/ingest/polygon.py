"""Polygon.io aggregates ingestion.

Polygon is `byo_only` in the credential catalog: its terms forbid serving its
data on to third parties. Claims produced here are pinned to the user whose key
fetched them and never enter shared coverage. **The adapter does not enforce
that rule** — the writer does, from `provider_key`. This adapter declares
`provider_key = "polygon"` and produces drafts; `ClaimDraft` deliberately has
no audience or licence field, so there is nowhere for an adapter to make that
decision even if it tried to.

Endpoint knowledge was harvested from v1
`app/data/.../providers/polygon.py` but that file is not ported: it inherits a
base class whose fetch path raises `AttributeError` on `self.timeout` and keeps
a cache that silently never works, and its symbol-shaped contract does not fit
this bitemporal protocol. Only the URL shape and the OHLCV field letters carry
over.

Each aggregate bar's millisecond `t` is the **start** (open) of the bar window
(per Polygon's docs). It is the `event_date`. The `knowledge_date` is the bar's
own close: a daily bar for 2024-03-01 was knowable at that day's close,
regardless of when the fetch ran. Getting this from `now()` would silently let a
backtest peek at prices that had not happened yet.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta
from typing import Any

from omni.ingest.protocol import ClaimDraft, Unavailable

SOURCE = "polygon"
PROVIDER_KEY = "polygon"
CLAIM_TYPE = "price_snapshot"

AGGREGATES_URL = (
    "https://api.polygon.io/v2/aggs/ticker/{symbol}"
    "/range/{multiplier}/{timespan}/{from_date}/{to_date}"
)

AggFetcher = Callable[[str], Awaitable[dict[str, Any]]]


def _event_date(bar: dict[str, Any]) -> datetime | None:
    ts_ms = bar.get("t")
    if ts_ms is None:
        return None
    try:
        return datetime.fromtimestamp(ts_ms / 1000, tz=UTC)
    except (TypeError, ValueError, OSError):
        return None


def _close_knowledge_date(event_date: datetime) -> datetime:
    # Polygon's `t` is the open of the bar window. The bar finalizes at the
    # session close of that trading day. We do not have the market calendar, so
    # the conservative calendar-derived bound is the start of the next UTC day:
    # the figure was definitely knowable by then, and a backtest reading this
    # date can never have peeked. Derived from the bar alone, never `now()`.
    return (
        event_date.replace(hour=0, minute=0, second=0, microsecond=0)
        + timedelta(days=1)
    )


def parse_aggregates(
    payload: dict[str, Any],
    *,
    symbol: str,
    currency: str | None = None,
) -> list[ClaimDraft]:
    """Flatten a Polygon aggregates response into claim drafts.

    Polygon signals failure with `{"status": "ERROR", ...}` over HTTP 200 and a
    valid empty range with `{"resultsCount": 0}`. The first is the source being
    unable to answer, so it raises `Unavailable`; the second is an honest
    "nothing happened in this window", so it returns `[]`. Treating them the
    same would either crash on every market holiday or swallow real errors.
    """
    if payload.get("status") == "ERROR":
        raise Unavailable(
            f"Polygon returned status ERROR for {symbol}: "
            f"{payload.get('error', 'unknown')}"
        )

    results = payload.get("results") or []
    drafts: list[ClaimDraft] = []
    for bar in results:
        event_date = _event_date(bar)
        if event_date is None:
            # No timestamp means no event_date, and without it the bitemporal
            # guarantee cannot be made. Skip rather than guess.
            continue
        drafts.append(
            ClaimDraft(
                claim_type=CLAIM_TYPE,
                key=symbol,
                value={
                    "open": bar.get("o"),
                    "high": bar.get("h"),
                    "low": bar.get("l"),
                    "close": bar.get("c"),
                    "volume": bar.get("v"),
                },
                event_date=event_date,
                knowledge_date=_close_knowledge_date(event_date),
                confidence=1.0,
                unit=currency,
            )
        )
    return drafts


async def _fetch_aggregates(
    symbol: str,
    *,
    api_key: str,
    multiplier: int,
    timespan: str,
    from_date: str | None,
    to_date: str | None,
) -> dict[str, Any]:
    import httpx

    # Default to a trailing 2-year window when the caller specifies no range.
    # The fill pipeline constructs the adapter with an API key but no dates
    # (it is not a backfill); a live first-time ingest wants available history,
    # and 2 years is the free-tier depth. Explicit dates (backfills, tests)
    # override this.
    if not from_date:
        from_date = (datetime.now(UTC) - timedelta(days=730)).strftime("%Y-%m-%d")
    if not to_date:
        to_date = datetime.now(UTC).strftime("%Y-%m-%d")
    url = AGGREGATES_URL.format(
        symbol=symbol,
        multiplier=multiplier,
        timespan=timespan,
        from_date=from_date,
        to_date=to_date,
    )
    headers = {"Authorization": f"Bearer {api_key}"}
    params = {"adjusted": "true", "sort": "asc", "limit": 50000}
    async with httpx.AsyncClient(timeout=30.0) as client:
        response = await client.get(url, params=params, headers=headers)
        if response.status_code != 200:
            raise Unavailable(
                f"Polygon returned HTTP {response.status_code} for {symbol}"
            )
        return response.json()


class PolygonAdapter:
    source = SOURCE
    provider_key = PROVIDER_KEY

    def __init__(
        self,
        *,
        api_key: str | None = None,
        fetch_fn: AggFetcher | None = None,
        currency: str | None = None,
        multiplier: int = 1,
        timespan: str = "day",
        from_date: str | None = None,
        to_date: str | None = None,
    ) -> None:
        self._api_key = api_key
        self._fetch_fn = fetch_fn
        self._currency = currency
        self._multiplier = multiplier
        self._timespan = timespan
        self._from_date = from_date
        self._to_date = to_date

    async def fetch(self, key: str) -> list[ClaimDraft]:
        fetch_fn = self._fetch_fn
        if fetch_fn is None:
            if not self._api_key:
                raise Unavailable("no Polygon API key configured")

            async def fetch_fn(symbol: str) -> dict[str, Any]:
                return await _fetch_aggregates(
                    symbol,
                    api_key=self._api_key,
                    multiplier=self._multiplier,
                    timespan=self._timespan,
                    from_date=self._from_date,
                    to_date=self._to_date,
                )

        payload = await fetch_fn(key)
        return parse_aggregates(
            payload or {}, symbol=key, currency=self._currency
        )
