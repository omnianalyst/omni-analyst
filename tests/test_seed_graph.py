"""Seeding the entity graph with structurally-true relationships.

The point of the seed is the last test: after seeding, a company's
`related_coverage` reaches claims held on the macro entity, which is the
cross-domain reach the divergence refusal said was missing. The earlier tests
hold the guarantees the seed makes on the way there -- macro creation,
idempotency, honest skipping of absent endpoints, and a reason on every edge.
"""

from datetime import UTC, datetime, timedelta
from uuid import UUID

import pytest

from omni.coverage.graph import related_coverage
from omni.coverage.seed_graph import (
    MACRO_KIND,
    MACRO_RELATION,
    MACRO_SOURCE,
    MACRO_SYMBOL,
    seed_known_relationships,
)

NOW = datetime(2026, 7, 27, tzinfo=UTC)


async def _entity(db, symbol, kind="asset", name=None):
    return await db.pool.fetchval(
        "INSERT INTO entity (kind, symbol, name) VALUES ($1, $2, $3) "
        "RETURNING id",
        kind,
        symbol,
        name or symbol,
    )


async def _macro_claim(db, entity_id, *, key="vix_close", value='{"vix": 18.2}'):
    return await db.pool.fetchval(
        """
        INSERT INTO claim (entity_id, claim_type, key, value, source,
                           event_date, knowledge_date, confidence,
                           redistributable, audience_user_id)
        VALUES ($1, 'perception_macro', $2, $3::jsonb, 'fred',
                $4, $5, 0.9, 'allowed', NULL)
        RETURNING id
        """,
        entity_id,
        key,
        value,
        NOW - timedelta(days=1),
        NOW,
    )


@pytest.fixture(autouse=True)
async def _clean(db):
    await db.pool.execute("TRUNCATE entity CASCADE")
    yield


class TestSeedKnownRelationships:
    async def test_a_company_gains_an_influenced_by_edge_to_macro(self, db):
        company = await _entity(db, "ACME", kind="company")

        written = await seed_known_relationships(db.pool)

        assert written >= 1
        macro = await db.pool.fetchrow(
            "SELECT id, kind, symbol FROM entity "
            "WHERE kind = $1 AND symbol = $2",
            MACRO_KIND,
            MACRO_SYMBOL,
        )
        assert macro is not None
        edge = await db.pool.fetchrow(
            "SELECT relation, weight, source FROM entity_edge "
            "WHERE from_entity = $1 AND to_entity = $2",
            company,
            macro["id"],
        )
        assert edge is not None
        assert edge["relation"] == MACRO_RELATION
        assert edge["source"] == MACRO_SOURCE

    async def test_the_macro_entity_is_created_when_absent(self, db):
        await _entity(db, "ACME", kind="company")
        before = await db.pool.fetchval(
            "SELECT count(*) FROM entity WHERE kind = $1 AND symbol = $2",
            MACRO_KIND,
            MACRO_SYMBOL,
        )
        assert before == 0

        await seed_known_relationships(db.pool)

        macro = await db.pool.fetchrow(
            "SELECT kind, symbol, name FROM entity "
            "WHERE kind = $1 AND symbol = $2",
            MACRO_KIND,
            MACRO_SYMBOL,
        )
        assert macro is not None
        assert macro["kind"] == MACRO_KIND
        assert macro["symbol"] == MACRO_SYMBOL

    async def test_running_twice_is_a_noop_the_second_time(self, db):
        await _entity(db, "ACME", kind="company")
        await _entity(db, "BTC")
        await _entity(db, "ETH")
        await _entity(db, "COIN", kind="company")
        await _entity(db, "MSTR", kind="company")

        await seed_known_relationships(db.pool)
        rows_after_first = await db.pool.fetch(
            "SELECT from_entity, to_entity, relation, source FROM entity_edge "
            "ORDER BY from_entity, to_entity, relation"
        )

        await seed_known_relationships(db.pool)
        rows_after_second = await db.pool.fetch(
            "SELECT from_entity, to_entity, relation, source FROM entity_edge "
            "ORDER BY from_entity, to_entity, relation"
        )

        assert rows_after_first == rows_after_second
        assert len(rows_after_first) > 0

    async def test_a_pair_whose_endpoint_is_missing_is_skipped_not_conjured(
        self, db
    ):
        await _entity(db, "ACME", kind="company")
        await _entity(db, "BTC")
        # COIN, MSTR, ETH are intentionally absent.

        written = await seed_known_relationships(db.pool)

        # Only the company -> macro edge is writable: every cross-domain pair
        # names an absent endpoint, so none of them count as written.
        assert written == 1

        # No entity was created to satisfy a cross-domain pair.
        for missing in ("COIN", "MSTR", "ETH"):
            found = await db.pool.fetchval(
                "SELECT id FROM entity WHERE symbol = $1", missing
            )
            assert found is None

        # No edge references a BTC -> COIN / BTC -> MSTR / ETH -> COIN link.
        cross_domain = await db.pool.fetch(
            "SELECT relation FROM entity_edge WHERE relation = 'influences'"
        )
        assert cross_domain == []

    async def test_every_edge_carries_a_non_empty_source(self, db):
        await _entity(db, "ACME", kind="company")
        await _entity(db, "BTC")
        await _entity(db, "ETH")
        await _entity(db, "COIN", kind="company")
        await _entity(db, "MSTR", kind="company")

        await seed_known_relationships(db.pool)

        sources = await db.pool.fetch(
            "SELECT source FROM entity_edge WHERE source IS NULL OR source = ''"
        )
        assert sources == []
        total = await db.pool.fetchval("SELECT count(*) FROM entity_edge")
        assert total > 0

    async def test_related_coverage_reaches_macro_claims_from_a_company(
        self, db
    ):
        company = await _entity(db, "ACME", kind="company")

        await seed_known_relationships(db.pool)

        macro_id: UUID = await db.pool.fetchval(
            "SELECT id FROM entity WHERE kind = $1 AND symbol = $2",
            MACRO_KIND,
            MACRO_SYMBOL,
        )
        claim_id = await _macro_claim(db, macro_id)

        rows = await related_coverage(db.pool, company, audience=None)

        reached = {
            (r["entity_id"], r["claim_type"], r["key"], r["hop"]) for r in rows
        }
        assert (macro_id, "perception_macro", "vix_close", 1) in reached
        claim_ids = {r["id"] for r in rows}
        assert claim_id in claim_ids
