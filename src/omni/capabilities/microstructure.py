"""Market microstructure analytics as pure capabilities.

Ported from v1 `app/services/quant/microstructure_analytics.py` (424 lines).
The work order (J2) named five things to carry: order-book imbalance,
effective and realised spread, Kyle's lambda, order-flow toxicity (VPIN), and
trade classification. Everything else in the v1 module was dropped:

- `calculate_amihud_illiquidity` floors its denominator with `volumes + 1e-10`,
  turning a zero-volume day into a giant but finite illiquidity instead of an
  undefined one. Not in the work-order scope; left out rather than rewritten.
- `calculate_price_impact` hardcodes `daily_volume = 1000000` for the
  square-root permanent-impact term -- fabrication by this repo's rule -- and
  was outside scope in any case.
- `get_liquidity_metrics` returns a dict of hardcoded numbers
  (`quoted_spread_bps: 2.5`, `vpin: 0.45`, ...) flagged in v1 itself as
  placeholders and gated behind `ENABLE_SIMULATED_DATA`. Dropped.

The v1 module was a stateful singleton (`MicrostructureAnalytics` held per-
symbol `order_books` / `trades` dicts) and its callers fetched via
`market_data_service`. Neither carries: per PORTING.md the fetch stays in the
caller, and per the work order the book and the trades arrive as arguments.

The endpoint at `app/api/v1/endpoints/quant_analytics.py:120`
(`/microstructure/orderbook/analyze`) was the census's one `wired` surface
here. It did nothing the `OrderBookSnapshot` properties did not already do --
`mid_price`, `weighted_mid_price`, `spread`, `spread_bps`,
`order_imbalance`, `book_pressure` -- and was read only to confirm the input
shape (`bids`/`asks` as `(price, size)` pairs, top-of-book at index 0).

Microstructure is the most input-quality-sensitive analytics surface in this
rebuild: every measure is a ratio over quantities that are routinely missing,
zero, or crossed in real books. v1 defaulted on every one of those
(`return 0.0`), which is precisely why the census marked this endpoint
`wired`. Per AGENTS.md that is how hallucinated coverage enters the store, so
this module raises `Unavailable` from `omni.ingest.protocol` instead. A
crossed book, an empty side, a zero-total-depth book, a trade that predates
its quote, a price series that does not move -- each is degenerate input, not
a measurement of zero.

Sign conventions (stated once, kept everywhere):

- Order imbalance / book pressure: +1 means all resting size is on the bid
  (bid-heavy), -1 means all on the ask. Matches v1.
- Effective spread: `2 * |trade_price - mid|`. Always non-negative. A trade
  at the mid costs nothing; a buy that lifts the ask pays the half-spread,
  doubled to express it on a round-trip basis.
- Price improvement: signed. Positive is good for the trader (better than
  mid). A buy below mid is positive; a sell above mid is positive.
- Realised spread: signed by side, as the liquidity-provider's round-trip
  revenue. For a buy, `2 * (trade_price - future_mid)`; for a sell,
  `2 * (future_mid - trade_price)`. A buy whose price reverts to a lower
  future mid shows a *positive* realised spread (the MM sold high at the ask
  and bought back lower -- the paid impact was temporary, not permanent). This
  is the standard convention; the prior sign was flipped and contradicted this
  docstring's own interpretation.
- Kyle's lambda: signed. Positive lambda means signed buy flow pushes prices
  up (the usual sign); a negative lambda would mean flow predicts price in
  the wrong direction.

Entry points are async, matching `crossasset.py` / `macro.py`. Leaf helpers
(`_find_prevailing_quote`, the `OrderBook` properties) are sync.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

import numpy as np

from omni.ingest.protocol import Unavailable


@dataclass(frozen=True)
class OrderBook:
    """A snapshot of an order book.

    `bids` and `asks` are ``(price, size)`` pairs sorted best-first (highest
    bid at index 0, lowest ask at index 0), matching the shape v1's endpoint
    consumed. The validators and properties raise `Unavailable` on any input
    that would make the requested measure undefined.
    """

    bids: list[tuple[float, float]]
    asks: list[tuple[float, float]]

    def _validate_two_sided(self) -> None:
        if not self.bids:
            raise Unavailable("order book has no bids")
        if not self.asks:
            raise Unavailable("order book has no asks")
        best_bid = self.bids[0][0]
        best_ask = self.asks[0][0]
        if best_bid >= best_ask:
            raise Unavailable(
                f"crossed or locked book: best bid {best_bid} >= best ask {best_ask}"
            )

    @property
    def best_bid(self) -> float:
        self._validate_two_sided()
        return self.bids[0][0]

    @property
    def best_ask(self) -> float:
        self._validate_two_sided()
        return self.asks[0][0]

    @property
    def mid_price(self) -> float:
        self._validate_two_sided()
        return (self.bids[0][0] + self.asks[0][0]) / 2

    @property
    def spread(self) -> float:
        self._validate_two_sided()
        return self.asks[0][0] - self.bids[0][0]

    @property
    def spread_bps(self) -> float:
        mid = self.mid_price
        if mid <= 0:
            raise Unavailable(
                f"non-positive mid price {mid}; cannot express spread in bps"
            )
        return (self.spread / mid) * 10000

    @property
    def weighted_mid_price(self) -> float:
        self._validate_two_sided()
        bid_price, bid_size = self.bids[0]
        ask_price, ask_size = self.asks[0]
        total_size = bid_size + ask_size
        if total_size <= 0:
            raise Unavailable(
                "zero total size at top of book; cannot compute weighted mid"
            )
        return (bid_price * ask_size + ask_price * bid_size) / total_size

    @property
    def order_imbalance(self) -> float:
        # +1 = all bid size, -1 = all ask size, over the top 5 levels per side.
        self._validate_two_sided()
        bid_depth = sum(size for _, size in self.bids[:5])
        ask_depth = sum(size for _, size in self.asks[:5])
        total_depth = bid_depth + ask_depth
        if total_depth == 0:
            raise Unavailable(
                "zero total depth across top 5 levels; imbalance undefined"
            )
        return (bid_depth - ask_depth) / total_depth

    @property
    def book_pressure(self) -> float:
        # Distance-weighted imbalance over the top 10 levels per side. The
        # weight is `1 / (1 + |price - mid| / mid)`, so depth farther from
        # the mid is discounted, with the discount steepening as the
        # distance grows relative to the mid (e.g. at mid=100 a level 1
        # tick away carries weight ~0.990, 10 ticks away ~0.909 -- a mild
        # decay, not a halving). Same sign convention as order_imbalance.
        self._validate_two_sided()
        mid = self.mid_price
        if mid <= 0:
            raise Unavailable(
                f"non-positive mid price {mid}; cannot weight pressure by distance"
            )
        bid_pressure = 0.0
        ask_pressure = 0.0
        for price, size in self.bids[:10]:
            weight = 1.0 / (1.0 + abs(price - mid) / mid)
            bid_pressure += size * weight
        for price, size in self.asks[:10]:
            weight = 1.0 / (1.0 + abs(price - mid) / mid)
            ask_pressure += size * weight
        total_pressure = bid_pressure + ask_pressure
        if total_pressure == 0:
            raise Unavailable("zero weighted pressure; book_pressure undefined")
        return (bid_pressure - ask_pressure) / total_pressure


def _find_prevailing_quote(
    quotes: Sequence[dict], timestamp: datetime
) -> dict:
    """Latest quote at or before `timestamp`, assuming quotes are ascending.

    Raises `Unavailable` when the timestamp predates every quote -- no quote
    was prevailing when the trade printed, so any mid we returned would be
    invented. v1 instead returned `quotes[0]` in that case, which is a future
    quote relative to the trade and is exactly the fabrication this rebuild
    refuses.
    """
    prevailing: dict | None = None
    for q in quotes:
        if q["timestamp"] <= timestamp:
            prevailing = q
        else:
            break
    if prevailing is None:
        raise Unavailable(
            f"no quote prevailing at {timestamp!r}; trade predates the first quote"
        )
    return prevailing


async def effective_spread(
    trades: Sequence[dict],
    quotes: Sequence[dict],
    horizon: timedelta = timedelta(minutes=5),
) -> dict[str, float]:
    """Effective, realised and price-improvement spreads.

    For each of the last 100 trades, look up the quote prevailing at the
    trade time and compute:

    - `effective_spread`: `2 * |trade_price - mid|`. Round-trip cost at
      execution; zero for a trade at the mid, the full quoted spread for a
      trade at the ask or bid.
    - `price_improvement`: signed difference vs mid. Positive is good for the
      trader (a buy filled below mid, a sell filled above).
    - `realised_spread`: the temporary-vs-permanent decomposition, using the
      mid `horizon` (default 5m) after the trade. For a non-negative
      `horizon` every trade that resolved a present quote also resolves a
      future quote, so each trade contributes a realised observation. If no
      realised observation can be derived at all (e.g. a negative `horizon`
      whose future timestamp predates every quote) the call raises
      `Unavailable` rather than fabricating a zero mean.

    `side` must be present on every trade and equal ``"buy"`` or ``"sell"``;
    it determines the sign of price_improvement and realised_spread.

    The means are over the trades that contributed each measure, so
    `effective_spread` / `price_improvement` always reflect the same trades,
    while `realised_spread` may reflect a subset. v1 returned ``0.0`` when
    fewer than 5 trades or 5 quotes were supplied; we raise instead, because
    a mean over too few trades is not a stable execution-cost reading.
    """
    if len(trades) < 5:
        raise Unavailable(f"need >=5 trades for effective spread, got {len(trades)}")
    if len(quotes) < 5:
        raise Unavailable(f"need >=5 quotes for effective spread, got {len(quotes)}")

    effective_spreads: list[float] = []
    realized_spreads: list[float] = []
    price_improvements: list[float] = []

    for trade in trades[-100:]:
        trade_time = trade["timestamp"]
        quote = _find_prevailing_quote(quotes, trade_time)
        mid = (quote["bid"] + quote["ask"]) / 2

        effective_spreads.append(2 * abs(trade["price"] - mid))

        side = trade["side"]
        if side == "buy":
            price_improvements.append(mid - trade["price"])
        elif side == "sell":
            price_improvements.append(trade["price"] - mid)
        else:
            raise Unavailable(
                f"trade has unrecognized side {side!r}; expected 'buy' or 'sell'"
            )

        try:
            future_quote = _find_prevailing_quote(quotes, trade_time + horizon)
        except Unavailable:
            continue
        future_mid = (future_quote["bid"] + future_quote["ask"]) / 2
        # MM-revenue sign: buy unwinds at the future mid (sold at trade price),
        # sell unwinds at the future mid (bought at trade price). Reversion
        # after a buy (future mid below the paid ask) is positive revenue.
        if side == "buy":
            realized_spreads.append(2 * (trade["price"] - future_mid))
        else:
            realized_spreads.append(2 * (future_mid - trade["price"]))

    if not realized_spreads:
        raise Unavailable(
            "no realised-spread observations derived; every future-quote "
            "lookup failed (check horizon vs the quote span)"
        )

    return {
        "effective_spread": float(np.mean(effective_spreads)),
        "realized_spread": float(np.mean(realized_spreads)),
        "price_improvement": float(np.mean(price_improvements)),
    }


async def kyle_lambda(trades: Sequence[dict]) -> float:
    """Kyle's lambda: permanent price impact per unit of signed order flow.

    Signed volume comes from the tick rule (an up-tick is a buy, a down-tick a
    sell; an unchanged tick inherits the prior direction, with the first
    observation treated as a buy to break the tie). Lambda is the OLS slope
    of price-change on signed-volume, scaled by 1e4 to match v1's reporting
    units.

    Three inputs are degenerate and raise:

    - fewer than 10 trades (v1's threshold) -- the regression is noise;
    - price does not move at all across the trades -- a zero-impact market is
      not a measurement of zero impact, it is the absence of one;
    - signed volume has zero variance (e.g. constant size and direction) --
      the slope is undefined.

    v1 returned ``0.0`` in each case. Returning zero on these inputs is how a
    broken feed (all trades same price, all trades same size) would look
    identical to a genuinely low-impact market.
    """
    if len(trades) < 10:
        raise Unavailable(
            f"need >=10 trades to regress Kyle's lambda, got {len(trades)}"
        )

    price_changes: list[float] = []
    signed_volumes: list[float] = []
    for i in range(1, len(trades)):
        price_change = trades[i]["price"] - trades[i - 1]["price"]
        if trades[i]["price"] > trades[i - 1]["price"]:
            signed_volume = trades[i]["volume"]
        elif trades[i]["price"] < trades[i - 1]["price"]:
            signed_volume = -trades[i]["volume"]
        else:
            signed_volume = (
                signed_volumes[-1] if signed_volumes else trades[i]["volume"]
            )
        price_changes.append(price_change)
        signed_volumes.append(signed_volume)

    if not price_changes:
        raise Unavailable("could not derive any price changes from the trades")

    if np.allclose(price_changes, 0.0):
        raise Unavailable(
            "price does not move across trades; zero-impact market is degenerate input"
        )

    x = np.asarray(signed_volumes, dtype=float)
    if np.ptp(x) == 0.0:
        raise Unavailable(
            "signed order flow has zero variance; lambda regression undefined"
        )

    y = np.asarray(price_changes, dtype=float)
    # OLS slope is ddof-independent by definition: Σ(x-x̄)(y-ȳ) / Σ(x-x̄)². The
    # prior np.cov(x,y)[0,1] / np.var(x) mixed ddof (cov=1, var=0) and
    # overstated the slope by n/(n-1) -- ~11% at the 10-trade floor -- and the
    # test had encoded that bias. Compute it directly so it is exactly the
    # regression coefficient regardless of sample size.
    x_dev = x - x.mean()
    slope = float(np.dot(x_dev, y - y.mean()) / np.dot(x_dev, x_dev))
    return slope * 1e4


async def order_flow_toxicity(
    trades: Sequence[dict],
    volume_bucket_size: float | None = None,
) -> float:
    """Volume-synchronized probability of informed trading (VPINmetrc style).

    Trades are bucketed by cumulative volume; within each bucket, the absolute
    imbalance between buy-classified and sell-classified volume is computed,
    and VPIN is the mean imbalance across buckets. Range is [0, 1]: 0 means
    every bucket is perfectly balanced, 1 means every bucket is one-sided.

    Trade direction uses the tick rule with the same convention as v1's VPIN:
    a non-decreasing tick (including the very first trade) is a buy, a
    strictly decreasing tick is a sell. (Kyle's lambda's tick rule, also
    ported here, instead inherits direction on a flat tick; v1's two
    classifiers genuinely disagreed, and each function preserves the rule its
    v1 counterpart used so the bucketing is bit-for-bit identical.)

    `volume_bucket_size` defaults to `daily_volume / 50` -- i.e. 50 buckets
    per day of trades, matching v1. Fewer than 50 trades, or fewer than 5
    completed buckets, raise; v1 returned ``0.0`` in both cases.
    """
    if len(trades) < 50:
        raise Unavailable(f"need >=50 trades for VPIN, got {len(trades)}")

    if volume_bucket_size is None:
        daily_volume = sum(t["volume"] for t in trades)
        if daily_volume <= 0:
            raise Unavailable(
                "total trade volume is zero; cannot size VPIN buckets"
            )
        volume_bucket_size = daily_volume / 50

    buy_volume: list[float] = []
    sell_volume: list[float] = []
    current_bucket_volume = 0.0
    current_buy = 0.0
    current_sell = 0.0

    for i, trade in enumerate(trades):
        if i == 0 or trade["price"] >= trades[i - 1]["price"]:
            current_buy += trade["volume"]
        else:
            current_sell += trade["volume"]

        current_bucket_volume += trade["volume"]
        if current_bucket_volume >= volume_bucket_size:
            buy_volume.append(current_buy)
            sell_volume.append(current_sell)
            current_bucket_volume = 0.0
            current_buy = 0.0
            current_sell = 0.0

    if len(buy_volume) < 5:
        raise Unavailable(
            f"only {len(buy_volume)} volume buckets completed; need >=5 for VPIN"
        )

    imbalances: list[float] = []
    for buy, sell in zip(buy_volume, sell_volume):
        total = buy + sell
        if total > 0:
            imbalances.append(abs(buy - sell) / total)

    if not imbalances:
        raise Unavailable("every bucket had zero volume; VPIN undefined")
    return float(np.mean(imbalances))


# --------------------------------------------------------------------------- #
# Time-series spread analysis (from v1 level2 router)
# --------------------------------------------------------------------------- #

def analyze_spread(
    spread_bps: Sequence[float],
    *,
    current_depth_imbalance: float | None = None,
) -> dict[str, Any]:
    """Spread level, trend and liquidity score over a series of snapshots.

    Extracted from the inline analytics in v1's
    ``GET /level2/{symbol}/spread-analysis`` handler (level2.py:182-237), which
    read historical order-book snapshots and computed the average / min / max /
    current spread, a widening-vs-narrowing trend, and a bucketed liquidity
    score inline. The snapshot fetch (and its single-reading fallback) stayed
    in the handler; this is the pure statistics-over-observations part that
    was ``needs-extraction``.

    ``spread_bps`` is the series of per-snapshot bid-ask spreads in basis
    points, ordered most-recent-first (index 0 is the current reading, as v1
    assumed -- it took ``current_spread = spreads[0]`` and compared the first
    three against the last three). At least one observation is required.

    ``current_depth_imbalance`` is the top-of-book depth imbalance
    ``|bid_depth - ask_depth| / (bid_depth + ask_depth)`` of the current
    snapshot, in [0, 1]. It is optional: v1 defaulted it to 0 (no penalty)
    when the snapshot carried no depth, which is the neutral balanced-book
    reading rather than a fabrication, so ``None`` simply skips the penalty.

    Two v1 default-substitutions are removed:

    - The no-snapshots branch fabricated a full response from a single current
      reading (``liquidity_score = 50.0``, ``spread_trend = "stable"``). With
      no series there is no trend and no honest liquidity reading, so an empty
      ``spread_bps`` raises ``Unavailable``.
    - The trend was ``"stable"`` whenever fewer than three observations were
      available, asserting a direction that cannot be determined from one or
      two points. With fewer than three the trend is ``None`` (unknown); with
      three or more it is ``"widening"`` / ``"narrowing"`` / ``"stable"`` per
      v1's recent-vs-older 1.1x / 0.9x thresholds.

    The liquidity buckets (<10 bps -> 90, <50 -> 70, <100 -> 50, else 30) and
    the -10 imbalance penalty are v1's real heuristics, kept verbatim. v1
    guarded the bucket test with ``if current_spread and ...`` whose ``and``
    was really there for the ``None`` current-spread case; since an empty
    series is refused up front the current reading is always a float, so the
    buckets use explicit comparisons and a genuine 0 bps spread scores 90
    (tightest), not the 30 v1's falsy-guard mis-assigned it.
    """
    spreads = list(spread_bps)
    if not spreads:
        raise Unavailable("no spread observations; trend and liquidity unknown")

    current_spread = spreads[0]
    avg_spread = sum(spreads) / len(spreads)

    if len(spreads) >= 3:
        recent_avg = sum(spreads[:3]) / 3
        older_avg = sum(spreads[-3:]) / 3 if len(spreads) >= 6 else avg_spread
        if recent_avg > older_avg * 1.1:
            trend = "widening"
        elif recent_avg < older_avg * 0.9:
            trend = "narrowing"
        else:
            trend = "stable"
    else:
        trend = None

    if current_spread < 10:
        liquidity_score = 90.0
    elif current_spread < 50:
        liquidity_score = 70.0
    elif current_spread < 100:
        liquidity_score = 50.0
    else:
        liquidity_score = 30.0

    if current_depth_imbalance is not None and current_depth_imbalance > 0.5:
        liquidity_score -= 10

    return {
        "current_spread_bps": float(current_spread),
        "avg_spread_bps": round(avg_spread, 4),
        "min_spread_bps": round(min(spreads), 4),
        "max_spread_bps": round(max(spreads), 4),
        "spread_trend": trend,
        "liquidity_score": float(max(0.0, min(100.0, liquidity_score))),
    }
