"""Populate company entities' CIK identifier from SEC's published ticker map.

`entities/resolve.py` reads a provider's lookup key out of `entity.identifiers`
(`cik` for SEC EDGAR), but nothing in this codebase ever wrote one -- the only
`INSERT INTO entity` writes `kind, symbol, name`, so `identifiers` stays `'{}'`.
The consequence is that `resolve.key_for(entity, "sec_edgar")` can only raise
`Unavailable`, which means no `fundamental_metric` gap can be filled even though
EDGAR is the one provider licensed for redistribution.

This module is the missing step. SEC publishes a single document mapping every
ticker it tracks to a CIK

    https://www.sec.gov/files/company_tickers.json

fetched once per run and reused for the whole universe. For each company entity
it looks up the symbol and, on an unambiguous match, merges `cik` into
`identifiers` without disturbing the keys other providers need.

The rule that overrides every other consideration is the one `resolve.py`
states: ambiguity resolves to nothing, never a guess. A ticker mapped to more
than one CIK, or a symbol absent from the map, writes nothing and is reported.
A wrong CIK would silently attribute another company's financials to this
entity, and the resulting claims would look well-formed -- exactly the
misattribution the coverage store exists to prevent.
"""

from __future__ import annotations

import json
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from omni.ingest.protocol import Unavailable

COMPANY_TICKERS_URL = "https://www.sec.gov/files/company_tickers.json"

# The JSONB key `resolve._PROVIDER_IDENTIFIER["sec_edgar"]` reads; this module
# writes. Kept as a literal rather than imported because resolve.py owns the
# full provider->key mapping and this module owns only the SEC half -- a
# literal here makes the coupling visible without widening either module's
# reach. `resolve.key_for` is the contract; if it ever stops reading "cik",
# the contract test in test_entity_identify.py fails.
CIK_KEY = "cik"
COMPANY_KIND = "company"

RESOLVED = "resolved"
ABSENT = "absent"
AMBIGUOUS = "ambiguous"

MapFetcher = Callable[[], Awaitable[dict | None]]


@dataclass(frozen=True)
class IdentifyOutcome:
    symbol: str
    status: str
    cik: str | None = None
    reason: str | None = None


@dataclass(frozen=True)
class IdentifyReport:
    outcomes: tuple[IdentifyOutcome, ...]
    fetch_error: str | None = None


def parse_ticker_map(payload: dict) -> dict[str, list[str]]:
    """Index SEC's ticker map as uppercase ticker -> list of CIK strings.

    A list per ticker, not a single value, so a ticker mapped to more than one
    CIK stays detectable: collapsing to one would hide the ambiguity this
    module must refuse to resolve. Tickers are uppercased because entity
    symbols are free-text and `resolve.py` matches case-insensitively; CIKs are
    stringified because the JSONB column holds text values and `key_for`
    returns them verbatim to the EDGAR adapter.
    """
    index: dict[str, list[str]] = {}
    for entry in payload.values():
        ticker = entry.get("ticker")
        cik = entry.get("cik_str")
        if not ticker or cik is None:
            continue
        index.setdefault(str(ticker).upper(), []).append(str(cik))
    return index


def _classify(
    index: dict[str, list[str]], symbol: str | None
) -> tuple[str, str | None, str | None]:
    """Decide one symbol's outcome against the ticker map.

    Returns ``(status, cik_or_None, reason_or_None)``. Ambiguity is measured
    over *distinct* CIKs: SEC's map lists a row per ticker per filer, and a
    duplicated row for the same CIK is not a second company.
    """
    if not symbol:
        return ABSENT, None, "entity has no symbol to look up"
    candidates = index.get(str(symbol).upper())
    if not candidates:
        return (
            ABSENT,
            None,
            f"symbol {symbol!r} absent from SEC ticker map",
        )
    distinct = sorted(set(candidates))
    if len(distinct) > 1:
        return (
            AMBIGUOUS,
            None,
            (
                f"ticker {symbol!r} maps to {len(distinct)} CIKs: "
                f"{', '.join(distinct)}"
            ),
        )
    return RESOLVED, distinct[0], None


async def _fetch_ticker_map(user_agent: str) -> dict:
    import httpx

    headers = {"User-Agent": user_agent}
    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            response = await client.get(COMPANY_TICKERS_URL, headers=headers)
    except httpx.HTTPError as exc:
        # Translate every transport failure into the project's single
        # source-failure signal, so the caller has one exception type to
        # contain. A connection error and a 503 are both "SEC did not answer".
        raise Unavailable(f"SEC company_tickers fetch failed: {exc}") from exc
    if response.status_code != 200:
        raise Unavailable(
            f"SEC company_tickers returned HTTP {response.status_code}"
        )
    return response.json()


async def assign_company_ciks(
    pool,
    *,
    user_agent: str | None = None,
    fetch_fn: MapFetcher | None = None,
) -> IdentifyReport:
    """Resolve and write each company entity's CIK into `identifiers`.

    The SEC ticker map is fetched once -- via `fetch_fn` if given, otherwise
    through the project's SEC User-Agent (the same mechanism `edgar.py` uses,
    because SEC blocks clients that do not identify themselves and a block
    affects every EDGAR fill this system will ever make). The one document is
    reused for every company: a single request for the whole universe.

    For each company entity, an unambiguous ticker -> CIK match merges `cik`
    into `identifiers` with a JSONB `||`, so a `polygon` or `coingecko` key
    already present survives. A symbol absent from the map, or a ticker mapped
    to more than one CIK, writes nothing and is returned in the report rather
    than failing silently. A failed fetch writes nothing and does not raise
    past this caller.

    The CIK is stored as a canonical unpadded string (e.g. ``"320193"``):
    `edgar._fetch_companyfacts` pads for its own URL via ``zfill(10)``, so
    storing padded here would own the same responsibility in two places.

    Idempotent: re-merging the same `cik` yields the same `identifiers`, so a
    second run leaves a correctly-populated row unchanged.
    """
    fetcher = fetch_fn
    if fetcher is None:
        if not user_agent:
            raise Unavailable(
                "assign_company_ciks needs fetch_fn or a SEC User-Agent"
            )

        async def fetcher() -> dict | None:
            return await _fetch_ticker_map(user_agent)

    try:
        payload = await fetcher()
    except Unavailable as exc:
        # Contain only the fetch: a transient SEC outage must not crash a
        # seeding run. The parse and writes are outside this guard, so a bug
        # there still surfaces. No identifier is written on failure -- a
        # guessed or placeholder CIK handed to `key_for` is worse than none.
        return IdentifyReport(outcomes=(), fetch_error=str(exc) or repr(exc))

    if not payload:
        return IdentifyReport(outcomes=(), fetch_error="empty ticker map")

    index = parse_ticker_map(payload)

    rows = await pool.fetch(
        "SELECT id, symbol FROM entity WHERE kind = $1", COMPANY_KIND
    )

    outcomes: list[IdentifyOutcome] = []
    for row in rows:
        symbol = row["symbol"]
        status, cik, reason = _classify(index, symbol)
        if status == RESOLVED and cik is not None:
            await pool.execute(
                "UPDATE entity "
                "SET identifiers = identifiers || $1::jsonb "
                "WHERE id = $2",
                json.dumps({CIK_KEY: cik}),
                row["id"],
            )
        outcomes.append(
            IdentifyOutcome(
                symbol=symbol or "", status=status, cik=cik, reason=reason
            )
        )

    return IdentifyReport(outcomes=tuple(outcomes))
