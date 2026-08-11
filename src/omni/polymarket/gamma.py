"""Polymarket Gamma + CLOB read surfaces, parsed honestly.

Two endpoints, one rule: if the response shape does not let us build a value
object truthfully, we raise `Unavailable` and the caller excludes the market.
No carry-forward of the last seen price, no 0.5 prior when the cutoff sample
is missing, no resolved-side guess when both outcomes report 1.0.

The caller owns the `httpx.AsyncClient`. That is what makes a deterministic
test possible: `httpx.MockTransport` returns canned bytes and we never touch
the network. It is also what makes a real run honest: a single client with
its own timeout and retry policy is the one place that policy lives.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping, Sequence
from datetime import UTC, datetime
from typing import Any

import httpx

from omni.ingest.protocol import Unavailable
from omni.polymarket.types import MarketPricePoint, ResolvedMarket

GAMMA_BASE_URL = "https://gamma-api.polymarket.com"
CLOB_BASE_URL = "https://clob.polymarket.com"


def _parse_stringified_array(raw: Any, field_name: str) -> list[Any]:
    """Gamma returns `outcomes`, `outcomePrices` and `clobTokenIds` as JSON
    strings, not arrays. Parse once, here, and refuse anything we cannot turn
    into a list rather than coercing a non-list into one element.
    """
    if raw is None:
        return []
    if isinstance(raw, list):
        return raw
    if isinstance(raw, str):
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise Unavailable(
                f"{field_name} is not valid JSON: {raw!r}"
            ) from exc
        if not isinstance(parsed, list):
            raise Unavailable(
                f"{field_name} parsed to {type(parsed).__name__}, not a list"
            )
        return parsed
    raise Unavailable(f"{field_name} is {type(raw).__name__}, expected list or JSON string")


def _parse_iso(ts: Any, field_name: str) -> datetime:
    if not isinstance(ts, str) or not ts:
        raise Unavailable(f"{field_name} missing or not a string: {ts!r}")
    candidate = ts.replace("Z", "+00:00")
    try:
        out = datetime.fromisoformat(candidate)
    except ValueError as exc:
        raise Unavailable(f"{field_name} {ts!r} is not ISO-8601") from exc
    if out.tzinfo is None:
        out = out.replace(tzinfo=UTC)
    return out.astimezone(UTC)


def _detect_resolved_yes(outcomes: list[Any], prices: list[Any]) -> bool:
    """Decide YES-won from the resolved price vector.

    On a clean resolution, the winner's price is `1` and the loser's is `0`.
    Polymarket prices are strings ("1.00"), not floats; coerce through
    `float()` and tolerate the small representation noise around 1 and 0 with
    `math.isclose`. If neither side is unambiguously the winner, refuse: a
    market whose ground truth is ambiguous cannot be scored, and picking one
    would manufacture ground truth.
    """
    import math

    if len(outcomes) < 2 or len(prices) < 2:
        raise Unavailable(
            f"need at least two outcomes/prices to detect resolution; "
            f"got outcomes={outcomes!r} prices={prices!r}"
        )
    yes_label = str(outcomes[0]).strip().lower()
    if yes_label != "yes":
        raise Unavailable(
            f"outcome[0] is {outcomes[0]!r}, expected 'Yes'; the YES/NO token "
            f"order assumption is wrong and proceeding would mis-score the "
            f"market"
        )
    try:
        yes_p = float(prices[0])
        no_p = float(prices[1])
    except (TypeError, ValueError) as exc:
        raise Unavailable(f"prices are not numeric: {prices!r}") from exc
    if not math.isfinite(yes_p) or not math.isfinite(no_p):
        raise Unavailable(f"prices are not finite: {prices!r}")
    yes_won = math.isclose(yes_p, 1.0, abs_tol=1e-6) and math.isclose(no_p, 0.0, abs_tol=1e-6)
    no_won = math.isclose(no_p, 1.0, abs_tol=1e-6) and math.isclose(yes_p, 0.0, abs_tol=1e-6)
    if yes_won == no_won:
        raise Unavailable(
            f"resolution is ambiguous: yes={yes_p}, no={no_p}; both or neither "
            f"are 1.0 — refusing to pick a side"
        )
    return yes_won


def _parse_market(raw: Mapping[str, Any]) -> ResolvedMarket:
    if not isinstance(raw, Mapping):
        raise Unavailable(f"market record is {type(raw).__name__}, not a mapping")
    condition_id = str(raw.get("id", "")).strip()
    question = str(raw.get("question") or raw.get("title") or "").strip()
    if not condition_id or not question:
        raise Unavailable(
            f"market missing id or question: id={raw.get('id')!r} q={raw.get('question')!r}"
        )
    category = str(raw.get("category") or "Other").strip() or "Other"
    outcomes = _parse_stringified_array(raw.get("outcomes"), "outcomes")
    prices = _parse_stringified_array(raw.get("outcomePrices"), "outcomePrices")
    resolved_yes = _detect_resolved_yes(outcomes, prices)

    tokens = _parse_stringified_array(raw.get("clobTokenIds"), "clobTokenIds")
    yes_token = str(tokens[0]) if len(tokens) >= 1 else None
    no_token = str(tokens[1]) if len(tokens) >= 2 else None

    try:
        volume = float(raw.get("volume") or 0.0)
    except (TypeError, ValueError):
        raise Unavailable(f"volume is not numeric: {raw.get('volume')!r}")

    return ResolvedMarket(
        condition_id=condition_id,
        question=question,
        category=category,
        resolved_yes=resolved_yes,
        resolution_date=_parse_iso(raw.get("endDate"), "endDate"),
        created_at=_parse_iso(raw.get("startDate"), "startDate"),
        yes_token_id=yes_token,
        no_token_id=no_token,
        neg_risk=bool(raw.get("negRisk", False)),
        slug=str(raw.get("slug") or ""),
        volume=volume,
    )


def _filter_markets(
    markets: Sequence[ResolvedMarket],
    *,
    categories: Sequence[str] | None,
    min_volume: float,
) -> list[ResolvedMarket]:
    cat_set = {c.strip().lower() for c in categories} if categories else None
    out: list[ResolvedMarket] = []
    for m in markets:
        if m.volume < min_volume:
            continue
        if cat_set is not None and m.category.lower() not in cat_set:
            continue
        out.append(m)
    return out


async def list_resolved_markets(
    client: httpx.AsyncClient,
    *,
    base_url: str = GAMMA_BASE_URL,
    limit: int = 100,
    offset: int = 0,
    categories: Sequence[str] | None = None,
    min_volume: float = 0.0,
    strict: bool = True,
    on_skip: Callable[[Mapping[str, Any], Unavailable], None] | None = None,
) -> list[ResolvedMarket]:
    """Fetch resolved markets, parsed and filtered, or raise `Unavailable`.

    Markets whose response shape we cannot parse truthfully raise and are not
    silently dropped — the caller sees a failure as a failure. The
    `Unavailability` may surface from the HTTP layer (status / transport) or
    the parser (missing field, ambiguous resolution, non-numeric price); both
    are honest refusals to fabricate.

    `strict=True` (default) raises on the first per-market parse failure,
    preserving the original contract: a single bad row aborts the batch. Use
    this in tests and any caller that wants to know about API drift loudly.

    `strict=False` switches to per-market tolerance: a row that raises
    `Unavailable` from `_parse_market` is skipped, and `on_skip(raw, exc)` is
    invoked if the callback is supplied. The non-Yes/No markets Polymarket
    returns in mixed batches (sports Over/Under, NegRisk multi-outcome, etc.)
    fail the strict YES/NO parser by design — silently treating "Over" as
    "Yes" would be the kind of substitution this project exists not to make,
    so the right behaviour is to skip them, record the skip, and continue.
    The HTTP-level failures (status, transport, non-list payload) still raise
    in either mode.
    """
    if limit <= 0 or limit > 500:
        raise ValueError(f"limit must be in 1..500, got {limit}")
    if offset < 0:
        raise ValueError(f"offset must be >= 0, got {offset}")
    if min_volume < 0:
        raise ValueError(f"min_volume must be >= 0, got {min_volume}")

    params = {
        "closed": "true",
        "limit": limit,
        "offset": offset,
        "order": "volume",
        "ascending": "false",
    }
    # Filter at the API level when the caller asks for one category. Multiple
    # categories are not supported by the param, so multi-category filters
    # still happen post-parse in `_filter_markets`.
    if categories is not None and len(categories) == 1:
        params["category"] = categories[0]
    try:
        resp = await client.get(f"{base_url}/markets", params=params)
        resp.raise_for_status()
        payload = resp.json()
    except httpx.HTTPError as exc:
        raise Unavailable(f"Gamma /markets request failed: {exc}") from exc
    if not isinstance(payload, list):
        raise Unavailable(
            f"Gamma /markets returned {type(payload).__name__}, expected a list"
        )

    parsed: list[ResolvedMarket] = []
    for item in payload:
        if not isinstance(item, Mapping):
            continue
        try:
            parsed.append(_parse_market(item))
        except (Unavailable, ValueError) as exc:
            if strict:
                raise
            if on_skip is not None:
                on_skip(item, exc if isinstance(exc, Unavailable) else Unavailable(str(exc)))
    return _filter_markets(parsed, categories=categories, min_volume=min_volume)


async def list_resolved_markets_until(
    client: httpx.AsyncClient,
    *,
    target_count: int,
    page_size: int = 100,
    max_pages: int = 20,
    categories: Sequence[str] | None = None,
    min_volume: float = 0.0,
    on_skip: Callable[[Mapping[str, Any], Unavailable], None] | None = None,
    on_page: Callable[[int, int], None] | None = None,
) -> list[ResolvedMarket]:
    """Page through Gamma until `target_count` parsed markets or `max_pages`.

    Gamma silently caps `/markets` at 100/page, so a single request cannot
    reach a 200+ Yes/No sample (most pages are sports). This helper issues
    sequential `list_resolved_markets` calls at increasing `offset` until it
    collects `target_count` markets, hits an empty page (Gamma exhausted), or
    hits `max_pages` (a safety net against a slow tail).

    `on_page(page_index, batch_size)` is invoked per page if supplied, so the
    runner can report progress without inspecting the accumulator.

    Sequential by design. The Gamma API is not declared concurrent-safe and
    racing it for parallel pages produced transient 502s in early testing.
    """
    if target_count <= 0:
        raise ValueError(f"target_count must be positive, got {target_count}")
    if page_size <= 0 or page_size > 500:
        raise ValueError(f"page_size must be in 1..500, got {page_size}")
    if max_pages <= 0:
        raise ValueError(f"max_pages must be positive, got {max_pages}")

    collected: list[ResolvedMarket] = []
    for page in range(max_pages):
        if len(collected) >= target_count:
            break
        batch = await list_resolved_markets(
            client,
            limit=page_size,
            offset=page * page_size,
            categories=categories,
            min_volume=min_volume,
            strict=False,
            on_skip=on_skip,
        )
        if on_page is not None:
            on_page(page, len(batch))
        if not batch:
            break
        collected.extend(batch)
    return collected[:target_count]


async def fetch_price_history(
    client: httpx.AsyncClient,
    *,
    token_id: str,
    start: datetime,
    end: datetime,
    base_url: str = CLOB_BASE_URL,
    fidelity: int = 60,
) -> list[MarketPricePoint]:
    """CLOB `/prices-history` for one token, as a sorted list of price points.

    The CLOB returns `{"history": {"<unix_seconds>": "<price>", ...}}`. We
    coerce to `MarketPricePoint` and refuse any non-numeric or out-of-range
    sample rather than filter it after the fact — one bad bar in the cutoff
    window would silently shift the benchmark.
    """
    if not token_id.strip():
        raise ValueError("token_id must be non-empty")
    if start.tzinfo is None or end.tzinfo is None:
        raise ValueError("start and end must be timezone-aware")
    if end < start:
        raise ValueError(f"end {end} precedes start {start}")
    if fidelity <= 0:
        raise ValueError(f"fidelity must be positive, got {fidelity}")

    params = {
        "market": token_id,
        "startTs": int(start.timestamp()),
        "endTs": int(end.timestamp()),
        "fidelity": fidelity,
    }
    try:
        resp = await client.get(f"{base_url}/prices-history", params=params)
        resp.raise_for_status()
        payload = resp.json()
    except httpx.HTTPError as exc:
        raise Unavailable(f"CLOB /prices-history failed: {exc}") from exc
    if not isinstance(payload, Mapping):
        raise Unavailable(
            f"CLOB /prices-history returned {type(payload).__name__}, expected a mapping"
        )
    history = payload.get("history")
    if history is None:
        return []
    # CLOB returns `[{"t": <unix_seconds>, "p": <price>}, ...]` -- a list of
    # short-keyed objects. Refuse anything else rather than coerce: a future
    # shape change that drops `t` or `p` would silently hand back zero-point
    # lists and the cutoff benchmark would land on whatever was last seen.
    if not isinstance(history, list):
        raise Unavailable(
            f"`history` is {type(history).__name__}, expected a list of {{t, p}} objects"
        )

    points: list[MarketPricePoint] = []
    for sample in history:
        if not isinstance(sample, Mapping):
            raise Unavailable(
                f"price sample {sample!r} is not a mapping; expected {{t, p}}"
            )
        try:
            ts_int = int(sample["t"])
            price = float(sample["p"])
        except (KeyError, TypeError, ValueError) as exc:
            raise Unavailable(
                f"price sample ({sample!r}) is not parseable"
            ) from exc
        points.append(
            MarketPricePoint(
                at=datetime.fromtimestamp(ts_int, tz=UTC),
                yes_price=price,
            )
        )
    points.sort(key=lambda p: p.at)
    return points
