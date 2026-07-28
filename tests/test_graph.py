"""The entity graph — cross-domain traversal, audience-scoped throughout.

The redistribution rule is the one that must not break. The leak test mirrors
the one in tests/test_gaps.py: a neighbour's `byo_only` claim owned by another
user must not surface through `related_coverage`. A traversal that reads around
visibility is a leak, so these tests hold the rule against the CTE composition.
"""

from datetime import UTC, datetime, timedelta
from uuid import uuid4

import asyncpg
import pytest

from omni.coverage.graph import (
    MAX_DEPTH,
    find_path,
    neighbours,
    relate,
    related_coverage,
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


async def _claim(
    db,
    entity_id,
    key="price",
    *,
    value='{"amount": 100}',
    source="polygon",
    confidence=0.9,
    knowledge_date=None,
    event_date=None,
    audience=None,
    claim_type="price_snapshot",
):
    redistributable = "allowed" if audience is None else "byo_only"
    kd = knowledge_date or NOW
    ed = event_date or (kd - timedelta(days=1))
    return await db.pool.fetchval(
        """
        INSERT INTO claim (entity_id, claim_type, key, value, source,
                           event_date, knowledge_date, confidence,
                           redistributable, audience_user_id)
        VALUES ($1,$2,$3,$4::jsonb,$5,$6,$7,$8,$9,$10)
        RETURNING id
        """,
        entity_id,
        claim_type,
        key,
        value,
        source,
        ed,
        kd,
        confidence,
        redistributable,
        audience,
    )


@pytest.fixture(autouse=True)
async def _clean(db):
    await db.pool.execute("TRUNCATE entity CASCADE")
    yield


class TestRelate:
    async def test_creating_an_edge_and_reading_it_back(self, db):
        btc = await _entity(db, "BTC")
        coin = await _entity(db, "COIN")
        await relate(db.pool, btc, coin, relation="influences", source="manual")

        row = await db.pool.fetchrow(
            "SELECT from_entity, to_entity, relation, weight, source "
            "FROM entity_edge"
        )
        assert row["from_entity"] == btc
        assert row["to_entity"] == coin
        assert row["relation"] == "influences"
        assert row["weight"] == 1.0
        assert row["source"] == "manual"

    async def test_a_repeat_updates_weight_rather_than_raising(self, db):
        btc = await _entity(db, "BTC")
        coin = await _entity(db, "COIN")
        await relate(
            db.pool, btc, coin, relation="influences", weight=0.5, source="manual"
        )
        # Same identity, revised weight: an update, not a second row.
        await relate(
            db.pool, btc, coin, relation="influences", weight=2.5, source="manual"
        )

        rows = await db.pool.fetch(
            "SELECT weight FROM entity_edge WHERE relation = 'influences'"
        )
        assert len(rows) == 1
        assert rows[0]["weight"] == 2.5

    async def test_a_different_relation_is_a_distinct_edge(self, db):
        btc = await _entity(db, "BTC")
        coin = await _entity(db, "COIN")
        await relate(db.pool, btc, coin, relation="influences", source="manual")
        await relate(db.pool, btc, coin, relation="correlates", source="manual")

        count = await db.pool.fetchval("SELECT count(*) FROM entity_edge")
        assert count == 2

    async def test_a_self_loop_is_rejected_by_the_schema(self, db):
        btc = await _entity(db, "BTC")
        with pytest.raises(asyncpg.CheckViolationError):
            await relate(db.pool, btc, btc, relation="self", source="manual")


class TestNeighbours:
    async def test_finds_an_edge_from_both_directions_with_direction_reported(
        self, db
    ):
        btc = await _entity(db, "BTC")
        coin = await _entity(db, "COIN")
        await relate(db.pool, btc, coin, relation="influences", source="manual")

        from_btc = await neighbours(db.pool, btc)
        from_coin = await neighbours(db.pool, coin)

        # BTC points at COIN: from BTC it is an outgoing edge.
        assert len(from_btc) == 1
        assert from_btc[0]["entity_id"] == coin
        assert from_btc[0]["direction"] == "out"
        assert from_btc[0]["relation"] == "influences"

        # From COIN's side the same edge is incoming, and the neighbour is BTC.
        assert len(from_coin) == 1
        assert from_coin[0]["entity_id"] == btc
        assert from_coin[0]["direction"] == "in"

    async def test_a_true_cycle_yields_both_directions(self, db):
        btc = await _entity(db, "BTC")
        coin = await _entity(db, "COIN")
        await relate(db.pool, btc, coin, relation="influences", source="manual")
        await relate(db.pool, coin, btc, relation="influences", source="manual")

        rows = await neighbours(db.pool, btc)
        directions = sorted(r["direction"] for r in rows)
        assert directions == ["in", "out"]
        assert all(r["entity_id"] == coin for r in rows)

    async def test_relation_filter_scopes_the_edges_returned(self, db):
        btc = await _entity(db, "BTC")
        coin = await _entity(db, "COIN")
        await relate(db.pool, btc, coin, relation="influences", source="manual")
        await relate(db.pool, btc, coin, relation="correlates", source="manual")

        rows = await neighbours(db.pool, btc, relation="influences")
        assert [r["relation"] for r in rows] == ["influences"]

    async def test_min_weight_filters_out_light_edges(self, db):
        btc = await _entity(db, "BTC")
        coin = await _entity(db, "COIN")
        await relate(
            db.pool, btc, coin, relation="weak", weight=0.1, source="manual"
        )
        await relate(
            db.pool, btc, coin, relation="strong", weight=5.0, source="manual"
        )

        rows = await neighbours(db.pool, btc, min_weight=1.0)
        assert {r["relation"] for r in rows} == {"strong"}


class TestRelatedCoverage:
    async def test_subject_and_neighbour_claims_tagged_with_hop(self, db):
        btc = await _entity(db, "BTC")
        coin = await _entity(db, "COIN")
        await relate(db.pool, btc, coin, relation="influences", source="manual")
        await _claim(db, btc, key="bars", value='{"p": 30000}')
        await _claim(db, coin, key="bars", value='{"p": 250}')

        rows = await related_coverage(db.pool, btc, audience=None, depth=1)

        by_entity = {r["entity_id"]: r for r in rows}
        assert by_entity[btc]["hop"] == 0
        assert by_entity[coin]["hop"] == 1

    async def test_an_isolated_entity_returns_only_its_own_claims(self, db):
        btc = await _entity(db, "BTC")
        await _claim(db, btc, key="bars")
        rows = await related_coverage(db.pool, btc, audience=None, depth=1)
        assert [r["entity_id"] for r in rows] == [btc]
        assert rows[0]["hop"] == 0

    async def test_a_neighbours_byo_only_claim_owned_by_another_user_is_hidden(
        self, db
    ):
        """The leak this module exists to prevent.

        A neighbour holds a private claim under another user's credential. The
        traversal reaches the neighbour (the edge is shared), but composing the
        visibility CTE into the walk must keep that claim out of this audience's
        view. Serving it here makes this deployment the redistributor.
        """
        btc = await _entity(db, "BTC")
        coin = await _entity(db, "COIN")
        owner_a = uuid4()
        owner_b = uuid4()
        await relate(db.pool, btc, coin, relation="influences", source="manual")

        # Shared coverage on BTC; private BYO coverage on COIN owned by A.
        await _claim(db, btc, key="bars", source="fred")
        await _claim(db, coin, key="bars", source="polygon", audience=owner_a)

        visible_to_b = await related_coverage(db.pool, btc, audience=owner_b)
        visible_to_a = await related_coverage(db.pool, btc, audience=owner_a)

        # B sees the shared BTC claim and the COIN neighbour, but NOT A's
        # private claim on COIN.
        b_keys = {(r["entity_id"], r["key"]) for r in visible_to_b}
        assert (btc, "bars") in b_keys
        assert (coin, "bars") not in b_keys

        # A sees their own private claim on the neighbour. The traversal
        # reaches COIN for A; the audience rule admits A's own data.
        a_keys = {(r["entity_id"], r["key"]) for r in visible_to_a}
        assert (btc, "bars") in a_keys
        assert (coin, "bars") in a_keys

    async def test_relation_filter_scopes_traversed_edges_not_the_subject(self, db):
        btc = await _entity(db, "BTC")
        coin = await _entity(db, "COIN")
        eth = await _entity(db, "ETH")
        await relate(db.pool, btc, coin, relation="influences", source="manual")
        await relate(db.pool, btc, eth, relation="correlates", source="manual")
        await _claim(db, btc, key="bars")
        await _claim(db, coin, key="bars")
        await _claim(db, eth, key="bars")

        rows = await related_coverage(
            db.pool, btc, audience=None, depth=1, relation="influences"
        )
        entities = {r["entity_id"] for r in rows}
        # Subject always present; only the 'influences' neighbour reachable.
        assert entities == {btc, coin}
        assert eth not in entities

    async def test_depth_is_capped_at_max_depth(self, db):
        a = await _entity(db, "A")
        b = await _entity(db, "B")
        c = await _entity(db, "C")
        d = await _entity(db, "D")
        await relate(db.pool, a, b, relation="r", source="manual")
        await relate(db.pool, b, c, relation="r", source="manual")
        await relate(db.pool, c, d, relation="r", source="manual")
        await _claim(db, d, key="bars")

        # Caller asks for depth far beyond the cap; D must still be unreachable.
        assert MAX_DEPTH == 2
        rows = await related_coverage(db.pool, a, audience=None, depth=10)
        entities = {r["entity_id"] for r in rows}
        assert d not in entities

    async def test_traversal_terminates_on_a_cyclic_graph(self, db):
        """A cycle must not hang the recursion. hop tagging still works."""
        a = await _entity(db, "A")
        b = await _entity(db, "B")
        c = await _entity(db, "C")
        # A -> B -> C -> A, a directed cycle.
        await relate(db.pool, a, b, relation="r", source="manual")
        await relate(db.pool, b, c, relation="r", source="manual")
        await relate(db.pool, c, a, relation="r", source="manual")
        await _claim(db, a, key="bars")
        await _claim(db, b, key="bars")
        await _claim(db, c, key="bars")

        rows = await related_coverage(db.pool, a, audience=None, depth=2)

        # Each entity appears exactly once, at its shortest hop. No infinite
        # loop, no duplicate rows from re-visiting the cycle.
        seen = {}
        for r in rows:
            seen.setdefault(r["entity_id"], r["hop"])
        assert seen[a] == 0
        assert seen[b] == 1
        assert seen[c] == 1


class TestFindPath:
    async def test_returns_a_two_hop_path(self, db):
        a = await _entity(db, "A")
        b = await _entity(db, "B")
        c = await _entity(db, "C")
        await relate(db.pool, a, b, relation="influences", source="manual")
        await relate(db.pool, b, c, relation="tracks", source="manual")

        path = await find_path(db.pool, a, c, max_depth=3)
        assert path is not None
        assert path["entities"] == [str(a), str(b), str(c)]
        assert path["relations"] == ["influences", "tracks"]
        assert path["depth"] == 2

    async def test_finds_the_reverse_direction_of_an_edge(self, db):
        """The path may traverse an edge backwards: B is reachable from A via
        the A->B edge even when asking A<-B."""
        a = await _entity(db, "A")
        b = await _entity(db, "B")
        await relate(db.pool, a, b, relation="influences", source="manual")

        path = await find_path(db.pool, b, a, max_depth=3)
        assert path is not None
        assert path["entities"] == [str(b), str(a)]
        assert path["relations"] == ["influences"]

    async def test_returns_none_when_no_path_within_max_depth(self, db):
        a = await _entity(db, "A")
        b = await _entity(db, "B")
        c = await _entity(db, "C")
        await relate(db.pool, a, b, relation="r", source="manual")
        await relate(db.pool, b, c, relation="r", source="manual")

        # Two hops away, but max_depth=1 forbids it.
        path = await find_path(db.pool, a, c, max_depth=1)
        assert path is None

    async def test_returns_none_when_no_path_exists_at_all(self, db):
        a = await _entity(db, "A")
        z = await _entity(db, "Z")
        # Two isolated entities, no edges anywhere.
        path = await find_path(db.pool, a, z, max_depth=5)
        assert path is None

    async def test_terminates_on_a_cyclic_graph_and_returns_shortest(self, db):
        a = await _entity(db, "A")
        b = await _entity(db, "B")
        c = await _entity(db, "C")
        # A -> B -> C -> A cycle plus a direct A -> C shortcut.
        await relate(db.pool, a, b, relation="r", source="manual")
        await relate(db.pool, b, c, relation="r", source="manual")
        await relate(db.pool, c, a, relation="r", source="manual")
        await relate(db.pool, a, c, relation="shortcut", source="manual")

        path = await find_path(db.pool, a, c, max_depth=5)
        assert path is not None
        assert path["depth"] == 1
        assert path["relations"] == ["shortcut"]
