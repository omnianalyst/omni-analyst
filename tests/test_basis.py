"""The basis (cross-venue) producer: price dislocation -> a convergence call.

Pure-logic tests cover the barrier/confidence construction (the load-bearing
arithmetic and orientation); integration tests prove it reads multi-venue price
coverage through the real visibility rule and records through the real ledger.
Every test states what bug it catches.

The dedicated ``TEST_DATABASE_URL`` (``omni_v2_agent_basis``) keeps this suite
off the shared test database: concurrent agents TRUNCATE it.
"""

import json
import math
from datetime import UTC, datetime, timedelta

import pytest

from omni.conviction.basis import (
    basis_call,
    produce_basis_prediction_from_coverage,
)

AS_OF = datetime(2025, 1, 31, tzinfo=UTC)
DAY = timedelta(days=1)


@pytest.fixture(autouse=True)
async def _clean(db):
    await db.pool.execute("TRUNCATE entity CASCADE")
    yield


async def _entity(db):
    return await db.pool.fetchval(
        "INSERT INTO entity (kind, symbol, name) "
        "VALUES ('crypto_asset','BTC','BTC') RETURNING id"
    )


async def _venue_price(db, entity_id, venue, close, event_date):
    # `close` may be the string "NaN" to model a poisonous non-finite input
    # (Postgres JSONB cannot hold a bare NaN, so the string is what reaches the
    # producer). `source` is set to the venue so two venues never collide on the
    # shared-claim identity, and value carries `venue` exactly as exchanges.py
    # writes it.
    await db.pool.execute(
        "INSERT INTO claim (entity_id, claim_type, key, value, source, "
        "event_date, knowledge_date, confidence, redistributable) "
        "VALUES ($1,'price_snapshot','BTC',$2::jsonb,$3,$4,$4,1.0,'allowed')",
        entity_id,
        json.dumps({"close": close, "venue": venue}),
        venue,
        event_date,
    )


async def _seed_series(db, entity_id, venue, prices, end_at):
    # `prices` oldest-first; daily candles, the last one lands on `end_at`.
    n = len(prices)
    for i, p in enumerate(prices):
        await _venue_price(db, entity_id, venue, p, end_at - DAY * (n - 1 - i))


class TestBasisCallArithmetic:
    def test_invalidation_is_the_spread_widening_level_down(self):
        # Hand-derived, NOT copied from the implementation.
        # entry (anchor, rich) = 102; target (other, cheap) = 100.
        # current_spread = +2 -> anchor rich -> direction down.
        # spread_vol = 0.5 -> widening = widening_k * spread_vol = 2 * 0.5 = 1.0.
        # invalidation (upper) = entry + widening = 102 + 1.0 = 103.0.
        # target (lower) = the other venue's price = 100.0 (full convergence).
        entry, target, spread_vol, k = 102.0, 100.0, 0.5, 2.0
        widening = k * spread_vol
        out = basis_call(entry=entry, target=target, spread_vol=spread_vol, widening_k=k)
        assert out is not None
        direction, upper, lower, conf = out
        assert direction == "down"
        assert upper == pytest.approx(entry + widening)
        assert upper == pytest.approx(103.0)  # the hand-derived widening level
        assert lower == pytest.approx(100.0)  # convergence target is the other leg
        assert 0.0 < conf < 1.0

    def test_invalidation_is_the_spread_widening_level_up(self):
        # Anchor cheap: entry = 100, target = 102 -> current_spread = -2 -> up.
        # Invalidation is a FALL of `widening` from entry (the spread widens
        # against a long). Target is the rise to the other venue's price.
        entry, target, spread_vol, k = 100.0, 102.0, 0.5, 2.0
        widening = k * spread_vol
        out = basis_call(entry=entry, target=target, spread_vol=spread_vol, widening_k=k)
        assert out is not None
        direction, upper, lower, _ = out
        assert direction == "up"
        assert upper == pytest.approx(102.0)  # convergence target
        assert lower == pytest.approx(entry - widening)
        assert lower == pytest.approx(99.0)

    def test_barriers_straddle_entry_in_the_correct_orientation(self):
        # The invalidation sits on the AGAINST side (the widening buffer); the
        # convergence target on the TOWARD side. down: invalidation ABOVE,
        # target BELOW; up: target ABOVE, invalidation BELOW.
        down = basis_call(entry=102.0, target=100.0, spread_vol=0.5, widening_k=2.0)
        up = basis_call(entry=100.0, target=102.0, spread_vol=0.5, widening_k=2.0)
        assert down is not None and up is not None
        _, d_upper, d_lower, _ = down
        _, u_upper, u_lower, _ = up
        assert d_upper > 102.0 > d_lower
        assert u_upper > 100.0 > u_lower
        # down: upper-entry is the widening buffer; entry-lower is the dislocation.
        assert d_upper - 102.0 == pytest.approx(2.0 * 0.5)
        assert 102.0 - d_lower == pytest.approx(2.0)
        # up: upper-entry is the dislocation; entry-lower is the widening buffer.
        assert u_upper - 100.0 == pytest.approx(2.0)
        assert 100.0 - u_lower == pytest.approx(2.0 * 0.5)

    def test_zero_spread_dispersion_abstains(self):
        # Lockstep venues -> no honest widening level. Refuse, don't fabricate.
        assert basis_call(entry=102.0, target=100.0, spread_vol=0.0) is None

    def test_float_dust_spread_dispersion_abstains(self):
        # The exact bug class AGENTS.md names: a near-zero dispersion that is NOT
        # caught by the straddle geometry. spread_vol = 1e-12 is far below any
        # real signal (a sub-pico spread on a $100 asset) so it must be treated
        # as zero, but its widening (2e-12) exceeds the ULP of entry (1.4e-14),
        # so upper = entry + 2e-12 is strictly > entry and the straddle check
        # ALONE would let it through -- fabricating a (near-zero-confidence)
        # call from pure noise. Only the explicit tolerance guard refuses it.
        # A guard written as `spread_vol == 0` would pass this straight through.
        assert basis_call(entry=100.0, target=98.0, spread_vol=1e-12) is None

    def test_non_finite_spread_vol_abstains(self):
        assert basis_call(entry=102.0, target=100.0, spread_vol=float("inf")) is None

    def test_zero_dislocation_abstains(self):
        # Venues already agree (entry == target) -> nothing to converge to.
        assert basis_call(entry=100.0, target=100.0, spread_vol=0.5) is None

    def test_nan_entry_abstains(self):
        # NaN must be refused explicitly: every comparison against NaN is False,
        # so a range check written as a comparison passes NaN straight through.
        assert basis_call(entry=float("nan"), target=100.0, spread_vol=0.5) is None

    def test_nan_target_abstains(self):
        assert basis_call(entry=102.0, target=float("nan"), spread_vol=0.5) is None


class TestProduceFromCoverage:
    async def test_anchor_rich_produces_a_down_prediction(self, db):
        # anchor = "aaa" (deterministic first key, no funding). Its latest price
        # (102) is ABOVE the other venue (100) -> it falls to converge -> down.
        e = await _entity(db)
        await _seed_series(db, e, "aaa", [100.0, 100.0, 102.0], AS_OF)
        await _seed_series(db, e, "bbb", [99.0, 98.0, 100.0], AS_OF)

        pid = await produce_basis_prediction_from_coverage(
            db.pool,
            entity_id=e,
            audience_user_id=None,
            horizon_ends_at=AS_OF + DAY * 5,
            as_of=AS_OF,
        )
        assert pid is not None
        row = await db.pool.fetchrow(
            "SELECT method, direction, entry_price, upper_barrier, lower_barrier "
            "FROM prediction WHERE id=$1",
            pid,
        )
        assert row["method"] == "basis.crossvenue"
        assert row["direction"] == "down"
        entry = float(row["entry_price"])
        upper = float(row["upper_barrier"])
        lower = float(row["lower_barrier"])
        assert upper > entry > lower
        # The target (lower) is the other venue's price: full convergence.
        assert lower == pytest.approx(100.0)

    async def test_anchor_cheap_produces_an_up_prediction(self, db):
        # anchor = "aaa" but its latest price (100) is BELOW the other (102) ->
        # it rises to converge -> up.
        e = await _entity(db)
        await _seed_series(db, e, "aaa", [100.0, 100.0, 100.0], AS_OF)
        await _seed_series(db, e, "bbb", [101.0, 102.0, 102.0], AS_OF)

        pid = await produce_basis_prediction_from_coverage(
            db.pool,
            entity_id=e,
            audience_user_id=None,
            horizon_ends_at=AS_OF + DAY * 5,
            as_of=AS_OF,
        )
        assert pid is not None
        row = await db.pool.fetchrow(
            "SELECT direction, entry_price, upper_barrier, lower_barrier "
            "FROM prediction WHERE id=$1",
            pid,
        )
        assert row["direction"] == "up"
        entry = float(row["entry_price"])
        upper = float(row["upper_barrier"])
        # Target (upper) is the other venue's price.
        assert upper == pytest.approx(102.0)
        assert entry == pytest.approx(100.0)

    async def test_invalidation_barrier_arithmetic_is_hand_derived(self, db):
        # The whole point: the invalidation level is derived from the spread's
        # OWN dispersion, asserted here against numbers worked out by hand.
        # anchor "aaa": [100, 100, 102], other "bbb": [99, 98, 100] (each over
        # d1,d2,d3 ending at AS_OF). Paired spreads = [1, 2, 2].
        #   mean = 5/3; variance(ddof=0) = ((-2/3)^2 + (1/3)^2 + (1/3)^2)/3
        #                            = (4/9 + 1/9 + 1/9)/3 = (6/9)/3 = 2/9
        #   spread_vol = sqrt(2/9) = sqrt(2)/3
        # latest: anchor 102 (rich) -> down. widening = 2 * sqrt(2)/3.
        #   upper = 102 + 2*sqrt(2)/3 ; lower = 100 (convergence target).
        e = await _entity(db)
        await _seed_series(db, e, "aaa", [100.0, 100.0, 102.0], AS_OF)
        await _seed_series(db, e, "bbb", [99.0, 98.0, 100.0], AS_OF)

        pid = await produce_basis_prediction_from_coverage(
            db.pool,
            entity_id=e,
            audience_user_id=None,
            horizon_ends_at=AS_OF + DAY * 5,
            as_of=AS_OF,
        )
        assert pid is not None
        row = await db.pool.fetchrow(
            "SELECT entry_price, upper_barrier, lower_barrier, provenance "
            "FROM prediction WHERE id=$1",
            pid,
        )
        spread_vol = math.sqrt(2) / 3  # hand-derived, NOT copied from output
        widening = 2.0 * spread_vol
        entry = 102.0
        assert float(row["entry_price"]) == pytest.approx(entry)
        assert float(row["upper_barrier"]) == pytest.approx(entry + widening)
        assert float(row["lower_barrier"]) == pytest.approx(100.0)
        prov = row["provenance"]
        if isinstance(prov, (str, bytes)):
            prov = json.loads(prov)
        assert prov["assumptions"]["spread_vol"] == pytest.approx(spread_vol)
        assert prov["assumptions"]["convergence_target"] == pytest.approx(100.0)

    async def test_only_one_venue_abstains(self, db):
        # One venue is a price, not a basis.
        e = await _entity(db)
        await _seed_series(db, e, "aaa", [100.0, 101.0, 102.0], AS_OF)

        pid = await produce_basis_prediction_from_coverage(
            db.pool,
            entity_id=e,
            audience_user_id=None,
            horizon_ends_at=AS_OF + DAY * 5,
            as_of=AS_OF,
        )
        assert pid is None
        assert await db.pool.fetchval("SELECT count(*) FROM prediction") == 0

    async def test_insufficient_spread_history_abstains(self, db):
        # Two venues, but only one common date -> a single spread point cannot
        # set a widening level.
        e = await _entity(db)
        await _seed_series(db, e, "aaa", [100.0, 100.0, 102.0], AS_OF)
        await _seed_series(db, e, "bbb", [99.0], AS_OF - DAY * 2)

        pid = await produce_basis_prediction_from_coverage(
            db.pool,
            entity_id=e,
            audience_user_id=None,
            horizon_ends_at=AS_OF + DAY * 5,
            as_of=AS_OF,
        )
        assert pid is None
        assert await db.pool.fetchval("SELECT count(*) FROM prediction") == 0

    async def test_lockstep_venues_abstain_via_zero_spread_vol(self, db):
        # A constant spread -> spread_vol ~ 0 -> no honest widening level. The
        # spread is 0.1 (not exactly representable in binary64): np.std returns
        # ~1.4e-17, NOT 0.0, so the straddle check alone cannot catch this --
        # the explicit tolerance guard is the only thing preventing a fabricated
        # call from float dust. This is the exact bug class AGENTS.md names.
        e = await _entity(db)
        await _seed_series(db, e, "aaa", [100.0, 100.0, 102.0], AS_OF)
        await _seed_series(db, e, "bbb", [99.9, 99.9, 101.9], AS_OF)  # spread = 0.1 always

        pid = await produce_basis_prediction_from_coverage(
            db.pool,
            entity_id=e,
            audience_user_id=None,
            horizon_ends_at=AS_OF + DAY * 5,
            as_of=AS_OF,
        )
        assert pid is None
        assert await db.pool.fetchval("SELECT count(*) FROM prediction") == 0

    async def test_no_dislocation_abstains(self, db):
        # Venues agree at the latest print -> nothing to converge to, even with a
        # dispersive spread history.
        e = await _entity(db)
        await _seed_series(db, e, "aaa", [100.0, 101.0, 100.0], AS_OF)
        await _seed_series(db, e, "bbb", [99.0, 100.0, 100.0], AS_OF)

        pid = await produce_basis_prediction_from_coverage(
            db.pool,
            entity_id=e,
            audience_user_id=None,
            horizon_ends_at=AS_OF + DAY * 5,
            as_of=AS_OF,
        )
        assert pid is None
        assert await db.pool.fetchval("SELECT count(*) FROM prediction") == 0

    async def test_a_nan_price_in_the_spread_history_abstains(self, db):
        # A poisonous non-finite input must not propagate into a confident
        # number: the spread series carries NaN -> the finite check refuses.
        e = await _entity(db)
        # aaa carries a NaN close on d2 (a common date); latest (d3) is finite.
        await _venue_price(db, e, "aaa", 100.0, AS_OF - DAY * 2)
        await _venue_price(db, e, "aaa", "NaN", AS_OF - DAY)
        await _venue_price(db, e, "aaa", 102.0, AS_OF)
        await _seed_series(db, e, "bbb", [99.0, 98.0, 100.0], AS_OF)

        pid = await produce_basis_prediction_from_coverage(
            db.pool,
            entity_id=e,
            audience_user_id=None,
            horizon_ends_at=AS_OF + DAY * 5,
            as_of=AS_OF,
        )
        assert pid is None
        assert await db.pool.fetchval("SELECT count(*) FROM prediction") == 0

    async def test_method_column_is_basis_crossvenue(self, db):
        e = await _entity(db)
        await _seed_series(db, e, "aaa", [100.0, 100.0, 102.0], AS_OF)
        await _seed_series(db, e, "bbb", [99.0, 98.0, 100.0], AS_OF)

        pid = await produce_basis_prediction_from_coverage(
            db.pool,
            entity_id=e,
            audience_user_id=None,
            horizon_ends_at=AS_OF + DAY * 5,
            as_of=AS_OF,
        )
        assert pid is not None
        method = await db.pool.fetchval(
            "SELECT method FROM prediction WHERE id=$1", pid
        )
        assert method == "basis.crossvenue"

    async def test_funding_identifies_the_perp_as_anchor(self, db):
        # A funding_rate claim names venue "perp"; that venue is the anchor even
        # though "aaa" sorts first. The perp prints rich (102) -> direction down.
        e = await _entity(db)
        await _seed_series(db, e, "aaa", [100.0, 100.0, 100.0], AS_OF)
        await _seed_series(db, e, "perp", [101.0, 101.0, 102.0], AS_OF)
        await db.pool.execute(
            "INSERT INTO claim (entity_id, claim_type, key, value, source, "
            "event_date, knowledge_date, confidence, redistributable) "
            "VALUES ($1,'funding_rate','perp:BTC',$2::jsonb,'deriv',$3,$3,1.0,'allowed')",
            e,
            json.dumps({"rate": "0.0001", "venue": "perp", "symbol": "BTC"}),
            AS_OF,
        )

        pid = await produce_basis_prediction_from_coverage(
            db.pool,
            entity_id=e,
            audience_user_id=None,
            horizon_ends_at=AS_OF + DAY * 5,
            as_of=AS_OF,
        )
        assert pid is not None
        row = await db.pool.fetchrow(
            "SELECT direction, entry_price, provenance FROM prediction WHERE id=$1",
            pid,
        )
        # Anchor is the perp (102), not "aaa" (100): entry reflects the perp leg.
        assert float(row["entry_price"]) == pytest.approx(102.0)
        assert row["direction"] == "down"
        prov = row["provenance"]
        if isinstance(prov, (str, bytes)):
            prov = json.loads(prov)
        assert prov["assumptions"]["anchor_venue"] == "perp"
