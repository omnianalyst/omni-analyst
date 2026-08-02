"""Behaviour tests for the microstructure capability.

Every assertion is arithmetic on a constructed input. Every failure path
called out in work order J2 -- crossed book, empty side, zero total size,
trade timestamped before its quote, fewer trades than the regression needs,
flat price series -- has a test that it raises `Unavailable` rather than
substituting v1's `0.0`. The shape of the output is never the assertion.

Sign conventions exercised below (stated here once, kept in the module):

- Order imbalance / book pressure: +1 = all bid size, -1 = all ask size.
- Effective spread: `2 * |trade_price - mid|`. 0 at mid; the full quoted
  spread at the ask (half-spread doubled).
- Price improvement: signed, positive = better than mid.
- Realised spread: signed by side.
- Kyle's lambda: signed, positive = buy flow moves price up.

`pytest-asyncio` runs in `auto` mode in this repo, so each test is `async def`
and awaits the capability directly.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from omni.capabilities.microstructure import (
    OrderBook,
    analyze_spread,
    effective_spread,
    kyle_lambda,
    order_flow_toxicity,
)
from omni.ingest.protocol import Unavailable

# ---------------------------------------------------------------------------
# OrderBook properties
# ---------------------------------------------------------------------------


class TestOrderBook:
    def test_symmetric_book_has_zero_imbalance(self):
        book = OrderBook(
            bids=[(99, 100), (98, 50)],
            asks=[(101, 100), (102, 50)],
        )
        assert book.order_imbalance == 0.0

    def test_all_size_on_bid_is_plus_one(self):
        # Asks must exist as a priced side but contribute no size, so the
        # imbalance ratio sees all depth on the bid.
        book = OrderBook(bids=[(99, 100), (98, 100)], asks=[(101, 0), (102, 0)])
        assert book.order_imbalance == 1.0

    def test_all_size_on_ask_is_minus_one(self):
        book = OrderBook(bids=[(99, 0), (98, 0)], asks=[(101, 100), (102, 100)])
        assert book.order_imbalance == -1.0

    def test_imbalance_uses_top_five_levels_only(self):
        # The top five bid levels sum to 600 and the top five ask levels to
        # 500, so the top-5 imbalance is (600-500)/1100 = 1/11. Levels beyond
        # index 4 are deliberately asymmetric (a huge ask tail, a tiny bid
        # tail) so summing the whole book lands on a different number, and
        # the fifth bid level (size 200) is distinct from the first four so a
        # [:4] slice also fails this assertion. An implementation that summed
        # all levels would return (601-10499)/11100 = -0.8917.
        book = OrderBook(
            bids=[(99, 100), (98, 100), (97, 100), (96, 100), (95, 200)]
            + [(94, 1)],
            asks=[(101, 100), (102, 100), (103, 100), (104, 100), (105, 100)]
            + [(106, 9999)],
        )
        assert book.order_imbalance == pytest.approx(1 / 11)

    def test_mid_and_spread_arithmetic(self):
        book = OrderBook(bids=[(99, 100)], asks=[(101, 100)])
        assert book.mid_price == 100.0
        assert book.spread == 2.0
        assert book.spread_bps == 200.0

    def test_weighted_mid_equals_mid_when_sizes_equal(self):
        book = OrderBook(bids=[(99, 100)], asks=[(101, 100)])
        assert book.weighted_mid_price == 100.0

    def test_weighted_mid_tilts_toward_smaller_side(self):
        # Sizes 50 bid vs 100 ask -> weighted mid sits closer to the bid price
        # (the bid price is weighted by the larger ask size and vice versa).
        book = OrderBook(bids=[(99, 50)], asks=[(101, 100)])
        assert book.weighted_mid_price == pytest.approx(
            (99 * 100 + 101 * 50) / 150
        )

    def test_book_pressure_zero_on_symmetric_book(self):
        book = OrderBook(
            bids=[(99, 100), (98, 50)],
            asks=[(101, 100), (102, 50)],
        )
        assert book.book_pressure == pytest.approx(0.0)

    def test_book_pressure_sign_matches_imbalance(self):
        # Bid-heavy book -> positive pressure, same sign as imbalance.
        book = OrderBook(bids=[(99, 200)], asks=[(101, 50)])
        assert book.book_pressure > 0.0
        assert book.order_imbalance > 0.0

    def test_book_pressure_weighted_by_distance_profile(self):
        # Total size is identical on each side (200 bid / 200 ask), so a
        # plain unweighted top-10 imbalance is exactly 0.0. The distance
        # weighting the function exists to apply discounts the far ask level
        # (price 111, ~11% from mid) more than the far bid level (price 97,
        # ~3% from mid), so pressure is positive. Hand-derived independently
        # of the function:
        #   bid_pressure = 100/1.01 + 100/1.03 = 196.09727963...
        #   ask_pressure = 100/1.01 + 100/1.11 = 189.09999108...
        #   pressure     = (bp-ap)/(bp+ap)     = 0.0181654676...
        # An implementation that drops the weight loop returns 0.0.
        book = OrderBook(
            bids=[(99, 100), (97, 100)],
            asks=[(101, 100), (111, 100)],
        )
        assert book.book_pressure == pytest.approx(0.018165467625899233)

    def test_crossed_book_raises(self):
        book = OrderBook(bids=[(101, 10)], asks=[(100, 10)])
        with pytest.raises(Unavailable, match="crossed or locked book"):
            _ = book.mid_price

    def test_locked_book_raises(self):
        # bid == ask is a locked market, not a quote.
        book = OrderBook(bids=[(100, 10)], asks=[(100, 10)])
        with pytest.raises(Unavailable, match="crossed or locked book"):
            _ = book.spread

    def test_empty_bid_side_raises(self):
        book = OrderBook(bids=[], asks=[(101, 10)])
        with pytest.raises(Unavailable, match="no bids"):
            _ = book.mid_price

    def test_empty_ask_side_raises(self):
        book = OrderBook(bids=[(99, 10)], asks=[])
        with pytest.raises(Unavailable, match="no asks"):
            _ = book.mid_price

    def test_zero_total_depth_raises_on_imbalance(self):
        book = OrderBook(
            bids=[(99, 0), (98, 0), (97, 0), (96, 0), (95, 0)],
            asks=[(101, 0), (102, 0), (103, 0), (104, 0), (105, 0)],
        )
        with pytest.raises(Unavailable, match="zero total depth"):
            _ = book.order_imbalance

    def test_zero_top_size_raises_on_weighted_mid(self):
        book = OrderBook(bids=[(99, 0)], asks=[(101, 0)])
        with pytest.raises(Unavailable, match="zero total size at top of book"):
            _ = book.weighted_mid_price


# ---------------------------------------------------------------------------
# Effective spread
# ---------------------------------------------------------------------------


def _quote(timestamp: datetime, bid: float = 99.0, ask: float = 101.0) -> dict:
    return {"timestamp": timestamp, "bid": bid, "ask": ask}


def _trade(
    timestamp: datetime, price: float, side: str = "buy", volume: float = 10.0
) -> dict:
    return {"timestamp": timestamp, "price": price, "side": side, "volume": volume}


class TestEffectiveSpread:
    async def test_trade_at_mid_has_zero_effective_spread(self):
        # Quotes spaced 1s apart; trades halfway between. Mid = 100.
        t0 = datetime(2026, 1, 1, 9, 30, tzinfo=UTC)
        quotes = [_quote(t0 + timedelta(seconds=i)) for i in range(10)]
        trades = [
            _trade(t0 + timedelta(seconds=i) + timedelta(milliseconds=500), 100.0)
            for i in range(5)
        ]
        out = await effective_spread(trades, quotes, horizon=timedelta(seconds=1))
        assert out["effective_spread"] == 0.0

    async def test_trade_at_ask_is_half_spread_doubled(self):
        # Spread = 2, half-spread = 1. Buy at ask (101): 2 * |101 - 100| = 2,
        # which is exactly the full quoted spread.
        t0 = datetime(2026, 1, 1, 9, 30, tzinfo=UTC)
        quotes = [_quote(t0 + timedelta(seconds=i)) for i in range(10)]
        trades = [
            _trade(t0 + timedelta(seconds=i) + timedelta(milliseconds=500), 101.0, "buy")
            for i in range(5)
        ]
        out = await effective_spread(trades, quotes, horizon=timedelta(seconds=2))
        assert out["effective_spread"] == 2.0

    async def test_trade_at_bid_is_same_magnitude(self):
        # Sell at bid (99): 2 * |99 - 100| = 2. Same cost magnitude as the ask.
        t0 = datetime(2026, 1, 1, 9, 30, tzinfo=UTC)
        quotes = [_quote(t0 + timedelta(seconds=i)) for i in range(10)]
        trades = [
            _trade(t0 + timedelta(seconds=i) + timedelta(milliseconds=500), 99.0, "sell")
            for i in range(5)
        ]
        out = await effective_spread(trades, quotes, horizon=timedelta(seconds=2))
        assert out["effective_spread"] == 2.0

    async def test_price_improvement_sign_by_side(self):
        # A buy filled below mid is positive improvement; mid = 100.
        t0 = datetime(2026, 1, 1, 9, 30, tzinfo=UTC)
        quotes = [_quote(t0 + timedelta(seconds=i)) for i in range(10)]
        trades = [
            _trade(t0 + timedelta(seconds=i) + timedelta(milliseconds=500), 99.5, "buy")
            for i in range(5)
        ]
        out = await effective_spread(trades, quotes, horizon=timedelta(seconds=2))
        # buy at 99.5 vs mid 100 -> improvement = mid - price = +0.5
        assert out["price_improvement"] == pytest.approx(0.5)

    async def test_realized_spread_when_future_mid_reverts(self):
        # Buy at ask 101, mid 100. With quotes every 1s and a 5s horizon, the
        # prevailing quote 5s after each trade is still bid 99 / ask 101 ->
        # future_mid = 100. The MM sold at 101 and the fair value (future mid)
        # is 100, so the MM kept the half-spread: realized (buy) =
        # 2 * (trade_price - future_mid) = 2 * (101 - 100) = +2. Positive, per
        # the standard MM-revenue convention the docstring states (the prior
        # sign was flipped and returned -2, contradicting that interpretation).
        t0 = datetime(2026, 1, 1, 9, 30, tzinfo=UTC)
        quotes = [_quote(t0 + timedelta(seconds=i)) for i in range(10)]
        trades = [
            _trade(t0 + timedelta(seconds=i) + timedelta(milliseconds=500), 101.0, "buy")
            for i in range(5)
        ]
        out = await effective_spread(trades, quotes, horizon=timedelta(seconds=5))
        assert out["realized_spread"] == pytest.approx(2.0)

    async def test_too_few_trades_raises(self):
        t0 = datetime(2026, 1, 1, 9, 30, tzinfo=UTC)
        quotes = [_quote(t0 + timedelta(seconds=i)) for i in range(10)]
        trades = [_trade(t0 + timedelta(seconds=i), 100.0) for i in range(4)]
        with pytest.raises(Unavailable, match=">=5 trades"):
            await effective_spread(trades, quotes)

    async def test_too_few_quotes_raises(self):
        t0 = datetime(2026, 1, 1, 9, 30, tzinfo=UTC)
        quotes = [_quote(t0 + timedelta(seconds=i)) for i in range(4)]
        trades = [_trade(t0 + timedelta(seconds=i), 100.0) for i in range(5)]
        with pytest.raises(Unavailable, match=">=5 quotes"):
            await effective_spread(trades, quotes)

    async def test_trade_before_first_quote_raises(self):
        # First quote at t0; first trade at t0 - 1s predates every quote.
        t0 = datetime(2026, 1, 1, 9, 30, tzinfo=UTC)
        quotes = [_quote(t0 + timedelta(seconds=i)) for i in range(10)]
        trades = [_trade(t0 + timedelta(seconds=i), 100.0) for i in range(5)]
        trades[0] = _trade(t0 - timedelta(seconds=1), 100.0)
        with pytest.raises(Unavailable, match="predates the first quote"):
            await effective_spread(trades, quotes)

    async def test_no_realised_observations_raises_not_zero(self):
        # A negative horizon pushes every `trade_time + horizon` before the
        # first quote, so no realised observation is derived. v1/old-port
        # fabricated realized_spread = 0.0 here (Q4 Finding 5); the module
        # now raises instead.
        t0 = datetime(2026, 1, 1, 9, 30, tzinfo=UTC)
        quotes = [_quote(t0 + timedelta(seconds=i)) for i in range(10)]
        trades = [
            _trade(t0 + timedelta(seconds=i) + timedelta(milliseconds=500), 101.0, "buy")
            for i in range(5)
        ]
        with pytest.raises(Unavailable, match="no realised-spread observations"):
            await effective_spread(trades, quotes, horizon=timedelta(seconds=-100))


# ---------------------------------------------------------------------------
# Kyle's lambda
# ---------------------------------------------------------------------------


def _kyle_trades(prices: list[float], volumes: list[float]) -> list[dict]:
    return [{"price": p, "volume": v} for p, v in zip(prices, volumes)]


class TestKyleLambda:
    async def test_known_slope(self):
        # price[i] = price[i-1] + k * volume[i]  -> each price_change = k * vol.
        # All up-ticks, so signed_volume = +vol, and y = k * x exactly. The OLS
        # slope of y on x is exactly k (ddof-independent by definition), scaled
        # by 1e4 -> 10.0. The prior cov(ddof=1)/var(ddof=0) form returned
        # k * n/(n-1) = 11.25 instead -- the bias this test had encoded.
        volumes = [100, 200, 50, 150, 80, 120, 60, 90, 110, 70]
        k = 0.001
        prices = [100.0]
        for v in volumes[1:]:
            prices.append(prices[-1] + k * v)
        out = await kyle_lambda(_kyle_trades(prices, volumes))
        assert out == pytest.approx(10.0)

    async def test_slope_unchanged_when_price_and_flow_flip_together(self):
        # Mirror of test_known_slope: monotonically falling prices -> signed
        # volume is -|vol|, price_change = -k * |vol| = k * signed_vol. Both
        # signed_volume and price_change flip sign together, so the OLS slope is
        # unchanged (k * 1e4 = 10.0). Confirms lambda reflects the cov(x, y)
        # sign, not the direction of price movement alone.
        volumes = [100, 200, 50, 150, 80, 120, 60, 90, 110, 70]
        k = 0.001
        prices = [100.0]
        for v in volumes[1:]:
            prices.append(prices[-1] - k * v)
        out = await kyle_lambda(_kyle_trades(prices, volumes))
        assert out == pytest.approx(10.0)

    async def test_too_few_trades_raises(self):
        trades = _kyle_trades(
            [100, 100.1, 100.2, 100.3, 100.4, 100.5, 100.6, 100.7, 100.8],
            [10, 20, 30, 40, 50, 60, 70, 80, 90],
        )
        with pytest.raises(Unavailable, match=">=10 trades"):
            await kyle_lambda(trades)

    async def test_flat_price_series_raises(self):
        # Every trade at the same price -> price_changes all zero.
        trades = _kyle_trades([100.0] * 12, [10 * (i + 1) for i in range(12)])
        with pytest.raises(Unavailable, match="does not move"):
            await kyle_lambda(trades)

    async def test_zero_variance_signed_volume_raises(self):
        # Constant volume with strictly increasing prices -> signed_volumes
        # are all +V (zero variance), even though price does move. The volume
        # is deliberately not exactly representable (3.14159): np.std of the
        # constant series is ~4.4e-16, not 0.0, which is the exact input that
        # defeated the old `float(np.std(x)) == 0.0` guard (Q4 Finding 1).
        trades = _kyle_trades(
            [100.0 + 0.01 * i for i in range(12)], [3.14159] * 12
        )
        with pytest.raises(Unavailable, match="zero variance"):
            await kyle_lambda(trades)


# ---------------------------------------------------------------------------
# VPIN / order-flow toxicity
# ---------------------------------------------------------------------------


def _vpin_trades(prices: list[float], volume: float = 10.0) -> list[dict]:
    return [{"price": p, "volume": volume} for p in prices]


class TestOrderFlowToxicity:
    async def test_all_buys_is_one(self):
        # Monotonically rising prices, equal volume, default bucket size =
        # total/50 -> 50 buckets, each all-buy -> imbalance 1 -> VPIN 1.0.
        prices = [100.0 + 0.01 * i for i in range(50)]
        out = await order_flow_toxicity(_vpin_trades(prices))
        assert out == 1.0

    async def test_all_sells_is_one(self):
        # Strictly falling prices: every trade after the first is a sell. The
        # first trade is a buy by convention, so the first bucket has one buy
        # and the rest sells -- still one-sided enough that VPIN is 1.0 only
        # if the first bucket also completes one-sided. With 50 trades at
        # volume 10 and bucket_size 10, the first bucket is buy=10 (the first
        # trade), and the next 49 trades are sells -- bucket 2 onwards are
        # sell-only. The first bucket's imbalance is |10-0|/10 = 1; the rest
        # are 1. VPIN = 1.0.
        prices = [100.0 - 0.01 * i for i in range(50)]
        out = await order_flow_toxicity(_vpin_trades(prices))
        assert out == 1.0

    async def test_balanced_flow_within_buckets_is_zero(self):
        # bucket_size = 20; price alternates 100 / 99.99 so within each bucket
        # we get one buy (the up-tick) and one sell (the down-tick). The first
        # trade is a forced buy; the second trade is a sell (p1 < p0). Bucket
        # completes with cb=10, cs=10, imbalance 0. Pattern repeats.
        prices = [100.0 if i % 2 == 0 else 99.99 for i in range(50)]
        out = await order_flow_toxicity(_vpin_trades(prices), volume_bucket_size=20.0)
        assert out == pytest.approx(0.0)

    async def test_partial_imbalance_is_one_third(self):
        # bucket_size = 30; pattern [100.00, 100.01, 99.99] repeating. Each
        # bucket contains forced-buy (t0 of bucket), one up-tick buy, one
        # down-tick sell -> cb=20, cs=10 -> imbalance = 10/30 = 1/3.
        prices = []
        for i in range(60):
            mod = i % 3
            if mod == 0:
                prices.append(100.00)
            elif mod == 1:
                prices.append(100.01)
            else:
                prices.append(99.99)
        out = await order_flow_toxicity(_vpin_trades(prices), volume_bucket_size=30.0)
        assert out == pytest.approx(1.0 / 3.0)

    async def test_too_few_trades_raises(self):
        trades = _vpin_trades([100.0 + 0.01 * i for i in range(49)])
        with pytest.raises(Unavailable, match=">=50 trades"):
            await order_flow_toxicity(trades)

    async def test_too_few_buckets_raises(self):
        # 50 trades of volume 10 -> total 500. Bucket size 1e6 means no bucket
        # ever completes; buy_volume stays empty.
        trades = _vpin_trades([100.0 + 0.01 * i for i in range(50)])
        with pytest.raises(Unavailable, match=">=5 for VPIN"):
            await order_flow_toxicity(trades, volume_bucket_size=1e6)


# ---------------------------------------------------------------------------
# analyze_spread (time-series spread analysis from the v1 level2 router)
# ---------------------------------------------------------------------------

def test_spread_stats_are_hand_computed():
    # Index 0 = most recent. avg = (12 + 4 + 8) / 3 = 8.
    out = analyze_spread([12.0, 4.0, 8.0])
    assert out["current_spread_bps"] == 12.0
    assert out["avg_spread_bps"] == 8.0
    assert out["min_spread_bps"] == 4.0
    assert out["max_spread_bps"] == 12.0


def test_spread_trend_widening_when_recent_above_older():
    # First three (recent) average 20, last three (older) average 10.
    # 20 > 10 * 1.1 -> widening.
    out = analyze_spread([20.0, 20.0, 20.0, 10.0, 10.0, 10.0])
    assert out["spread_trend"] == "widening"


def test_spread_trend_narrowing_when_recent_below_older():
    out = analyze_spread([5.0, 5.0, 5.0, 50.0, 50.0, 50.0])
    assert out["spread_trend"] == "narrowing"


def test_spread_trend_stable_when_recent_within_band():
    # recent avg 10, older avg 10 -> within 0.9..1.1 band -> stable.
    out = analyze_spread([10.0, 10.0, 10.0, 10.0, 10.0, 10.0])
    assert out["spread_trend"] == "stable"


def test_spread_trend_unknown_with_fewer_than_three_observations():
    # v1 returned "stable" on <3 points, asserting a direction it cannot
    # determine; here the trend is None (unknown).
    out = analyze_spread([5.0, 7.0])
    assert out["spread_trend"] is None


def test_spread_liquidity_buckets_by_current_spread():
    assert analyze_spread([5.0])["liquidity_score"] == 90.0  # < 10 bps
    assert analyze_spread([40.0])["liquidity_score"] == 70.0  # < 50 bps
    assert analyze_spread([80.0])["liquidity_score"] == 50.0  # < 100 bps
    assert analyze_spread([150.0])["liquidity_score"] == 30.0  # else


def test_spread_zero_bps_scores_tightest_not_worst():
    # v1's `if current_spread and ...` falsy-guard sent a 0 bps spread to the
    # 30 bucket; an explicit comparison sends it to 90 (a locked/zero spread
    # is maximum liquidity).
    assert analyze_spread([0.0])["liquidity_score"] == 90.0


def test_spread_depth_imbalance_penalises_liquidity():
    # 40 bps -> 70; imbalance 0.6 (> 0.5) -> -10 = 60.
    out = analyze_spread([40.0], current_depth_imbalance=0.6)
    assert out["liquidity_score"] == 60.0


def test_spread_depth_imbalance_below_threshold_does_not_penalise():
    out = analyze_spread([40.0], current_depth_imbalance=0.3)
    assert out["liquidity_score"] == 70.0


def test_spread_empty_raises():
    with pytest.raises(Unavailable):
        analyze_spread([])
