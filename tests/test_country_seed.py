"""The country-universe seeder (W3.1).

The load-bearing test is `test_reseed_preserves_an_identifier_written_between_boots`:
it proves the upsert MERGES identifiers instead of overwriting them, so a key a
later pass writes between two boots survives a re-seed. Clobbering it would
silently un-resolve every country on the next boot and re-open every gap -- the
fabricated-coverage shape the store exists to avoid.

The rest hold the guarantees the sovereign class depends on. `predictable` is
the pool of countries that can produce a resolved prediction at all, so the
tests check it against the identifiers actually written rather than against the
same property that computed it. The ETF-uniqueness and FX-orientation tests
guard the two silent-misattribution failures: two countries sharing a ticker
predicts one country from another's prices, and an inverted pair (USDJPY read as
JPYUSD) flips the direction of every call on that country.
"""

import json

import pytest

from omni.entities._country_seed_data import COUNTRIES, REGIONS
from omni.entities.country_seed import (
    COUNTRY_KIND,
    MEMBER_OF_REGION,
    REGION_KIND,
    run,
    seed_country_universe,
)


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


def _country(iso2):
    return next(c for c in COUNTRIES if c.iso2 == iso2)


@pytest.fixture(autouse=True)
async def _clean(db):
    # TRUNCATE entity CASCADE drops claim/gap/edges with it, so every test starts
    # from an empty graph -- and lets the no-claim test assert a true zero.
    await db.pool.execute("TRUNCATE entity CASCADE")
    yield


class TestSeed:
    async def test_creates_country_and_region_entities_with_expected_counts(self, db):
        report = await seed_country_universe(db.pool)

        assert report.countries == len(COUNTRIES)
        assert report.regions == len(REGIONS)
        assert report.unlinked_regions == ()

        kinds = {
            r["kind"]: r["n"]
            for r in await db.pool.fetch(
                "SELECT kind, count(*)::int AS n FROM entity GROUP BY kind"
            )
        }
        assert kinds[COUNTRY_KIND] == len(COUNTRIES)
        assert kinds[REGION_KIND] == len(REGIONS)

    async def test_every_country_gets_a_member_of_region_edge(self, db):
        report = await seed_country_universe(db.pool)

        # The count comes from INSERT ... ON CONFLICT DO NOTHING, so on a fresh
        # graph it proves the rows were actually written.
        assert report.region_edges == len(COUNTRIES)

        relations = {
            r["relation"]: r["n"]
            for r in await db.pool.fetch(
                "SELECT relation, count(*)::int AS n FROM entity_edge GROUP BY relation"
            )
        }
        assert relations[MEMBER_OF_REGION] == len(COUNTRIES)

        # And the edge points at the right region, not just at some region.
        germany = await _entity(db, kind=COUNTRY_KIND, symbol="DE")
        europe = await _entity(db, kind=REGION_KIND, symbol="europe")
        assert germany is not None and europe is not None
        edge = await db.pool.fetchrow(
            "SELECT relation FROM entity_edge "
            "WHERE from_entity = $1 AND to_entity = $2 AND relation = $3",
            germany["id"],
            europe["id"],
            MEMBER_OF_REGION,
        )
        assert edge is not None, "DE -> europe member_of_region edge missing"

    async def test_germany_carries_its_iso_codes_etf_fx_and_currency(self, db):
        await seed_country_universe(db.pool)

        germany = await _entity(db, kind=COUNTRY_KIND, symbol="DE")
        assert germany["name"] == "Germany"
        ids = await _identifiers(db.pool, germany["id"])
        assert ids == {
            "iso2": "DE",
            "iso3": "DEU",
            "etf_symbol": "EWG",
            "fx_pair": "EURUSD",
            "currency": "EUR",
        }

    async def test_seed_is_idempotent(self, db):
        await seed_country_universe(db.pool)
        first_entities = await db.pool.fetchval("SELECT count(*)::int FROM entity")
        first_edges = await db.pool.fetchval("SELECT count(*)::int FROM entity_edge")

        await seed_country_universe(db.pool)
        second_entities = await db.pool.fetchval("SELECT count(*)::int FROM entity")
        second_edges = await db.pool.fetchval("SELECT count(*)::int FROM entity_edge")

        assert first_entities == len(COUNTRIES) + len(REGIONS)
        assert second_entities == first_entities
        assert second_edges == first_edges

    async def test_reseed_preserves_an_identifier_written_between_boots(self, db):
        # The correctness property for the upsert: identifiers are MERGED, never
        # overwritten. A later pass could write a key between boots (a resolved
        # provider id, a later-sourced ETF ticker); a re-seed that clobbered
        # identifiers would drop it and un-resolve the country. A wrong
        # `SET identifiers = EXCLUDED.identifiers` drops the injected key and
        # this test fails.
        await seed_country_universe(db.pool)
        japan = await _entity(db, kind=COUNTRY_KIND, symbol="JP")

        await db.pool.execute(
            "UPDATE entity SET identifiers = identifiers || $1::jsonb WHERE id = $2",
            json.dumps({"polygon": "EWJ"}),
            japan["id"],
        )
        before = await _identifiers(db.pool, japan["id"])
        assert before["polygon"] == "EWJ"

        await seed_country_universe(db.pool)

        after = await _identifiers(db.pool, japan["id"])
        assert after["polygon"] == "EWJ", "re-seed clobbered an identifier"
        assert after["iso3"] == "JPN", "re-seed dropped the iso3 key"
        assert after["etf_symbol"] == "EWJ", "re-seed dropped the etf_symbol key"

    async def test_a_country_with_no_etf_and_no_fx_is_seeded_but_not_predictable(self, db):
        # Russia carries neither: its US-listed fund was closed after the 2022
        # sanctions and RUB has no usable USD quote. It is a real sovereign, so
        # it is seeded -- but nothing downstream may write a prediction on it,
        # because there is no series to score one against. This is the guard
        # against a future "if not predictable: continue" that would silently
        # drop real countries from the universe, and against an is_predictable
        # that always answers True.
        russia = _country("RU")
        assert russia.etf_symbol is None and russia.fx_pair is None
        assert russia.is_predictable is False

        await seed_country_universe(db.pool)

        row = await _entity(db, kind=COUNTRY_KIND, symbol="RU")
        assert row is not None, "RU (no etf, no fx) was skipped"
        ids = await _identifiers(db.pool, row["id"])
        assert "etf_symbol" not in ids
        assert "fx_pair" not in ids
        assert ids["iso3"] == "RUS"

        n_unpredictable = sum(1 for c in COUNTRIES if not c.is_predictable)
        assert n_unpredictable > 0, "test is vacuous without an unpredictable country"

    async def test_a_country_with_either_target_alone_is_predictable(self, db):
        # Each target on its own is enough, and the seeder writes the one that
        # exists without inventing the other.
        us = _country("US")
        assert us.etf_symbol == "SPY" and us.fx_pair is None
        assert us.is_predictable is True

        czechia = _country("CZ")
        assert czechia.etf_symbol is None and czechia.fx_pair == "CZKUSD"
        assert czechia.is_predictable is True

        await seed_country_universe(db.pool)

        us_ids = await _identifiers(db.pool, (await _entity(db, kind=COUNTRY_KIND, symbol="US"))["id"])
        assert us_ids["etf_symbol"] == "SPY"
        assert "fx_pair" not in us_ids

        cz_ids = await _identifiers(db.pool, (await _entity(db, kind=COUNTRY_KIND, symbol="CZ"))["id"])
        assert cz_ids["fx_pair"] == "CZKUSD"
        assert "etf_symbol" not in cz_ids

    async def test_reported_predictable_count_matches_the_identifiers_written(self, db):
        # Cross-checked against the database rather than against is_predictable,
        # so the assertion is not the property restating itself: a country the
        # report calls predictable must actually carry a target key.
        report = await seed_country_universe(db.pool)

        with_target = await db.pool.fetchval(
            "SELECT count(*)::int FROM entity WHERE kind = $1 "
            "AND (identifiers ? 'etf_symbol' OR identifiers ? 'fx_pair')",
            COUNTRY_KIND,
        )
        with_etf = await db.pool.fetchval(
            "SELECT count(*)::int FROM entity WHERE kind = $1 AND identifiers ? 'etf_symbol'",
            COUNTRY_KIND,
        )
        with_fx = await db.pool.fetchval(
            "SELECT count(*)::int FROM entity WHERE kind = $1 AND identifiers ? 'fx_pair'",
            COUNTRY_KIND,
        )
        assert report.predictable == with_target
        assert report.with_etf == with_etf
        assert report.with_fx == with_fx
        assert with_target < len(COUNTRIES), "test is vacuous if every country is predictable"

    async def test_no_seeded_row_writes_a_claim(self, db):
        # TRUNCATE entity CASCADE emptied claim; the seeder writes identity only.
        before = await db.pool.fetchval("SELECT count(*)::int FROM claim")

        await seed_country_universe(db.pool)

        after = await db.pool.fetchval("SELECT count(*)::int FROM claim")
        assert before == 0
        assert after == 0, "seeder wrote a claim -- it must write identity only"

    async def test_reseed_refreshes_name_from_the_static_list(self, db):
        await seed_country_universe(db.pool)
        jp_id = await db.pool.fetchval(
            "SELECT id FROM entity WHERE kind = $1 AND symbol = 'JP'", COUNTRY_KIND
        )
        await db.pool.execute("UPDATE entity SET name = 'STALE NAME' WHERE id = $1", jp_id)

        await seed_country_universe(db.pool)

        name = await db.pool.fetchval("SELECT name FROM entity WHERE id = $1", jp_id)
        assert name == "Japan"


class TestReferenceData:
    def test_iso_codes_are_well_formed_and_unique(self):
        malformed = [
            c.iso2 for c in COUNTRIES if len(c.iso2) != 2 or not c.iso2.isupper() or not c.iso2.isalpha()
        ]
        assert malformed == [], f"iso2 must be 2 uppercase letters: {malformed}"

        malformed3 = [
            c.iso3 for c in COUNTRIES if len(c.iso3) != 3 or not c.iso3.isupper() or not c.iso3.isalpha()
        ]
        assert malformed3 == [], f"iso3 must be 3 uppercase letters: {malformed3}"

        iso2s = [c.iso2 for c in COUNTRIES]
        iso3s = [c.iso3 for c in COUNTRIES]
        assert len(set(iso2s)) == len(iso2s), (
            "duplicate iso2: "
            f"{sorted({i for i in iso2s if iso2s.count(i) > 1})}"
        )
        assert len(set(iso3s)) == len(iso3s), (
            "duplicate iso3: "
            f"{sorted({i for i in iso3s if iso3s.count(i) > 1})}"
        )

    def test_no_two_countries_share_an_etf_symbol(self):
        # Two countries on one ticker would predict one country from another's
        # price series -- silent misattribution, the failure the coverage store
        # exists to prevent. Nones are excluded: "no fund" is not a collision.
        tickers = [c.etf_symbol for c in COUNTRIES if c.etf_symbol is not None]
        shared = sorted({t for t in tickers if tickers.count(t) > 1})
        assert shared == [], f"etf_symbol shared by more than one country: {shared}"
        assert len(tickers) < len(COUNTRIES), "test is vacuous if every country has an ETF"

    def test_fx_pairs_follow_the_local_currency_first_convention(self):
        # `<CCY>USD` means USD per one unit of the local currency, for every
        # country without exception. A pair transcribed the interbank way
        # (USDJPY) would invert the series and flip the direction of every
        # prediction on that country, so the orientation is asserted here rather
        # than trusted.
        wrong = [
            (c.iso2, c.fx_pair, c.currency)
            for c in COUNTRIES
            if c.fx_pair is not None
            and (len(c.fx_pair) != 6 or not c.fx_pair.endswith("USD") or c.fx_pair[:3] != c.currency)
        ]
        assert wrong == [], f"fx_pair must be <local currency>USD: {wrong}"

    def test_every_country_region_has_a_region_entity_to_link_to(self):
        # The Country constructor already refuses an unknown region, so this
        # holds by construction -- but it is the assertion that catches a region
        # dropped from REGIONS after the fact.
        referenced = {c.region for c in COUNTRIES}
        declared = {slug for slug, _ in REGIONS}
        assert referenced <= declared, f"country regions with no region entry: {referenced - declared}"

    def test_country_list_is_in_the_planned_size_band(self):
        assert 40 <= len(COUNTRIES) <= 60


class TestEntryPoint:
    async def test_run_seeds_and_logs_counts(self, db, caplog):
        import logging

        with caplog.at_level(logging.INFO, logger="omni.entities.country_seed"):
            report = await run(db.pool)

        assert report.countries == len(COUNTRIES)
        summary = [r for r in caplog.records if "country universe seeded" in r.message]
        assert summary, "run() logged no summary line"
        assert str(len(COUNTRIES)) in summary[0].message
