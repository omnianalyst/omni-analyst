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

import asyncio
import math
import time
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta
from typing import Any

from omni.ingest.protocol import ClaimDraft, Unavailable, get_json

SOURCE = "polygon"
PROVIDER_KEY = "polygon"
CLAIM_TYPE = "price_snapshot"

AGGREGATES_URL = (
    "https://api.polygon.io/v2/aggs/ticker/{symbol}"
    "/range/{multiplier}/{timespan}/{from_date}/{to_date}"
)

# Polygon's free tier allows 5 API calls/min. Space consecutive calls at least
# this far apart so the fill loop cannot burst past the limit when it processes
# multiple price gaps in one cycle. Paid tiers can lower this via the env var.
_MIN_REQUEST_INTERVAL = float(
    __import__("os").environ.get("OMNI_POLYGON_MIN_INTERVAL", "13.0")
)

AggFetcher = Callable[[str], Awaitable[dict[str, Any]]]

_rate_lock = asyncio.Lock()
_last_request_ts = 0.0


async def _respect_rate_limit() -> None:
    """At most one Polygon request starts per _MIN_REQUEST_INTERVAL. The lock
    guards only the timing decision, not the network call, so a slow response
    does not block the next caller beyond the interval."""
    global _last_request_ts
    async with _rate_lock:
        elapsed = time.monotonic() - _last_request_ts
        remaining = _MIN_REQUEST_INTERVAL - elapsed
        if remaining > 0:
            await asyncio.sleep(remaining)
        _last_request_ts = time.monotonic()


def _event_date(bar: dict[str, Any]) -> datetime | None:
    ts_ms = bar.get("t")
    if (
        isinstance(ts_ms, bool)
        or not isinstance(ts_ms, (int, float))
        or not math.isfinite(ts_ms)
        or ts_ms <= 0
    ):
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


def _validated_ohlcv(bar: dict[str, Any], *, symbol: str) -> dict[str, int | float]:
    fields = {
        "open": "o",
        "high": "h",
        "low": "l",
        "close": "c",
        "volume": "v",
    }
    values: dict[str, int | float] = {}
    for name, field in fields.items():
        value = bar.get(field)
        if (
            isinstance(value, bool)
            or not isinstance(value, (int, float))
            or not math.isfinite(value)
            or value <= 0
        ):
            raise Unavailable(
                f"Polygon returned malformed {symbol} bar: {name} must be a "
                f"finite positive number, got {value!r}"
            )
        values[name] = value

    if not (
        values["low"] <= values["open"] <= values["high"]
        and values["low"] <= values["close"] <= values["high"]
    ):
        raise Unavailable(
            f"Polygon returned inconsistent {symbol} OHLC bar: "
            f"open={values['open']!r}, high={values['high']!r}, "
            f"low={values['low']!r}, close={values['close']!r}"
        )
    return values


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

    results = payload.get("results", [])
    if not isinstance(results, list):
        raise Unavailable(f"Polygon returned malformed results for {symbol}")

    drafts: list[ClaimDraft] = []
    for bar in results:
        if not isinstance(bar, dict):
            raise Unavailable(f"Polygon returned a malformed bar for {symbol}: {bar!r}")
        event_date = _event_date(bar)
        if event_date is None:
            raise Unavailable(
                f"Polygon returned malformed {symbol} bar timestamp: {bar.get('t')!r}"
            )
        value = _validated_ohlcv(bar, symbol=symbol)
        drafts.append(
            ClaimDraft(
                claim_type=CLAIM_TYPE,
                key=symbol,
                value=value,
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

    # Ingest depth follows the deployment profile when the caller specifies no
    # range: solo fetches one year, full two. The fill pipeline constructs the
    # adapter with an API key but no dates (it is not a backfill); a live
    # first-time ingest wants available history at the profile's depth, which
    # is also the free-tier cap. Explicit dates (backfills, tests) override.
    if not from_date:
        from omni.config import settings

        from_date = (
            datetime.now(UTC) - timedelta(days=settings.polygon_history_days)
        ).strftime("%Y-%m-%d")
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
    await _respect_rate_limit()
    async with httpx.AsyncClient(timeout=30.0) as client:
        response = await get_json(client, url, params=params, headers=headers)
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
