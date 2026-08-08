"""Convergence — independent claim families agreeing inside a window.

The load-bearing property is that the threshold counts distinct claim FAMILIES,
never events. `TestVolumeIsNotConvergence` is the test that separates a correct
implementation from one that merely counts rows: every other test here passes
for a volume threshold too.

The second property that must not break is the audience boundary, held here in
both directions — a byo_only claim corroborates for its own owner and is
invisible to everyone else, including to the shared network.
"""

from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest

from omni.convergence import CLAIM_FAMILIES, detect, detect_all

AS_OF = datetime(2026, 8, 7, 12, 0, tzinfo=UTC)
WINDOW = timedelta(hours=1)
WINDOW_START = AS_OF - WINDOW
TICK = timedelta(microseconds=1)


async def _entity(db, symbol="BTC"):
    return await db.pool.fetchval(
        "INSERT INTO entity (kind, symbol, name) VALUES ('crypto', $1, $1) "
        "RETURNING id",
        symbol,
    )


async def _claim(
    db,
    entity_id,
    claim_type,
    *,
    key="BTCUSDT",
    source="binance",
    knowledge_date=AS_OF,
    audience=None,
    value='{"observed": 1}',
):
    redistributable = "allowed" if audience is None else "byo_only"
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
        knowledge_date,
        knowledge_date,
        0.9,
        redistributable,
        audience,
    )


async def _find(db, entity_id, *, audience=None, min_families=2, as_of=AS_OF):
    return await detect(
        db.pool,
        entity_id=entity_id,
        audience_user_id=audience,
        window=WINDOW,
        min_families=min_families,
        as_of=as_of,
    )


@pytest.fixture(autouse=True)
async def _clean(db):
    await db.pool.execute("TRUNCATE entity CASCADE")
    yield


class TestSchema:
    async def test_migration_041_added_the_convergence_claim_type(self, db):
        present = await db.pool.fetchval(
            "SELECT EXISTS (SELECT 1 FROM pg_enum e "
            "JOIN pg_type t ON t.oid = e.enumtypid "
            "WHERE t.typname = 'claim_type' AND e.enumlabel = 'convergence')"
        )
        assert present is True

    def test_families_group_only_non_independent_claim_types(self):
        assert CLAIM_FAMILIES["funding_rate"] == CLAIM_FAMILIES["basis"]
        assert CLAIM_FAMILIES["orderbook_snapshot"] == CLAIM_FAMILIES["trade_tape"]
        assert CLAIM_FAMILIES["news_event"] == CLAIM_FAMILIES["perception_news"]
        assert CLAIM_FAMILIES["price_snapshot"] != CLAIM_FAMILIES["trade_tape"]
        # Derived types are functions of claims already counted; admitting one
        # would let a single underlying observation vote twice.
        assert "perception_divergence" not in CLAIM_FAMILIES
        assert "regime_assessment" not in CLAIM_FAMILIES


class TestDiversityTriggers:
    async def test_three_distinct_families_converge_and_are_named(self, db):
        entity_id = await _entity(db)
        await _claim(db, entity_id, "price_snapshot")
        await _claim(db, entity_id, "news_event")
        await _claim(db, entity_id, "funding_rate")

        found = await _find(db, entity_id, min_families=3)

        assert found is not None
        assert found.entity_id == entity_id
        assert found.families == ("derivatives", "narrative", "price")
        assert found.family_count == 3
        assert found.window_start == WINDOW_START
        assert found.window_end == AS_OF

    async def test_exactly_min_families_triggers_and_one_fewer_does_not(self, db):
        entity_id = await _entity(db)
        await _claim(db, entity_id, "price_snapshot")
        await _claim(db, entity_id, "news_event")

        assert await _find(db, entity_id, min_families=3) is None

        await _claim(db, entity_id, "trade_tape")

        found = await _find(db, entity_id, min_families=3)
        assert found is not None
        assert found.family_count == 3
        assert found.families == ("microstructure", "narrative", "price")

    async def test_min_families_below_two_is_refused(self, db):
        entity_id = await _entity(db)
        await _claim(db, entity_id, "price_snapshot")

        with pytest.raises(ValueError, match="min_families"):
            await _find(db, entity_id, min_families=1)


class TestVolumeIsNotConvergence:
    async def test_ten_claims_from_one_family_do_not_converge(self, db):
        entity_id = await _entity(db)
        for minute in range(10):
            await _claim(
                db,
                entity_id,
                "trade_tape",
                knowledge_date=AS_OF - timedelta(minutes=minute),
            )

        assert await _find(db, entity_id, min_families=2) is None

    async def test_a_flooding_family_plus_one_other_converges_at_exactly_two(self, db):
        entity_id = await _entity(db)
        for minute in range(10):
            await _claim(
                db,
                entity_id,
                "trade_tape",
                knowledge_date=AS_OF - timedelta(minutes=minute),
            )
        await _claim(db, entity_id, "news_event")

        found = await _find(db, entity_id, min_families=2)

        assert found is not None
        assert found.families == ("microstructure", "narrative")
        assert len(found.claim_ids) == 11

    async def test_two_claim_types_of_one_family_are_still_one_family(self, db):
        entity_id = await _entity(db)
        await _claim(db, entity_id, "orderbook_snapshot")
        await _claim(db, entity_id, "trade_tape")

        assert await _find(db, entity_id, min_families=2) is None


class TestWindow:
    async def test_claims_on_both_window_edges_are_included(self, db):
        entity_id = await _entity(db)
        await _claim(db, entity_id, "price_snapshot", knowledge_date=WINDOW_START)
        await _claim(db, entity_id, "news_event", knowledge_date=AS_OF)

        found = await _find(db, entity_id, min_families=2)

        assert found is not None
        assert found.families == ("narrative", "price")

    async def test_a_claim_one_tick_before_the_window_is_excluded(self, db):
        entity_id = await _entity(db)
        await _claim(
            db, entity_id, "price_snapshot", knowledge_date=WINDOW_START - TICK
        )
        await _claim(db, entity_id, "news_event", knowledge_date=AS_OF)

        assert await _find(db, entity_id, min_families=2) is None

    async def test_a_claim_knowable_after_as_of_is_excluded(self, db):
        entity_id = await _entity(db)
        await _claim(db, entity_id, "price_snapshot", knowledge_date=AS_OF + TICK)
        await _claim(db, entity_id, "news_event", knowledge_date=AS_OF)

        assert await _find(db, entity_id, min_families=2) is None


class TestAudienceScoping:
    async def test_another_users_private_claim_does_not_contribute(self, db):
        entity_id = await _entity(db)
        owner = uuid4()
        stranger = uuid4()
        await _claim(db, entity_id, "news_event")
        await _claim(db, entity_id, "price_snapshot", audience=owner)

        assert await _find(db, entity_id, audience=stranger, min_families=2) is None
        assert await _find(db, entity_id, audience=None, min_families=2) is None

        owned = await _find(db, entity_id, audience=owner, min_families=2)
        assert owned is not None
        assert owned.families == ("narrative", "price")

    async def test_detect_all_does_not_leak_a_private_family_across_audiences(
        self, db
    ):
        entity_id = await _entity(db)
        owner = uuid4()
        await _claim(db, entity_id, "news_event")
        await _claim(db, entity_id, "funding_rate", audience=owner)

        stranger_view = await detect_all(
            db.pool,
            audience_user_id=uuid4(),
            window=WINDOW,
            min_families=2,
            as_of=AS_OF,
        )
        owner_view = await detect_all(
            db.pool,
            audience_user_id=owner,
            window=WINDOW,
            min_families=2,
            as_of=AS_OF,
        )

        assert stranger_view == []
        assert [c.entity_id for c in owner_view] == [entity_id]


class TestProvenance:
    async def test_claim_ids_are_the_actual_constituent_claims(self, db):
        entity_id = await _entity(db)
        expected = {
            await _claim(db, entity_id, "price_snapshot"),
            await _claim(db, entity_id, "news_event"),
            await _claim(db, entity_id, "protocol_fees"),
        }
        await _claim(
            db, entity_id, "onchain_flow", knowledge_date=WINDOW_START - TICK
        )

        found = await _find(db, entity_id, min_families=3)

        assert found is not None
        assert set(found.claim_ids) == expected

        rows = await db.pool.fetch(
            "SELECT claim_type::text AS claim_type, entity_id FROM claim "
            "WHERE id = ANY($1)",
            list(found.claim_ids),
        )
        assert len(rows) == 3
        assert {r["entity_id"] for r in rows} == {entity_id}
        assert {CLAIM_FAMILIES[r["claim_type"]] for r in rows} == set(found.families)


class TestDetectAll:
    async def test_one_convergence_per_qualifying_entity_and_none_for_the_rest(
        self, db
    ):
        qualifying = await _entity(db, "BTC")
        flooded = await _entity(db, "ETH")
        also_qualifying = await _entity(db, "SOL")

        await _claim(db, qualifying, "price_snapshot")
        await _claim(db, qualifying, "funding_rate")
        for minute in range(5):
            await _claim(
                db,
                flooded,
                "trade_tape",
                knowledge_date=AS_OF - timedelta(minutes=minute),
            )
        await _claim(db, also_qualifying, "news_event")
        await _claim(db, also_qualifying, "onchain_flow")

        found = await detect_all(
            db.pool,
            audience_user_id=None,
            window=WINDOW,
            min_families=2,
            as_of=AS_OF,
        )

        by_entity = {c.entity_id: c for c in found}
        assert set(by_entity) == {qualifying, also_qualifying}
        assert by_entity[qualifying].families == ("derivatives", "price")
        assert by_entity[also_qualifying].families == ("flow", "narrative")

    async def test_no_claims_anywhere_yields_no_convergences(self, db):
        await _entity(db)

        found = await detect_all(
            db.pool,
            audience_user_id=None,
            window=WINDOW,
            min_families=2,
            as_of=AS_OF,
        )

        assert found == []


class TestAbsence:
    async def test_an_entity_with_no_claims_returns_none(self, db):
        entity_id = await _entity(db)

        assert await _find(db, entity_id, min_families=2) is None

    async def test_an_unknown_entity_returns_none_without_raising(self, db):
        assert await _find(db, uuid4(), min_families=2) is None

    async def test_only_unmapped_claim_types_do_not_converge(self, db):
        entity_id = await _entity(db)
        await _claim(db, entity_id, "regime_assessment")
        await _claim(db, entity_id, "perception_divergence")
        await _claim(db, entity_id, "sector_score")

        assert await _find(db, entity_id, min_families=2) is None


# --- Gate: family membership is independence of upstream, not subject matter --


class TestFamilyMembershipIsIndependenceNotSubject:
    """Two rules pull in opposite directions and both have to hold.

    Admit too little and convergence under-fires: a genuinely independent
    observation stream never corroborates anything, so the detector is blind to
    exactly the agreement it exists to find.

    Admit too much and it fabricates corroboration: three summaries of one chain
    read, counted as three families, look like three independent witnesses.

    The resolving rule is upstream independence. `macro_series_point` is in
    because a FRED unemployment print is not a function of a price, a book or a
    chain read. The on-chain aggregates are folded into `flow` because they are
    different summaries of the state an `onchain_flow` claim reads.
    """

    def test_macro_is_its_own_family_because_it_shares_no_upstream(self):
        assert CLAIM_FAMILIES["macro_series_point"] == "macro"
        assert "macro" not in {
            family
            for claim_type, family in CLAIM_FAMILIES.items()
            if claim_type != "macro_series_point"
        }

    def test_onchain_aggregates_cannot_outvote_the_flow_they_summarise(self):
        # TVL, chain TVL and stablecoin supply are three views of one chain
        # state. Three families here would be three witnesses to one reading.
        for claim_type in ("onchain_tvl", "chain_tvl", "stablecoin_supply"):
            assert CLAIM_FAMILIES[claim_type] == CLAIM_FAMILIES["onchain_flow"]

    def test_filings_do_not_outvote_the_fundamentals_they_report(self):
        assert CLAIM_FAMILIES["filing_event"] == CLAIM_FAMILIES["fundamental_metric"]

    def test_every_derived_claim_type_is_excluded(self):
        # Each of these is computed FROM claims already in the map, so counting
        # one lets a single underlying observation vote twice.
        derived = (
            "perception_divergence",
            "yield_curve_signal",
            "sahm_rule_signal",
            "inflation_signal",
            "output_gap_signal",
            "lei_signal",
            "regime_assessment",
            "sector_score",
            "manipulation_signal",
            "convergence",
        )
        admitted = [c for c in derived if c in CLAIM_FAMILIES]
        assert not admitted, (
            f"derived claim types admitted to the family map: {admitted}. Each "
            f"is a function of claims already counted, so admitting it lets one "
            f"observation corroborate itself."
        )

    def test_convergence_cannot_corroborate_a_convergence(self):
        # The specific recursion: if 'convergence' were a family, yesterday's
        # convergence would count as a witness toward today's.
        assert "convergence" not in CLAIM_FAMILIES
