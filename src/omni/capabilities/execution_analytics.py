"""Execution analytics / transaction cost analysis -- the measurement path only.

v1 source: ``../software/backend/app/services/quant/execution_analytics.py``
(638 lines, read-only). Ported here as pure capabilities.

This module measures executions that have already happened. Everything in it
takes a *completed* set of fills and (where a benchmark is needed) a
*historical* price/volume path, and reports what the execution cost. Nothing in
it places, sizes, schedules or recommends an order. ``src/omni/execution/
broker.py`` remains the only layer in v2 that acts on the world; a second one
is not created here, and this module imports nothing from the execution tier
(keeping measurement decoupled from the actor).

What carried, and from where:

- ``post_trade_tca`` slippage arithmetic (arrival slippage, VWAP slippage, the
  implementation-shortfall decomposition, execution-quality labelling, outlier
  z-scoring). v1 wrapped these on a single ``Execution`` dataclass whose
  ``arrival_price`` / ``vwap_benchmark`` / ``close_price`` were pre-computed
  scalars handed in by the caller -- which let a stale or invented VWAP enter
  the metric unchanged. The port instead computes interval VWAP/TWAP from a
  benchmark window of (price, volume) bars, so a window with no volume raises
  rather than dividing by zero against a smuggled-in scalar.
- ``MarketImpactModel`` -- the three estimators (linear, square-root, power
  law). These take inputs and return an estimated impact; they do not decide an
  order. Their coefficients are carried verbatim as named module constants
  (below) and are uncalibrated, exactly as v1 left them; the report flags this.

What did NOT carry (each decides or recommends rather than measures, and the
work order forbids it):

- ``pre_trade_analytics`` -- returned ``recommended_algorithm`` (IS / VWAP /
  POV / TWAP chosen from an urgency score). Choosing an algorithm is a
  decision, not a measurement.
- ``_generate_execution_schedule`` -- **the execution scheduler**: it slices an
  order into time slices with per-slice quantities (TWAP even slices, VWAP
  U-shaped volume curve, fallback 30/50/20 split). This is the function the
  work order names explicitly; slicing an order is what an execution tier does,
  and v2's execution tier is deliberately wired to nothing pending a product
  decision.
- ``_generate_execution_recommendations`` -- emitted advice strings ("use POV
  or VWAP algorithms", "consider splitting across multiple days").
- ``_calculate_execution_risk`` / ``_calculate_aggregate_impact`` /
  ``_calculate_portfolio_execution_risk`` -- pre-trade risk/impact scores that
  exist only to feed ``pre_trade_analytics`` and its recommendations.
- ``ExecutionAlgorithm`` enum and the ``Order`` / ``Execution`` dataclasses --
  scaffolding for the pre-trade/recommendation path. ``Order`` carried
  ``urgency`` and ``start_time``/``end_time`` (scheduling inputs); the honest
  replacements are the ``Fill`` and ``BenchmarkBar`` value objects below, which
  hold only completed-execution state.

Deviations from v1, and why:

1. Implementation shortfall uses ``decision_price`` as the single numeraire
   for *every* component (delay / trading / opportunity). v1 mixed denominators
   -- ``arrival_price`` for delay and opportunity, ``market_price_at_execution``
   for trading -- so its three components never summed exactly to a checkable
   total. With one numeraire the decomposition is exactly additive in currency
   (Perold/Wagner) and converts to bps by the decision notional;
   ``ImplementationShortfall.check_additivity`` is called inside
   ``implementation_shortfall`` so a future edit that breaks additivity fails
   loudly (the same contract ``capabilities/attribution.py`` defends).
2. ``decision_price`` is an explicit required keyword argument. v1 read it from
   ``Execution.arrival_price``. The work order flags the classic fake: a
   shortfall measured against the first fill's own price is definitionally
   zero. Making the decision price a required argument forces the caller to
   supply a reference that is independent of the fills.
3. Where v1 substituted a default on missing input -- ``pre_trade_analytics``
   filled volatility=0.02, spread=0.0001, avg_daily_volume=1e6, price=100
   whenever the caller omitted them -- the port raises ``Unavailable``. Those
   defaults lived entirely in the refused pre-trade path, so they are gone with
   it; no new default is introduced here. Undefined statistics (empty fills,
   zero fill size, zero window volume, fills outside the benchmark window, zero
   decision price, zero order quantity) raise ``Unavailable`` rather than
   returning a fabricated zero.
4. v1 exposed a module-level singleton ``execution_analytics = ExecutionAnalytics()``
   and the whole class was stateful (``self.impact_model = ...``). The port is
   pure functions and module-level constants; there is no instance and no
   global, matching how ``capabilities/regime.py`` treated its anchors.

No coefficients here are tuned and none are invented; each is named after the
v1 literal it preserves.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime

import numpy as np

from omni.ingest.protocol import Unavailable

# v1 expressed slippage in basis points (fraction * 10000); kept verbatim.
_BPS = 10_000.0

# Half-spread expressed in bps from a fractional spread: (spread / 2) * 10000.
# v1 wrote this as `spread * 5000` in both the linear and square-root models;
# the named constant makes the algebra obvious without changing the value.
_HALF_SPREAD_TO_BPS = 5000.0

# Execution-quality bands (v1 ``_assess_execution_quality``), in IS bps. These
# are labels on a measured value, not a decision; the bands are v1's and are
# not retuned here.
_QUALITY_EXCELLENT_BPS = 5.0
_QUALITY_GOOD_BPS = 10.0
_QUALITY_AVERAGE_BPS = 20.0
_QUALITY_POOR_BPS = 50.0

# Outlier flag threshold (v1 ``_identify_outlier_executions``): |z| > 2.
_OUTLIER_Z_THRESHOLD = 2.0

# Market-impact coefficients. These are v1's calibration literals, carried
# bit-for-bit; none is tuned and v1 itself never calibrated them (the linear
# coefs were arbitrary, the square-root coefs echo Almgren et al., the
# power-law k=1.0 is "uncalibrated"). Named so a future calibration lives in
# one place.
_LINEAR_TEMP_COEF = 0.1
_LINEAR_PERM_COEF = 0.05
_LINEAR_VOL_MULT = 1.5
_SQRT_TEMP_COEF = 0.314
_SQRT_PERM_COEF = 0.142
_SQRT_ANNUALISE = float(np.sqrt(252))
_POWER_LAW_COEF = 1.0
_POWER_LAW_ALPHA = 0.6


@dataclass(frozen=True)
class Fill:
    """A single completed fill. ``size`` is quantity filled and is positive."""

    price: float
    size: float
    timestamp: datetime


@dataclass(frozen=True)
class BenchmarkBar:
    """One bar of the historical price/volume path a benchmark is built from."""

    timestamp: datetime
    price: float
    volume: float


@dataclass
class ImplementationShortfall:
    """Implementation shortfall decomposed into exactly-additive components.

    ``total_bps`` is computed independently of the components and
    ``check_additivity`` asserts they agree to floating tolerance. This is the
    same contract ``capabilities/attribution.py`` enforces for factor
    attribution: the check runs inside ``implementation_shortfall`` so a future
    edit that desynchronises the total from its parts fails loudly.
    """

    total_bps: float
    delay_cost_bps: float
    trading_cost_bps: float
    opportunity_cost_bps: float
    fill_rate: float
    side: str
    decision_price: float
    market_price_at_execution: float
    close_price: float
    fill_vwap: float
    filled_quantity: float
    order_quantity: float
    n_fills: int

    @property
    def components_bps(self) -> dict[str, float]:
        return {
            "delay_cost_bps": self.delay_cost_bps,
            "trading_cost_bps": self.trading_cost_bps,
            "opportunity_cost_bps": self.opportunity_cost_bps,
        }

    def check_additivity(self, atol: float = 1e-9) -> None:
        total = self.delay_cost_bps + self.trading_cost_bps + self.opportunity_cost_bps
        if not np.isclose(total, self.total_bps, atol=atol):
            raise AssertionError(
                f"implementation shortfall not additive: "
                f"delay + trading + opportunity = {total}, total_bps = {self.total_bps}"
            )

    def to_dict(self) -> dict:
        return {
            "total_bps": float(self.total_bps),
            "components_bps": {k: float(v) for k, v in self.components_bps.items()},
            "fill_rate": float(self.fill_rate),
            "side": self.side,
            "decision_price": float(self.decision_price),
            "market_price_at_execution": float(self.market_price_at_execution),
            "close_price": float(self.close_price),
            "fill_vwap": float(self.fill_vwap),
            "filled_quantity": float(self.filled_quantity),
            "order_quantity": float(self.order_quantity),
            "n_fills": int(self.n_fills),
        }


# --------------------------------------------------------------------------- #
# Internal helpers
# --------------------------------------------------------------------------- #


def _side_sign(side: str) -> float:
    """+1 for buy, -1 for sell. v1 negated slippage for sells; same convention.

    A buy pays the slippage (cost > 0 when price rises against you); a sell
    earns it. Kept as the explicit sign so the arithmetic reads identically to
    v1's ``if side == "sell": x = -x`` branches.
    """
    if side == "buy":
        return 1.0
    if side == "sell":
        return -1.0
    raise Unavailable(f"side must be 'buy' or 'sell', got {side!r}")


def _require_fills(fills: Sequence[Fill]) -> list[Fill]:
    fills = list(fills)
    if len(fills) == 0:
        raise Unavailable("no fills: implementation shortfall is undefined")
    if any(f.size == 0 for f in fills):
        raise Unavailable("fill with zero size: size-weighted average is undefined")
    if any(f.size < 0 for f in fills):
        raise Unavailable(f"fill with negative size: {next(f.size for f in fills if f.size < 0)!r}")
    return fills


def _require_fills_in_window(fills: Sequence[Fill], bars: Sequence[BenchmarkBar]) -> None:
    """Every fill must fall inside the benchmark window [first_bar, last_bar].

    A fill outside the window the benchmark was built from is being measured
    against a benchmark that did not contain it -- silently clipping or
    extrapolating is how a flattering zero appears.
    """
    if len(bars) == 0:
        raise Unavailable("benchmark window is empty")
    start = min(b.timestamp for b in bars)
    end = max(b.timestamp for b in bars)
    for f in fills:
        if f.timestamp < start or f.timestamp > end:
            raise Unavailable(
                f"fill at {f.timestamp.isoformat()} is outside the benchmark "
                f"window [{start.isoformat()}, {end.isoformat()}]"
            )


# --------------------------------------------------------------------------- #
# Benchmarks built from a historical price/volume path
# --------------------------------------------------------------------------- #


def fill_vwap(fills: Sequence[Fill]) -> float:
    """Size-weighted average price of completed fills.

    Raises ``Unavailable`` on an empty fill set or any zero-size fill -- both
    make the weighted average undefined. v1 took the execution price as a
    scalar; the port computes it from the fills so a zero-size fill cannot hide.
    ``_require_fills`` rejects zero and negative sizes, so the surviving total
    is strictly positive and the weighted average cannot divide by zero.
    """
    fills = _require_fills(fills)
    total_size = float(sum(f.size for f in fills))
    return float(sum(f.price * f.size for f in fills)) / total_size


def interval_vwap(bars: Sequence[BenchmarkBar]) -> float:
    """Volume-weighted average price over the benchmark window.

    Raises ``Unavailable`` when the window has zero total volume: VWAP is
    undefined, and v1's structure would have divided by a smuggled-in scalar
    benchmark instead. The work order requires this raise rather than a
    ZeroDivisionError.
    """
    bars = list(bars)
    if len(bars) == 0:
        raise Unavailable("benchmark window is empty: interval VWAP is undefined")
    total_volume = float(sum(b.volume for b in bars))
    if total_volume == 0.0:
        raise Unavailable("benchmark window has zero volume: interval VWAP is undefined")
    return float(sum(b.price * b.volume for b in bars)) / total_volume


def interval_twap(bars: Sequence[BenchmarkBar]) -> float:
    """Time-weighted average price over uniformly-spaced benchmark bars.

    Mirrors v1's TWAP schedule, which distributed execution *evenly* across the
    window -- the equal-weight mean of bar prices. Raises ``Unavailable`` on an
    empty window.
    """
    bars = list(bars)
    if len(bars) == 0:
        raise Unavailable("benchmark window is empty: interval TWAP is undefined")
    return float(np.mean([b.price for b in bars]))


def participation_rate(fills: Sequence[Fill], bars: Sequence[BenchmarkBar]) -> float:
    """Filled quantity as a fraction of total window volume.

    Exactly 1.0 when the fills account for the whole interval volume. Raises
    ``Unavailable`` on empty fills, empty bars, zero fill size, a fill outside
    the window, or zero window volume (the last makes the rate undefined).
    """
    fills = _require_fills(fills)
    _require_fills_in_window(fills, bars)
    total_volume = float(sum(b.volume for b in bars))
    if total_volume == 0.0:
        raise Unavailable("benchmark window has zero volume: participation rate is undefined")
    return float(sum(f.size for f in fills)) / total_volume


# --------------------------------------------------------------------------- #
# Slippage vs a benchmark price
# --------------------------------------------------------------------------- #


def benchmark_slippage_bps(
    fills: Sequence[Fill], benchmark_price: float, side: str
) -> float:
    """Fill VWAP vs a benchmark price, in bps, signed for side.

    Positive is costly for the executor in both directions (a buy that lifts
    the market, a sell that hits it). Raises ``Unavailable`` on empty/zero
    fills or a zero benchmark price.
    """
    fills = _require_fills(fills)
    if benchmark_price == 0.0:
        raise Unavailable("benchmark price is zero: slippage is undefined")
    sign = _side_sign(side)
    executed = fill_vwap(fills)
    return float(sign * (executed - benchmark_price) / benchmark_price * _BPS)


def arrival_slippage_bps(
    fills: Sequence[Fill], arrival_price: float, side: str
) -> float:
    """Fill VWAP vs the arrival/decision price, in bps."""
    return benchmark_slippage_bps(fills, arrival_price, side)


def vwap_slippage_bps(
    fills: Sequence[Fill], bars: Sequence[BenchmarkBar], side: str
) -> float:
    """Fill VWAP vs the interval VWAP, in bps.

    Zero when the size-weighted fill price equals the window VWAP. Raises
    ``Unavailable`` when the window has zero volume (VWAP undefined) or a fill
    falls outside the window.
    """
    fills = _require_fills(fills)
    _require_fills_in_window(fills, bars)
    return benchmark_slippage_bps(fills, interval_vwap(bars), side)


def twap_slippage_bps(
    fills: Sequence[Fill], bars: Sequence[BenchmarkBar], side: str
) -> float:
    """Fill VWAP vs the interval TWAP, in bps."""
    fills = _require_fills(fills)
    _require_fills_in_window(fills, bars)
    return benchmark_slippage_bps(fills, interval_twap(bars), side)


# --------------------------------------------------------------------------- #
# Implementation shortfall -- decomposed, exactly additive
# --------------------------------------------------------------------------- #


def implementation_shortfall(
    fills: Sequence[Fill],
    *,
    decision_price: float,
    market_price_at_execution: float,
    close_price: float,
    order_quantity: float,
    side: str,
) -> ImplementationShortfall:
    """Implementation shortfall decomposed into delay / trading / opportunity.

    The decomposition is the Perold/Wagner one expressed in the decision price
    as a single numeraire, so the three components sum *exactly* to the total
    (verified by ``check_additivity``, called below). v1 used mixed
    denominators and so could not be checked; see the module docstring.

    ``decision_price`` is a required keyword argument. It must be supplied
    independently of the fills -- measuring shortfall against the first fill's
    own price is definitionally zero and is the classic way this metric is
    faked (the work order calls this out explicitly).

    Raises ``Unavailable`` on empty/zero fills, zero decision price, zero order
    quantity, fill_rate outside (0, 1] when opportunity cost is nonzero, or an
    unknown side.
    """
    fills = _require_fills(fills)
    sign = _side_sign(side)
    if decision_price == 0.0:
        raise Unavailable("decision_price is zero: shortfall is undefined")
    if order_quantity <= 0:
        raise Unavailable(
            f"order_quantity must be positive, got {order_quantity}"
        )

    filled_quantity = float(sum(f.size for f in fills))
    fill_rate = filled_quantity / float(order_quantity)
    if fill_rate > 1.0 + 1e-12:
        raise Unavailable(
            f"filled quantity {filled_quantity} exceeds order_quantity "
            f"{order_quantity}: fill_rate > 1"
        )

    executed_price = fill_vwap(fills)
    d = float(decision_price)
    m = float(market_price_at_execution)
    c = float(close_price)

    # Currency-accurate components, decision notional as the single numeraire.
    # delay:   market moved between the decision and the time of execution.
    # trading: how far the fill price sat from the market at execution (impact).
    # opportunity: the unfilled remainder valued at the close.
    delay_rel = sign * fill_rate * (m - d) / d
    trading_rel = sign * fill_rate * (executed_price - m) / d
    opportunity_rel = sign * (1.0 - fill_rate) * (c - d) / d

    # Independent total: the filled leg at the fill price plus the unfilled leg
    # at the close, vs the decision price. Must equal the component sum.
    total_rel = sign * (
        fill_rate * (executed_price - d) + (1.0 - fill_rate) * (c - d)
    ) / d

    result = ImplementationShortfall(
        total_bps=float(total_rel * _BPS),
        delay_cost_bps=float(delay_rel * _BPS),
        trading_cost_bps=float(trading_rel * _BPS),
        opportunity_cost_bps=float(opportunity_rel * _BPS),
        fill_rate=float(fill_rate),
        side=side,
        decision_price=d,
        market_price_at_execution=m,
        close_price=c,
        fill_vwap=float(executed_price),
        filled_quantity=filled_quantity,
        order_quantity=float(order_quantity),
        n_fills=len(fills),
    )
    result.check_additivity()
    return result


# --------------------------------------------------------------------------- #
# Cross-execution aggregation -- faithful ports of v1's post-trade stats
# --------------------------------------------------------------------------- #


def assess_execution_quality(implementation_shortfall_bps: float) -> str:
    """Label an IS-bps value. Ported verbatim from v1 ``_assess_execution_quality``.

    Classification of a measured cost, not a decision. Bands are v1's and are
    not retuned.
    """
    is_bps = abs(float(implementation_shortfall_bps))
    if is_bps < _QUALITY_EXCELLENT_BPS:
        return "Excellent"
    if is_bps < _QUALITY_GOOD_BPS:
        return "Good"
    if is_bps < _QUALITY_AVERAGE_BPS:
        return "Average"
    if is_bps < _QUALITY_POOR_BPS:
        return "Poor"
    return "Very Poor"


def slippage_summary(slippage_bps: Sequence[float]) -> dict[str, float]:
    """Mean / median / std of a slippage series, in bps.

    Ported from the stat shape of v1 ``_calculate_benchmark_comparison`` (the
    per-benchmark mean/median/std block). v1 computed these inside a method
    that re-derived slippage from ``Execution`` objects; the port takes the
    already-computed slippage values so any benchmark (arrival / VWAP / TWAP /
    close) is handled by one function.
    """
    values = list(slippage_bps)
    if len(values) == 0:
        raise Unavailable("slippage series is empty: summary is undefined")
    arr = np.asarray(values, dtype=float)
    return {
        "mean_bps": float(np.mean(arr)),
        "median_bps": float(np.median(arr)),
        "std_bps": float(np.std(arr)),
        "n": int(arr.size),
    }


def identify_outliers(
    values: Sequence[float], *, z_threshold: float = _OUTLIER_Z_THRESHOLD
) -> list[dict]:
    """Indices whose value is more than ``z_threshold`` std from the mean.

    Ported from v1 ``_identify_outlier_executions`` (which flagged IS-bps
    outliers at |z| > 2). Raises on an empty series. A constant series has no
    spread and returns ``[]``; the check is ``np.ptp(arr) == 0`` (exact
    regardless of magnitude) rather than ``np.std == 0``, because ``np.std``
    of a constant non-integer series is a tiny nonzero float and dividing by
    it fabricates z-scores from floating-point residue. v1's ``len > 3`` floor
    is not retained: ``n == 1`` returns ``[]`` and ``n == 2`` distinct values
    computes z-scores.
    """
    values = list(values)
    if len(values) == 0:
        raise Unavailable("values is empty: outlier detection is undefined")
    arr = np.asarray(values, dtype=float)
    mean = float(np.mean(arr))
    std = float(np.std(arr))
    if np.ptp(arr) == 0.0:
        return []
    outliers: list[dict] = []
    for i, v in enumerate(arr):
        z = (float(v) - mean) / std
        if abs(z) > z_threshold:
            outliers.append({"index": i, "value": float(v), "z_score": float(z)})
    return outliers


# --------------------------------------------------------------------------- #
# Market-impact estimation -- faithful ports of v1 ``MarketImpactModel``
# --------------------------------------------------------------------------- #
#
# Each estimator returns (temporary_bps, permanent_bps) as v1 did, except the
# power-law model which v1 returned as a single scalar. The arithmetic is
# v1's bit-for-bit; the coefficients are the named constants above. None is
# calibrated and v1 never calibrated them either -- they are placeholders
# carried so a future calibration has a fixed point to replace.


def estimate_linear_impact(
    participation_rate: float, volatility: float, spread: float
) -> tuple[float, float]:
    """Linear market-impact model. Returns (temporary_bps, permanent_bps).

    Ported verbatim from v1 ``MarketImpactModel.estimate_linear_impact``:
    temporary = 0.1 * participation * 1.5 * vol * 10000 + spread * 5000,
    permanent = 0.05 * participation * vol * 10000.
    """
    temporary = (
        _LINEAR_TEMP_COEF
        * float(participation_rate)
        * _LINEAR_VOL_MULT
        * float(volatility)
        * _BPS
    )
    permanent = (
        _LINEAR_PERM_COEF * float(participation_rate) * float(volatility) * _BPS
    )
    temporary += float(spread) * _HALF_SPREAD_TO_BPS
    return float(temporary), float(permanent)


def estimate_square_root_impact(
    order_size: float,
    adv: float,
    volatility: float,
    spread: float,
) -> tuple[float, float]:
    """Square-root market-impact model (Almgren et al.). Returns (temp, perm) bps.

    Ported verbatim from v1 ``MarketImpactModel.estimate_square_root_impact``.
    Raises ``Unavailable`` when ``adv`` is zero -- v1 would divide by zero
    inside ``participation = order_size / adv``.
    """
    if adv == 0:
        raise Unavailable("adv is zero: square-root impact is undefined")
    participation = float(order_size) / float(adv)
    temporary = (
        _SQRT_TEMP_COEF
        * float(np.sqrt(participation))
        * float(volatility)
        * _SQRT_ANNUALISE
        * _BPS
    )
    permanent = (
        _SQRT_PERM_COEF * float(np.sqrt(participation)) * float(volatility) * _BPS
    )
    temporary += float(spread) * _HALF_SPREAD_TO_BPS
    return float(temporary), float(permanent)


def estimate_power_law_impact(
    order_size: float,
    market_cap: float,
    volatility: float,
    alpha: float = _POWER_LAW_ALPHA,
) -> float:
    """Power-law market-impact model. Returns impact in bps.

    Ported verbatim from v1 ``MarketImpactModel.estimate_power_law_impact``:
    impact = 1.0 * (order_size / market_cap) ** 0.6 * volatility, in bps.
    Raises ``Unavailable`` when ``market_cap`` is zero.
    """
    if market_cap == 0:
        raise Unavailable("market_cap is zero: power-law impact is undefined")
    ratio = float(order_size) / float(market_cap)
    impact = _POWER_LAW_COEF * float(np.power(ratio, alpha)) * float(volatility)
    return float(impact * _BPS)
