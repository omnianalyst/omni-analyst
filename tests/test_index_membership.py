"""Dated index membership (migration 056).

Membership used to be a static tuple list overwritten on every refresh, so the
system could answer "what is in the index" and nothing at all about "what was
in the index on a date". That is the defect that makes the ETF-versus-
constituent experiment survivorship-biased, and it cannot be repaired
retroactively -- only started.

Two properties carry the weight here.

`observed_on` is the date the list was derived from its source, never the clock.
The seeder runs on every boot; a clock-stamped snapshot would have each boot
assert a fresh observation of a list nobody re-checked, which is a fabricated
measurement wearing a real date.

A departure is written as `present = false` rather than simply stopping. An
absent row means nobody looked, and that has to stay distinguishable from a
member being gone -- otherwise the history reads as though every index held
everything until the day the table was created.
"""

from datetime import UTC, date, datetime

import asyncpg
import pytest

from omni.entities import seed as seed_module
from omni.entities._seed_data import (
    SP500_ACCESSED_ON,
    SP500_CONSTITUENTS,
    SP500_INDEX_SYMBOL,
    SP500_SOURCE,
)
from omni.entities.seed import seed_market_universe, snapshot_index_membership


@pytest.fixture(autouse=True)
async def _clean(db):
    await db.pool.execute("TRUNCATE entity CASCADE")
    await db.pool.execute("TRUNCATE index_membership")
    yield
    await db.pool.execute("TRUNCATE index_membership")


async def _rows(pool, *, present=None):
    sql = "SELECT * FROM index_membership WHERE index_symbol = $1"
    if present is not None:
        sql += f" AND present IS {'TRUE' if present else 'FALSE'}"
    sql += " ORDER BY observed_on, member_symbol"
    return await pool.fetch(sql, SP500_INDEX_SYMBOL)


async def _record(pool, symbol, observed_on, present):
    await pool.execute(
        "INSERT INTO index_membership "
        "(index_symbol, member_symbol, observed_on, present, source) "
        "VALUES ($1, $2, $3, $4, 'test')",
        SP500_INDEX_SYMBOL,
        symbol,
        observed_on,
        present,
    )


class TestSnapshot:
    async def test_records_every_constituent_present_on_the_observed_date(self, db):
        present, absent, departed = await snapshot_index_membership(db.pool)

        assert present == len(SP500_CONSTITUENTS)
        assert absent == 0
        assert departed == ()

        rows = await _rows(db.pool)
        assert len(rows) == len(SP500_CONSTITUENTS)
        assert {r["member_symbol"] for r in rows} == {s for s, _, _, _ in SP500_CONSTITUENTS}
        assert all(r["present"] is True for r in rows)
        assert all(r["observed_on"] == SP500_ACCESSED_ON for r in rows)
        assert all(r["source"] == SP500_SOURCE for r in rows)

    async def test_is_stamped_with_the_observation_date_not_the_clock(self, db, monkeypatch):
        """The anti-fabrication property.

        A boot in November replaying an August list must record an August
        observation. An implementation reaching for CURRENT_DATE or now() looks
        correct on the day the list is fetched and quietly starts inventing
        observations the day after, which is the failure mode that is hardest to
        notice because every row still looks plausible.
        """
        observed = date(2020, 1, 2)
        monkeypatch.setattr(seed_module, "SP500_ACCESSED_ON", observed)

        await snapshot_index_membership(db.pool)

        stamps = {r["observed_on"] for r in await _rows(db.pool)}
        assert stamps == {observed}
        # The wall clock, not date.today(): the claim is that the seeder
        # stamps SP500_ACCESSED_ON rather than the moment it happened to run.
        assert datetime.now(UTC).date() not in stamps

    async def test_reseeding_the_same_observation_writes_nothing_new(self, db):
        first_present, _, _ = await snapshot_index_membership(db.pool)
        before = await _rows(db.pool)
        recorded = {(r["member_symbol"], r["recorded_at"]) for r in before}

        second_present, second_absent, departed = await snapshot_index_membership(db.pool)

        after = await _rows(db.pool)
        assert first_present == len(SP500_CONSTITUENTS)
        assert (second_present, second_absent, departed) == (0, 0, ())
        assert len(after) == len(before)
        # DO NOTHING, not DO UPDATE: an observation already recorded is never
        # rewritten. A DO UPDATE would leave the same row count with a fresh
        # recorded_at, which is history being edited in place.
        assert {(r["member_symbol"], r["recorded_at"]) for r in after} == recorded

    async def test_a_member_that_left_is_recorded_absent_not_merely_missing(self, db):
        earlier = date(SP500_ACCESSED_ON.year - 1, 1, 2)
        kept = SP500_CONSTITUENTS[0][0]
        await _record(db.pool, kept, earlier, True)
        await _record(db.pool, "DELISTED", earlier, True)

        present, absent, departed = await snapshot_index_membership(db.pool)

        assert departed == ("DELISTED",)
        assert absent == 1
        assert present == len(SP500_CONSTITUENTS)

        gone = await db.pool.fetchrow(
            "SELECT present, source FROM index_membership "
            "WHERE index_symbol = $1 AND member_symbol = 'DELISTED' AND observed_on = $2",
            SP500_INDEX_SYMBOL,
            SP500_ACCESSED_ON,
        )
        assert gone is not None, "a departure must be recorded, not left as a silence"
        assert gone["present"] is False
        assert gone["source"] == SP500_SOURCE

        # The earlier observation is untouched. It said DELISTED was in the index
        # on that date and it was; rewriting it would be editing history.
        prior = await db.pool.fetchrow(
            "SELECT present FROM index_membership "
            "WHERE index_symbol = $1 AND member_symbol = 'DELISTED' AND observed_on = $2",
            SP500_INDEX_SYMBOL,
            earlier,
        )
        assert prior["present"] is True

    async def test_a_member_that_already_left_does_not_leave_twice(self, db):
        """The comparison is against the most recent prior observation only.

        Unioning every historical member instead would re-report DELISTED as
        departing at every future observation, which turns one real event into
        an unbounded stream of fabricated ones.
        """
        oldest = date(SP500_ACCESSED_ON.year - 2, 1, 2)
        previous = date(SP500_ACCESSED_ON.year - 1, 1, 2)
        kept = SP500_CONSTITUENTS[0][0]
        await _record(db.pool, kept, oldest, True)
        await _record(db.pool, "DELISTED", oldest, True)
        await _record(db.pool, kept, previous, True)
        await _record(db.pool, "DELISTED", previous, False)

        present, absent, departed = await snapshot_index_membership(db.pool)

        assert departed == ()
        assert absent == 0
        assert present == len(SP500_CONSTITUENTS)

        # Scoped to THIS observation, not to all history. The fixture above
        # deliberately records DELISTED's own departure at `previous`, so an
        # unscoped assertion contradicts the test's own setup -- it fails on the
        # row the test just inserted rather than on a re-report. What must not
        # happen is a SECOND departure written today for a member already gone.
        departures_today = [
            row for row in await _rows(db.pool, present=False)
            if row["observed_on"] == SP500_ACCESSED_ON
        ]
        assert departures_today == []

    async def test_one_observation_cannot_hold_two_answers(self, db):
        """What makes re-seeding idempotent is the key, not the caller's care."""
        await _record(db.pool, "DUPE", SP500_ACCESSED_ON, True)

        with pytest.raises(asyncpg.UniqueViolationError):
            await _record(db.pool, "DUPE", SP500_ACCESSED_ON, False)


class TestSeedIntegration:
    async def test_seeding_the_universe_writes_the_snapshot(self, db):
        report = await seed_market_universe(db.pool)

        assert report.membership_present == len(SP500_CONSTITUENTS)
        assert report.membership_absent == 0
        assert report.departed == ()
        assert len(await _rows(db.pool)) == len(SP500_CONSTITUENTS)

    async def test_a_seed_whose_membership_table_is_missing_fails_loudly(self, db):
        """Pure DB work, so a failure here is a real defect and must raise.

        The dangerous alternative is a seeder that swallows the error and
        reports a successful run with zero snapshot rows -- the store would then
        hold an unbroken membership history with a silent hole in it, and
        nothing downstream could tell the hole from a period of no change.
        """
        await db.pool.execute("ALTER TABLE index_membership RENAME TO index_membership_absent")
        try:
            with pytest.raises(asyncpg.UndefinedTableError):
                await seed_market_universe(db.pool)
        finally:
            await db.pool.execute(
                "ALTER TABLE index_membership_absent RENAME TO index_membership"
            )
