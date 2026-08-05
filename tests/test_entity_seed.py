"""The market-universe seeder (Phase A).

The load-bearing test is `test_reseed_preserves_a_cik_written_between_boots`: it
proves the upsert MERGES identifiers instead of overwriting them, so a CIK
`assign_company_ciks` wrote between two boots survives a re-seed. Clobbering it
would silently un-resolve every company on the next boot and re-open every
fundamentals gap -- the fabricated-coverage shape the store exists to avoid. The
other tests hold the guarantees on the way there: the right entities exist with
the right identifiers, the sector edges link constituents to their ETFs,
idempotency holds, and the static data is internally consistent (every sector in
the constituents has an ETF to link to).
"""

import json

import pytest

from omni.entities._seed_data import (
    INDICES,
    SECTOR_ETFS,
    SP500_CONSTITUENTS,
)
from omni.entities.seed import (
    COMPANY_KIND,
    INDEX_KIND,
    MEMBER_OF_SECTOR,
    SECTOR_ETF_KIND,
    seed_market_universe,
    run,
)


async def _identifiers(pool, entity_id):
    raw = await pool.fetchval(
        "SELECT identifiers FROM entity WHERE id = $1", entity_id
    )
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
    await db.pool.execute("TRUNCATE entity CASCADE")
    yield


class TestSeed:
    async def test_creates_companies_etfs_and_indices_with_right_kinds(self, db):
        report = await seed_market_universe(db.pool)

        assert report.companies == len(SP500_CONSTITUENTS)
        assert report.sector_etfs == len(SECTOR_ETFS)
        assert report.indices == len(INDICES)
        assert report.unlinked == ()

        kinds = {
            r["kind"]: r["n"]
            for r in await db.pool.fetch(
                "SELECT kind, count(*)::int AS n FROM entity GROUP BY kind"
            )
        }
        assert kinds[COMPANY_KIND] == len(SP500_CONSTITUENTS)
        assert kinds[SECTOR_ETF_KIND] == len(SECTOR_ETFS)
        assert kinds[INDEX_KIND] == len(INDICES)

    async def test_companies_get_polygon_identifier_equal_to_symbol(self, db):
        await seed_market_universe(db.pool)

        aapl = await _entity(db, kind=COMPANY_KIND, symbol="AAPL")
        assert (await _identifiers(db.pool, aapl["id"]))["polygon"] == "AAPL"

    async def test_etfs_get_polygon_and_gics_sector(self, db):
        await seed_market_universe(db.pool)

        xlf = await _entity(db, kind=SECTOR_ETF_KIND, symbol="XLF")
        ids = await _identifiers(db.pool, xlf["id"])
        assert ids["polygon"] == "XLF"
        assert ids["gics_sector"] == "Financials"

    async def test_indices_get_polygon_identifier(self, db):
        # identify.assign_company_ciks only touches kind='company', so the
        # seeder must set polygon on indices itself or they stay unpriceable.
        await seed_market_universe(db.pool)

        spy = await _entity(db, kind=INDEX_KIND, symbol="SPY")
        assert (await _identifiers(db.pool, spy["id"]))["polygon"] == "SPY"

    async def test_member_of_sector_edge_links_constituent_to_its_etf(self, db):
        await seed_market_universe(db.pool)

        aapl = await _entity(db, kind=COMPANY_KIND, symbol="AAPL")
        xlk = await _entity(db, kind=SECTOR_ETF_KIND, symbol="XLK")

        edge = await db.pool.fetchrow(
            "SELECT relation, weight, source FROM entity_edge "
            "WHERE from_entity = $1 AND to_entity = $2 AND relation = $3",
            aapl["id"],
            xlk["id"],
            MEMBER_OF_SECTOR,
        )
        # Direction is load-bearing: from the constituent, to the ETF it is a
        # member of. A swapped from/to would point the deduction chain the wrong
        # way (ETF -> member -> ?).
        assert edge is not None, "AAPL -> XLK member_of_sector edge missing"
        assert edge["relation"] == MEMBER_OF_SECTOR
        assert edge["source"] == "omni.seed"

    async def test_every_constituent_with_a_known_sector_gets_an_edge(self, db):
        report = await seed_market_universe(db.pool)

        sectors_with_etf = {s for _, _, s in SECTOR_ETFS}
        expected = sum(1 for _, _, s in SP500_CONSTITUENTS if s in sectors_with_etf)
        assert report.edges == expected
        assert report.unlinked == ()

        n = await db.pool.fetchval(
            "SELECT count(*)::int FROM entity_edge WHERE relation = $1",
            MEMBER_OF_SECTOR,
        )
        assert n == expected

    async def test_seed_is_idempotent(self, db):
        await seed_market_universe(db.pool)
        first = await db.pool.fetchval("SELECT count(*)::int FROM entity")
        first_edges = await db.pool.fetchval(
            "SELECT count(*)::int FROM entity_edge WHERE relation = $1",
            MEMBER_OF_SECTOR,
        )

        await seed_market_universe(db.pool)
        second = await db.pool.fetchval("SELECT count(*)::int FROM entity")
        second_edges = await db.pool.fetchval(
            "SELECT count(*)::int FROM entity_edge WHERE relation = $1",
            MEMBER_OF_SECTOR,
        )

        assert second == first
        assert second_edges == first_edges

    async def test_reseed_preserves_a_cik_written_between_boots(self, db):
        # The correctness property for the upsert: identifiers are MERGED, never
        # overwritten. assign_company_ciks writes `cik` between boots; a re-seed
        # that clobbered identifiers would un-resolve every company and re-open
        # every fundamentals gap. A wrong `SET identifiers = EXCLUDED.identifiers`
        # drops the cik and this test fails.
        await seed_market_universe(db.pool)
        aapl = await _entity(db, kind=COMPANY_KIND, symbol="AAPL")

        await db.pool.execute(
            "UPDATE entity SET identifiers = identifiers || $1::jsonb WHERE id = $2",
            json.dumps({"cik": "0000320193"}),
            aapl["id"],
        )
        before = await _identifiers(db.pool, aapl["id"])
        assert before["cik"] == "0000320193"

        await seed_market_universe(db.pool)

        after = await _identifiers(db.pool, aapl["id"])
        assert after["cik"] == "0000320193", "re-seed clobbered the CIK"
        assert after["polygon"] == "AAPL", "re-seed dropped the polygon key"

    async def test_reseed_refreshes_name_from_the_static_list(self, db):
        # Name refreshes (a refreshed list correcting a stale name); identifiers
        # do not. The two behave differently on conflict by design.
        await seed_market_universe(db.pool)
        aapl_id = await db.pool.fetchval(
            "SELECT id FROM entity WHERE kind='company' AND symbol='AAPL'"
        )
        await db.pool.execute(
            "UPDATE entity SET name = 'STALE NAME' WHERE id = $1", aapl_id
        )

        await seed_market_universe(db.pool)

        name = await db.pool.fetchval(
            "SELECT name FROM entity WHERE id = $1", aapl_id
        )
        assert name == "Apple Inc."

    async def test_no_constituent_symbol_collides_with_an_etf_or_index(self, db):
        # The entity UNIQUE is (kind, symbol), so a collision across kinds would
        # be legal at the schema but would mean the same ticker is seeded twice
        # under different kinds -- ambiguous for resolve(). The static data must
        # keep these sets disjoint.
        constituent_symbols = {s for s, _, _ in SP500_CONSTITUENTS}
        other_symbols = {s for s, *_ in SECTOR_ETFS} | {s for s, _ in INDICES}
        overlap = constituent_symbols & other_symbols
        assert not overlap, f"constituent symbol(s) reused as ETF/index: {overlap}"


class TestEntryPoint:
    async def test_run_seeds_and_logs_counts(self, db, caplog):
        import logging

        with caplog.at_level(logging.INFO, logger="omni.entities.seed"):
            report = await run(db.pool)

        assert report.companies == len(SP500_CONSTITUENTS)
        assert report.sector_etfs == len(SECTOR_ETFS)
        summary = [
            r for r in caplog.records if "market universe seeded" in r.message
        ]
        assert summary, "run() logged no summary line"
        assert str(len(SP500_CONSTITUENTS)) in summary[0].message
