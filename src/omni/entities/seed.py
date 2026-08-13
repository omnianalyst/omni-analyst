"""Seed the market universe the autonomous layer scans.

Phase A of AUTONOMOUS_PLAN.md. Before this, the system only held entities a user
had typed in -- a powerful engine pointed at nothing unless someone asked. The
autonomous deduction chain (macro -> sector -> stock) needs a *universe* to walk,
and this is it: the S&P 500 constituents, the 11 GICS sector ETFs the sector
scanner scores, and the broad-market barometers.

What this module does, and just as importantly what it does NOT:

  * It seeds IDENTITY only -- `kind`, `symbol`, `name`, the GICS sector and
    sub-industry, and the `polygon` key each entity fetches by. It writes no
    claims, no prices, no fundamentals.
  * It records one dated membership snapshot per observation of the constituent
    list (migration 056). That is reference data with a date on it, not a
    measurement: the date is the one the list was derived from its source, and a
    re-run of an already-recorded observation writes nothing.
  * Every constituent is linked to its sector ETF by an `entity_edge`
    (`member_of_sector`). That edge is the navigation the deduction chain walks;
    without it the seeded universe is a flat list, not a globe to scan.
  * It does NOT resolve CIKs. `assign_company_ciks` already does that against
    SEC's live map, and the scheduler already runs it on every boot -- after
    this seeder, so the companies exist by the time identify reads them. Keeping
    CIK resolution out of this module preserves the single source of truth for
    provider-key resolution (`entities/identify.py`) and the single source of
    truth for the SEC fetch's failure semantics.

Idempotency is load-bearing: the scheduler runs this on every boot (self-healing
for a refreshed static list, same reason `populate_identifiers` runs on every
boot). The upsert merges identifiers with `||` rather than overwriting, so a CIK
`assign_company_ciks` wrote between boots survives a re-seed. Clobbering it would
silently un-resolve every company on the next boot and re-open every
fundamentals gap -- the fabricated-coverage shape the store exists to avoid.

The static reference data lives in `_seed_data.py`; see that file's header for
why a hardcoded ticker list is legitimate seed identity, not fabricated coverage.
"""

from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass

from omni.entities._seed_data import (
    INDICES,
    MACRO_ENTITIES,
    SECTOR_ETFS,
    SP500_ACCESSED_ON,
    SP500_CONSTITUENTS,
    SP500_INDEX_SYMBOL,
    SP500_SOURCE,
)

logger = logging.getLogger("omni.entities.seed")

COMPANY_KIND = "company"
SECTOR_ETF_KIND = "sector_etf"
INDEX_KIND = "index"
MACRO_KIND = "macro"

# The relation linking a constituent to the ETF that represents its GICS sector.
# The sector scanner (Phase C) walks this edge from a high-scoring ETF to the
# stocks it should demand; the synthesis finding (Phase E) traces it back to
# show "XLK strongest -> AAPL stands out". A constant here, not imported,
# because no other module owns this string yet -- when Phase C consumes it, it
# will read this literal or carry its own the same way the scheduler carries the
# trend method.
MEMBER_OF_SECTOR = "member_of_sector"
EDGE_SOURCE = "omni.seed"

# Polygon indexes every tradeable instrument by ticker, which IS the entity's
# symbol for equities, ETFs and the broad-market trackers seeded here. Setting
# it in the seeder (rather than relying on assign_company_ciks, which only runs
# on kind='company' and only on a successful CIK resolve) means ETFs, indices
# and companies absent from SEC's map are all still priceable -- the trend
# producer is price-based and works for any entity with a polygon key.
_POLYGON_KEY = "polygon"
_GICS_SECTOR_KEY = "gics_sector"

# Sub-industry is the level at which two companies are actually comparable. The
# sector alone puts a software house and a semiconductor fab in the same bucket,
# which is why /scanner/companies could rank a company against its sector but
# not against its peers. Stored on the company entity because that is where the
# sector already lives for ETFs, and the upsert's `||` merge lets a GICS
# reclassification refresh it on the next boot without touching the CIK.
_GICS_SUB_INDUSTRY_KEY = "gics_sub_industry"

# Upsert a row keyed by (kind, symbol). On conflict, MERGE identifiers (||) so a
# CIK written between boots survives -- overwriting would silently un-resolve
# every company. Name refreshes from the static list; identifiers never lose
# keys. Returns the id in both the insert and update paths.
_UPSERT_ENTITY = """
INSERT INTO entity (kind, symbol, name, identifiers)
VALUES ($1, $2, $3, $4::jsonb)
ON CONFLICT (kind, symbol) DO UPDATE
SET name = EXCLUDED.name,
    identifiers = entity.identifiers || EXCLUDED.identifiers
RETURNING id
"""

_INSERT_EDGE = """
INSERT INTO entity_edge (from_entity, to_entity, relation, weight, source)
VALUES ($1, $2, $3, 1.0, $4)
ON CONFLICT (from_entity, to_entity, relation) DO NOTHING
"""

# DO NOTHING, never DO UPDATE. An observation already recorded for a date is
# never rewritten -- rewriting history is how a survivorship-biased record gets
# made, not corrected.
_INSERT_MEMBERSHIP = """
INSERT INTO index_membership
    (index_symbol, member_symbol, observed_on, present, source)
VALUES ($1, $2, $3, $4, $5)
ON CONFLICT (index_symbol, member_symbol, observed_on) DO NOTHING
"""

# The members present at the most recent observation STRICTLY BEFORE this one.
# Strictly before matters: comparing today's snapshot against itself after the
# present rows land would report every member as a departure on a second run.
_PREVIOUS_MEMBERS = """
SELECT member_symbol
FROM index_membership
WHERE index_symbol = $1
  AND present
  AND observed_on = (
      SELECT max(observed_on)
      FROM index_membership
      WHERE index_symbol = $1 AND observed_on < $2
  )
"""


@dataclass(frozen=True)
class SeedReport:
    companies: int = 0
    sector_etfs: int = 0
    indices: int = 0
    macro_entities: int = 0
    edges: int = 0
    # Constituents whose GICS sector had no ETF to link to. Zero with the shipped
    # data (all 11 sectors are covered), but a sector with no ETF must not crash
    # the seed -- it is reported so an operator sees the dangling constituents
    # rather than finding them silently unlinked later.
    unlinked: tuple[tuple[str, str], ...] = ()
    # Rows written by this run's membership snapshot. Both are zero on a re-run
    # of an already-recorded observation, which is the idempotency guarantee
    # rather than a failure. `departed` names the members the previous
    # observation held and this one does not -- the half of the history a static
    # list cannot express.
    membership_present: int = 0
    membership_absent: int = 0
    departed: tuple[str, ...] = ()

    @property
    def total(self) -> int:
        return self.companies + self.sector_etfs + self.indices + self.macro_entities


async def _upsert(pool, kind: str, symbol: str, name: str, identifiers: dict) -> str:
    row = await pool.fetchrow(
        _UPSERT_ENTITY, kind, symbol, name, json.dumps(identifiers)
    )
    return row["id"]


async def snapshot_index_membership(pool) -> tuple[int, int, tuple[str, ...]]:
    """Record what the index held on the date the list was observed.

    Returns (present_written, absent_written, departed_symbols).

    `observed_on` is `SP500_ACCESSED_ON`, the date the constituent list was
    derived from its canonical source -- not the clock. The seeder runs on every
    boot, so stamping now() would have each boot assert a fresh observation of a
    list nobody re-checked, which is a fabricated measurement wearing a real
    date. Refreshing the tuples and the accessed date together is what advances
    the history.

    A member the previous observation held and this one does not is written as
    `present = false`, so a departure is recorded rather than merely stopping.
    An absent row means nobody looked; that has to stay distinguishable from a
    member being gone.
    """
    current = sorted({symbol for symbol, _, _, _ in SP500_CONSTITUENTS})
    previous = {
        row["member_symbol"]
        for row in await pool.fetch(
            _PREVIOUS_MEMBERS, SP500_INDEX_SYMBOL, SP500_ACCESSED_ON
        )
    }
    departed = tuple(sorted(previous.difference(current)))

    present_written = 0
    for symbol in current:
        status = await pool.execute(
            _INSERT_MEMBERSHIP,
            SP500_INDEX_SYMBOL,
            symbol,
            SP500_ACCESSED_ON,
            True,
            SP500_SOURCE,
        )
        if status.endswith("1"):
            present_written += 1

    absent_written = 0
    for symbol in departed:
        status = await pool.execute(
            _INSERT_MEMBERSHIP,
            SP500_INDEX_SYMBOL,
            symbol,
            SP500_ACCESSED_ON,
            False,
            SP500_SOURCE,
        )
        if status.endswith("1"):
            absent_written += 1

    return present_written, absent_written, departed


async def seed_market_universe(pool) -> SeedReport:
    """Idempotently seed sector ETFs, indices, and S&P 500 constituents.

    ETFs and indices get a `polygon` identifier (their symbol) so the fill loop
    can fetch their prices; ETFs also carry their `gics_sector`, the link key
    each constituent's sector maps to. Constituents get `polygon` and a
    `member_of_sector` edge to their sector ETF. Re-running leaves a correctly-
    seeded store unchanged and preserves any CIK `assign_company_ciks` wrote.
    """
    sector_etf_ids: dict[str, str] = {}
    etf_count = 0
    for symbol, name, gics_sector in SECTOR_ETFS:
        entity_id = await _upsert(
            pool,
            SECTOR_ETF_KIND,
            symbol,
            name,
            {_POLYGON_KEY: symbol, _GICS_SECTOR_KEY: gics_sector},
        )
        sector_etf_ids[gics_sector] = entity_id
        etf_count += 1

    index_count = 0
    for symbol, name in INDICES:
        await _upsert(pool, INDEX_KIND, symbol, name, {_POLYGON_KEY: symbol})
        index_count += 1

    macro_count = 0
    for symbol, name in MACRO_ENTITIES:
        await _upsert(pool, MACRO_KIND, symbol, name, {})
        macro_count += 1

    company_count = 0
    edge_count = 0
    unlinked: list[tuple[str, str]] = []
    for symbol, name, gics_sector, gics_sub_industry in SP500_CONSTITUENTS:
        company_id = await _upsert(
            pool,
            COMPANY_KIND,
            symbol,
            name,
            {
                _POLYGON_KEY: symbol,
                _GICS_SECTOR_KEY: gics_sector,
                _GICS_SUB_INDUSTRY_KEY: gics_sub_industry,
            },
        )
        company_count += 1
        etf_id = sector_etf_ids.get(gics_sector)
        if etf_id is None:
            # Should not happen with the shipped data; reported, not crashed.
            unlinked.append((symbol, gics_sector))
            continue
        inserted = await pool.execute(
            _INSERT_EDGE, company_id, etf_id, MEMBER_OF_SECTOR, EDGE_SOURCE
        )
        if inserted.endswith("1"):
            edge_count += 1

    present_written, absent_written, departed = await snapshot_index_membership(pool)

    return SeedReport(
        companies=company_count,
        sector_etfs=etf_count,
        indices=index_count,
        macro_entities=macro_count,
        edges=edge_count,
        unlinked=tuple(unlinked),
        membership_present=present_written,
        membership_absent=absent_written,
        departed=departed,
    )


def _log_report(report: SeedReport) -> None:
    logger.info(
        "market universe seeded: %d companies, %d sector etfs, %d indices, "
        "%d macro, %d sector edges",
        report.companies,
        report.sector_etfs,
        report.indices,
        report.macro_entities,
        report.edges,
    )
    logger.info(
        "index membership observed %s: %d present rows, %d departure rows written",
        SP500_ACCESSED_ON.isoformat(),
        report.membership_present,
        report.membership_absent,
    )
    if report.departed:
        logger.info(
            "%d member(s) left %s since the previous observation: %s",
            len(report.departed),
            SP500_INDEX_SYMBOL,
            ", ".join(report.departed),
        )
    if report.unlinked:
        logger.warning(
            "%d constituents had no sector ETF to link to: %s",
            len(report.unlinked),
            ", ".join(f"{s}({sec})" for s, sec in report.unlinked),
        )


async def run(pool) -> SeedReport:
    """Seed the universe against ``pool`` and log the outcome.

    The scheduler calls this at startup, before `populate_identifiers`, so the
    company entities exist by the time identify reads them. Pure DB work, no
    external API, so a failure here is a real defect -- it is allowed to raise
    (unlike populate_identifiers, which contains SEC failures because a SEC
    outage must not stop the loops). A seeder that cannot write to its own
    database is not bootable.
    """
    report = await seed_market_universe(pool)
    _log_report(report)
    return report


async def main() -> None:
    """CLI entry: seed, then resolve CIKs.

    `python -m omni.entities.seed` stands up the universe and the identifiers in
    one command -- the full Phase A deliverable ("create entities, resolve CIKs,
    set polygon identifiers"). The scheduler does the same two steps itself on
    boot; this main exists for an operator bootstrapping a fresh database
    without starting the loops.
    """
    from omni.config import settings
    from omni.db import connect, migrate
    from omni.entities.identify import run as populate_identifiers

    client = await connect(settings.database_url)
    try:
        await migrate(client)
        await run(client.pool)
        await populate_identifiers(client.pool, user_agent=settings.sec_user_agent)
    finally:
        await client.close()


if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    asyncio.run(main())
