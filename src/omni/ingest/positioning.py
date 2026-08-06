"""Positioning perception adapter -- options-derived positioning.

`perception_positioning` is the one perception claim type that captures what
positioned participants do rather than what they say. The honest signals are
options skew, short interest and ETF flows. This adapter covers the options
component only; the other two are gaps named in the report.

Source coverage against the credential catalog:

* Options skew -- served. Polygon's ``/v3/snapshot/options/{underlying}``
  returns the listed chain (contract type, open interest, day volume, implied
  volatility) from which put/call ratios and the IV skew derive directly.
  Polygon is a real catalog entry (``byo_only``); the endpoint is documented,
  not invented.
* Short interest -- NOT served by any wired provider. FMP exposes a short
  interest endpoint but has no v2 adapter, and v1 never fetched short interest
  from a keyed source (its options data came entirely from yfinance, which the
  v2 catalog marks ``prohibited``). Building that path is a separate work order.
* ETF flows -- NOT served by any catalog provider. Named as a gap.

Polygon is ``byo_only`` in the catalog: its terms forbid serving its data on to
third parties, so claims produced here are pinned to the credential owner by the
writer -- never by this adapter. The adapter declares ``provider_key = "polygon"``
and produces drafts; ``ClaimDraft`` carries no audience or licence field, so
there is nowhere for an adapter to make that decision even if it tried.

Where v1 substitutes a default on missing input -- e.g. ``previousClose`` falling
back to ``100`` and ``impliedVolatility`` to ``0.3`` in
``app/api/v1/endpoints/options.py`` -- this adapter raises ``Unavailable`` or
emits nothing instead. A fabricated IV is exactly how hallucinated positioning
would enter the store.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from typing import Any

from omni.ingest.protocol import ClaimDraft, Unavailable, get_json

SOURCE = "polygon"
PROVIDER_KEY = "polygon"
CLAIM_TYPE = "perception_positioning"

SNAPSHOT_URL = "https://api.polygon.io/v3/snapshot/options/{underlying}"

SnapshotFetcher = Callable[[str], Awaitable[dict[str, Any]]]


def _ms_to_datetime(ms: Any) -> datetime | None:
    if ms is None:
        return None
    try:
        return datetime.fromtimestamp(ms / 1000, tz=UTC)
    except (TypeError, ValueError, OverflowError, OSError):
        return None


def parse_options_snapshot(
    payload: dict[str, Any],
    *,
    symbol: str,
) -> list[ClaimDraft]:
    """Reduce a Polygon options snapshot to a positioning claim draft.

    The snapshot lists every listed contract for the underlying. This folds it
    into one summary: aggregate put/call open-interest and volume ratios and the
    mean implied-volatility skew (put mean minus call mean). Those are the
    quantities a positioning read actually uses; the per-strike detail stays in
    ``evidence``.

    Polygon signals failure with ``{"status": "ERROR", ...}`` over HTTP 200; an
    empty ``results`` list is a valid "no contracts listed". The first raises
    ``Unavailable`` (source could not answer), the second returns ``[]``.

    No usable signal -- every ratio uncomputable -- returns ``[]`` rather than
    emitting a claim whose value is entirely null. No quote timestamp anywhere
    returns ``[]`` too: without one the bitemporal guarantee cannot be made, and
    guessing ``now()`` would let a backtest peek at a state whose time is
    unknown.
    """
    if payload.get("status") == "ERROR":
        raise Unavailable(
            f"Polygon returned status ERROR for options snapshot {symbol}: "
            f"{payload.get('error', 'unknown')}"
        )

    results = payload.get("results") or []
    if not results:
        return []

    call_oi = put_oi = 0
    call_vol = put_vol = 0
    call_ivs: list[float] = []
    put_ivs: list[float] = []
    expirations: set[str] = set()
    last_event: datetime | None = None

    for contract in results:
        details = contract.get("details") or {}
        ctype = details.get("contract_type")
        if details.get("expiration_date"):
            expirations.add(details["expiration_date"])

        oi = contract.get("open_interest") or 0
        vol = (contract.get("day") or {}).get("volume") or 0
        iv = contract.get("implied_volatility")

        if ctype == "call":
            call_oi += oi
            call_vol += vol
            if iv is not None:
                call_ivs.append(iv)
        elif ctype == "put":
            put_oi += oi
            put_vol += vol
            if iv is not None:
                put_ivs.append(iv)

        quoted_at = _ms_to_datetime((contract.get("last_quote") or {}).get("last_updated"))
        if quoted_at is not None and (last_event is None or quoted_at > last_event):
            last_event = quoted_at

    put_call_oi = (put_oi / call_oi) if call_oi else None
    put_call_vol = (put_vol / call_vol) if call_vol else None
    call_iv_mean = sum(call_ivs) / len(call_ivs) if call_ivs else None
    put_iv_mean = sum(put_ivs) / len(put_ivs) if put_ivs else None
    iv_skew = (
        put_iv_mean - call_iv_mean if call_iv_mean is not None and put_iv_mean is not None else None
    )

    signals = [put_call_oi, put_call_vol, iv_skew]
    if all(s is None for s in signals):
        # Nothing on this chain reported OI, volume or IV. Emitting a claim
        # whose value is entirely null is noise; the honest answer is no claim.
        return []
    if last_event is None:
        # No contract carried a quote timestamp, so when this state held is
        # unknown. A bitemporal claim cannot be made from an unknown event_date.
        return []

    present = sum(1 for s in signals if s is not None)
    confidence = present / len(signals)

    return [
        ClaimDraft(
            claim_type=CLAIM_TYPE,
            key=symbol,
            value={
                "put_call_oi_ratio": put_call_oi,
                "put_call_volume_ratio": put_call_vol,
                "call_iv_mean": call_iv_mean,
                "put_iv_mean": put_iv_mean,
                "iv_skew": iv_skew,
                "contracts_observed": len(results),
            },
            event_date=last_event,
            # A real-time quote is knowable at the instant it prints, so the
            # bound is the quote time itself -- unlike a daily OHLCV bar, whose
            # open timestamp precedes the knowable close.
            knowledge_date=last_event,
            confidence=confidence,
            unit="ratio",
            evidence={
                "underlying": symbol,
                "expirations": sorted(expirations),
                "source_endpoint": "v3/snapshot/options",
            },
        )
    ]


async def _fetch_options_snapshot(
    symbol: str,
    *,
    api_key: str,
) -> dict[str, Any]:
    import httpx

    url = SNAPSHOT_URL.format(underlying=symbol)
    headers = {"Authorization": f"Bearer {api_key}"}
    params = {"limit": 250}
    async with httpx.AsyncClient(timeout=30.0) as client:
        response = await get_json(client, url, params=params, headers=headers)
        if response.status_code != 200:
            raise Unavailable(
                f"Polygon returned HTTP {response.status_code} for options snapshot {symbol}"
            )
        return response.json()


class PositioningAdapter:
    source = SOURCE
    provider_key = PROVIDER_KEY

    def __init__(
        self,
        *,
        api_key: str | None = None,
        fetch_fn: SnapshotFetcher | None = None,
    ) -> None:
        self._api_key = api_key
        self._fetch_fn = fetch_fn

    async def fetch(self, key: str) -> list[ClaimDraft]:
        fetch_fn = self._fetch_fn
        if fetch_fn is None:
            if not self._api_key:
                raise Unavailable("no Polygon API key configured")

            async def fetch_fn(underlying: str) -> dict[str, Any]:
                return await _fetch_options_snapshot(underlying, api_key=self._api_key)

        payload = await fetch_fn(key)
        return parse_options_snapshot(payload or {}, symbol=key)
