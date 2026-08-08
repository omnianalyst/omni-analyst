"""The crypto-universe seeder (Phase 1).

The load-bearing test is `test_reseed_preserves_an_identifier_written_between_boots`:
it proves the upsert MERGES identifiers instead of overwriting them, so a key a
later pass writes between two boots survives a re-seed. Clobbering it would
silently un-resolve every asset on the next boot and re-open every price gap --
the fabricated-coverage shape the store exists to avoid. The other tests hold
the guarantees on the way there: the right entity kinds and edges exist, the
coingecko ids have not drifted from the verified map, no claim is written, and
an asset with no contract address is seeded rather than skipped.
"""

import json

import pytest

from omni.entities._crypto_seed_data import (
    CHAINS,
    CRYPTO_ASSETS,
    PROTOCOLS,
    SECTORS,
)
from omni.entities.crypto_seed import (
    ASSET_KIND,
    CHAIN_KIND,
    GOVERNS,
    ISSUED_ON,
    MEMBER_OF_SECTOR,
    PROTOCOL_KIND,
    SECTOR_KIND,
    run,
    seed_crypto_universe,
)
from omni.ingest.coingecko import SYMBOL_TO_ID


async def _identifiers(pool, entity_id):
    raw = await pool.fetchval("SELECT identifiers FROM entity WHERE id = $1", entity_id)
    return json.loads(raw) if isinstance(raw, str) else raw


async def _entity(db, *, kind=None, symbol=None):
    where = []
    params = []
    if kind is not None:
        params.append(kind)
        where.append(f"kind = ${len(params)}")
    if symbol is not None:
        params.append(symbol)
        where.append(f"symbol = ${len(params)}")
    sql = "SELECT id, kind, symbol, name, identifiers FROM entity"
    if where:
        sql += " WHERE " + " AND ".join(where)
    return await db.pool.fetchrow(sql, *params)


@pytest.fixture(autouse=True)
async def _clean(db):
    # TRUNCATE entity CASCADE drops claim/gap/edges with it, so every test starts
    # from an empty graph -- and lets the no-claim test assert a true zero.
    await db.pool.execute("TRUNCATE entity CASCADE")
    yield


class TestSeed:
    async def test_creates_expected_entity_kinds_and_counts(self, db):
        report = await seed_crypto_universe(db.pool)

        assert report.assets == len(CRYPTO_ASSETS)
        assert report.chains == len(CHAINS)
        assert report.protocols == len(PROTOCOLS)
        assert report.sectors == len(SECTORS)
        assert report.unlinked_chains == ()

        kinds = {
            r["kind"]: r["n"]
            for r in await db.pool.fetch(
                "SELECT kind, count(*)::int AS n FROM entity GROUP BY kind"
            )
        }
        assert kinds[ASSET_KIND] == len(CRYPTO_ASSETS)
        assert kinds[CHAIN_KIND] == len(CHAINS)
        assert kinds[PROTOCOL_KIND] == len(PROTOCOLS)
        assert kinds[SECTOR_KIND] == len(SECTORS)

    async def test_writes_all_three_edge_relations(self, db):
        report = await seed_crypto_universe(db.pool)

        # Every asset links to its chain; every asset links to its sector; the
        # three assets with a defillama_slug link to their protocol. The per-edge
        # counts come from INSERT ... ON CONFLICT DO NOTHING, so they prove the
        # rows were actually written on a fresh graph.
        assert report.issued_on_edges == len(CRYPTO_ASSETS)
        assert report.sector_edges == len(CRYPTO_ASSETS)
        with_slug = [a for a in CRYPTO_ASSETS if a.defillama_slug is not None]
        assert len(with_slug) == len(PROTOCOLS)
        assert report.governs_edges == len(with_slug)

        relations = {
            r["relation"]: r["n"]
            for r in await db.pool.fetch(
                "SELECT relation, count(*)::int AS n FROM entity_edge GROUP BY relation"
            )
        }
        assert relations[ISSUED_ON] == len(CRYPTO_ASSETS)
        assert relations[MEMBER_OF_SECTOR] == len(CRYPTO_ASSETS)
        assert relations[GOVERNS] == len(with_slug)

    async def test_btc_links_to_its_chain_sector_and_carries_coingecko_id(self, db):
        await seed_crypto_universe(db.pool)

        btc = await _entity(db, kind=ASSET_KIND, symbol="BTC")
        assert btc is not None
        assert btc["name"] == "Bitcoin"
        assert (await _identifiers(db.pool, btc["id"]))["coingecko"] == "bitcoin"

        chain = await _entity(db, kind=CHAIN_KIND, symbol="bitcoin")
        sector = await _entity(db, kind=SECTOR_KIND, symbol="l1")
        assert chain is not None and sector is not None

        issued = await db.pool.fetchrow(
            "SELECT relation FROM entity_edge "
            "WHERE from_entity = $1 AND to_entity = $2 AND relation = $3",
            btc["id"],
            chain["id"],
            ISSUED_ON,
        )
        assert issued is not None, "BTC -> bitcoin issued_on edge missing"

        member = await db.pool.fetchrow(
            "SELECT relation FROM entity_edge "
            "WHERE from_entity = $1 AND to_entity = $2 AND relation = $3",
            btc["id"],
            sector["id"],
            MEMBER_OF_SECTOR,
        )
        assert member is not None, "BTC -> l1 member_of_sector edge missing"

    async def test_uni_links_to_its_protocol_via_a_governs_edge(self, db):
        await seed_crypto_universe(db.pool)

        uni = await _entity(db, kind=ASSET_KIND, symbol="UNI")
        proto = await _entity(db, kind=PROTOCOL_KIND, symbol="uniswap")
        assert uni is not None and proto is not None
        assert (await _identifiers(db.pool, uni["id"]))["defillama"] == "uniswap"

        governs = await db.pool.fetchrow(
            "SELECT relation FROM entity_edge "
            "WHERE from_entity = $1 AND to_entity = $2 AND relation = $3",
            uni["id"],
            proto["id"],
            GOVERNS,
        )
        assert governs is not None, "UNI -> uniswap governs edge missing"

    async def test_seed_is_idempotent(self, db):
        await seed_crypto_universe(db.pool)
        first_entities = await db.pool.fetchval("SELECT count(*)::int FROM entity")
        first_edges = await db.pool.fetchval("SELECT count(*)::int FROM entity_edge")

        await seed_crypto_universe(db.pool)
        second_entities = await db.pool.fetchval("SELECT count(*)::int FROM entity")
        second_edges = await db.pool.fetchval("SELECT count(*)::int FROM entity_edge")

        assert second_entities == first_entities
        assert second_edges == first_edges

    async def test_reseed_preserves_an_identifier_written_between_boots(self, db):
        # The correctness property for the upsert: identifiers are MERGED, never
        # overwritten. A later pass could write a key between boots (a resolved
        # contract address, an internal note); a re-seed that clobbered
        # identifiers would drop it and un-resolve the asset. A wrong
        # `SET identifiers = EXCLUDED.identifiers` drops the injected key and
        # this test fails.
        await seed_crypto_universe(db.pool)
        btc = await _entity(db, kind=ASSET_KIND, symbol="BTC")

        await db.pool.execute(
            "UPDATE entity SET identifiers = identifiers || $1::jsonb WHERE id = $2",
            json.dumps({"polygon": "X:BTC-USD"}),
            btc["id"],
        )
        before = await _identifiers(db.pool, btc["id"])
        assert before["polygon"] == "X:BTC-USD"

        await seed_crypto_universe(db.pool)

        after = await _identifiers(db.pool, btc["id"])
        assert after["polygon"] == "X:BTC-USD", "re-seed clobbered an identifier"
        assert after["coingecko"] == "bitcoin", "re-seed dropped the coingecko key"

    async def test_reseed_refreshes_name_from_the_static_list(self, db):
        await seed_crypto_universe(db.pool)
        btc_id = await db.pool.fetchval(
            "SELECT id FROM entity WHERE kind=$1 AND symbol='BTC'", ASSET_KIND
        )
        await db.pool.execute("UPDATE entity SET name = 'STALE NAME' WHERE id = $1", btc_id)

        await seed_crypto_universe(db.pool)

        name = await db.pool.fetchval("SELECT name FROM entity WHERE id = $1", btc_id)
        assert name == "Bitcoin"

    async def test_coingecko_ids_do_not_drift_from_the_verified_map(self, db):
        # The coingecko_id in the data file is an independent literal copied from
        # coingecko.SYMBOL_TO_ID; this check catches them diverging. It is not
        # tautological: a single-character typo in the literal makes `drifts`
        # non-empty. (Deriving the id from the map at construction would make
        # this assertion always pass -- which is why the data file uses literals
        # instead.)
        checked = 0
        drifts: list[tuple[str, str, str]] = []
        for asset in CRYPTO_ASSETS:
            mapped = SYMBOL_TO_ID.get(asset.symbol.lower())
            if mapped is None:
                continue
            checked += 1
            if asset.coingecko_id != mapped:
                drifts.append((asset.symbol, asset.coingecko_id, mapped))

        assert checked == len(CRYPTO_ASSETS), (
            "every asset symbol should resolve via the verified map; "
            f"{len(CRYPTO_ASSETS) - checked} did not"
        )
        assert drifts == [], "coingecko_id drifted from SYMBOL_TO_ID: " + repr(drifts)

    async def test_no_seeded_row_writes_a_claim(self, db):
        # TRUNCATE entity CASCADE emptied claim; the seeder writes identity only.
        before = await db.pool.fetchval("SELECT count(*)::int FROM claim")

        await seed_crypto_universe(db.pool)

        after = await db.pool.fetchval("SELECT count(*)::int FROM claim")
        assert before == 0
        assert after == 0, "seeder wrote a claim -- it must write identity only"

    async def test_an_asset_with_no_contract_address_is_seeded_not_skipped(self, db):
        # BTC's contract_address is None: it is a native coin with no contract.
        # It must be created (not skipped) and must carry no contract_address
        # identifier key. This is the guard against a future "if address is None:
        # continue" that would silently drop every native coin from the universe.
        await seed_crypto_universe(db.pool)

        btc = await _entity(db, kind=ASSET_KIND, symbol="BTC")
        assert btc is not None, "BTC (contract_address=None) was skipped"
        ids = await _identifiers(db.pool, btc["id"])
        assert "contract_address" not in ids

        n_none = sum(1 for a in CRYPTO_ASSETS if a.contract_address is None)
        n_assets = await db.pool.fetchval(
            "SELECT count(*)::int FROM entity WHERE kind = $1", ASSET_KIND
        )
        assert n_assets == len(CRYPTO_ASSETS)
        assert n_none > 0, "test is vacuous without a None-address asset"

    async def test_a_sourced_contract_address_is_stored(self, db):
        # USDT carries a canonical Ethereum address; it is stored as an
        # identifier so a future on-chain adapter can key by it. Pairs with the
        # None-address test to prove the field is honest in both states.
        await seed_crypto_universe(db.pool)

        usdt = await _entity(db, kind=ASSET_KIND, symbol="USDT")
        ids = await _identifiers(db.pool, usdt["id"])
        assert ids["contract_address"] == ("0xdAC17F958D2ee523a2206206994597C13D831ec7")

    async def test_every_asset_chain_has_an_entity_to_link_to(self, db):
        # Data-consistency: the CryptoAsset constructor already refuses an
        # unknown chain, so this should hold by construction -- but it is the
        # assertion that catches a chain dropped from CHAINS after the fact.
        report = await seed_crypto_universe(db.pool)
        assert report.unlinked_chains == ()

        referenced = {a.chain for a in CRYPTO_ASSETS}
        present = {
            r["symbol"]
            for r in await db.pool.fetch("SELECT symbol FROM entity WHERE kind = $1", CHAIN_KIND)
        }
        assert referenced <= present, f"asset chains with no chain entity: {referenced - present}"


class TestEntryPoint:
    async def test_run_seeds_and_logs_counts(self, db, caplog):
        import logging

        with caplog.at_level(logging.INFO, logger="omni.entities.crypto_seed"):
            report = await run(db.pool)

        assert report.assets == len(CRYPTO_ASSETS)
        summary = [r for r in caplog.records if "crypto universe seeded" in r.message]
        assert summary, "run() logged no summary line"
        assert str(len(CRYPTO_ASSETS)) in summary[0].message
