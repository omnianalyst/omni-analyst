"""The plumbing, and the ways a pull lies about what it fetched.

Every guard here exists because something broke on 2026-08-09: a rate-limited
walk that returned a short series and looked like a young asset, a documented
30-day cap that hid 5.9 years, a bar's close read on the day the bar opened, and
six agents refetching each other's data. The happy path is not tested much
because the happy path was never the problem.

A guard that cannot be shown to fire is decoration.
"""

from __future__ import annotations

import io
import json
import zipfile
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import UUID, uuid4

import numpy as np
import pandas as pd
import pytest

from omni.ingest.protocol import Unavailable
from omni.research.harness import evaluate
from omni.research.registry import Registry
from omni.research.sources import (
    MAX_PAGES,
    BinanceArchiveSource,
    Cache,
    CCXTOhlcvSource,
    ClaimStoreSource,
    Panel,
    Universe,
    align,
    apply_fill,
    apply_universe,
    validate_observations,
)

DAY_MS = 86_400_000
EPOCH_2024 = int(datetime(2024, 1, 1, tzinfo=UTC).timestamp() * 1000)


def _bars(n: int, *, start_ms: int = EPOCH_2024, price: float = 100.0, volume: float = 10.0):
    """`[open_ms, o, h, l, c, v]`, one per day, with a distinguishable close."""
    return [
        [start_ms + i * DAY_MS, price + i, price + i + 1.0, price + i - 1.0,
         price + i + 0.5, volume + i]
        for i in range(n)
    ]


class Venue:
    """A ccxt-shaped pager that can be made to fail exactly where a venue does.

    `fail_first` is a rate limit that clears; `fail_from_page` is one that does
    not. The distinction is the whole test: one must be retried through, the
    other must raise rather than hand back what it managed.
    """

    def __init__(self, bars, *, page: int = 1000, fail_first: int = 0,
                 fail_from_page: int | None = None, endless: bool = False):
        self.bars = bars
        self.page = page
        self.fail_first = fail_first
        self.fail_from_page = fail_from_page
        self.endless = endless
        self.calls = 0
        self.pages = 0
        self._failed: dict[int | None, int] = {}

    async def __call__(self, symbol, *, since=None, limit=None):
        self.calls += 1
        if self.fail_from_page is not None and self.pages >= self.fail_from_page:
            raise TimeoutError("rate limited")
        seen = self._failed.get(since, 0)
        if seen < self.fail_first:
            self._failed[since] = seen + 1
            raise TimeoutError("rate limited")
        self.pages += 1
        if self.endless:
            base = EPOCH_2024 if since is None else since
            return [[base, 1.0, 1.0, 1.0, 1.0, 1.0]]
        if since is None:
            return self.bars[-(limit or 1):]
        rows = [b for b in self.bars if b[0] >= since]
        return rows[: (limit or self.page)]


def _source(tmp_path, venue, **kwargs) -> CCXTOhlcvSource:
    return CCXTOhlcvSource(
        venue="testvenue",
        fetch_fn=venue,
        cache=Cache(tmp_path / "cache"),
        attempts=kwargs.pop("attempts", 4),
        backoff=0.0,
        **kwargs,
    )


class TestATruncatedPullRaisesRatherThanReturningShort:
    """The failure that dropped ETH and SOL out of a measured universe.

    Breaking out of a paging loop on any exception turns a rate limit into a
    short series, and a short series is indistinguishable from an asset with
    little history -- which is exactly the quantity being measured.
    """

    async def test_a_rate_limit_that_clears_is_retried_through(self, tmp_path):
        venue = Venue(_bars(30), page=10, fail_first=2)
        source = _source(tmp_path, venue)

        rows = await source.observations(
            ["BTC/USDT"], start=datetime(2024, 1, 1, tzinfo=UTC), use_cache=False
        )

        assert len(rows) == 30
        assert rows["event_date"].max() == pd.Timestamp(
            EPOCH_2024 + 29 * DAY_MS, unit="ms", tz=UTC
        )

    async def test_a_rate_limit_that_does_not_clear_raises(self, tmp_path):
        venue = Venue(_bars(30), page=10, fail_from_page=1)
        source = _source(tmp_path, venue)

        with pytest.raises(Unavailable, match="short history"):
            await source.observations(
                ["BTC/USDT"], start=datetime(2024, 1, 1, tzinfo=UTC), use_cache=False
            )

    async def test_the_partial_rows_are_not_handed_back_as_the_series(self, tmp_path):
        # Ten bars were fetched successfully before the venue stopped answering.
        # Returning those ten would read as an asset listed ten days ago.
        venue = Venue(_bars(30), page=10, fail_from_page=1)
        source = _source(tmp_path, venue)

        with pytest.raises(Unavailable):
            await source.observations(
                ["BTC/USDT"], start=datetime(2024, 1, 1, tzinfo=UTC), use_cache=False
            )

        assert venue.pages == 1

    async def test_a_walk_still_advancing_at_the_page_cap_raises(self, tmp_path):
        venue = Venue([], endless=True)
        source = _source(tmp_path, venue)

        with pytest.raises(Unavailable, match="truncate"):
            await source.observations(
                ["BTC/USDT"], start=datetime(2024, 1, 1, tzinfo=UTC), use_cache=False
            )

        assert venue.pages == MAX_PAGES

    async def test_a_missing_start_is_refused_rather_than_paged_once(self, tmp_path):
        # ccxt with no `since` returns a single page. That is a truncation the
        # caller never asked for and nothing downstream can see.
        source = _source(tmp_path, Venue(_bars(30)))

        with pytest.raises(ValueError, match="single page"):
            await source.observations(["BTC/USDT"], use_cache=False)


class TestTheCacheReturnsIdenticalDataAndNeverPartialData:
    async def test_a_second_run_is_identical_and_costs_no_requests(self, tmp_path):
        venue = Venue(_bars(30), page=10)
        cache = Cache(tmp_path / "cache")
        first = await CCXTOhlcvSource(
            venue="testvenue", fetch_fn=venue, cache=cache, backoff=0.0
        ).observations(["BTC/USDT"], start=datetime(2024, 1, 1, tzinfo=UTC))
        calls = venue.calls

        second = await CCXTOhlcvSource(
            venue="testvenue", fetch_fn=venue, cache=cache, backoff=0.0
        ).observations(["BTC/USDT"], start=datetime(2024, 1, 1, tzinfo=UTC))

        pd.testing.assert_frame_equal(first, second)
        assert venue.calls == calls

    async def test_a_different_window_is_a_different_entry(self, tmp_path):
        # A cache keyed on the source alone would serve January's pull for a
        # request that asked for January and February.
        venue = Venue(_bars(60), page=100)
        cache = Cache(tmp_path / "cache")
        source = CCXTOhlcvSource(
            venue="testvenue", fetch_fn=venue, cache=cache, backoff=0.0
        )

        short = await source.observations(
            ["BTC/USDT"],
            start=datetime(2024, 1, 1, tzinfo=UTC),
            end=datetime(2024, 1, 20, tzinfo=UTC),
        )
        full = await source.observations(
            ["BTC/USDT"],
            start=datetime(2024, 1, 1, tzinfo=UTC),
            end=datetime(2024, 2, 20, tzinfo=UTC),
        )

        assert len(short) == 20
        assert len(full) == 51

    async def test_a_pull_that_raised_leaves_nothing_cached(self, tmp_path):
        """The cache must not learn a truncation.

        A partial entry written before the walk finished would serve the short
        series forever, and the second run has no network fault to blame it on.
        """
        cache_dir = tmp_path / "cache"
        broken = Venue(_bars(30), page=10, fail_from_page=1)
        with pytest.raises(Unavailable):
            await CCXTOhlcvSource(
                venue="testvenue", fetch_fn=broken, cache=Cache(cache_dir), backoff=0.0
            ).observations(["BTC/USDT"], start=datetime(2024, 1, 1, tzinfo=UTC))

        assert list(cache_dir.glob("*.pkl")) == []

        healthy = Venue(_bars(30), page=10)
        rows = await CCXTOhlcvSource(
            venue="testvenue", fetch_fn=healthy, cache=Cache(cache_dir), backoff=0.0
        ).observations(["BTC/USDT"], start=datetime(2024, 1, 1, tzinfo=UTC))

        assert len(rows) == 30


class TestPointInTimeStampingHidesTheFuture:
    """A bar is stamped with its OPEN; its close does not exist until it closes.

    Previously `knowledge_date = event_date`, which asserted a day's closing
    price was knowable when the day began. Producers filter on
    `knowledge_date <= as_of`, so a replay at cutoff T entered at a price from
    T + 1d -- on the signal as well as the entry -- and nothing raised.
    """

    async def test_a_bars_close_is_not_visible_on_the_bars_own_date(self, tmp_path):
        source = _source(tmp_path, Venue(_bars(20), page=100))
        panel = await source.panel(
            ["BTC/USDT"],
            start=datetime(2024, 1, 1, tzinfo=UTC),
            universe=Universe(min_assets=1),
        )

        # The bar opening 2024-01-05 is the fifth; its close is 100 + 4 + 0.5.
        close_of_the_fifth = 104.5
        visible_through_the_fifth = panel.frame.loc[:"2024-01-05", "BTC/USDT"]

        assert close_of_the_fifth not in set(visible_through_the_fifth.dropna())
        assert panel.frame.loc[pd.Timestamp("2024-01-06"), "BTC/USDT"] == close_of_the_fifth

    async def test_the_panel_starts_the_day_after_the_first_bar_opens(self, tmp_path):
        source = _source(tmp_path, Venue(_bars(20), page=100))
        panel = await source.panel(
            ["BTC/USDT"],
            start=datetime(2024, 1, 1, tzinfo=UTC),
            universe=Universe(min_assets=1),
        )

        assert panel.frame.index[0] == pd.Timestamp("2024-01-02")

    async def test_as_of_hides_what_was_not_yet_knowable(self, tmp_path):
        source = _source(tmp_path, Venue(_bars(20), page=100))
        panel = await source.panel(
            ["BTC/USDT"],
            start=datetime(2024, 1, 1, tzinfo=UTC),
            as_of=datetime(2024, 1, 5, 12, 0, tzinfo=UTC),
            universe=Universe(min_assets=1),
        )

        # At noon on the 5th the newest closed bar is the 4th's, knowable at
        # 00:00 on the 5th. The 5th's own bar has not closed.
        assert panel.frame.index[-1] == pd.Timestamp("2024-01-05")
        assert panel.frame.loc[pd.Timestamp("2024-01-05"), "BTC/USDT"] == 103.5

    def test_a_fact_knowable_before_it_happened_is_refused(self):
        backwards = pd.DataFrame(
            {
                "asset": ["BTC"],
                "event_date": [pd.Timestamp("2024-06-01", tz=UTC)],
                "knowledge_date": [pd.Timestamp("2024-05-01", tz=UTC)],
                "value": [1.0],
            }
        )

        with pytest.raises(Unavailable, match="cannot be known before it happens"):
            validate_observations(backwards, source="fake")


class TestNothingForwardFillsUnlessAsked:
    @staticmethod
    def _gapped() -> pd.DataFrame:
        index = pd.date_range("2024-01-01", periods=10, freq="D")
        frame = pd.DataFrame({"A": np.arange(10, dtype=float)}, index=index)
        frame.loc[index[3:6], "A"] = np.nan
        return frame

    def test_the_default_leaves_a_gap_as_a_gap(self):
        frame, held = apply_fill(self._gapped(), fill="none", hold_limit=None)

        assert int(frame["A"].isna().sum()) == 3
        assert held == 0

    def test_an_unbounded_hold_is_refused(self):
        with pytest.raises(ValueError, match="unbounded hold"):
            apply_fill(self._gapped(), fill="hold", hold_limit=None)

    def test_a_bounded_hold_fills_only_as_far_as_it_was_allowed(self):
        frame, held = apply_fill(self._gapped(), fill="hold", hold_limit=2)

        assert held == 2
        assert frame["A"].iloc[3] == 2.0
        assert frame["A"].iloc[4] == 2.0
        assert np.isnan(frame["A"].iloc[5])

    def test_a_hold_never_carries_a_delisted_asset_past_its_last_print(self):
        """The survivorship bug wearing a fill policy's clothes.

        Holding past the last print keeps a dead name in the cross-section at a
        frozen price, contributing a zero return to whichever leg holds it --
        which is not what happened to anyone who owned it.
        """
        index = pd.date_range("2024-01-01", periods=10, freq="D")
        frame = pd.DataFrame({"LIVE": np.arange(10, dtype=float),
                              "DEAD": np.arange(10, dtype=float)}, index=index)
        frame.loc[index[5:], "DEAD"] = np.nan

        filled, held = apply_fill(frame, fill="hold", hold_limit=3)

        assert held == 0
        assert bool(filled["DEAD"].iloc[5:].isna().all())

    async def test_a_panel_reports_the_cells_it_synthesized(self, tmp_path):
        bars = _bars(20)
        del bars[7]
        source = _source(tmp_path, Venue(bars, page=100))

        panel = await source.panel(
            ["BTC/USDT"],
            start=datetime(2024, 1, 1, tzinfo=UTC),
            fill="hold",
            hold_limit=1,
            universe=Universe(min_assets=1),
        )

        assert panel.held_cells == 1
        assert any("carried forward" in w for w in panel.warnings)


class TestTheUniversePolicyIsStatedAndReproducible:
    @staticmethod
    def _panel_frames():
        index = pd.date_range("2024-01-01", periods=30, freq="D")
        frame = pd.DataFrame(
            {name: np.linspace(1.0, 2.0, 30) for name in ("AAA", "BBB", "CCC", "DDD")},
            index=index,
        )
        frame.loc[index[:25], "DDD"] = np.nan
        liquidity = pd.DataFrame(
            {"AAA": 500.0, "BBB": 5_000.0, "CCC": 50_000.0, "DDD": 1.0}, index=index
        )
        return frame, liquidity

    def test_the_same_policy_twice_selects_the_same_universe_in_the_same_order(self):
        frame, liquidity = self._panel_frames()
        policy = Universe(min_liquidity=Decimal(1000), min_assets=2, max_assets=2)

        first, _ = apply_universe(frame, policy=policy, liquidity=liquidity, source="s")
        second, _ = apply_universe(frame, policy=policy, liquidity=liquidity, source="s")

        assert list(first.columns) == list(second.columns) == ["BBB", "CCC"]

    def test_a_liquidity_floor_removes_the_names_below_it_and_says_why(self):
        frame, liquidity = self._panel_frames()

        kept, dropped = apply_universe(
            frame,
            policy=Universe(min_liquidity=Decimal(1000), min_assets=1),
            liquidity=liquidity,
            source="s",
        )

        assert list(kept.columns) == ["BBB", "CCC"]
        assert "below the 1000 floor" in dropped["AAA"]

    def test_a_minimum_history_removes_a_name_with_too_few_observations(self):
        frame, liquidity = self._panel_frames()

        kept, dropped = apply_universe(
            frame,
            policy=Universe(min_history_days=10, min_assets=1),
            liquidity=liquidity,
            source="s",
        )

        assert "DDD" not in kept.columns
        assert "below the 10 floor" in dropped["DDD"]

    def test_a_top_n_cut_breaks_ties_on_the_symbol(self):
        # A ranking that depends on dict order gives the same script a different
        # universe on the same day.
        index = pd.date_range("2024-01-01", periods=5, freq="D")
        frame = pd.DataFrame({n: 1.0 for n in ("ZZZ", "AAA", "MMM")}, index=index)
        liquidity = pd.DataFrame({n: 100.0 for n in ("ZZZ", "AAA", "MMM")}, index=index)

        kept, _ = apply_universe(
            frame,
            policy=Universe(min_assets=1, max_assets=2),
            liquidity=liquidity,
            source="s",
        )

        assert list(kept.columns) == ["AAA", "MMM"]

    def test_too_few_survivors_is_a_refusal_not_a_thin_panel(self):
        frame, liquidity = self._panel_frames()

        with pytest.raises(Unavailable, match="it is not a result"):
            apply_universe(
                frame,
                policy=Universe(min_liquidity=Decimal(100000), min_assets=10),
                liquidity=liquidity,
                source="s",
            )

    def test_a_liquidity_floor_on_a_source_without_liquidity_is_an_error(self):
        """Not a filter that quietly passes everything.

        That silent pass is how "top 50 by volume" becomes "every symbol", and
        the universe is the input every cross-sectional number depends on.
        """
        frame, _ = self._panel_frames()

        with pytest.raises(Unavailable, match="no liquidity series"):
            apply_universe(
                frame,
                policy=Universe(min_liquidity=Decimal(10), min_assets=1),
                liquidity=None,
                source="s",
            )

    def test_an_unsatisfiable_policy_is_refused_at_construction(self):
        with pytest.raises(ValueError, match="can never be satisfied"):
            Universe(min_assets=10, max_assets=5)

    def test_a_float_liquidity_floor_is_refused(self):
        # Decimal so the floor is the number the policy names, not the nearest
        # binary64 to it.
        with pytest.raises(TypeError, match="Decimal"):
            Universe(min_liquidity=1000.0)


def _zip_bytes(name: str, text: str) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr(name, text)
    return buffer.getvalue()


def _kline_csv(month_start: datetime, days: int, *, price: float = 10.0) -> str:
    lines = []
    for i in range(days):
        opened = int((month_start + timedelta(days=i)).timestamp() * 1000)
        closed = opened + DAY_MS - 1
        close = price + i
        lines.append(
            f"{opened},{price},{price + 1},{price - 1},{close},1000,"
            f"{closed},{close * 1000},50,500,{close * 500},0"
        )
    return "\n".join(lines)


METRICS_HEADER = (
    "create_time,symbol,sum_open_interest,sum_open_interest_value,"
    "count_toptrader_long_short_ratio,sum_toptrader_long_short_ratio,"
    "count_long_short_ratio,sum_taker_long_short_vol_ratio"
)


class FakeArchive:
    """The S3 bucket, in memory, with a controllable failure per object."""

    def __init__(self, objects: dict[str, bytes], *, fail_first: dict[str, int] | None = None,
                 always_fail: set[str] | None = None):
        self.objects = objects
        self.fail_first = dict(fail_first or {})
        self.always_fail = set(always_fail or ())
        self.gets: list[str] = []

    def _listing(self, prefix: str) -> str:
        keys = sorted(k for k in self.objects if k.startswith(prefix) and "/" not in k[len(prefix):])
        children = sorted(
            {k[len(prefix):].split("/")[0] for k in self.objects if k.startswith(prefix)}
            - {k[len(prefix):] for k in keys}
        )
        body = ["<?xml version='1.0'?><ListBucketResult>"]
        for key in keys:
            body.append(f"<Contents><Key>{key}</Key></Contents>")
        for child in children:
            body.append(f"<CommonPrefixes><Prefix>{prefix}{child}/</Prefix></CommonPrefixes>")
        body.append("<IsTruncated>false</IsTruncated></ListBucketResult>")
        return "".join(body)

    async def __call__(self, url: str) -> bytes:
        self.gets.append(url)
        if "?delimiter=" in url:
            import urllib.parse

            query = urllib.parse.parse_qs(urllib.parse.urlparse(url).query)
            return self._listing(query["prefix"][0]).encode()
        key = url.split("data.binance.vision/", 1)[1]
        if key in self.always_fail:
            raise TimeoutError("object unavailable")
        remaining = self.fail_first.get(key, 0)
        if remaining:
            self.fail_first[key] = remaining - 1
            raise TimeoutError("object unavailable")
        return self.objects[key]


def _kline_objects(symbol: str, months: dict[str, tuple[datetime, int]]) -> dict[str, bytes]:
    prefix = f"data/futures/um/monthly/klines/{symbol}/1d/"
    return {
        f"{prefix}{symbol}-1d-{month}.zip": _zip_bytes(
            f"{symbol}-1d-{month}.csv", _kline_csv(start, days)
        )
        for month, (start, days) in months.items()
    }


class TestTheArchiveProvesItsOwnHistoryDepth:
    """`data.binance.vision` is why "unmeasurable past 30 days" was wrong twice.

    The bucket is listable, so how much history exists is a fact -- the files
    that are there -- rather than a number read out of documentation.
    """

    def _source(self, tmp_path, archive, **kwargs):
        return BinanceArchiveSource(
            http_get=archive, cache=Cache(tmp_path / "cache"), attempts=3, backoff=0.0,
            **kwargs,
        )

    async def test_coverage_is_the_files_that_exist_not_a_documented_limit(self, tmp_path):
        objects = _kline_objects("MATICUSDT", {
            "2024-01": (datetime(2024, 1, 1, tzinfo=UTC), 31),
            "2024-02": (datetime(2024, 2, 1, tzinfo=UTC), 29),
            "2024-03": (datetime(2024, 3, 1, tzinfo=UTC), 31),
        })
        source = self._source(tmp_path, FakeArchive(objects))

        spans = await source.coverage(["MATICUSDT"])

        assert spans["MATICUSDT"].observations == 3
        assert spans["MATICUSDT"].start == pd.Timestamp("2024-01-01", tz=UTC)
        assert spans["MATICUSDT"].end == pd.Timestamp("2024-03-31", tz=UTC)
        assert "object listing" in spans["MATICUSDT"].probe

    async def test_the_symbol_list_includes_names_no_longer_listed(self, tmp_path):
        """The survivorship recovery, and the only source here that has one.

        A universe built from a venue's live market list omits everything that
        died, and the long leg of any cross-sectional test over it was selected
        on having survived.
        """
        objects = {
            **_kline_objects("BTCUSDT", {"2024-01": (datetime(2024, 1, 1, tzinfo=UTC), 31)}),
            **_kline_objects("MATICUSDT", {"2024-01": (datetime(2024, 1, 1, tzinfo=UTC), 31)}),
        }
        source = self._source(tmp_path, FakeArchive(objects))

        assert await source.symbols() == ("BTCUSDT", "MATICUSDT")
        assert source.survivorship.value == "recovers_delisted"

    async def test_a_listed_object_that_never_downloads_raises(self, tmp_path):
        """The reference run collected these into an error list and continued.

        For research that is an unmarked hole in the middle of a series: the
        panel is short by exactly the days that failed and nothing says so.
        """
        objects = _kline_objects("BTCUSDT", {
            "2024-01": (datetime(2024, 1, 1, tzinfo=UTC), 31),
            "2024-02": (datetime(2024, 2, 1, tzinfo=UTC), 29),
        })
        broken = next(k for k in objects if "2024-02" in k)
        source = self._source(tmp_path, FakeArchive(objects, always_fail={broken}))

        with pytest.raises(Unavailable, match="2024-02"):
            await source.observations(["BTCUSDT"], use_cache=False)

    async def test_an_object_that_fails_twice_is_retried_not_dropped(self, tmp_path):
        objects = _kline_objects("BTCUSDT", {
            "2024-01": (datetime(2024, 1, 1, tzinfo=UTC), 31),
            "2024-02": (datetime(2024, 2, 1, tzinfo=UTC), 29),
        })
        flaky = next(k for k in objects if "2024-02" in k)
        source = self._source(tmp_path, FakeArchive(objects, fail_first={flaky: 2}))

        rows = await source.observations(["BTCUSDT"], use_cache=False)

        assert len(rows) == 60

    async def test_a_klines_close_lands_on_the_day_after_the_bar(self, tmp_path):
        """`close_time` is the venue's own statement of when the bar ended.

        The archive publishes it, so the moment a close existed is read rather
        than assumed from a duration.
        """
        objects = _kline_objects("BTCUSDT", {"2024-01": (datetime(2024, 1, 1, tzinfo=UTC), 31)})
        source = self._source(tmp_path, FakeArchive(objects), liquidity_field="quote_volume")

        panel = await source.panel(["BTCUSDT"], universe=Universe(min_assets=1))

        assert panel.frame.index[0] == pd.Timestamp("2024-01-02")
        assert panel.frame.loc[pd.Timestamp("2024-01-02"), "BTCUSDT"] == 10.0

    async def test_a_metrics_snapshot_is_knowable_when_it_is_taken(self, tmp_path):
        """Not the klines case, and the difference is not cosmetic.

        A bar describes an interval that has to finish; a snapshot describes the
        moment it was taken. Stamping a snapshot a day later hides it from a
        replay that could legitimately have seen it.
        """
        rows = [
            f"2024-01-05 {hour:02d}:00:00,BTCUSDT,100,{1000 + hour},1.0,1.0,1.0,1.0"
            for hour in range(24)
        ]
        key = "data/futures/um/daily/metrics/BTCUSDT/BTCUSDT-metrics-2024-01-05.zip"
        objects = {key: _zip_bytes("m.csv", "\n".join([METRICS_HEADER, *rows]))}
        source = self._source(
            tmp_path, FakeArchive(objects), dataset="metrics",
            field="sum_open_interest_value",
        )

        panel = await source.panel(["BTCUSDT"], universe=Universe(min_assets=1))

        assert panel.frame.index[0] == pd.Timestamp("2024-01-05")
        assert panel.frame.loc[pd.Timestamp("2024-01-05"), "BTCUSDT"] == 1023.0

    async def test_a_metrics_file_without_its_header_is_refused(self, tmp_path):
        key = "data/futures/um/daily/metrics/BTCUSDT/BTCUSDT-metrics-2024-01-05.zip"
        objects = {key: _zip_bytes("m.csv", "2024-01-05 00:00:00,BTCUSDT,100,1000")}
        source = self._source(
            tmp_path, FakeArchive(objects), dataset="metrics",
            field="sum_open_interest_value",
        )

        with pytest.raises(Unavailable, match="create_time"):
            await source.observations(["BTCUSDT"], use_cache=False)


class TestASourceSaysWhatItCannotSeeAboutSurvivorship:
    async def test_ccxt_declares_that_it_only_lists_the_living(self, tmp_path):
        source = _source(tmp_path, Venue(_bars(20), page=100))

        panel = await source.panel(
            ["BTC/USDT"],
            start=datetime(2024, 1, 1, tzinfo=UTC),
            universe=Universe(min_assets=1),
        )

        assert panel.survivorship.value == "survivors_only"
        assert any("survived to the end of the sample" in w for w in panel.warnings)

    async def test_the_venue_reports_its_own_earliest_bar_rather_than_a_guess(self, tmp_path):
        venue = Venue(_bars(400, start_ms=int(datetime(2020, 1, 1, tzinfo=UTC).timestamp() * 1000)))
        source = _source(tmp_path, venue)

        spans = await source.coverage(["BTC/USDT"])

        assert spans["BTC/USDT"].start == pd.Timestamp("2020-01-01", tz=UTC)
        assert "REST earliest and latest bar" in spans["BTC/USDT"].probe

    async def test_a_venue_that_ignores_the_probe_gets_no_depth_reported(self, tmp_path):
        """Measured live: bybit reads `since=0` as "no since" and answers with its
        NEWEST bar, so the first version of this probe reported BTC/USDT as one
        day old on a venue that has served it since 2021-07-05.

        One day of history is not a harmless understatement. It is exactly what a
        newly listed asset looks like, and a minimum-history filter would drop
        the name for it.
        """

        class IgnoresSince(Venue):
            async def __call__(self, symbol, *, since=None, limit=None):
                return await super().__call__(symbol, since=None, limit=limit)

        source = _source(tmp_path, IgnoresSince(_bars(400)))

        with pytest.raises(Unavailable, match="ignoring `since`"):
            await source.coverage(["BTC/USDT"])


class TestAlignmentPutsEveryAssetOnOneGrid:
    @staticmethod
    def _observations(records):
        return pd.DataFrame.from_records(
            records, columns=["asset", "event_date", "knowledge_date", "value"]
        )

    def test_assets_with_different_spans_share_one_index(self):
        rows = self._observations(
            [
                ("A", pd.Timestamp(f"2024-01-0{d}", tz=UTC),
                 pd.Timestamp(f"2024-01-0{d}", tz=UTC), float(d))
                for d in range(1, 6)
            ]
            + [
                ("B", pd.Timestamp(f"2024-01-0{d}", tz=UTC),
                 pd.Timestamp(f"2024-01-0{d}", tz=UTC), float(d) * 10)
                for d in range(3, 6)
            ]
        )

        frame = align(rows)

        assert list(frame.columns) == ["A", "B"]
        assert len(frame.index) == 5
        assert bool(frame["B"].iloc[:2].isna().all())

    def test_a_missing_day_becomes_a_row_of_nan_not_a_shorter_series(self):
        rows = self._observations(
            [
                ("A", pd.Timestamp("2024-01-01", tz=UTC), pd.Timestamp("2024-01-01", tz=UTC), 1.0),
                ("A", pd.Timestamp("2024-01-05", tz=UTC), pd.Timestamp("2024-01-05", tz=UTC), 5.0),
            ]
        )

        frame = align(rows)

        assert len(frame.index) == 5
        assert int(frame["A"].isna().sum()) == 3

    def test_several_observations_in_one_day_collapse_the_way_the_caller_said(self):
        rows = self._observations(
            [
                ("A", pd.Timestamp("2024-01-01T00:00", tz=UTC),
                 pd.Timestamp("2024-01-01T00:00", tz=UTC), 1.0),
                ("A", pd.Timestamp("2024-01-01T08:00", tz=UTC),
                 pd.Timestamp("2024-01-01T08:00", tz=UTC), 2.0),
                ("A", pd.Timestamp("2024-01-01T16:00", tz=UTC),
                 pd.Timestamp("2024-01-01T16:00", tz=UTC), 6.0),
            ]
        )

        assert align(rows, reduce="last").iloc[0, 0] == 6.0
        assert align(rows, reduce="mean").iloc[0, 0] == 3.0
        assert align(rows, reduce="sum").iloc[0, 0] == 9.0

    def test_an_unknown_reduce_is_refused(self):
        rows = self._observations(
            [("A", pd.Timestamp("2024-01-01", tz=UTC),
              pd.Timestamp("2024-01-01", tz=UTC), 1.0)]
        )

        with pytest.raises(ValueError, match="unknown reduce"):
            align(rows, reduce="median")

    def test_a_naive_bound_is_refused(self):
        rows = self._observations(
            [("A", pd.Timestamp("2024-01-01", tz=UTC),
              pd.Timestamp("2024-01-01", tz=UTC), 1.0)]
        )

        with pytest.raises(ValueError, match="naive"):
            align(rows, as_of=datetime(2024, 1, 1))  # noqa: DTZ001 - naive is the point


async def _entity(db, symbol: str) -> UUID:
    return await db.pool.fetchval(
        "INSERT INTO entity (kind, symbol, name) VALUES ('crypto_asset',$1,$1) "
        "ON CONFLICT (kind, symbol) DO UPDATE SET name = EXCLUDED.name RETURNING id",
        symbol,
    )


async def _price(db, entity_id, *, close, volume, event_date, knowledge_date=None,
                 audience=None, source="binance"):
    await db.pool.execute(
        "INSERT INTO claim (entity_id, claim_type, key, value, source, event_date, "
        "knowledge_date, confidence, redistributable, audience_user_id) "
        "VALUES ($1,'price_snapshot','close',$2::jsonb,$3,$4,$5,1.0,$6,$7)",
        entity_id,
        json.dumps({"close": close, "volume": volume, "venue": "binance"}),
        source,
        event_date,
        knowledge_date or event_date + timedelta(days=1),
        "byo_only" if audience else "allowed",
        audience,
    )


class TestTheClaimStoreSourceIsAudienceScoped:
    """The rule most likely to be broken by accident, reached from a new path.

    `credential_owner` is an access-control key. A research panel that joined
    `claim` to `entity` directly would be the path by which one operator's
    licensed data reaches another, and no test downstream of here would see it.
    """

    async def test_a_private_claim_is_invisible_to_everyone_else(self, db, database_url):
        owner, other = uuid4(), uuid4()
        entity_id = await _entity(db, "BTC")
        for day in range(5):
            await _price(
                db, entity_id, close=100.0 + day, volume=10.0,
                event_date=datetime(2024, 1, 1, tzinfo=UTC) + timedelta(days=day),
                audience=owner,
            )

        def source(audience):
            return ClaimStoreSource(
                claim_source="binance", audience=audience, database_url=database_url
            )

        assert "BTC" in await source(owner).coverage()
        assert await source(other).coverage() == {}
        assert await source(None).coverage() == {}

    async def test_the_shared_network_is_visible_to_everyone(self, db, database_url):
        entity_id = await _entity(db, "ETH")
        for day in range(5):
            await _price(
                db, entity_id, close=200.0 + day, volume=10.0,
                event_date=datetime(2024, 2, 1, tzinfo=UTC) + timedelta(days=day),
                source="shared",
            )

        spans = await ClaimStoreSource(
            claim_source="shared", database_url=database_url
        ).coverage()

        assert spans["ETH"].observations == 5
        assert "audience-scoped" in spans["ETH"].probe

    async def test_the_panel_is_indexed_by_when_the_claim_became_knowable(
        self, db, database_url
    ):
        entity_id = await _entity(db, "SOL")
        for day in range(30):
            await _price(
                db, entity_id, close=50.0 + day, volume=100.0,
                event_date=datetime(2024, 3, 1, tzinfo=UTC) + timedelta(days=day),
                source="pit",
            )

        panel = await ClaimStoreSource(
            claim_source="pit", database_url=database_url
        ).panel(universe=Universe(min_assets=1), use_cache=False)

        # The bar for 2024-03-01 was written knowable on 2024-03-02.
        assert panel.frame.index[0] == pd.Timestamp("2024-03-02")
        assert panel.frame.loc[pd.Timestamp("2024-03-02"), "SOL"] == 50.0

    async def test_a_claim_with_no_usable_field_is_skipped_not_zero_filled(
        self, db, database_url
    ):
        entity_id = await _entity(db, "DOT")
        await db.pool.execute(
            "INSERT INTO claim (entity_id, claim_type, key, value, source, event_date, "
            "knowledge_date, confidence, redistributable) "
            "VALUES ($1,'price_snapshot','close',$2::jsonb,'holey',$3,$3,1.0,'allowed')",
            entity_id,
            json.dumps({"volume": 5.0}),
            datetime(2024, 4, 1, tzinfo=UTC),
        )
        for day in range(1, 6):
            await _price(
                db, entity_id, close=10.0 + day, volume=5.0,
                event_date=datetime(2024, 4, 1, tzinfo=UTC) + timedelta(days=day),
                source="holey",
            )

        rows = await ClaimStoreSource(
            claim_source="holey", database_url=database_url
        ).observations(use_cache=False)

        assert len(rows) == 5
        assert 0.0 not in set(rows["value"])

    async def test_a_panel_drives_the_harness_end_to_end(self, db, database_url, tmp_path):
        rng = np.random.default_rng(17)
        names = [f"A{i:02d}" for i in range(12)]
        for i, name in enumerate(names):
            entity_id = await _entity(db, name)
            level = 100.0
            for day in range(120):
                level *= float(np.exp(0.02 * rng.normal()))
                await _price(
                    db, entity_id, close=level, volume=1_000.0 + i,
                    event_date=datetime(2024, 5, 1, tzinfo=UTC) + timedelta(days=day),
                    source="e2e",
                )

        panel = await ClaimStoreSource(
            claim_source="e2e", liquidity_field="volume", database_url=database_url,
            cache=Cache(tmp_path / "cache"),
        ).panel(universe=Universe(min_liquidity=Decimal(100), min_assets=10))

        verdicts = evaluate(
            name="sources.e2e", source="claim_store", horizons=(3,),
            signal=lambda prices: -(prices / prices.shift(5) - 1.0),
            prices=panel.frame, registry=Registry(path=tmp_path / "r.jsonl"),
            permutation_draws=10, record=False,
        )

        assert isinstance(panel, Panel)
        assert len(panel.frame.columns) == 12
        assert len(verdicts) == 1
        assert verdicts[0].gross.n >= 20
