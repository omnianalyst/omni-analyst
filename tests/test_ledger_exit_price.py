"""The price a resolved prediction actually closed at.

GATE A found that 35.1% of resolved predictions expired without touching a
barrier, and that the ledger recorded only *that* they expired -- so a third of
every expectancy figure was scored at zero because no observed exit existed.
`_resolve_one` already reads the price path to decide the outcome; these tests
pin the requirement that it now also records the price it decided against.

The two properties that matter, and which each have a test that fails if they
are dropped:

1. An `expiry` exit is the LAST OBSERVED price in the window, at its OWN
   timestamp -- never the entry, never zero, and never carried forward to the
   horizon. `resolved_at` is when the horizon elapsed; `exit_at` is when the
   price was seen. They are different facts and the row records both.
2. Nothing here changes which outcome is chosen. The distribution test resolves
   a fixed scenario set covering every branch of `_decide_outcome` and asserts
   the outcomes are exactly what they were before exit prices existed.
"""

import json
from collections import Counter
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import asyncpg
import pytest

from omni.conviction.ledger import resolve_due_predictions

NOW = datetime.now(UTC)

ENTRY = 100.0
UPPER = 110.0
LOWER = 90.0


async def _entity(db, symbol="AAPL"):
    return await db.pool.fetchval(
        "INSERT INTO entity (kind, symbol, name) VALUES ('company',$1,$1) RETURNING id",
        symbol,
    )


async def _price_claim(db, entity_id, price, event_date, *, high=None, low=None, owner=None):
    shared = owner is None
    value = {}
    if price is not None:
        value["price"] = price
    if high is not None:
        value["high"] = high
    if low is not None:
        value["low"] = low
    return await db.pool.fetchval(
        """
        INSERT INTO claim (entity_id, claim_type, key, value, source,
                           event_date, knowledge_date, confidence,
                           redistributable, audience_user_id)
        VALUES ($1,'price_snapshot','seed',$2::jsonb,$3,$4,$5,1.0,$6,$7)
        RETURNING id
        """,
        entity_id,
        json.dumps(value),
        "seed" if shared else "polygon",
        event_date,
        event_date,
        "allowed" if shared else "byo_only",
        owner,
    )


async def _seed_prediction(
    db,
    entity_id,
    *,
    direction="up",
    entry=ENTRY,
    upper=UPPER,
    lower=LOWER,
    confidence=0.8,
    method="fundamentals.dcf_valuation",
    created_at,
    horizon_ends_at,
    audience_user_id=None,
):
    return await db.pool.fetchval(
        """
        INSERT INTO prediction (entity_id, method, direction, confidence,
                                entry_price, upper_barrier, lower_barrier,
                                horizon_ends_at, provenance, created_at,
                                audience_user_id)
        VALUES ($1,$2,$3::prediction_direction,$4,$5,$6,$7,$8,'{}'::jsonb,$9,$10)
        RETURNING id
        """,
        entity_id,
        method,
        direction,
        confidence,
        entry,
        upper,
        lower,
        horizon_ends_at,
        created_at,
        audience_user_id,
    )


async def _row(db, pid):
    return await db.pool.fetchrow(
        "SELECT outcome, resolved_at, exit_price, exit_at, entry_price, "
        "upper_barrier, lower_barrier, horizon_ends_at "
        "FROM prediction WHERE id=$1",
        pid,
    )


@pytest.fixture(autouse=True)
async def _clean(db):
    await db.pool.execute("TRUNCATE entity CASCADE")
    yield


class TestBarrierExit:
    """A barrier outcome closed AT the barrier, at the observation that touched
    it. Both facts are on the row; neither was recorded before."""

    async def test_an_upper_resolution_records_the_upper_barrier(self, db):
        e = await _entity(db)
        crossed_at = NOW - timedelta(days=5)
        pid = await _seed_prediction(
            db, e, direction="up",
            created_at=NOW - timedelta(days=10),
            horizon_ends_at=NOW - timedelta(days=1),
        )
        await _price_claim(db, e, 104.0, NOW - timedelta(days=8))
        await _price_claim(db, e, 111.0, crossed_at)

        assert await resolve_due_predictions(db.pool) == 1
        row = await _row(db, pid)
        assert row["outcome"] == "upper"
        assert float(row["exit_price"]) == float(row["upper_barrier"]) == UPPER
        # Not the price that carried it through the barrier (111.0): the
        # position closes AT the barrier, which is the falsifiable level fixed
        # at write time, not wherever the discrete sample happened to print.
        assert float(row["exit_price"]) != 111.0
        assert row["exit_at"] == crossed_at
        assert row["exit_at"] != row["horizon_ends_at"]

    async def test_a_lower_resolution_records_the_lower_barrier(self, db):
        e = await _entity(db)
        crossed_at = NOW - timedelta(days=4)
        pid = await _seed_prediction(
            db, e, direction="down",
            created_at=NOW - timedelta(days=10),
            horizon_ends_at=NOW - timedelta(days=1),
        )
        await _price_claim(db, e, 96.0, NOW - timedelta(days=7))
        await _price_claim(db, e, 88.0, crossed_at)

        assert await resolve_due_predictions(db.pool) == 1
        row = await _row(db, pid)
        assert row["outcome"] == "lower"
        assert float(row["exit_price"]) == float(row["lower_barrier"]) == LOWER
        assert float(row["exit_price"]) != 88.0
        assert row["exit_at"] == crossed_at


class TestExpiryExit:
    """The headline. An expiry is the third of the sample GATE A found scored at
    zero for want of a recorded price."""

    async def test_an_expiry_records_the_last_observed_price(self, db):
        e = await _entity(db)
        last_seen_at = NOW - timedelta(days=2)
        pid = await _seed_prediction(
            db, e, direction="up",
            created_at=NOW - timedelta(days=10),
            horizon_ends_at=NOW - timedelta(days=1),
        )
        # Inside the barriers throughout; the last observation is 103.0, which
        # is neither the entry, nor the high-water mark, nor zero.
        await _price_claim(db, e, ENTRY, NOW - timedelta(days=8))
        await _price_claim(db, e, 105.0, NOW - timedelta(days=4))
        await _price_claim(db, e, 103.0, last_seen_at)

        assert await resolve_due_predictions(db.pool) == 1
        row = await _row(db, pid)
        assert row["outcome"] == "expiry"
        exit_price = float(row["exit_price"])
        assert exit_price == 103.0
        assert exit_price != 0.0
        assert exit_price != float(row["entry_price"])
        assert exit_price != 105.0
        # exit_at is the observation's own time, NOT the horizon. resolved_at is
        # the horizon. Recording the exit at the horizon would assert a price
        # was observed at a moment it was not.
        assert row["exit_at"] == last_seen_at
        assert row["resolved_at"] == row["horizon_ends_at"]
        assert row["exit_at"] < row["resolved_at"]

    async def test_the_exit_is_not_carried_forward_past_its_own_observation(self, db):
        """A price printed after the horizon is outside the window and must not
        become the exit, and the last in-window price must not be re-stamped
        with a later time. The exit price and exit_at travel together."""
        e = await _entity(db)
        horizon = NOW - timedelta(days=3)
        last_in_window = NOW - timedelta(days=4)
        pid = await _seed_prediction(
            db, e, direction="up",
            created_at=NOW - timedelta(days=10),
            horizon_ends_at=horizon,
        )
        await _price_claim(db, e, 104.0, last_in_window)
        await _price_claim(db, e, 108.0, NOW - timedelta(days=1))  # after horizon

        assert await resolve_due_predictions(db.pool) == 1
        row = await _row(db, pid)
        assert row["outcome"] == "expiry"
        assert float(row["exit_price"]) == 104.0
        assert row["exit_at"] == last_in_window
        assert row["exit_at"] < row["horizon_ends_at"]

    async def test_an_expiry_with_no_scalar_price_records_no_exit(self, db):
        """A bar carrying only a range and no close gives no observed closing
        price. The outcome is still expiry -- the range shows the barriers were
        never touched -- but the mid of a range is an invention, so the exit
        stays NULL rather than being manufactured."""
        e = await _entity(db)
        pid = await _seed_prediction(
            db, e, direction="up",
            created_at=NOW - timedelta(days=10),
            horizon_ends_at=NOW - timedelta(days=1),
        )
        await _price_claim(db, e, None, NOW - timedelta(days=5), high=105.0, low=98.0)

        assert await resolve_due_predictions(db.pool) == 1
        row = await _row(db, pid)
        assert row["outcome"] == "expiry"
        assert row["exit_price"] is None
        assert row["exit_at"] is None


class TestPendingHasNoExit:
    async def test_no_visible_price_stays_pending_with_a_null_exit(self, db):
        """Could-not-score and expired-at-this-price are different facts.

        With no price in the window the prediction stays pending, and it
        does not acquire an exit price -- an expiry with a NULL exit would be a
        third, incoherent state."""
        e = await _entity(db)
        pid = await _seed_prediction(
            db, e, direction="up",
            created_at=NOW - timedelta(days=10),
            horizon_ends_at=NOW - timedelta(days=1),
        )

        assert await resolve_due_predictions(db.pool) == 0
        row = await _row(db, pid)
        assert row["outcome"] == "pending"
        assert row["resolved_at"] is None
        assert row["exit_price"] is None
        assert row["exit_at"] is None


class TestExitConstraint:
    """A price with no time is unattributable; a time with no price measures
    nothing. 044 makes the half-populated row impossible."""

    async def _pending(self, db):
        e = await _entity(db)
        return await _seed_prediction(
            db, e,
            created_at=NOW - timedelta(days=10),
            horizon_ends_at=NOW + timedelta(days=5),
        )

    async def test_a_price_without_a_time_is_rejected(self, db):
        pid = await self._pending(db)
        with pytest.raises(
            asyncpg.CheckViolationError, match="prediction_exit_price_is_timestamped"
        ):
            await db.pool.execute(
                "UPDATE prediction SET exit_price = 103.5 WHERE id=$1", pid
            )

    async def test_a_time_without_a_price_is_rejected(self, db):
        pid = await self._pending(db)
        with pytest.raises(
            asyncpg.CheckViolationError, match="prediction_exit_price_is_timestamped"
        ):
            await db.pool.execute(
                "UPDATE prediction SET exit_at = $2 WHERE id=$1", pid, NOW
            )

    async def test_both_set_and_both_null_are_accepted(self, db):
        """The constraint permits the two coherent states -- otherwise the two
        rejections above would prove nothing about which rows it rejects."""
        pid = await self._pending(db)
        await db.pool.execute(
            "UPDATE prediction SET exit_price = 103.5, exit_at = $2 WHERE id=$1",
            pid,
            NOW,
        )
        assert float((await _row(db, pid))["exit_price"]) == 103.5

        await db.pool.execute(
            "UPDATE prediction SET exit_price = NULL, exit_at = NULL WHERE id=$1", pid
        )
        assert (await _row(db, pid))["exit_price"] is None


# Every branch of _decide_outcome, with the outcome each produced BEFORE exit
# prices existed. Recorded by running this exact set against the pre-044 ledger;
# the assertion is that adding a recorded price to a decision does not change the
# decision. `horizon_days` is days BEFORE NOW (negative = still in the future);
# prices are (days_before_now, price, high, low).
_SCENARIOS = (
    {"name": "up_crosses_upper", "direction": "up",
     "prices": ((8, 105.0, None, None), (5, 111.0, None, None))},
    {"name": "down_crosses_lower", "direction": "down",
     "prices": ((8, 95.0, None, None), (5, 89.0, None, None))},
    {"name": "up_expires_inside", "direction": "up",
     "prices": ((8, 100.0, None, None), (4, 105.0, None, None), (2, 103.0, None, None))},
    {"name": "neutral_expires_inside", "direction": "neutral",
     "prices": ((6, 99.0, None, None),)},
    {"name": "no_price_at_all", "direction": "up", "prices": ()},
    {"name": "both_crossed_upper_first", "direction": "up",
     "prices": ((6, 111.0, None, None), (3, 89.0, None, None))},
    {"name": "both_crossed_lower_first", "direction": "up",
     "prices": ((6, 89.0, None, None), (3, 111.0, None, None))},
    {"name": "one_bar_spans_both", "direction": "up",
     "prices": ((5, 100.0, 115.0, 85.0),)},
    {"name": "bar_high_touches_upper", "direction": "up",
     "prices": ((5, 105.0, 112.0, 100.0),)},
    {"name": "bar_low_touches_lower", "direction": "down",
     "prices": ((5, 95.0, 100.0, 88.0),)},
    {"name": "horizon_not_yet_passed", "direction": "up", "horizon_days": -5,
     "prices": ((1, 102.0, None, None),)},
    {"name": "price_only_after_horizon", "direction": "up", "horizon_days": 6,
     "prices": ((2, 111.0, None, None),)},
)

_OUTCOMES_BEFORE_044 = {
    "up_crosses_upper": "upper",
    "down_crosses_lower": "lower",
    "up_expires_inside": "expiry",
    "neutral_expires_inside": "expiry",
    "no_price_at_all": "pending",
    "both_crossed_upper_first": "upper",
    "both_crossed_lower_first": "lower",
    "one_bar_spans_both": "lower",
    "bar_high_touches_upper": "upper",
    "bar_low_touches_lower": "lower",
    "horizon_not_yet_passed": "pending",
    "price_only_after_horizon": "pending",
}


class TestOutcomesUnchanged:
    async def test_the_outcome_distribution_is_what_it_was_before_exit_prices(self, db):
        """044 adds a recorded price to an existing decision. If any outcome
        moves, that is a bug in this change, not a new behaviour."""
        ids = {}
        for spec in _SCENARIOS:
            e = await _entity(db, spec["name"])
            ids[spec["name"]] = await _seed_prediction(
                db, e, direction=spec["direction"],
                created_at=NOW - timedelta(days=10),
                horizon_ends_at=NOW - timedelta(days=spec.get("horizon_days", 1)),
            )
            for days, price, high, low in spec["prices"]:
                await _price_claim(
                    db, e, price, NOW - timedelta(days=days), high=high, low=low
                )

        await resolve_due_predictions(db.pool)

        got = {
            name: await db.pool.fetchval(
                "SELECT outcome FROM prediction WHERE id=$1", pid
            )
            for name, pid in ids.items()
        }
        assert got == _OUTCOMES_BEFORE_044
        assert Counter(got.values()) == Counter(_OUTCOMES_BEFORE_044.values())
        assert Counter(got.values()) == {
            "upper": 3, "lower": 4, "pending": 3, "expiry": 2
        }


class TestAudienceScopedExit:
    async def test_another_audiences_price_never_becomes_this_ones_exit(self, db):
        """Resolution is audience-scoped through visible_claims_cte, and the
        exit price is read from the same scoped path. A byo_only price belonging
        to another audience is not merely excluded from the outcome decision --
        it must not reach the recorded exit either, which is a second channel
        out of the same private series."""
        owner = uuid4()
        stranger = uuid4()
        e = await _entity(db)
        owners_last_at = NOW - timedelta(days=3)

        pid = await _seed_prediction(
            db, e, direction="up",
            created_at=NOW - timedelta(days=10),
            horizon_ends_at=NOW - timedelta(days=1),
            audience_user_id=owner,
        )
        await _price_claim(db, e, 103.0, owners_last_at, owner=owner)
        # Later in the window, inside the barriers, and invisible to the owner:
        # an unscoped read would take THIS as the last observed price.
        await _price_claim(db, e, 107.0, NOW - timedelta(days=2), owner=stranger)

        assert await resolve_due_predictions(db.pool) == 1
        row = await _row(db, pid)
        assert row["outcome"] == "expiry"
        assert float(row["exit_price"]) == 103.0
        assert float(row["exit_price"]) != 107.0
        assert row["exit_at"] == owners_last_at
