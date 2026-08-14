"""Freezing launch cohorts, against a real database.

Everything here defends one number: the fraction of launches that die. The ways
it can be corrupted are all quiet:

- filtering junk out at collection time removes the denominator itself;
- a truncated feed page records a small cohort that reads as a quiet day;
- a dead pool skipped rather than recorded as absent turns a death into a gap;
- a sweep that failed leaving no trace makes survivors look like the whole
  population.

None of those raise on their own. Each is tested here because each produces a
base rate that is wrong in the direction that makes launch trading look good.
"""

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from omni.research import launches
from omni.research.launches import (
    PAGE_SIZE,
    FeedUnavailable,
    Observation,
    discover,
    known_pools,
    parse_pool,
    record,
    reobserve,
    sweep,
)

NOW = datetime(2026, 8, 10, 5, 0, tzinfo=UTC)
NETWORK = "eth"


def _entry(address, *, liquidity="1000", volume="500", buys=10, sells=3, name="AAA / WETH"):
    return {
        "id": f"eth_{address}",
        "attributes": {
            "address": address,
            "name": name,
            "pool_created_at": "2026-08-10T04:00:00Z",
            "base_token_price_usd": "0.0001",
            "reserve_in_usd": liquidity,
            "volume_usd": {"h24": volume},
            "fdv_usd": "50000",
            "market_cap_usd": None,
            "transactions": {"h24": {"buys": buys, "sells": sells,
                                     "buyers": buys, "sellers": sells}},
        },
        "relationships": {
            "base_token": {"data": {"id": f"eth_{address}_base"}},
            "quote_token": {"data": {"id": "eth_weth"}},
        },
    }


def _feed(entries):
    async def fetch(url):
        return {"data": entries}

    return fetch


@pytest.fixture(autouse=True)
async def _clean(db):
    await db.pool.execute("TRUNCATE launch_sweep, launch_observation CASCADE")
    yield


async def _rows(db, address=None):
    if address:
        return await db.pool.fetch(
            "SELECT * FROM launch_observation WHERE pool_address=$1 ORDER BY observed_at",
            address,
        )
    return await db.pool.fetch("SELECT * FROM launch_observation ORDER BY observed_at")


class TestNothingIsFilteredOnTheWayIn:
    async def test_a_dead_on_arrival_pool_is_recorded_like_any_other(self, db):
        """The denominator is made of these.

        A pool with no liquidity and four dollars of volume is the modal launch.
        Skipping it because it looks like junk would remove most of the
        population and leave a base rate computed over the survivors.
        """
        entries = [
            _entry("0xalive", liquidity="9801", volume="2882"),
            _entry("0xdead", liquidity="0", volume="4", buys=1, sells=0),
        ]
        observed = await discover(NETWORK, fetch=_feed(entries))
        await record(
            db.pool, network=NETWORK, kind="discover",
            observations=observed, observed_at=NOW,
        )

        rows = await _rows(db)
        assert len(rows) == 2
        dead = next(r for r in rows if r["pool_address"] == "0xdead")
        assert dead["liquidity_usd"] == 0
        assert dead["present"] is True

    async def test_missing_fields_are_null_not_zero(self, db):
        """Absent is not zero. `market_cap_usd` is routinely omitted on a new
        pool, and storing 0 would assert a measured market cap of nothing."""
        observed = await discover(NETWORK, fetch=_feed([_entry("0xa")]))
        await record(db.pool, network=NETWORK, kind="discover",
                     observations=observed, observed_at=NOW)

        (row,) = await _rows(db)
        assert row["market_cap_usd"] is None
        assert row["fdv_usd"] == Decimal(50000)


class TestATruncatedFeedRaises:
    async def test_a_short_page_mid_walk_is_refused(self, db):
        """A throttled read returns a short page, and a short page recorded as a
        cohort understates how many launches happened that day."""
        short = [_entry(f"0x{i}") for i in range(PAGE_SIZE - 5)]

        with pytest.raises(FeedUnavailable, match="truncated read"):
            await discover(NETWORK, pages=3, fetch=_feed(short))

    async def test_a_short_final_page_is_fine(self, db):
        """Only a short page with more pages still requested is suspicious."""
        observed = await discover(NETWORK, pages=1, fetch=_feed([_entry("0xa")]))
        assert len(observed) == 1

    async def test_an_unreachable_feed_writes_no_cohort(self, db):
        async def broken(url):
            raise FeedUnavailable("network down")

        with pytest.raises(FeedUnavailable):
            await discover(NETWORK, fetch=broken)
        assert await _rows(db) == []


class TestADeathIsRecordedNotSkipped:
    async def test_a_pool_the_venue_stops_serving_is_marked_absent(self, db):
        """The outcome the whole table exists to capture.

        Skipping it would leave a gap, and a gap is indistinguishable from the
        collector not having run -- which is the difference between a death and
        a missing day.
        """
        first = await discover(NETWORK, fetch=_feed([_entry("0xa"), _entry("0xb")]))
        await record(db.pool, network=NETWORK, kind="discover",
                     observations=first, observed_at=NOW)

        # The venue now serves only 0xa.
        later = await reobserve(NETWORK, ["0xa", "0xb"], fetch=_feed([_entry("0xa")]))
        await record(db.pool, network=NETWORK, kind="reobserve",
                     observations=later, observed_at=NOW + timedelta(days=1))

        b_rows = await _rows(db, "0xb")
        assert len(b_rows) == 2
        assert b_rows[0]["present"] is True
        assert b_rows[1]["present"] is False
        assert b_rows[1]["liquidity_usd"] is None

    async def test_an_absent_row_cannot_carry_measurements(self, db):
        """Enforced in the schema, not just in the writer."""
        with pytest.raises(Exception, match="absent_measures_nothing"):
            await record(
                db.pool, network=NETWORK, kind="reobserve",
                observations=[Observation(
                    pool_address="0xz", present=False, liquidity_usd=Decimal(5),
                )],
                observed_at=NOW,
            )


class TestTheSweepMakesAbsenceReadable:
    async def test_feed_failure_is_returned_to_the_operation_health_caller(
        self, db, monkeypatch
    ):
        async def unavailable(_network):
            raise FeedUnavailable("upstream timeout")

        async def none_known(*_args, **_kwargs):
            return []

        monkeypatch.setattr(launches, "discover", unavailable)
        monkeypatch.setattr(launches, "known_pools", none_known)
        errors: list[str] = []

        counts = await sweep(db.pool, networks=[NETWORK], now=NOW, errors=errors)

        assert counts == {}
        assert errors == ["discover eth: upstream timeout"]

    async def test_every_sweep_is_recorded_with_its_count(self, db):
        observed = await discover(NETWORK, fetch=_feed([_entry("0xa"), _entry("0xb")]))
        await record(db.pool, network=NETWORK, kind="discover",
                     observations=observed, observed_at=NOW)

        (sweep,) = await db.pool.fetch("SELECT * FROM launch_sweep")
        assert sweep["kind"] == "discover"
        assert sweep["pools_seen"] == 2
        assert sweep["network"] == NETWORK

    async def test_a_naive_instant_is_refused(self, db):
        with pytest.raises(ValueError, match="naive"):
            await record(
                db.pool, network=NETWORK, kind="discover",
                observations=[], observed_at=datetime(2026, 8, 10, 5, 0),  # noqa: DTZ001
            )

    async def test_known_pools_returns_the_follow_up_set(self, db):
        observed = await discover(NETWORK, fetch=_feed([_entry("0xa"), _entry("0xb")]))
        await record(db.pool, network=NETWORK, kind="discover",
                     observations=observed, observed_at=NOW)

        found = await known_pools(db.pool, NETWORK, since=NOW - timedelta(days=1))
        assert sorted(found) == ["0xa", "0xb"]


class TestParsing:
    def test_an_entry_without_an_address_is_dropped(self):
        assert parse_pool({"attributes": {}}) is None

    def test_a_negative_or_nan_amount_becomes_null(self):
        entry = _entry("0xa", liquidity="-5")
        assert parse_pool(entry).liquidity_usd is None

    def test_the_honeypot_columns_survive_parsing(self):
        """Buys against unique sellers is the cheapest honeypot tell there is."""
        parsed = parse_pool(_entry("0xa", buys=400, sells=0))
        assert parsed.buys_24h == 400
        assert parsed.sells_24h == 0
        assert parsed.sellers_24h == 0
