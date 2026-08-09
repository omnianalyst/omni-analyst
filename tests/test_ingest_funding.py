"""Funding history from any ccxt venue, and the two ways it could lie.

The claim this writes has to be indistinguishable from `derivatives.parse_funding`'s
except for the venue in its key, because `carry_loop._settlements` and
`crosssectional._funding_window` read both and neither knows which adapter ran.

The weight here sits on truncation. A walk that stops early produces a short,
well-formed series that is **indistinguishable from an asset with little
history** -- which is exactly the quantity being measured -- so it cannot be
allowed to end quietly.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from omni.ingest.funding import (
    MAX_PAGES,
    CCXTFundingAdapter,
    parse_funding_history,
)
from omni.ingest.protocol import Unavailable

HOUR_MS = 3_600_000
START_MS = int(datetime(2025, 1, 1, tzinfo=UTC).timestamp() * 1000)


def _entry(i: int, rate: float | str, *, symbol: str = "BTC/USDC:USDC") -> dict:
    return {
        "symbol": symbol,
        "fundingRate": rate,
        "timestamp": START_MS + i * HOUR_MS,
    }


class TestTheClaimItWrites:
    def test_the_venue_is_the_key_prefix(self):
        """`split_part(key, ':', 1)` is how both readers filter by venue.

        Hyperliquid's own symbol carries a colon (`BTC/USDC:USDC`), so this is
        the case where a key format that assumed one colon would break.
        """
        drafts = parse_funding_history(
            [_entry(0, "0.00001")], symbol="BTC/USDC:USDC", venue="hyperliquid"
        )

        assert drafts[0].key == "hyperliquid:BTC/USDC:USDC"
        assert drafts[0].key.split(":", 1)[0] == "hyperliquid"

    def test_a_settlement_is_knowable_when_it_settles(self):
        """Not the OHLCV case, and the difference is load-bearing.

        A bar is stamped with its OPEN and is not knowable until it closes, so
        `parse_ohlcv` adds a bar duration. A funding settlement is published at
        the instant it settles, so stamping it later would hide real
        settlements from a point-in-time replay that should see them.
        """
        drafts = parse_funding_history(
            [_entry(0, "0.00001")], symbol="BTC", venue="hyperliquid"
        )

        assert drafts[0].knowledge_date == drafts[0].event_date

    def test_the_sign_is_preserved(self):
        # Positive means longs pay shorts. costs.py and portfolio/state.py both
        # rely on it, and a flip inverts the entire carry strategy.
        drafts = parse_funding_history(
            [_entry(0, "-0.00005")], symbol="BTC", venue="hyperliquid"
        )

        assert drafts[0].value["rate"] == "-0.00005"

    def test_a_zero_rate_is_a_real_observation(self):
        # Dropping it would leave a gap that reads as missing coverage over an
        # interval the venue really did settle.
        drafts = parse_funding_history(
            [_entry(0, "0")], symbol="BTC", venue="hyperliquid"
        )

        assert len(drafts) == 1
        assert drafts[0].value["rate"] == "0"

    def test_an_unparseable_rate_is_skipped_not_zeroed(self):
        drafts = parse_funding_history(
            [_entry(0, "0.001"), _entry(1, None), _entry(2, "nonsense")],
            symbol="BTC",
            venue="hyperliquid",
        )

        assert [d.value["rate"] for d in drafts] == ["0.001"]

    def test_a_non_finite_rate_is_not_a_rate(self):
        drafts = parse_funding_history(
            [_entry(0, "NaN"), _entry(1, "Infinity")],
            symbol="BTC",
            venue="hyperliquid",
        )

        assert drafts == []

    def test_a_payload_that_is_not_a_list_yields_nothing(self):
        assert parse_funding_history({"error": "rate limited"}, symbol="B", venue="v") == []


class TestTheWalkWillNotTruncateQuietly:
    def _pages(self, total: int):
        """A venue serving `total` hourly settlements, 500 to a page."""

        async def page(symbol, *, since, limit):
            limit = limit or 500
            if since is None:
                return [_entry(i, "0.00001") for i in range(min(limit, total))]
            first = max(0, (since - START_MS) // HOUR_MS)
            return [
                _entry(i, "0.00001")
                for i in range(first, min(first + limit, total))
            ]

        return page

    async def test_it_pages_past_the_first_window(self):
        adapter = CCXTFundingAdapter(
            venue="hyperliquid",
            since=datetime(2025, 1, 1, tzinfo=UTC),
            fetch_fn=self._pages(1200),
        )

        drafts = await adapter.fetch("BTC/USDC:USDC")

        assert len(drafts) == 1200

    async def test_a_venue_that_never_advances_stops_rather_than_looping(self):
        async def stuck(symbol, *, since, limit):
            return [_entry(0, "0.00001")]

        adapter = CCXTFundingAdapter(
            venue="hyperliquid",
            since=datetime(2025, 1, 1, tzinfo=UTC),
            fetch_fn=stuck,
        )

        drafts = await adapter.fetch("BTC")

        assert len(drafts) == 1
        assert adapter.last_stop_reason == "cursor did not advance"

    async def test_a_walk_that_hits_the_page_cap_raises_rather_than_returning_short(self):
        """The defect this module exists to not repeat.

        An endless venue must not return a tidy partial series: "not enough
        history" is precisely what a truncated walk impersonates, and it is the
        quantity the carry measurement reads.
        """
        adapter = CCXTFundingAdapter(
            venue="hyperliquid",
            since=datetime(2025, 1, 1, tzinfo=UTC),
            fetch_fn=self._pages(MAX_PAGES * 500 + 1),
        )

        with pytest.raises(Unavailable, match="still advancing"):
            await adapter.fetch("BTC")

    async def test_overlapping_pages_do_not_double_count(self):
        async def overlapping(symbol, *, since, limit):
            if since is None or since <= START_MS:
                return [_entry(i, "0.00001") for i in range(3)]
            first = max(0, (since - START_MS) // HOUR_MS - 1)  # re-serves one
            if first >= 5:
                return []
            return [_entry(i, "0.00001") for i in range(first, 5)]

        adapter = CCXTFundingAdapter(
            venue="hyperliquid",
            since=datetime(2025, 1, 1, tzinfo=UTC),
            fetch_fn=overlapping,
        )

        drafts = await adapter.fetch("BTC")

        stamps = [d.event_date for d in drafts]
        assert len(stamps) == len(set(stamps))


class TestTheAdapterNamesItsVenue:
    def test_an_unnamed_venue_is_refused(self):
        # The venue is the key prefix, so an empty one writes claims that no
        # venue filter will ever match -- invisible rather than wrong.
        with pytest.raises(ValueError, match="must be named"):
            CCXTFundingAdapter(venue="  ")

    async def test_an_unknown_ccxt_venue_is_unavailable_not_an_attribute_error(self):
        adapter = CCXTFundingAdapter(venue="not_a_real_exchange")

        with pytest.raises(Unavailable, match="no venue named"):
            await adapter.fetch("BTC")

    def test_the_provider_key_is_the_venue(self):
        # The writer resolves licence class from provider_key; collapsing two
        # venues onto one label is the P1.11 defect.
        assert CCXTFundingAdapter(venue="hyperliquid").provider_key == "hyperliquid"
