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

import asyncio
import json
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from omni.ingest.protocol import Unavailable

logger = logging.getLogger("omni.entities.identify")

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
    if not isinstance(payload, dict):
        # A well-formed-JSON-but-wrong-shape body (SEC serves a JSON error
        # object, wraps the map in {"data": [...]}, or changes the schema) is a
        # source failure: the structure is what SEC served, not logic we own.
        # Raise Unavailable here so the existing containment folds it into the
        # same fetch_error path as a non-JSON body. This guard catches only
        # structure; a wrong field name within a well-formed entry is a silent
        # logic bug that surfaces as an empty index and zero resolved symbols.
        raise Unavailable(
            "SEC ticker map payload is not a JSON object"
        )
    for entry in payload.values():
        if not isinstance(entry, dict):
            raise Unavailable(
                "SEC ticker map entry is not a JSON object"
            )
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
    try:
        return response.json()
    except json.JSONDecodeError as exc:
        # A 200 whose body is not JSON is still a source failure -- SEC serves
        # an HTML notice to clients it considers undeclared automated tools,
        # and a throttled or gzip-truncated payload decodes the same way. This
        # is decoding a remote payload, not our parse, so it is Unavailable --
        # the same signal the transport and status guards above raise.
        raise Unavailable(
            "SEC company_tickers returned HTTP 200 with a non-JSON body"
        ) from exc


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

    try:
        index = parse_ticker_map(payload)
    except Unavailable as exc:
        # parse_ticker_map raises Unavailable only for a structurally wrong
        # remote payload -- a source failure, same class as the fetch failing.
        # It is caught here for the same reason the fetch is: a body SEC served
        # in a shape we cannot index must not crash a seeding run. A logic bug
        # in the parse raises something other than Unavailable and still
        # surfaces, which is the distinction T2 preserved.
        return IdentifyReport(outcomes=(), fetch_error=str(exc) or repr(exc))

    rows = await pool.fetch(
        "SELECT id, symbol FROM entity WHERE kind = $1", COMPANY_KIND
    )

    outcomes: list[IdentifyOutcome] = []
    for row in rows:
        symbol = row["symbol"]
        status, cik, reason = _classify(index, symbol)
        if status == RESOLVED and cik is not None:
            # Polygon indexes equities by ticker, which IS the entity's symbol.
            # Setting it here (alongside the CIK) lets the fill pipeline's
            # key_for reach the Polygon adapter for price ingestion without a
            # separate seeding step. Crypto assets are a different kind and are
            # not processed here (their Polygon key is a coin slug, not symbol).
            updates = {CIK_KEY: cik}
            symbol = row["symbol"]
            if symbol:
                updates["polygon"] = symbol
            await pool.execute(
                "UPDATE entity "
                "SET identifiers = identifiers || $1::jsonb "
                "WHERE id = $2",
                json.dumps(updates),
                row["id"],
            )
        outcomes.append(
            IdentifyOutcome(
                symbol=symbol or "", status=status, cik=cik, reason=reason
            )
        )

    return IdentifyReport(outcomes=tuple(outcomes))


def _log_report(report: IdentifyReport) -> None:
    if report.fetch_error:
        logger.warning("identifier population failed: %s", report.fetch_error)
        return
    resolved = sum(1 for o in report.outcomes if o.status == RESOLVED)
    absent = sum(1 for o in report.outcomes if o.status == ABSENT)
    ambiguous = sum(1 for o in report.outcomes if o.status == AMBIGUOUS)
    logger.info(
        "identifiers populated: %d resolved, %d absent, %d ambiguous",
        resolved,
        absent,
        ambiguous,
    )
    # Every non-resolve is logged with its symbol and reason: the difference
    # between "the universe is seeded" and "EDGAR silently covers 60% of it" is
    # this list, so an operator can see exactly which symbols got no CIK.
    for outcome in report.outcomes:
        if outcome.status != RESOLVED:
            logger.info(
                "skip %s %s: %s", outcome.status, outcome.symbol, outcome.reason
            )


async def run(
    pool, *, user_agent: str | None = None, fetch_fn: MapFetcher | None = None
) -> None:
    """Run the CIK resolution step against ``pool`` and log the outcome.

    Every SEC failure is contained here and logged, never raised: a missing
    User-Agent raises ``Unavailable`` out of ``assign_company_ciks`` before the
    fetch, and a transport failure is folded into ``report.fetch_error`` inside
    it. Either way ``run`` logs and returns. The scheduler calls this at
    startup -- where identifier population is a precondition it should improve
    when it can, not a dependency it dies on -- and the CLI ``main`` below calls
    the same path.
    """
    try:
        report = await assign_company_ciks(
            pool, user_agent=user_agent, fetch_fn=fetch_fn
        )
    except Unavailable as exc:
        logger.warning("identifier population skipped: %s", exc)
        return
    _log_report(report)


async def main() -> None:
    from omni.config import settings
    from omni.db import connect, migrate

    client = await connect(settings.database_url)
    try:
        await migrate(client)
        await run(client.pool, user_agent=settings.sec_user_agent)
    finally:
        await client.close()


if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    asyncio.run(main())
