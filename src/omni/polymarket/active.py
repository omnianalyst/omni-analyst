"""Active (unresolved) Polymarket markets — the paper trader's universe.

Mirror of `gamma.list_resolved_markets` for the open universe. Same parser
discipline (refuse anything we cannot build truthfully), same `strict`
toggle for batch tolerance, same `on_skip` callback for visibility.

The `ActiveMarket` carries a `yes_price` snapshot at fetch time, which is the
paper trader's entry-price reference. For maker-only execution the trade is
placed AT the model's probability, not at this price; the snapshot is for
detecting edge (|model_prob − yes_price|) and for the resolve pass to detect
whether the market crossed the model's level.
"""

from __future__ import annotations

import math
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

import httpx

from omni.ingest.protocol import Unavailable
from omni.polymarket.gamma import GAMMA_BASE_URL, _parse_stringified_array


@dataclass(frozen=True)
class ActiveMarket:
    """An open Yes/No market with its current YES price snapshot.

    `yes_price` is what Gamma reports at fetch time — `outcomePrices[0]`
    parsed through float. For an open market this is the live mid (Gamma
    surfaces the CLOB midpoint as the price); slippage on actual entry will
    move the realized fill. The paper trader models that separately.

    `fetched_at` is when we observed the price. This is NOT a bitemporal
    knowledge_date — Gamma does not tell us when the underlying CLOB last
    printed — but it IS the moment we can prove we knew the price, which is
    what the paper trader's resolve pass needs to enforce no-lookahead.
    """

    condition_id: str
    question: str
    category: str
    yes_token_id: str | None
    no_token_id: str | None
    yes_price: float
    neg_risk: bool
    slug: str
    volume: float
    end_date: datetime | None
    fetched_at: datetime

    def __post_init__(self) -> None:
        if not self.condition_id.strip():
            raise ValueError("condition_id must be non-empty")
        if not self.question.strip():
            raise ValueError("question must be non-empty")
        if not math.isfinite(self.yes_price):
            raise ValueError(f"yes_price must be finite: {self.yes_price}")
        if not (0.0 <= self.yes_price <= 1.0):
            raise ValueError(f"yes_price must be in [0, 1], got {self.yes_price}")
        if self.volume < 0:
            raise ValueError(f"volume must not be negative: {self.volume}")
        if self.end_date is not None and self.end_date.tzinfo is None:
            raise ValueError("end_date must be timezone-aware if supplied")
        if self.fetched_at.tzinfo is None:
            raise ValueError("fetched_at must be timezone-aware")


def _parse_active_market(raw: Mapping[str, Any], fetched_at: datetime) -> ActiveMarket:
    if not isinstance(raw, Mapping):
        raise Unavailable(f"market record is {type(raw).__name__}, not a mapping")

    condition_id = str(raw.get("id", "")).strip()
    question = str(raw.get("question") or raw.get("title") or "").strip()
    if not condition_id or not question:
        raise Unavailable(
            f"market missing id or question: id={raw.get('id')!r} q={raw.get('question')!r}"
        )

    outcomes = _parse_stringified_array(raw.get("outcomes"), "outcomes")
    if not outcomes or str(outcomes[0]).strip().lower() != "yes":
        raise Unavailable(
            f"outcome[0] is {outcomes[0] if outcomes else None!r}, expected 'Yes'"
        )

    prices = _parse_stringified_array(raw.get("outcomePrices"), "outcomePrices")
    if len(prices) < 1:
        raise Unavailable("outcomePrices missing")
    try:
        yes_price = float(prices[0])
    except (TypeError, ValueError) as exc:
        raise Unavailable(f"yes_price not numeric: {prices[0]!r}") from exc
    if not math.isfinite(yes_price) or not (0.0 <= yes_price <= 1.0):
        # An open market can momentarily print 0.0 or 1.0 (one-sided book);
        # outside [0, 1] is data corruption, not a price.
        raise Unavailable(f"yes_price out of [0, 1]: {yes_price}")

    tokens = _parse_stringified_array(raw.get("clobTokenIds"), "clobTokenIds")
    yes_token = str(tokens[0]) if len(tokens) >= 1 else None
    no_token = str(tokens[1]) if len(tokens) >= 2 else None

    category = str(raw.get("category") or "Other").strip() or "Other"
    try:
        volume = float(raw.get("volume") or 0.0)
    except (TypeError, ValueError):
        raise Unavailable(f"volume not numeric: {raw.get('volume')!r}")

    end_date_raw = raw.get("endDate")
    end_date: datetime | None = None
    if end_date_raw:
        try:
            ts = str(end_date_raw).replace("Z", "+00:00")
            end_date = datetime.fromisoformat(ts)
            if end_date.tzinfo is None:
                end_date = end_date.replace(tzinfo=UTC)
        except ValueError:
            end_date = None

    return ActiveMarket(
        condition_id=condition_id,
        question=question,
        category=category,
        yes_token_id=yes_token,
        no_token_id=no_token,
        yes_price=yes_price,
        neg_risk=bool(raw.get("negRisk", False)),
        slug=str(raw.get("slug") or ""),
        volume=volume,
        end_date=end_date,
        fetched_at=fetched_at,
    )


def _filter_active(
    markets: Sequence[ActiveMarket],
    *,
    categories: Sequence[str] | None,
    min_volume: float,
) -> list[ActiveMarket]:
    cat_set = {c.strip().lower() for c in categories} if categories else None
    out: list[ActiveMarket] = []
    for m in markets:
        if m.volume < min_volume:
            continue
        if cat_set is not None and m.category.lower() not in cat_set:
            continue
        out.append(m)
    return out


async def list_active_markets(
    client: httpx.AsyncClient,
    *,
    base_url: str = GAMMA_BASE_URL,
    limit: int = 100,
    offset: int = 0,
    categories: Sequence[str] | None = None,
    min_volume: float = 0.0,
    strict: bool = True,
    on_skip: Callable[[Mapping[str, Any], Unavailable], None] | None = None,
    fetched_at: datetime | None = None,
) -> list[ActiveMarket]:
    """Fetch active (open) Yes/No markets with current price snapshot.

    Mirrors `list_resolved_markets` shape: same `strict` toggle (raise on
    first parse failure vs skip with `on_skip` callback), same category and
    volume filtering. The `fetched_at` timestamp is per-batch, not per-market
    — the paper trader scans in batches and a single batch shares one
    observation moment.
    """
    if limit <= 0 or limit > 500:
        raise ValueError(f"limit must be in 1..500, got {limit}")
    if offset < 0:
        raise ValueError(f"offset must be >= 0, got {offset}")
    if min_volume < 0:
        raise ValueError(f"min_volume must be >= 0, got {min_volume}")

    params = {
        "closed": "false",
        "active": "true",
        "limit": limit,
        "offset": offset,
        "order": "volume",
        "ascending": "false",
    }
    if categories is not None and len(categories) == 1:
        params["category"] = categories[0]

    try:
        resp = await client.get(f"{base_url}/markets", params=params)
        resp.raise_for_status()
        payload = resp.json()
    except httpx.HTTPError as exc:
        raise Unavailable(f"Gamma /markets (active) request failed: {exc}") from exc
    if not isinstance(payload, list):
        raise Unavailable(
            f"Gamma /markets returned {type(payload).__name__}, expected a list"
        )

    ts = fetched_at if fetched_at is not None else datetime.now(UTC)
    parsed: list[ActiveMarket] = []
    for item in payload:
        if not isinstance(item, Mapping):
            continue
        try:
            parsed.append(_parse_active_market(item, ts))
        except (Unavailable, ValueError) as exc:
            if strict:
                raise
            if on_skip is not None:
                on_skip(item, exc if isinstance(exc, Unavailable) else Unavailable(str(exc)))
    return _filter_active(parsed, categories=categories, min_volume=min_volume)


async def fetch_current_resolution(
    client: httpx.AsyncClient,
    *,
    condition_id: str,
    base_url: str = GAMMA_BASE_URL,
) -> tuple[bool | None, float | None]:
    """Look up whether a market has resolved and to which side.

    Returns `(resolved_yes, final_yes_price)`. `(None, _)` means the market
    is still open. Used by the paper trader's resolve pass to update the
    P&L log as positions close.

    Reuses the resolved-market parser's `_detect_resolved_yes` rule (clean
    1.0/0.0 outcome prices) by re-fetching the market row from Gamma; if the
    resolved-side prices are not at the extremes, the market is treated as
    unresolved — a partial-resolution or ambiguous state we refuse to score.
    """
    from omni.polymarket.gamma import _detect_resolved_yes

    try:
        resp = await client.get(f"{base_url}/markets/{condition_id}")
        resp.raise_for_status()
        payload = resp.json()
    except httpx.HTTPError as exc:
        raise Unavailable(f"Gamma /markets/{condition_id} failed: {exc}") from exc
    if not isinstance(payload, Mapping):
        raise Unavailable(f"unexpected payload type: {type(payload).__name__}")

    outcomes = _parse_stringified_array(payload.get("outcomes"), "outcomes")
    prices = _parse_stringified_array(payload.get("outcomePrices"), "outcomePrices")
    if not outcomes or not prices:
        return (None, None)

    try:
        current_price = float(prices[0])
    except (TypeError, ValueError):
        current_price = None

    try:
        resolved_yes = _detect_resolved_yes(outcomes, prices)
    except Unavailable:
        # Not at extremes => market still open or ambiguous.
        return (None, current_price)

    closed = bool(payload.get("closed", False))
    if not closed:
        # Prices hit an extreme transiently without an official close.
        return (None, current_price)
    return (resolved_yes, current_price)


__all__ = [
    "ActiveMarket",
    "fetch_current_resolution",
    "list_active_markets",
]
