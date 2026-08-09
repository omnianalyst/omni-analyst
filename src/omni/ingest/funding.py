"""Funding-rate history from any ccxt venue that publishes it.

`derivatives.py` already ingests funding, but only Binance's: it speaks
`/fapi/v1/fundingRate` directly and its `BASE_URLS` holds one entry. This module
is the same claim from any venue ccxt covers, which is what Findings 23-25
turned from a nicety into a requirement -- funding is a parameter of each
venue's contract, not a property of the asset, and Hyperliquid pays +4.31%/yr
more than Binance on the same names.

The claim it writes is deliberately identical in shape to `parse_funding`'s, so
`carry_loop._settlements` and `crosssectional._funding_window` read both without
knowing which produced them. What differs is the venue in the key, and that is
the whole point: **`split_part(key, ':', 1)` is how both of those queries filter,
and a blended ranking across venues has no unit** (Finding 25).

Two things this does NOT do, both on purpose:

  * It does not normalise cadence. Hyperliquid settles hourly and Binance every
    eight hours, and each claim is one settlement exactly as the venue paid it.
    `apply_funding` values settlements as they arrive, so accrual is already
    cadence-agnostic; it is *ranking* that must not mix venues, and the venue
    filter is where that is enforced rather than here. Rewriting rates to a
    common period would make the stored number something no venue ever paid.
  * It does not skip a zero rate. Zero funding is a real observation and the
    interval it covers is real; dropping it would leave a gap that reads as
    missing coverage.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from typing import Any

from omni.ingest.protocol import ClaimDraft, Unavailable

logger = logging.getLogger(__name__)

SOURCE = "funding"
FUNDING_RATE = "funding_rate"

# ccxt's per-request ceiling for this endpoint on the venues in use. Asking for
# more is silently truncated rather than rejected, which is the failure mode
# that made a rate-limited page look like an asset with no history.
PAGE_LIMIT = 500

# A walk that needs more pages than this is not paging, it is looping. At 500
# settlements a page an hourly venue covers ~21 days per page, so 400 pages is
# over twenty years -- far past any venue's inception and still a bound.
MAX_PAGES = 400


def _event_date(ts_ms: Any) -> datetime | None:
    if ts_ms is None:
        return None
    try:
        return datetime.fromtimestamp(int(ts_ms) / 1000, tz=UTC)
    except (TypeError, ValueError, OSError):
        return None


def _decimal(value: Any) -> Decimal | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        parsed = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return None
    return parsed if parsed.is_finite() else None


def parse_funding_history(
    payload: Any, *, symbol: str, venue: str
) -> list[ClaimDraft]:
    """Flatten ccxt's normalised funding history into drafts.

    ccxt returns `{'symbol', 'fundingRate', 'timestamp', 'datetime', 'info'}`
    per settlement. The rate's sign is preserved unchanged -- positive means
    longs pay shorts -- because `venue/costs.py` and `portfolio/state.py` both
    rely on that convention and a flip inverts the carry strategy.

    `knowledge_date` equals `event_date`, which is correct here and is not the
    OHLCV case: a bar is stamped with its OPEN and is not knowable until it
    closes, whereas a funding settlement is published at the instant it settles.
    Stamping it later would hide real settlements from a point-in-time replay.

    An entry with no timestamp or an unparseable rate is skipped rather than
    emitted with a substituted zero.
    """
    entries = payload if isinstance(payload, list) else []

    drafts: list[ClaimDraft] = []
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        when = _event_date(entry.get("timestamp"))
        rate = _decimal(entry.get("fundingRate"))
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


class CCXTFundingAdapter:
    """Walks one symbol's funding history on one venue.

    `venue` is both the ccxt exchange id and the venue written into every key,
    so the two cannot drift -- P1.11 was a day spent on a `source` label that
    had collapsed two venues into one, and the fix was keeping them distinct
    rather than making the reader agree with the collapse.
    """

    source = SOURCE
    provider_key: str

    def __init__(
        self,
        *,
        venue: str,
        since: datetime | None = None,
        page_limit: int | None = None,
        fetch_fn: Any = None,
    ) -> None:
        if not venue or not venue.strip():
            raise ValueError("venue must be named; it becomes the key prefix")
        self.provider_key = venue
        self._venue = venue
        self._since_ms = None if since is None else int(since.timestamp() * 1000)
        self._page_limit = PAGE_LIMIT if page_limit is None else page_limit
        self._fetch_fn = fetch_fn
        self.last_stop_reason: str | None = None

    async def _walk(self, page_fetch: Any, symbol: str) -> list[dict[str, Any]]:
        """Page forward from `since` until the venue stops advancing.

        A transient error is retried rather than swallowed. Breaking out of the
        loop on any exception is what turned a rate limit into a short series
        indistinguishable from an asset with little history -- it silently
        dropped ETH and SOL, whose history matches BTC's, out of a measured
        universe. Truncation impersonates exactly the thing being measured, so
        a walk that cannot finish raises.
        """
        if self._since_ms is None:
            self.last_stop_reason = "no since requested: single page"
            return await page_fetch(symbol, since=None, limit=self._page_limit)

        collected: dict[int, dict[str, Any]] = {}
        cursor = self._since_ms
        for _ in range(MAX_PAGES):
            page = await page_fetch(symbol, since=cursor, limit=self._page_limit)
            if not page:
                self.last_stop_reason = "empty page"
                return list(collected.values())
            stamped = {
                int(e["timestamp"]): e
                for e in page
                if isinstance(e, dict) and e.get("timestamp") is not None
            }
            if not stamped:
                self.last_stop_reason = "page carried no timestamps"
                return list(collected.values())
            newest = max(stamped)
            if newest + 1 <= cursor:
                # The venue re-served a window already walked past. Merging it
                # would double-count nothing (the dict dedupes) but continuing
                # would page forever, which reads as slow progress not a fault.
                self.last_stop_reason = "cursor did not advance"
                return list(collected.values())
            collected.update(stamped)
            cursor = newest + 1
        raise Unavailable(
            f"{self._venue} funding history for {symbol} still advancing after "
            f"{MAX_PAGES} pages; the cap would truncate the series and a "
            f"truncated walk is indistinguishable from a short history"
        )

    async def _default_fetch(self, symbol: str) -> list[dict[str, Any]]:
        import ccxt.async_support as ccxt_async

        try:
            exchange_cls = getattr(ccxt_async, self._venue)
        except AttributeError as exc:
            raise Unavailable(f"ccxt has no venue named {self._venue!r}") from exc

        exchange = exchange_cls({"enableRateLimit": True})
        if not exchange.has.get("fetchFundingRateHistory"):
            await exchange.close()
            raise Unavailable(
                f"{self._venue} does not publish funding history through ccxt"
            )

        async def page(sym: str, *, since: int | None, limit: int | None):
            return await exchange.fetch_funding_rate_history(
                sym, since=since, limit=limit
            )

        try:
            return await self._walk(page, symbol)
        finally:
            await exchange.close()

    async def fetch(self, key: str) -> list[ClaimDraft]:
        try:
            if self._fetch_fn is not None:
                history = await self._walk(self._fetch_fn, key)
            else:
                history = await self._default_fetch(key)
        except Unavailable:
            raise
        except Exception as exc:
            raise Unavailable(
                f"{self._venue} funding history for {key} unavailable: {exc}"
            ) from exc
        return parse_funding_history(history, symbol=key, venue=self._venue)
