"""The convergence producer: co-occurrence turned into a directional call.

The headline is `TestSignAgreementIsNotCoOccurrence`. `detect` measures that
independent families all spoke at once; a direction needs them to have pointed
the SAME WAY. Every other test here would pass for an implementation that
emitted a call from any convergence `detect` found and picked its direction from
the price family alone; that one would not.

The second load-bearing property is that the barriers are derived from the
constituent claims rather than chosen, so the invalidation level is asserted
against arithmetic written out in the test, not read back from the module.

The dedicated ``TEST_DATABASE_URL`` (``omni_v2_agent_conv``) keeps this suite off
the shared test database: concurrent agents TRUNCATE it.
"""

import json
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest

from omni.convergence import detect
from omni.conviction.convergence_producer import (
    produce_convergence_prediction_from_coverage,
)

AS_OF = datetime(2026, 3, 10, 12, 0, tzinfo=UTC)
WINDOW = timedelta(days=1)
DAY = timedelta(days=1)
HORIZON = AS_OF + timedelta(days=7)

# Realized vol of the three-close series used by the up/down scenarios, in
# price units, hand-derived so the target barrier is checkable:
#   up:   ln(102/100) = 0.019802627, ln(105/102) = 0.028987537
#         population stdev = |r1 - r2| / 2 = 0.004592455
#         vol = 0.004592455 * 105 (the last close) = 0.482207753
UP_VOL = 0.0045924547885367595 * 105.0
DOWN_VOL = 0.0045924547885367595 * 100.0


@pytest.fixture(autouse=True)
async def _clean(db):
    await db.pool.execute("TRUNCATE entity CASCADE")
    yield


async def _entity(db, symbol="BTC"):
    return await db.pool.fetchval(
        "INSERT INTO entity (kind, symbol, name) "
        "VALUES ('crypto_asset',$1,$1) RETURNING id",
        symbol,
    )


async def _claim(db, entity_id, claim_type, value, event_date, *, audience=None):
    await db.pool.execute(
        "INSERT INTO claim (entity_id, claim_type, key, value, source, "
        "event_date, knowledge_date, confidence, redistributable, "
        "audience_user_id) "
        "VALUES ($1,$2,$3,$4::jsonb,'seed',$5,$5,1.0,$6,$7)",
        entity_id,
        claim_type,
        f"{claim_type}:{event_date.isoformat()}",
        json.dumps(value),
        event_date,
        "allowed" if audience is None else "byo_only",
        audience,
    )


async def _prices(db, entity_id, closes):
    # `closes` oldest-first, one per day ending at AS_OF. Only the last two fall
    # inside the one-day convergence window; the earliest is history that feeds
    # realized vol without voting.
    for offset, close in enumerate(reversed(closes)):
        await _claim(
            db, entity_id, "price_snapshot", {"close": close}, AS_OF - DAY * offset
        )


async def _book(db, entity_id, mids):
    # `mids` oldest-first across the window: [window open, window close].
    for offset, mid in enumerate(reversed(mids)):
        await _claim(
            db,
            entity_id,
            "orderbook_snapshot",
            {"mid": str(mid), "venue": "binance"},
            AS_OF - DAY * offset,
        )


async def _flow(db, entity_id, direction, amount, *, audience=None):
    await _claim(
        db,
        entity_id,
        "onchain_flow",
        {
            "kind": f"exchange_{direction}",
            "exchange": "binance",
            "direction": direction,
            "amount_eth": amount,
            "chain": "eth",
        },
        AS_OF,
        audience=audience,
    )


async def _whale_flow(db, entity_id, amount):
    # onchain.py's shape for a transfer touching no known exchange.
    await _claim(
        db,
        entity_id,
        "onchain_flow",
        {
            "kind": "whale",
            "exchange": None,
            "direction": "whale",
            "amount_eth": amount,
            "chain": "eth",
        },
        AS_OF,
    )


async def _produce(db, entity_id, *, audience=None, min_families=3):
    return await produce_convergence_prediction_from_coverage(
        db.pool,
        entity_id=entity_id,
        audience_user_id=audience,
        horizon_ends_at=HORIZON,
        as_of=AS_OF,
        min_families=min_families,
    )


async def _row(db, prediction_id):
    return await db.pool.fetchrow(
        "SELECT method, direction, confidence, entry_price, upper_barrier, "
        "lower_barrier, provenance FROM prediction WHERE id = $1",
        prediction_id,
    )


async def _up_coverage(db, entity_id):
    """Three families all pointing up over the window."""
    await _prices(db, entity_id, [100.0, 102.0, 105.0])
    await _book(db, entity_id, [101.0, 106.0])
    await _flow(db, entity_id, "outflow", 50.0)


async def _down_coverage(db, entity_id):
    await _prices(db, entity_id, [105.0, 102.0, 100.0])
    await _book(db, entity_id, [101.0, 99.0])
    await _flow(db, entity_id, "inflow", 50.0)


class TestDirectionComesFromAgreementInSign:
    async def test_three_families_pointing_up_produce_an_up_call(self, db):
        e = await _entity(db)
        await _up_coverage(db, e)

        pid = await _produce(db, e)

        assert pid is not None
        row = await _row(db, pid)
        assert row["direction"] == "up"
        assert float(row["entry_price"]) == pytest.approx(105.0)

    async def test_three_families_pointing_down_produce_a_down_call(self, db):
        e = await _entity(db)
        await _down_coverage(db, e)

        pid = await _produce(db, e)

        assert pid is not None
        row = await _row(db, pid)
        assert row["direction"] == "down"
        assert float(row["entry_price"]) == pytest.approx(100.0)

    async def test_the_flow_convention_is_reserve_pys_and_not_its_inverse(self, db):
        # Inflow is supply arriving where it can be sold (bearish); outflow is
        # supply leaving (bullish). Inverting this reads every flow backwards,
        # and with price and book held flat-but-down the flow is the family that
        # decides whether the three agree at all.
        e = await _entity(db)
        await _prices(db, e, [105.0, 102.0, 100.0])
        await _book(db, e, [101.0, 99.0])
        await _flow(db, e, "outflow", 50.0)  # points UP against two DOWNs

        assert await _produce(db, e) is None


class TestSignAgreementIsNotCoOccurrence:
    """The headline: `detect` finds three families; they point different ways."""

    async def test_three_families_co_occurring_but_split_on_sign_abstain(self, db):
        e = await _entity(db)
        # The dissenting family's window-open level sits INSIDE the barriers an
        # up call would take, so nothing but the sign check refuses this: drop
        # it and a majority-of-three up call is written.
        await _prices(db, e, [100.0, 102.0, 105.0])  # up
        await _book(db, e, [104.0, 101.0])  # down
        await _flow(db, e, "outflow", 50.0)  # up

        converged = await detect(
            db.pool,
            entity_id=e,
            audience_user_id=None,
            window=WINDOW,
            min_families=3,
            as_of=AS_OF,
        )
        assert converged is not None
        assert converged.family_count == 3  # co-occurrence is genuinely present

        assert await _produce(db, e) is None
        assert await db.pool.fetchval("SELECT count(*) FROM prediction") == 0


class TestSilentFamiliesCannotAgree:
    async def test_a_family_with_no_directional_content_does_not_vote(self, db):
        # `narrative` carries headlines and article counts, never a sign. Three
        # families co-occur, only two point, and a three-family demand is unmet.
        e = await _entity(db)
        await _prices(db, e, [100.0, 102.0, 105.0])
        await _book(db, e, [101.0, 106.0])
        await _claim(db, e, "news_event", {"title": "t", "url": "u"}, AS_OF)

        assert await _produce(db, e, min_families=3) is None
        assert await db.pool.fetchval("SELECT count(*) FROM prediction") == 0

    async def test_an_unlabelled_whale_transfer_is_not_an_exchange_flow(self, db):
        # It moves the `flow` family into the window -- three families co-occur
        # -- but a transfer touching no known exchange carries no direction, so
        # counting it would put a third agreeing family behind a down call that
        # only two families support.
        e = await _entity(db)
        await _prices(db, e, [105.0, 102.0, 100.0])
        await _book(db, e, [101.0, 99.0])
        await _whale_flow(db, e, 500.0)

        assert await _produce(db, e, min_families=3) is None
        assert await db.pool.fetchval("SELECT count(*) FROM prediction") == 0

    async def test_a_silent_family_does_not_inflate_the_agreeing_count(self, db):
        e = await _entity(db)
        await _prices(db, e, [100.0, 102.0, 105.0])
        await _book(db, e, [101.0, 106.0])
        await _claim(db, e, "news_event", {"title": "t", "url": "u"}, AS_OF)

        pid = await _produce(db, e, min_families=2)

        assert pid is not None
        row = await _row(db, pid)
        provenance = json.loads(row["provenance"])
        assumptions = provenance["assumptions"]
        assert sorted(assumptions["families_present"]) == [
            "microstructure",
            "narrative",
            "price",
        ]
        assert sorted(assumptions["families_agreeing"]) == ["microstructure", "price"]
        assert assumptions["agreeing_family_count"] == 2
        assert float(row["confidence"]) == pytest.approx(0.75)


class TestBarriersAreDerived:
    async def test_the_invalidation_is_the_nearest_window_open_level_up(self, db):
        # Hand-derived, NOT read back from the module.
        #   price opened the window at 102 and closed it at 105 -> points up,
        #     and stops pointing up if price returns to 102.
        #   the book opened at 101 and closed at 106 -> points up, and stops
        #     pointing up at 101.
        # The FIRST of those agreements to break on the way down is the nearest
        # level below entry, i.e. max(102, 101) = 102. That is the invalidation.
        # The target is vol-scaled: 105 + 2 * 0.482207753 = 105.964415506.
        e = await _entity(db)
        await _up_coverage(db, e)

        pid = await _produce(db, e)

        assert pid is not None
        row = await _row(db, pid)
        assert float(row["lower_barrier"]) == pytest.approx(102.0)
        assert float(row["upper_barrier"]) == pytest.approx(105.0 + 2 * UP_VOL)
        assert float(row["upper_barrier"]) == pytest.approx(105.964415506)

    async def test_the_invalidation_is_the_nearest_window_open_level_down(self, db):
        # Mirror: price opened at 102, the book at 101, entry is 100, so both
        # levels sit ABOVE entry and the first agreement to break on the way up
        # is the nearest, min(102, 101) = 101.
        # Target = 100 - 2 * 0.459245479 = 99.081509042.
        e = await _entity(db)
        await _down_coverage(db, e)

        pid = await _produce(db, e)

        assert pid is not None
        row = await _row(db, pid)
        assert float(row["upper_barrier"]) == pytest.approx(101.0)
        assert float(row["lower_barrier"]) == pytest.approx(100.0 - 2 * DOWN_VOL)
        assert float(row["lower_barrier"]) == pytest.approx(99.081509042)

    async def test_barriers_straddle_entry_in_the_directions_orientation(self, db):
        # up:   target ABOVE, invalidation BELOW.
        # down: invalidation ABOVE, target BELOW.
        up_entity = await _entity(db, "BTC")
        await _up_coverage(db, up_entity)
        up_id = await _produce(db, up_entity)

        down_entity = await _entity(db, "ETH")
        await _down_coverage(db, down_entity)
        down_id = await _produce(db, down_entity)

        assert up_id is not None and down_id is not None
        up = await _row(db, up_id)
        down = await _row(db, down_id)

        assert float(up["upper_barrier"]) > float(up["entry_price"]) > float(
            up["lower_barrier"]
        )
        assert float(down["upper_barrier"]) > float(down["entry_price"]) > float(
            down["lower_barrier"]
        )
        # The invalidation sits on the against side in both cases.
        assert float(up["lower_barrier"]) == pytest.approx(102.0)
        assert float(down["upper_barrier"]) == pytest.approx(101.0)


class TestConfidenceIsTheFamilyCount:
    async def test_confidence_rises_with_the_number_of_agreeing_families(self, db):
        two = await _entity(db, "BTC")
        await _prices(db, two, [100.0, 102.0, 105.0])
        await _book(db, two, [101.0, 106.0])
        two_id = await _produce(db, two, min_families=2)

        three = await _entity(db, "ETH")
        await _up_coverage(db, three)
        three_id = await _produce(db, three, min_families=2)

        assert two_id is not None and three_id is not None
        two_conf = float((await _row(db, two_id))["confidence"])
        three_conf = float((await _row(db, three_id))["confidence"])

        assert three_conf > two_conf
        # 1 - 2**-n: the chance n independent unbiased reads all point this way,
        # complemented. No multiplier, no weight, no tuned blend.
        assert two_conf == pytest.approx(0.75)
        assert three_conf == pytest.approx(0.875)


class TestAbstention:
    async def test_below_the_family_threshold_abstains(self, db):
        e = await _entity(db)
        await _prices(db, e, [100.0, 102.0, 105.0])
        await _book(db, e, [101.0, 106.0])

        assert await _produce(db, e, min_families=3) is None
        assert await db.pool.fetchval("SELECT count(*) FROM prediction") == 0

    async def test_zero_realized_volatility_abstains(self, db):
        # Every close identical -> vol ~ 0 -> no honest target barrier. The book
        # and the flow still agree, so this is the only reason to refuse.
        e = await _entity(db)
        await _prices(db, e, [100.0, 100.0, 100.0])
        await _book(db, e, [99.0, 101.0])
        await _flow(db, e, "outflow", 50.0)

        assert await _produce(db, e, min_families=2) is None
        assert await db.pool.fetchval("SELECT count(*) FROM prediction") == 0

    async def test_a_nan_flow_amount_abstains_rather_than_voting(self, db):
        # Every comparison against NaN is False, so an unguarded sign test reads
        # NaN as a confident negative and manufactures a third agreeing family.
        e = await _entity(db)
        await _prices(db, e, [100.0, 102.0, 105.0])
        await _book(db, e, [101.0, 106.0])
        await _flow(db, e, "outflow", "NaN")

        assert await _produce(db, e, min_families=3) is None
        assert await db.pool.fetchval("SELECT count(*) FROM prediction") == 0

    async def test_a_nan_price_abstains_rather_than_anchoring_entry_on_it(self, db):
        e = await _entity(db)
        await _prices(db, e, [100.0, "NaN", 105.0])
        await _book(db, e, [101.0, 106.0])
        await _flow(db, e, "outflow", 50.0)

        assert await _produce(db, e, min_families=3) is None
        assert await db.pool.fetchval("SELECT count(*) FROM prediction") == 0

    async def test_no_price_coverage_abstains(self, db):
        # The book and the flow agree, but nothing anchors entry.
        e = await _entity(db)
        await _book(db, e, [101.0, 106.0])
        await _flow(db, e, "outflow", 50.0)

        assert await _produce(db, e, min_families=2) is None
        assert await db.pool.fetchval("SELECT count(*) FROM prediction") == 0


class TestAudienceScoping:
    async def test_another_audiences_private_claim_does_not_contribute_a_family(
        self, db
    ):
        e = await _entity(db)
        owner = uuid4()
        stranger = uuid4()
        await _prices(db, e, [100.0, 102.0, 105.0])
        await _book(db, e, [101.0, 106.0])
        await _flow(db, e, "outflow", 50.0, audience=owner)

        assert await _produce(db, e, audience=None) is None
        assert await _produce(db, e, audience=stranger) is None
        assert await db.pool.fetchval("SELECT count(*) FROM prediction") == 0

        pid = await _produce(db, e, audience=owner)

        assert pid is not None
        row = await _row(db, pid)
        assert float(row["confidence"]) == pytest.approx(0.875)
        assumptions = json.loads(row["provenance"])["assumptions"]
        assert "flow" in assumptions["families_agreeing"]


class TestLedgerWiring:
    async def test_the_recorded_method_is_convergence_multistream(self, db):
        e = await _entity(db)
        await _up_coverage(db, e)

        pid = await _produce(db, e)

        assert pid is not None
        row = await _row(db, pid)
        assert row["method"] == "convergence.multistream"

    async def test_the_constituent_claims_are_recorded_as_provenance(self, db):
        e = await _entity(db)
        await _up_coverage(db, e)

        pid = await _produce(db, e)

        assert pid is not None
        recorded = json.loads((await _row(db, pid))["provenance"])["input_claims"]
        in_window = await db.pool.fetch(
            "SELECT id::text AS id FROM claim WHERE entity_id = $1 "
            "AND knowledge_date >= $2",
            e,
            AS_OF - WINDOW,
        )
        assert set(recorded) == {r["id"] for r in in_window}
