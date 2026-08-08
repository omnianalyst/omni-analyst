"""Seed the crypto universe the autonomous layer scans.

The equities seeder (`seed.py`) stands up the S&P 500, sector ETFs and macro
barometers but seeds **zero** crypto entities, so every crypto adapter in this
wave has nothing to attach a claim to. This module is the crypto half of Phase
A: the chains, protocols, sectors and assets the loops demand coverage for.

What this module does, and just as importantly what it does NOT:

  * It seeds IDENTITY only -- `kind`, `symbol`, `name`, and the `coingecko` key
    each asset fetches by (plus a `defillama` slug / `contract_address` where
    one is known). It writes no claims, no prices, no market caps, no TVLs.
  * Every asset is linked to its native chain by an `issued_on` edge, to its
    sector by a `member_of_sector` edge, and -- where the asset carries a
    verified DeFiLlama slug -- to its protocol by a `governs` edge. Those edges
    are the navigation the deduction chain walks.
  * It does NOT resolve anything live. CoinGecko ids come from the verified
    static map in `_crypto_seed_data.py`; contract addresses are None for any
    asset whose address could not be sourced with certainty (see that file's
    header). Keeping live resolution out of this module preserves the single
    source of truth for provider-key resolution (`entities/identify.py` and
    `entities/resolve.py`).

Idempotency is load-bearing for the same reason it is in `seed.py`: the
scheduler runs this on every boot. The upsert merges identifiers with `||`
rather than overwriting, so a key written between boots (a CIK, a resolved
address, anything a later pass adds) survives a re-seed. Clobbering it would
silently un-resolve every asset on the next boot and re-open every price gap --
the fabricated-coverage shape the store exists to avoid.

The static reference data lives in `_crypto_seed_data.py`; see that file's
header for why a hardcoded asset list is legitimate seed identity, not
fabricated coverage.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass

from omni.entities._crypto_seed_data import (
    CHAINS,
    CRYPTO_ASSETS,
    PROTOCOLS,
    SECTORS,
)

logger = logging.getLogger("omni.entities.crypto_seed")

ASSET_KIND = "crypto_asset"
CHAIN_KIND = "chain"
PROTOCOL_KIND = "protocol"
SECTOR_KIND = "sector"

# `member_of_sector` is the same relation the equities seeder writes from each
# S&P 500 constituent to its sector ETF, so a query over sector edges spans both
# domains. Held as a literal here (not imported from seed.py) because no module
# owns the cross-domain string yet and importing would couple this seeder to the
# equities module's internals for a single constant.
ISSUED_ON = "issued_on"
GOVERNS = "governs"
MEMBER_OF_SECTOR = "member_of_sector"
EDGE_SOURCE = "omni.crypto_seed"

_COINGECKO_KEY = "coingecko"
_DEFILLAMA_KEY = "defillama"
_CONTRACT_KEY = "contract_address"

# Same upsert shape as seed.py: keyed by (kind, symbol), MERGE identifiers (||)
# so a key written between boots survives -- overwriting would silently
# un-resolve every asset. Name refreshes from the static list; identifiers never
# lose keys. Returns the id in both the insert and update paths.
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
class CryptoSeedReport:
    assets: int = 0
    chains: int = 0
    protocols: int = 0
    sectors: int = 0
    issued_on_edges: int = 0
    governs_edges: int = 0
    sector_edges: int = 0
    # Assets whose chain slug had no chain entity to link to. Empty with the
    # shipped data (the CryptoAsset constructor refuses an unknown chain), but a
    # chain dropped from CHAINS between boots must not crash the seed -- it is
    # reported so an operator sees the dangling assets rather than finding them
    # silently unlinked later.
    unlinked_chains: tuple[str, ...] = ()

    @property
    def total_entities(self) -> int:
        return self.assets + self.chains + self.protocols + self.sectors


async def _upsert(pool, kind: str, symbol: str, name: str, identifiers: dict) -> str:
    row = await pool.fetchrow(_UPSERT_ENTITY, kind, symbol, name, json.dumps(identifiers))
    return row["id"]


async def _edge(pool, frm: str, to: str, relation: str) -> bool:
    inserted = await pool.execute(_INSERT_EDGE, frm, to, relation, EDGE_SOURCE)
    return inserted.endswith("1")


def _asset_identifiers(asset) -> dict:
    identifiers = {_COINGECKO_KEY: asset.coingecko_id}
    if asset.defillama_slug is not None:
        identifiers[_DEFILLAMA_KEY] = asset.defillama_slug
    if asset.contract_address is not None:
        identifiers[_CONTRACT_KEY] = asset.contract_address
    return identifiers


async def seed_crypto_universe(pool) -> CryptoSeedReport:
    """Idempotently seed chains, protocols, sectors and crypto assets.

    Chains, protocols and sectors are upserted first so their ids exist for the
    edges that follow. Each asset gets a `coingecko` identifier (the key its
    adapter fetches by), plus a `defillama` slug and/or `contract_address` where
    one is known. Re-running leaves a correctly-seeded store unchanged and
    preserves any identifier a later pass wrote between boots.
    """
    chain_ids: dict[str, str] = {}
    for chain in CHAINS:
        chain_ids[chain.slug] = await _upsert(pool, CHAIN_KIND, chain.slug, chain.name, {})

    protocol_ids: dict[str, str] = {}
    for protocol in PROTOCOLS:
        protocol_ids[protocol.defillama_slug] = await _upsert(
            pool,
            PROTOCOL_KIND,
            protocol.defillama_slug,
            protocol.name,
            {_DEFILLAMA_KEY: protocol.defillama_slug},
        )

    sector_ids: dict[str, str] = {}
    for symbol, name in SECTORS:
        sector_ids[symbol] = await _upsert(pool, SECTOR_KIND, symbol, name, {})

    assets = 0
    issued_on = 0
    governs = 0
    sector_edges = 0
    unlinked: list[str] = []
    for asset in CRYPTO_ASSETS:
        asset_id = await _upsert(
            pool,
            ASSET_KIND,
            asset.symbol,
            asset.name,
            _asset_identifiers(asset),
        )
        assets += 1

        chain_id = chain_ids.get(asset.chain)
        if chain_id is None:
            # Should not happen with the shipped data (the constructor guards
            # it); reported, not crashed.
            unlinked.append(asset.symbol)
        else:
            if await _edge(pool, asset_id, chain_id, ISSUED_ON):
                issued_on += 1

        if asset.defillama_slug is not None:
            protocol_id = protocol_ids.get(asset.defillama_slug)
            if protocol_id is not None and await _edge(pool, asset_id, protocol_id, GOVERNS):
                governs += 1

        sector_id = sector_ids.get(asset.sector)
        if sector_id is not None and await _edge(pool, asset_id, sector_id, MEMBER_OF_SECTOR):
            sector_edges += 1

    return CryptoSeedReport(
        assets=assets,
        chains=len(CHAINS),
        protocols=len(PROTOCOLS),
        sectors=len(SECTORS),
        issued_on_edges=issued_on,
        governs_edges=governs,
        sector_edges=sector_edges,
        unlinked_chains=tuple(unlinked),
    )


def _log_report(report: CryptoSeedReport) -> None:
    logger.info(
        "crypto universe seeded: %d assets, %d chains, %d protocols, "
        "%d sectors, %d issued_on, %d governs, %d sector edges",
        report.assets,
        report.chains,
        report.protocols,
        report.sectors,
        report.issued_on_edges,
        report.governs_edges,
        report.sector_edges,
    )
    if report.unlinked_chains:
        logger.warning(
            "%d assets had no chain entity to link to: %s",
            len(report.unlinked_chains),
            ", ".join(report.unlinked_chains),
        )


async def run(pool) -> CryptoSeedReport:
    """Seed the crypto universe against ``pool`` and log the outcome.

    The scheduler calls this at startup alongside the equities seeder. Pure DB
    work, no external API, so a failure here is a real defect -- it is allowed
    to raise (unlike identify, which contains SEC failures because a SEC outage
    must not stop the loops). A seeder that cannot write to its own database is
    not bootable.
    """
    report = await seed_crypto_universe(pool)
    _log_report(report)
    return report
