"""Seed the country universe the sovereign layer scans.

W3.1 of WORLD_COVERAGE_PLAN.md. Before this the system had no geopolitical
dimension at all: macro claims hung off a single `US_MACRO` entity and there was
nothing for a sovereign claim, a sanctions event or a country-ETF prediction to
attach to. This module is the identity half of that dimension.

What this module does, and just as importantly what it does NOT:

  * It seeds IDENTITY only -- `kind`, `symbol`, `name`, and the ISO codes, ETF
    ticker, FX pair and currency each country is addressed by. It writes no
    claims, no prices, no macro series, no risk scores.
  * Every country is linked to its region by a `member_of_region` edge. That
    edge is navigation: a region-level move is how a scan reaches the countries
    under it.
  * It does NOT decide what is predictable at fill time. `Country.is_predictable`
    is settled in the reference data -- a country carries a short-horizon target
    (its ETF, its FX pair) or it does not. The seeder records the identifiers it
    has and reports the counts; a country with neither is seeded, not skipped,
    because it is a real sovereign that other claims may attach to.

Idempotency is load-bearing for the same reason it is in `seed.py` and
`crypto_seed.py`: the scheduler runs this on every boot. The upsert merges
identifiers with `||` rather than overwriting, so a key written between boots (a
resolved provider id, a later-sourced ETF ticker) survives a re-seed. Clobbering
it would silently un-resolve every country on the next boot and re-open every
gap -- the fabricated-coverage shape the store exists to avoid.

The static reference data lives in `_country_seed_data.py`; see that file's
header for why a hardcoded country list is legitimate seed identity, for the ETF
accuracy rule, and for the FX pair convention (`<CCY>USD`, USD per one unit of
the local currency).
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass

from omni.entities._country_seed_data import COUNTRIES, REGIONS

logger = logging.getLogger("omni.entities.country_seed")

COUNTRY_KIND = "country"
REGION_KIND = "region"

MEMBER_OF_REGION = "member_of_region"
EDGE_SOURCE = "omni.country_seed"

_ISO2_KEY = "iso2"
_ISO3_KEY = "iso3"
_ETF_KEY = "etf_symbol"
_FX_KEY = "fx_pair"
_CURRENCY_KEY = "currency"

# Same upsert shape as seed.py and crypto_seed.py: keyed by (kind, symbol),
# MERGE identifiers (||) so a key written between boots survives -- overwriting
# would silently un-resolve every country. Name refreshes from the static list;
# identifiers never lose keys. Returns the id in both the insert and update
# paths.
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


@dataclass(frozen=True)
class CountrySeedReport:
    countries: int = 0
    regions: int = 0
    region_edges: int = 0
    # Countries carrying a short-horizon target, and the two kinds of target.
    # Reported because the conviction gate needs resolved predictions to accrue:
    # `predictable` is the size of the pool that can produce them at all.
    predictable: int = 0
    with_etf: int = 0
    with_fx: int = 0
    # Countries whose region slug had no region entity to link to. Empty with
    # the shipped data (the Country constructor refuses an unknown region), but
    # a region dropped from REGIONS between boots must not crash the seed -- it
    # is reported so an operator sees the dangling countries rather than finding
    # them silently unlinked later.
    unlinked_regions: tuple[str, ...] = ()

    @property
    def total_entities(self) -> int:
        return self.countries + self.regions


async def _upsert(pool, kind: str, symbol: str, name: str, identifiers: dict) -> str:
    row = await pool.fetchrow(_UPSERT_ENTITY, kind, symbol, name, json.dumps(identifiers))
    return row["id"]


async def _edge(pool, frm: str, to: str, relation: str) -> bool:
    inserted = await pool.execute(_INSERT_EDGE, frm, to, relation, EDGE_SOURCE)
    return inserted.endswith("1")


def _country_identifiers(country) -> dict:
    identifiers = {_ISO2_KEY: country.iso2, _ISO3_KEY: country.iso3}
    if country.etf_symbol is not None:
        identifiers[_ETF_KEY] = country.etf_symbol
    if country.fx_pair is not None:
        identifiers[_FX_KEY] = country.fx_pair
    if country.currency is not None:
        identifiers[_CURRENCY_KEY] = country.currency
    return identifiers


async def seed_country_universe(pool) -> CountrySeedReport:
    """Idempotently seed regions and countries.

    Regions are upserted first so their ids exist for the edges that follow.
    Each country gets its ISO codes plus whichever of `etf_symbol`, `fx_pair`
    and `currency` are known -- an absent key means the reference data has no
    confident value, never a placeholder. Re-running leaves a correctly-seeded
    store unchanged and preserves any identifier a later pass wrote between
    boots.
    """
    region_ids: dict[str, str] = {}
    for slug, name in REGIONS:
        region_ids[slug] = await _upsert(pool, REGION_KIND, slug, name, {})

    countries = 0
    region_edges = 0
    predictable = 0
    with_etf = 0
    with_fx = 0
    unlinked: list[str] = []
    for country in COUNTRIES:
        country_id = await _upsert(
            pool,
            COUNTRY_KIND,
            country.iso2,
            country.name,
            _country_identifiers(country),
        )
        countries += 1
        if country.etf_symbol is not None:
            with_etf += 1
        if country.fx_pair is not None:
            with_fx += 1
        if country.is_predictable:
            predictable += 1

        region_id = region_ids.get(country.region)
        if region_id is None:
            # Should not happen with the shipped data (the constructor guards
            # it); reported, not crashed.
            unlinked.append(country.iso2)
        elif await _edge(pool, country_id, region_id, MEMBER_OF_REGION):
            region_edges += 1

    return CountrySeedReport(
        countries=countries,
        regions=len(REGIONS),
        region_edges=region_edges,
        predictable=predictable,
        with_etf=with_etf,
        with_fx=with_fx,
        unlinked_regions=tuple(unlinked),
    )


def _log_report(report: CountrySeedReport) -> None:
    logger.info(
        "country universe seeded: %d countries, %d regions, %d region edges, "
        "%d predictable (%d with etf, %d with fx)",
        report.countries,
        report.regions,
        report.region_edges,
        report.predictable,
        report.with_etf,
        report.with_fx,
    )
    if report.unlinked_regions:
        logger.warning(
            "%d countries had no region entity to link to: %s",
            len(report.unlinked_regions),
            ", ".join(report.unlinked_regions),
        )


async def run(pool) -> CountrySeedReport:
    """Seed the country universe against ``pool`` and log the outcome.

    The scheduler calls this at startup alongside the equities and crypto
    seeders. Pure DB work, no external API, so a failure here is a real defect --
    it is allowed to raise (unlike identify, which contains SEC failures because
    a SEC outage must not stop the loops). A seeder that cannot write to its own
    database is not bootable.
    """
    report = await seed_country_universe(pool)
    _log_report(report)
    return report
