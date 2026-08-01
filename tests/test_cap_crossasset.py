"""Behaviour tests for the cross-asset and edge-metric capabilities.

Every assertion is on a hand-computed value for a known input, not on shape.
Every cross-asset entry point also has a test that its dependency being
unavailable raises `Unavailable` rather than substituting the empty-matrix /
"NEUTRAL" / "unknown" default v1 returned. The edge-metric functions were
already honest in v1 (NaN / "INSUFFICIENT DATA"), so those tests assert the
documented NaN / empty result rather than a fabricated number.
"""

import math

import numpy as np
import pandas as pd
import pytest

from omni.capabilities.crossasset import (
    ICResult,
    _corr,
    _finite,
    cross_asset_correlations,
    detect_divergences,
    evaluate_signal,
    hit_rate,
    infer_cycle_phase,
    information_coefficient,
    quantile_analysis,
    roro_indicator,
    sector_rotation,
    time_series_ic,
    _summarize_ic,
)
from omni.ingest.protocol import Unavailable

# ---------------------------------------------------------------------------
# Cross-asset engine
# ---------------------------------------------------------------------------

class TestInferCyclePhase:
    def test_early_cycle_leadership(self):
        assert infer_cycle_phase(
            {"Financials", "Consumer Discretionary", "Industrials"}
        ) == "early_cycle"

    def test_mid_cycle_leadership(self):
        assert infer_cycle_phase(
            {"Technology", "Communication", "Industrials"}
        ) == "mid_cycle"

    def test_late_cycle_leadership(self):
        assert infer_cycle_phase({"Energy", "Materials", "Healthcare"}) == "late_cycle"

    def test_recession_leadership(self):
        assert infer_cycle_phase(
            {"Utilities", "Consumer Staples", "Healthcare"}
        ) == "recession"

    def test_empty_set_is_unknown(self):
        assert infer_cycle_phase(set()) == "unknown"

    def test_tie_resolves_to_earlier_phase_in_priority_order(self):
        # One leader in each of mid / late / recession, none early -> mid wins
        # by priority (early_count=0 != max; mid_count==max -> mid_cycle).
        assert infer_cycle_phase({"Technology", "Energy", "Utilities"}) == "mid_cycle"


class TestDetectDivergences:
    def test_flags_large_deviation_from_norm(self):
        # SPY/VIX expected -0.7; actual +0.1 -> diff +0.8 (>0.5, "high").
        # GLD/TLT and SPY/HYG exactly at norm -> not flagged.
        corr = {
            "SPY": {"VIX": 0.1, "HYG": 0.6},
            "VIX": {"SPY": 0.1},
            "HYG": {"SPY": 0.6},
            "GLD": {"TLT": 0.3},
            "TLT": {"GLD": 0.3},
        }
        out = detect_divergences(corr)
        assert len(out) == 1
        d = out[0]
        assert d["pair"] == "SPY/VIX"
        assert d["expected_correlation"] == -0.7
        assert d["actual_correlation"] == 0.1
        assert d["divergence"] == pytest.approx(0.8)
        assert d["label"] == "stocks_volatility"
        assert d["significance"] == "high"

    def test_moderate_significance_band(self):
        # SPY/VIX expected -0.7, actual -0.35 -> diff +0.35 (>0.3, <=0.5).
        corr = {"SPY": {"VIX": -0.35}, "VIX": {"SPY": -0.35}}
        out = detect_divergences(corr)
        assert len(out) == 1
        assert out[0]["significance"] == "moderate"

    def test_empty_when_all_at_norm(self):
        corr = {
            "SPY": {"VIX": -0.7, "HYG": 0.6},
            "VIX": {"SPY": -0.7},
            "HYG": {"SPY": 0.6},
            "GLD": {"TLT": 0.3},
            "TLT": {"GLD": 0.3},
        }
        assert detect_divergences(corr) == []

    def test_empty_when_symbols_absent(self):
        # Unrelated universe -> no expected pair resolvable.
        assert detect_divergences({"BTC": {"ETH": 0.8}, "ETH": {"BTC": 0.8}}) == []


class TestCrossAssetCorrelations:
    async def test_raises_when_too_few_symbols(self):
        with pytest.raises(Unavailable, match=">=4 symbols"):
            await cross_asset_correlations(
                {"A": [0.01] * 20, "B": [0.01] * 20, "C": [0.01] * 20}
            )

    async def test_raises_when_series_too_short(self):
        with pytest.raises(Unavailable, match=">=20 returns"):
            await cross_asset_correlations(
                {"A": [0.01] * 10, "B": [0.01] * 10, "C": [0.01] * 10, "D": [0.01] * 10}
            )

    async def test_known_correlation_matrix(self):
        # s1 linear; s2=s1 (corr 1.0); s3=-s1 (corr -1.0); s4=2*s1 (corr 1.0).
        s1 = [float(i) for i in range(1, 21)]
        s2 = list(s1)
        s3 = [-x for x in s1]
        s4 = [2.0 * x for x in s1]
        out = await cross_asset_correlations({"A": s1, "B": s2, "C": s3, "D": s4})
        m = out["matrix"]
        assert m["A"]["A"] == 1.0
        assert m["A"]["B"] == 1.0
        assert m["A"]["C"] == -1.0
        assert m["A"]["D"] == 1.0
        assert m["B"]["C"] == -1.0
        assert m["C"]["D"] == -1.0
        assert out["data_points"] == 20
        assert out["symbols_available"] == ["A", "B", "C", "D"]
        assert out["divergences"] == []
        assert out["correlation_shifts"] == []

    async def test_correlation_shift_detected_against_long_window(self):
        # Short: all four identical -> every pair correlates 1.0.
        # Long: B is negated -> every pair involving B flips to -1.0, a +2.0
        # shift. Assert the A/B entry precisely; B's flip necessarily also moves
        # B/C and B/D, so only assert the count is >= 1.
        s1 = [float(i) for i in range(1, 21)]
        short = {"A": s1, "B": list(s1), "C": list(s1), "D": list(s1)}
        lA = [float(i) for i in range(1, 56)]
        long = {"A": lA, "B": [-x for x in lA], "C": list(lA), "D": list(lA)}
        out = await cross_asset_correlations(short, long_returns=long)
        shifts = out["correlation_shifts"]
        assert len(shifts) >= 1
        ab = next(s for s in shifts if s["pair"] == "A/B")
        assert ab["short_term_corr"] == 1.0
        assert ab["long_term_corr"] == -1.0
        assert ab["divergence"] == pytest.approx(2.0)
        assert ab["signal"] == "correlation_spike"

    async def test_long_returns_omitted_yields_no_shifts(self):
        s1 = [float(i) for i in range(1, 21)]
        out = await cross_asset_correlations(
            {"A": s1, "B": list(s1), "C": [-x for x in s1], "D": list(s1)}
        )
        assert out["correlation_shifts"] == []


class TestRoroIndicator:
    async def test_all_components_risk_on(self):
        # VIX -0.25 over 5d -> vix_score clamp(2.5)=1.0, weight .30 -> 0.30
        # HYG 0.05 vs TLT 0.0 -> credit clamp(1.0)=1.0, weight .25 -> 0.25
        # UUP -0.05 -> dollar clamp(0.75)=0.75, weight .20 -> 0.15
        # IWM 0.05 vs SPY 0.0 -> breadth clamp(0.75)=0.75, weight .25 -> 0.1875
        # composite = 0.8875 -> STRONG_RISK_ON
        returns = {
            "VIX": [0.0] * 5 + [-0.05] * 5,
            "HYG": [0.0] * 9 + [0.05],
            "TLT": [0.0] * 10,
            "UUP": [0.0] * 9 + [-0.05],
            "IWM": [0.0] * 9 + [0.05],
            "SPY": [0.0] * 10,
        }
        out = await roro_indicator(returns)
        assert out["components"] == {
            "vix_direction": 1.0,
            "credit_spread": 1.0,
            "dollar_strength": 0.75,
            "breadth": 0.75,
        }
        assert out["score"] == pytest.approx(0.8875, abs=0.001)
        assert out["classification"] == "STRONG_RISK_ON"

    async def test_all_components_risk_off(self):
        returns = {
            "VIX": [0.0] * 5 + [0.05] * 5,
            "HYG": [0.0] * 9 + [-0.05],
            "TLT": [0.0] * 10,
            "UUP": [0.0] * 9 + [0.05],
            "IWM": [0.0] * 9 + [-0.05],
            "SPY": [0.0] * 10,
        }
        out = await roro_indicator(returns)
        assert out["components"]["vix_direction"] == -1.0
        assert out["components"]["credit_spread"] == -1.0
        assert out["components"]["dollar_strength"] == -0.75
        assert out["components"]["breadth"] == -0.75
        assert out["score"] == pytest.approx(-0.8875, abs=0.001)
        assert out["classification"] == "STRONG_RISK_OFF"

    async def test_single_component_composites_correctly(self):
        # Only VIX present with enough history -> composite = 0.30 -> RISK_ON.
        out = await roro_indicator({"VIX": [0.0] * 5 + [-0.05] * 5})
        assert out["components"] == {"vix_direction": 1.0}
        assert out["score"] == pytest.approx(0.30, abs=0.001)
        assert out["classification"] == "RISK_ON"

    async def test_short_series_is_skipped_not_fabricated_zero(self):
        # HYG/TLT present but <10 bars: v1 fabricated 0 credit reading; the port
        # skips the component entirely (composite is unaffected since 0*weight=0,
        # but the components dict must not lie).
        out = await roro_indicator(
            {"VIX": [0.0] * 5 + [-0.05] * 5, "HYG": [0.01] * 5, "TLT": [0.01] * 5}
        )
        assert "credit_spread" not in out["components"]
        assert out["components"] == {"vix_direction": 1.0}

    async def test_raises_when_no_component_computable(self):
        with pytest.raises(Unavailable, match="no RORO components"):
            await roro_indicator({})

    async def test_raises_when_all_present_series_too_short(self):
        with pytest.raises(Unavailable, match="no RORO components"):
            await roro_indicator({"VIX": [0.01, 0.02]})  # len < 5


class TestSectorRotation:
    async def test_ranks_and_computes_momentum(self):
        # Technology: 100 -> 109.5 over 20 bars (linear +0.5/bar).
        # ret_20d = (109.5-100)/100 = 0.095 -> 9.5
        # ret_5d  = (109.5-107.5)/107.5 = 0.01860... -> 1.86
        # momentum = 0.095*0.6 + 0.01860*0.4 = 0.06444 -> 0.0644
        tech = [100.0 + 0.5 * i for i in range(20)]
        energy = [50.0] * 20  # flat -> 0 return
        util = [100.0 - 0.5 * i for i in range(20)]  # 100 -> 90.5
        out = await sector_rotation(
            {"Technology": tech, "Energy": energy, "Utilities": util}
        )
        assert out["leaders"][0]["sector"] == "Technology"
        assert out["laggards"][-1]["sector"] == "Utilities"

        t = out["sectors"]["Technology"]
        assert t["return_20d"] == 9.5
        assert t["return_5d"] == pytest.approx(1.86, abs=0.01)
        assert t["momentum_score"] == pytest.approx(0.0644, abs=0.0001)

        e = out["sectors"]["Energy"]
        assert e["return_20d"] == 0.0
        assert e["return_5d"] == 0.0
        assert e["momentum_score"] == 0.0

        u = out["sectors"]["Utilities"]
        assert u["return_20d"] == -9.5

    async def test_cycle_phase_from_leaders(self):
        # Three mid/late/recession leaders -> tie at 1 each -> mid_cycle wins
        # by priority. Build prices so Technology (mid) leads momentum.
        tech = [100.0 + 1.0 * i for i in range(20)]      # strong up
        energy = [100.0 + 0.1 * i for i in range(20)]     # mild up
        util = [100.0 - 0.1 * i for i in range(20)]       # mild down
        out = await sector_rotation(
            {"Technology": tech, "Energy": energy, "Utilities": util}
        )
        # All three ranked into leaders[:3] / laggards[-3:]; cycle from the set.
        assert out["cycle_phase"] == "mid_cycle"

    async def test_raises_when_no_sector_has_enough_data(self):
        with pytest.raises(Unavailable, match="no sectors"):
            await sector_rotation({"Technology": [1.0, 2.0, 3.0]})

    async def test_skips_sector_with_non_positive_prices_only(self):
        # A sector whose prices are all <= 0 yields no positives -> skipped.
        # The other valid sector still produces a result.
        good = [100.0 + 0.5 * i for i in range(20)]
        out = await sector_rotation({"Good": good, "Bad": [-1.0] * 20})
        assert "Good" in out["sectors"]
        assert "Bad" not in out["sectors"]


# ---------------------------------------------------------------------------
# Edge metrics
# ---------------------------------------------------------------------------

class TestFinite:
    def test_passes_through_finite(self):
        assert _finite(1.5) == 1.5
        assert _finite(0) == 0.0

    def test_non_finite_becomes_none(self):
        assert _finite(float("nan")) is None
        assert _finite(float("inf")) is None
        assert _finite(float("-inf")) is None

    def test_non_numeric_becomes_none(self):
        assert _finite("not a number") is None
        assert _finite(None) is None


class TestCorr:
    def test_pearson_perfect_positive_and_negative(self):
        a = np.array([1.0, 2, 3, 4, 5])
        b = np.array([2.0, 4, 6, 8, 10])
        assert _corr(a, b, "pearson") == pytest.approx(1.0)
        assert _corr(a, -b, "pearson") == pytest.approx(-1.0)

    def test_spearman_monotone_is_one(self):
        a = np.array([1.0, 2, 3, 4, 5])
        b = np.array([10.0, 20, 30, 40, 50])
        assert _corr(a, b, "spearman") == pytest.approx(1.0)

    def test_constant_series_is_nan(self):
        assert math.isnan(_corr(np.array([1.0, 1, 1]), np.array([1.0, 2, 3]), "pearson"))

    def test_too_few_points_is_nan(self):
        assert math.isnan(_corr(np.array([1.0, 2]), np.array([1.0, 2]), "pearson"))

    def test_nan_inputs_are_masked(self):
        a = np.array([1.0, 2.0, np.nan, 4.0, 5.0])
        b = np.array([2.0, 4.0, 6.0, 8.0, 10.0])
        assert _corr(a, b, "pearson") == pytest.approx(1.0)

    def test_unknown_method_raises(self):
        with pytest.raises(ValueError, match="Unknown correlation method"):
            _corr(np.array([1.0, 2, 3, 4]), np.array([1.0, 2, 3, 4]), "kendall")


class TestInformationCoefficient:
    def test_perfect_cross_sectional_signal(self):
        df = pd.DataFrame({
            "date": [1] * 6 + [2] * 6,
            "asset": ["a", "b", "c", "d", "e", "f"] * 2,
            "signal": [1, 2, 3, 4, 5, 6] * 2,
            "forward_return": [0.1, 0.2, 0.3, 0.4, 0.5, 0.6] * 2,
        })
        ic = information_coefficient(df, "signal", "forward_return")
        assert ic.mean_ic == pytest.approx(1.0)
        assert ic.n_periods == 2
        assert ic.positive_ic_rate == 1.0
        # Too few periods to call it significant even though IC is perfect.
        assert ic.is_significant is False

    def test_too_few_cross_sections_yields_empty(self):
        df = pd.DataFrame({
            "date": [1, 1, 1, 1],
            "asset": ["a", "b", "c", "d"],
            "signal": [1, 2, 3, 4],
            "forward_return": [0.1, 0.2, 0.3, 0.4],
        })
        ic = information_coefficient(df, "signal", "forward_return")
        assert ic.n_periods == 0
        assert math.isnan(ic.mean_ic)

    def test_missing_column_raises(self):
        df = pd.DataFrame({
            "date": [1], "asset": ["a"], "signal": [1], "forward_return": [0.1],
        })
        with pytest.raises(ValueError, match="panel missing columns"):
            information_coefficient(df, "signal", "nope")


class TestTimeSeriesIC:
    def test_perfect_monotone(self):
        signal = pd.Series([float(i) for i in range(1, 11)])
        fwd = pd.Series([0.1 * i for i in range(1, 11)])
        ic = time_series_ic(signal, fwd, method="pearson")
        assert ic.mean_ic == pytest.approx(1.0)
        assert ic.n_periods == 10
        assert ic.positive_ic_rate == 1.0

    def test_too_short_returns_empty(self):
        ic = time_series_ic(pd.Series([1.0, 2.0]), pd.Series([1.0, 2.0]))
        assert ic.n_periods == 0
        assert math.isnan(ic.mean_ic)

    def test_overlap_shrinks_effective_sample_size(self):
        # Perfect correlation either way; with overlap the t-stat is computed
        # against n_eff = n/overlap. Assert it is finite and reduced vs the
        # non-overlapping t-stat.
        signal = pd.Series([float(i) for i in range(1, 21)])
        fwd = pd.Series([0.1 * i for i in range(1, 21)])
        plain = time_series_ic(signal, fwd, method="pearson", overlap=1)
        overlapped = time_series_ic(signal, fwd, method="pearson", overlap=4)
        assert math.isfinite(plain.t_stat)
        assert math.isfinite(overlapped.t_stat)
        assert abs(overlapped.t_stat) < abs(plain.t_stat)


class TestQuantileAnalysis:
    def test_monotone_signal_orders_quantiles(self):
        # 2 dates x 10 assets. signal 1..10, forward_return rises with signal.
        # 5 quantiles of 2 each -> means 0.015, 0.035, 0.055, 0.075, 0.095.
        df = pd.DataFrame({
            "date": [1] * 10 + [2] * 10,
            "asset": [f"a{i}" for i in range(10)] * 2,
            "signal": [1, 2, 3, 4, 5, 6, 7, 8, 9, 10] * 2,
            "forward_return": [0.01, 0.02, 0.03, 0.04, 0.05, 0.06, 0.07, 0.08, 0.09, 0.10] * 2,
        })
        q = quantile_analysis(df, "signal", "forward_return", n_quantiles=5)
        assert q.quantile_returns[1] == pytest.approx(0.015)
        assert q.quantile_returns[5] == pytest.approx(0.095)
        assert q.top_minus_bottom == pytest.approx(0.08)
        assert q.monotonicity == pytest.approx(1.0)
        assert q.n_periods == 2

    def test_missing_column_raises(self):
        df = pd.DataFrame({
            "date": [1], "asset": ["a"], "signal": [1], "forward_return": [0.1],
        })
        with pytest.raises(ValueError, match="panel missing columns"):
            quantile_analysis(df, "signal", "nope")


class TestHitRate:
    def test_perfect_aligned(self):
        assert hit_rate([1, 2, -3, -4], [0.5, 0.1, -0.2, -0.1]) == 1.0

    def test_perfect_inverted(self):
        assert hit_rate([1, 1, -1, -1], [-1, -1, 1, 1]) == 0.0

    def test_half_aligned(self):
        assert hit_rate([1, 1, -1, -1], [1, -1, 1, -1]) == 0.5

    def test_zero_signals_excluded(self):
        # signal 0 is masked out; remaining two both match -> 1.0.
        assert hit_rate([0, 1, -1], [1, 1, -1]) == 1.0

    def test_no_valid_pairs_is_nan(self):
        assert math.isnan(hit_rate([0, 0], [1, 2]))
        assert math.isnan(hit_rate([], []))


class TestICResultDict:
    def test_to_dict_nulls_non_finite_and_reports_significance(self):
        r = ICResult(
            mean_ic=float("nan"),
            ic_std=1.0,
            ic_ir=float("inf"),
            t_stat=1.0,
            p_value=0.5,
            n_periods=5,
            positive_ic_rate=0.6,
            method="spearman",
        )
        d = r.to_dict()
        assert d["mean_ic"] is None
        assert d["ic_ir"] is None
        assert d["ic_std"] == 1.0
        assert d["is_significant"] is False  # n_periods 5 < 12


class TestEvaluateSignal:
    def test_verdict_insufficient_when_too_few_periods(self):
        df = pd.DataFrame({
            "date": [1] * 6,
            "asset": ["a", "b", "c", "d", "e", "f"],
            "signal": [1, 2, 3, 4, 5, 6],
            "forward_return": [0.1, 0.2, 0.3, 0.4, 0.5, 0.6],
        })
        ev = evaluate_signal(df, "signal", "forward_return", name="alpha_1")
        assert ev.name == "alpha_1"
        assert ev.n_observations == 6
        assert ev.verdict.startswith("INSUFFICIENT DATA")
        # All-positive signal and return -> every sign matches.
        assert ev.hit_rate == 1.0

    def test_verdict_strong_edge_with_enough_periods(self):
        # 15 dates x 6 assets, signal == forward_return rank -> IC 1.0 each date.
        rows = []
        for d in range(15):
            for a in range(6):
                rows.append({
                    "date": d,
                    "asset": f"a{a}",
                    "signal": a + 1,
                    "forward_return": (a + 1) * 0.1,
                })
        df = pd.DataFrame(rows)
        ev = evaluate_signal(df, "signal", "forward_return")
        assert ev.ic.mean_ic == pytest.approx(1.0)
        assert ev.ic.is_significant is True
        assert ev.verdict.startswith("STRONG EDGE")

    def test_verdict_no_edge_when_ic_is_zero(self):
        # 15 dates x 6 assets; signal uncorrelated with a constant forward return
        # is degenerate. Instead use a deterministic anti-symmetric panel where
        # half the dates rank positively and half negatively so mean IC ~ 0 but
        # each within-date IC is +/-1 (well-defined, just inconsistent).
        rows = []
        for d in range(15):
            for a in range(6):
                sign = 1.0 if d % 2 == 0 else -1.0
                rows.append({
                    "date": d,
                    "asset": f"a{a}",
                    "signal": a + 1,
                    "forward_return": sign * (a + 1) * 0.1,
                })
        df = pd.DataFrame(rows)
        ev = evaluate_signal(df, "signal", "forward_return")
        assert ev.ic.n_periods == 15
        # ICs alternate +1 / -1 -> mean ~0, not significant.
        assert ev.ic.is_significant is False
        assert ev.verdict.startswith("NO MEASURABLE EDGE")


class TestFloatNoiseGuards:
    """The zero-dispersion guards must fire on a constant series whose std is
    float noise (~1e-17), not exactly 0.0. A guard written ``== 0.0`` or
    ``> 0`` misses it and divides the mean by ~1e-17, fabricating a giant ratio
    -- the same defect class that shipped a Sortino of -1.6e15 and a +5% gain
    reported as Value at Risk (AGENTS.md, "Never compare a float to zero")."""

    def test_constant_ic_series_is_degenerate_not_fabricated_ir(self):
        # [0.05]*20 is a perfectly constant IC series; std(ddof=1) is ~7e-18
        # (0.05 is not representable in binary64), so a guard written
        # ``ic_std == 0.0`` misses it and ic_ir = mean_ic / 7e-18 ~= 7e15.
        res = _summarize_ic(pd.Series([0.05] * 20), "spearman")
        # Designed constant-nonzero behaviour (perfect consistency) is an
        # infinite IR, not a meaningless finite giant. The ICResult ctor
        # normalises inf t_stat -> nan (it does not normalise ic_ir).
        assert res.ic_ir == np.inf      # was 7.02e15
        assert res.p_value == 0.0       # was 9.14e-303
        assert math.isnan(res.t_stat)   # was 3.14e16

    def test_constant_ic_treated_identically_to_exact_zero_std_constant(self):
        # 0.3 over 6 periods happens to give std(ddof=1) == 0.0 exactly; 0.05
        # over 20 gives ~7e-18 float noise. Both are degenerate-constant and
        # must be treated identically -- disagreeing on what counts as constant
        # is itself the defect (AGENTS.md: one tolerance, one idiom per module).
        exact = _summarize_ic(pd.Series([0.3] * 6), "spearman")
        noisy = _summarize_ic(pd.Series([0.05] * 20), "spearman")
        # Pre-fix: exact.ic_ir = inf (guard fired on exact 0.0 std) but
        # noisy.ic_ir = 7e15 (guard missed float-noise std). They must agree.
        assert exact.ic_ir == noisy.ic_ir == np.inf
        assert exact.p_value == noisy.p_value

    def test_constant_long_short_return_is_not_fabricated_sharpe(self):
        # 6 dates, each with an identical 0.05 top-minus-bottom spread ->
        # long_short_returns = [0.05]*6, whose std(ddof=1) is ~7.6e-18. A guard
        # ``if sd > 0`` passes and computes mu/7.6e-18*sqrt(252) ~= 1e17.
        rows = []
        for d in range(6):
            for a in range(10):
                signal = a + 1
                fwd = 0.05 if signal >= 9 else (0.0 if signal <= 2 else 0.02)
                rows.append({
                    "date": d,
                    "asset": f"a{a}",
                    "signal": signal,
                    "forward_return": fwd,
                })
        q = quantile_analysis(
            pd.DataFrame(rows), "signal", "forward_return", n_quantiles=5
        )
        assert math.isnan(q.long_short_sharpe)
        # The flat return still annualises honestly.
        assert q.long_short_return_ann == pytest.approx(0.05 * 252)

    def test_varying_long_short_return_still_computes_real_sharpe(self):
        # Control: a genuinely varying long-short return must still yield a
        # finite Sharpe -- the fix must not over-refuse real dispersion.
        rows = []
        for d in range(6):
            spread = 0.05 + 0.01 * d  # 0.05..0.10 -> genuinely varies
            for a in range(10):
                signal = a + 1
                fwd = spread if signal >= 9 else (0.0 if signal <= 2 else 0.02)
                rows.append({
                    "date": d,
                    "asset": f"a{a}",
                    "signal": signal,
                    "forward_return": fwd,
                })
        q = quantile_analysis(
            pd.DataFrame(rows), "signal", "forward_return", n_quantiles=5
        )
        assert math.isfinite(q.long_short_sharpe)
        assert q.long_short_sharpe > 0
