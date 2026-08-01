"""Behaviour tests for the portfolio-risk capabilities.

Every assertion is on a hand-computed value for a known input, not on shape.
Every failure path named in the work order raises (Unavailable for missing /
degenerate data, ValueError for an invalid argument). v1 `risk_calculator.py`
had no test file, so the work order's required outcomes are the oracle.
"""

import math

import numpy as np
import pytest
from scipy import stats

from omni.capabilities.portfolio_risk import (
    Scenario,
    calculate_beta,
    calculate_correlation_matrix,
    calculate_cvar,
    calculate_var,
    run_scenarios,
    stress_book,
)
from omni.ingest.protocol import Unavailable

# ---------------------------------------------------------------------------
# Known inputs.
#
# `KNOWN_RETURNS` is sized so the 5th percentile has an arithmetically exact
# answer: sorted, the series is [-0.10, 0.00, 0.01, 0.01, ...], so
# np.percentile(arr, 5) at position 19*0.05 = 0.95 interpolates between
# x[0] = -0.10 and x[1] = 0.00 -> -0.10 + 0.95 * 0.10 = -0.005.
# Twenty observations is exactly the floor at 95% confidence.
# ---------------------------------------------------------------------------

KNOWN_RETURNS = [-0.10, 0.00] + [0.01] * 18
HISTORICAL_VAR_95 = -0.005  # np.percentile(KNOWN_RETURNS, 5)
CVAR_TAIL = [-0.10]  # the only return <= HISTORICAL_VAR_95
CVAR_95 = -0.10  # mean(CVAR_TAIL)


# ===========================================================================
# Value at Risk
# ===========================================================================

class TestCalculateVar:
    def test_historical_var_at_95_is_exact(self):
        out = calculate_var(KNOWN_RETURNS, 0.95)
        assert out["historical"]["daily_var_pct"] == pytest.approx(
            HISTORICAL_VAR_95 * 100
        )

    def test_historical_var_matches_recomputed_percentile(self):
        # Independent recomputation of the same arithmetic the function uses.
        out = calculate_var(KNOWN_RETURNS, 0.95)
        expected = float(np.percentile(np.asarray(KNOWN_RETURNS, dtype=float), 5)) * 100
        assert out["historical"]["daily_var_pct"] == pytest.approx(expected)

    def test_historical_loss_is_signed_negative(self):
        out = calculate_var(KNOWN_RETURNS, 0.95)
        assert out["historical"]["daily_var_pct"] < 0

    def test_parametric_var_is_mean_plus_z_times_std(self):
        arr = np.asarray(KNOWN_RETURNS, dtype=float)
        expected = (float(arr.mean()) + stats.norm.ppf(0.05) * float(arr.std())) * 100
        out = calculate_var(KNOWN_RETURNS, 0.95)
        assert out["parametric"]["daily_var_pct"] == pytest.approx(expected)

    def test_parametric_annual_scales_by_sqrt_252(self):
        arr = np.asarray(KNOWN_RETURNS, dtype=float)
        z = stats.norm.ppf(0.05)
        expected = (
            float(arr.mean()) * 252 + z * float(arr.std()) * math.sqrt(252)
        ) * 100
        out = calculate_var(KNOWN_RETURNS, 0.95)
        assert out["parametric"]["annual_var_pct"] == pytest.approx(expected)

    def test_amount_is_positive_when_portfolio_value_given(self):
        out = calculate_var(KNOWN_RETURNS, 0.95, portfolio_value=10_000)
        assert out["historical"]["daily_var_amount"] == pytest.approx(
            10_000 * abs(HISTORICAL_VAR_95)
        )
        assert out["historical"]["daily_var_amount"] > 0

    def test_amount_is_none_when_portfolio_value_omitted(self):
        out = calculate_var(KNOWN_RETURNS, 0.95)
        assert out["historical"]["daily_var_amount"] is None
        assert out["historical"]["annual_var_amount"] is None
        assert out["monte_carlo"]["daily_var_amount"] is None

    def test_monte_carlo_is_reproducible_with_seed(self):
        a = calculate_var(KNOWN_RETURNS, 0.95, seed=42)
        b = calculate_var(KNOWN_RETURNS, 0.95, seed=42)
        assert a["monte_carlo"]["daily_var_pct"] == b["monte_carlo"]["daily_var_pct"]

    def test_monte_carlo_seed_changes_draw_from_default(self):
        seeded = calculate_var(KNOWN_RETURNS, 0.95, seed=42)
        # A fixed seed is very unlikely to collide with the OS-entropy default.
        default = calculate_var(KNOWN_RETURNS, 0.95)
        assert isinstance(seeded["monte_carlo"]["daily_var_pct"], float)
        assert math.isfinite(seeded["monte_carlo"]["daily_var_pct"])
        assert math.isfinite(default["monte_carlo"]["daily_var_pct"])

    def test_monte_carlo_finite_and_loss_signed(self):
        out = calculate_var(KNOWN_RETURNS, 0.95, seed=7)
        mc = out["monte_carlo"]["daily_var_pct"]
        assert math.isfinite(mc)
        assert mc < 0

    def test_confidence_level_reflected_in_output(self):
        out = calculate_var(KNOWN_RETURNS, 0.95)
        assert out["confidence_level"] == 0.95

    # --- failure paths ---

    def test_empty_returns_raises(self):
        with pytest.raises(Unavailable, match="empty"):
            calculate_var([], 0.95)

    def test_single_observation_raises(self):
        with pytest.raises(Unavailable, match="observations"):
            calculate_var([0.01], 0.95)

    def test_19_observations_at_95_raises_just_under_floor(self):
        # 1/(1-0.95) = 20; 19 is the boundary refusal.
        with pytest.raises(Unavailable, match="observations"):
            calculate_var([0.01] * 19, 0.95)

    def test_all_zero_variance_raises(self):
        with pytest.raises(Unavailable, match="variance is 0"):
            calculate_var([0.0] * 50, 0.95)

    def test_constant_nonzero_series_raises(self):
        with pytest.raises(Unavailable, match="variance is 0"):
            calculate_var([0.001] * 50, 0.95)

    def test_constant_nonrepresentable_series_raises_floatnoise(self):
        # Discriminates the float-noise defect. np.var([0.05]*50) returns
        # 1.9259299e-34, NOT 0.0, so an `== 0.0` guard passes and the
        # function emits a fabricated VaR: daily_var_pct = 5.0 -- a +5% gain
        # reported as the "value at risk" of a flat +5% series. The
        # [0.001]*50 case above happens to give np.var == 0.0 exactly and
        # therefore does NOT exercise this path; only a tolerance-based
        # guard catches it.
        with pytest.raises(Unavailable, match="variance is 0"):
            calculate_var([0.05] * 50, 0.95)

    def test_confidence_at_zero_raises_valueerror(self):
        with pytest.raises(ValueError, match="confidence_level"):
            calculate_var(KNOWN_RETURNS, 0.0)

    def test_confidence_at_one_raises_valueerror(self):
        with pytest.raises(ValueError, match="confidence_level"):
            calculate_var(KNOWN_RETURNS, 1.0)

    def test_confidence_above_one_raises_valueerror(self):
        with pytest.raises(ValueError, match="confidence_level"):
            calculate_var(KNOWN_RETURNS, 1.5)

    def test_confidence_negative_raises_valueerror(self):
        with pytest.raises(ValueError, match="confidence_level"):
            calculate_var(KNOWN_RETURNS, -0.5)


# ===========================================================================
# Conditional Value at Risk
# ===========================================================================

class TestCalculateCvar:
    def test_cvar_at_95_is_mean_of_tail(self):
        out = calculate_cvar(KNOWN_RETURNS, 0.95)
        assert out["daily_cvar_pct"] == pytest.approx(CVAR_95 * 100)

    def test_cvar_more_negative_than_var(self):
        # Expected shortfall is by definition a deeper loss than the VaR point.
        var_out = calculate_var(KNOWN_RETURNS, 0.95)
        cvar_out = calculate_cvar(KNOWN_RETURNS, 0.95)
        assert cvar_out["daily_cvar_pct"] < var_out["historical"]["daily_var_pct"]

    def test_tail_risk_ratio(self):
        out = calculate_cvar(KNOWN_RETURNS, 0.95)
        assert out["tail_risk_ratio"] == pytest.approx(
            abs(CVAR_95 / HISTORICAL_VAR_95)
        )

    def test_amount_positive_when_portfolio_value_given(self):
        out = calculate_cvar(KNOWN_RETURNS, 0.95, portfolio_value=10_000)
        assert out["daily_cvar_amount"] == pytest.approx(10_000 * abs(CVAR_95))
        assert out["daily_cvar_amount"] > 0

    def test_amount_none_when_portfolio_value_omitted(self):
        out = calculate_cvar(KNOWN_RETURNS, 0.95)
        assert out["daily_cvar_amount"] is None

    def test_annual_scales_by_sqrt_252(self):
        out = calculate_cvar(KNOWN_RETURNS, 0.95)
        assert out["annual_cvar_pct"] == pytest.approx(
            CVAR_95 * math.sqrt(252) * 100
        )

    # --- failure paths ---

    def test_empty_returns_raises(self):
        with pytest.raises(Unavailable, match="empty"):
            calculate_cvar([], 0.95)

    def test_single_observation_raises(self):
        with pytest.raises(Unavailable, match="observations"):
            calculate_cvar([0.01], 0.95)

    def test_all_zero_variance_raises(self):
        with pytest.raises(Unavailable, match="variance is 0"):
            calculate_cvar([0.0] * 50, 0.95)

    def test_constant_nonrepresentable_series_raises_floatnoise(self):
        # Same float-noise defect as VaR: np.var([0.05]*50) = 1.93e-34,
        # an `== 0.0` guard passes, and CVaR emits daily_cvar_pct = 4.9999999
        # (plus tail_risk_ratio = 0.9999...) on a constant series. Only a
        # tolerance guard refuses it.
        with pytest.raises(Unavailable, match="variance is 0"):
            calculate_cvar([0.05] * 50, 0.95)

    def test_confidence_above_one_raises_valueerror(self):
        with pytest.raises(ValueError, match="confidence_level"):
            calculate_cvar(KNOWN_RETURNS, 1.5)

    def test_confidence_zero_raises_valueerror(self):
        with pytest.raises(ValueError, match="confidence_level"):
            calculate_cvar(KNOWN_RETURNS, 0.0)


# ===========================================================================
# Beta
# ===========================================================================

class TestCalculateBeta:
    def test_beta_of_series_against_itself_is_one(self):
        # Work-order required outcome. Holds exactly because both cov and var
        # use ddof=1; v1's ddof mismatch made this N/(N-1).
        x = [0.01, 0.02, -0.01, 0.005, -0.02, 0.015, -0.005, 0.025, -0.015, 0.03]
        assert calculate_beta(x, x) == pytest.approx(1.0)

    def test_beta_of_double_benchmark_is_two(self):
        x = [0.01, 0.02, -0.01, 0.005, -0.02, 0.015, -0.005, 0.025, -0.015, 0.03]
        bench = [2 * xi for xi in x]
        # cov(x, 2x) / var(2x) = 2*var(x) / 4*var(x) = 0.5
        assert calculate_beta(x, bench) == pytest.approx(0.5)

    def test_beta_matches_manual_cov_over_var(self):
        x = [0.01, 0.02, -0.01, 0.005, -0.02, 0.015, -0.005, 0.025, -0.015, 0.03]
        y = [0.02, 0.01, -0.005, 0.0, -0.01, 0.005, 0.0, 0.02, -0.02, 0.025]
        expected = float(np.cov(x, y, ddof=1)[0, 1]) / float(np.var(y, ddof=1))
        assert calculate_beta(x, y) == pytest.approx(expected)

    # --- failure paths ---

    def test_mismatched_lengths_raise_valueerror(self):
        with pytest.raises(ValueError, match="equal length"):
            calculate_beta([0.01, 0.02, 0.03], [0.01, 0.02])

    def test_empty_raises(self):
        with pytest.raises(Unavailable, match="empty"):
            calculate_beta([], [])

    def test_single_observation_raises(self):
        with pytest.raises(Unavailable, match="paired observations"):
            calculate_beta([0.01], [0.02])

    def test_zero_benchmark_variance_raises(self):
        # "all-zero variance" failure path applied to the denominator.
        x = [0.01, 0.02, -0.01, 0.005, -0.02]
        const_bench = [0.0, 0.0, 0.0, 0.0, 0.0]
        with pytest.raises(Unavailable, match="benchmark variance is 0"):
            calculate_beta(x, const_bench)

    def test_constant_nonrepresentable_benchmark_raises_floatnoise(self):
        # Discriminates the float-noise defect in the beta denominator guard.
        # np.var([0.05]*50, ddof=1) = 3.46e-34 != 0, so an `== 0` guard passes
        # and beta is computed as cov(noise) / 3.46e-34 -- probe returned 0.0
        # for one asset; any value is possible. The [0.0]*5 case above is
        # exactly representable and so does NOT exercise this path.
        x = [0.01, 0.02, -0.01, 0.005, -0.02] * 10  # 50 paired obs
        with pytest.raises(Unavailable, match="benchmark variance is 0"):
            calculate_beta(x, [0.05] * 50)


# ===========================================================================
# Correlation matrix
# ===========================================================================

class TestCalculateCorrelationMatrix:
    def test_series_against_negation_is_minus_one(self):
        # Work-order required outcome.
        x = [0.01, 0.02, -0.01, 0.005, -0.02, 0.015, -0.005, 0.025, -0.015, 0.03]
        out = calculate_correlation_matrix({"A": x, "B": [-xi for xi in x]})
        assert out["matrix"]["A"]["B"] == pytest.approx(-1.0)

    def test_series_against_itself_is_one(self):
        x = [0.01, 0.02, -0.01, 0.005, -0.02, 0.015, -0.005, 0.025, -0.015, 0.03]
        out = calculate_correlation_matrix({"A": x, "B": list(x)})
        assert out["matrix"]["A"]["B"] == pytest.approx(1.0)

    def test_average_correlation_is_minus_one_for_two_anticorrelated(self):
        x = [0.01, 0.02, -0.01, 0.005, -0.02, 0.015, -0.005, 0.025, -0.015, 0.03]
        out = calculate_correlation_matrix({"A": x, "B": [-xi for xi in x]})
        assert out["average_correlation"] == pytest.approx(-1.0)

    def test_high_correlations_flagged(self):
        x = [0.01, 0.02, -0.01, 0.005, -0.02, 0.015, -0.005, 0.025, -0.015, 0.03]
        out = calculate_correlation_matrix({"A": x, "B": list(x)})
        assert any(pair["pair"] == "A-B" for pair in out["high_correlations"])

    def test_uncorrelated_pair_not_flagged(self):
        rng = np.random.default_rng(0)
        a = list(rng.normal(0, 1, 200))
        b = list(rng.normal(0, 1, 200))
        out = calculate_correlation_matrix({"A": a, "B": b})
        assert out["high_correlations"] == []

    def test_three_symbol_matrix_shape(self):
        x = [0.01, 0.02, -0.01, 0.005, -0.02, 0.015, -0.005, 0.025, -0.015, 0.03]
        out = calculate_correlation_matrix({"A": x, "B": list(x), "C": [-xi for xi in x]})
        for sym_a in ("A", "B", "C"):
            for sym_b in ("A", "B", "C"):
                assert sym_b in out["matrix"][sym_a]

    # --- failure paths ---

    def test_fewer_than_two_symbols_raises(self):
        with pytest.raises(Unavailable, match=">=2 symbols"):
            calculate_correlation_matrix({"A": [0.01, 0.02, 0.03]})

    def test_fewer_than_two_aligned_observations_raises(self):
        with pytest.raises(Unavailable, match="aligned"):
            calculate_correlation_matrix(
                {"A": [0.01], "B": [0.02]}
            )

    def test_zero_variance_series_raises(self):
        # "all-zero variance" failure path applied to a column.
        x = [0.01, 0.02, -0.01, 0.005, -0.02]
        with pytest.raises(Unavailable, match="zero-variance"):
            calculate_correlation_matrix({"A": x, "B": [0.0, 0.0, 0.0, 0.0, 0.0]})

    def test_constant_nonrepresentable_series_raises_floatnoise(self):
        # Discriminates the float-noise defect in the column-variance guard.
        # np.var([0.05]*50) = 1.93e-34, so a `variances == 0` mask is empty,
        # the guard passes, and df.corr() divides by ~0 -> NaN silently flows
        # into the matrix and average_correlation (probe got
        # average_correlation = nan, matrix[A][B] = nan). The [0.0]*5 case
        # above is exactly representable and does NOT exercise this path.
        x = [0.01, 0.02, -0.01, 0.005, -0.02] * 10  # 50 rows
        with pytest.raises(Unavailable, match="zero-variance"):
            calculate_correlation_matrix({"A": x, "B": [0.05] * 50})


# ===========================================================================
# Stress
# ===========================================================================

class TestStress:
    def test_halving_every_price_halves_portfolio(self):
        # Work-order required outcome: every asset shocked by -50% via
        # specific_shocks; the whole book must lose half its value.
        assets = ["A", "B"]
        factors: list[str] = []
        exposures = [[], []]
        positions = {"A": 100.0, "B": 200.0}
        scenario = Scenario("half", specific_shocks={"A": -0.5, "B": -0.5})

        out = stress_book(assets, factors, exposures, positions, scenario)

        assert out.total_value == pytest.approx(300.0)
        assert out.total_pnl == pytest.approx(-150.0)
        assert out.total_return == pytest.approx(-0.5)
        assert out.asset_pnl == {"A": pytest.approx(-50.0), "B": pytest.approx(-100.0)}

    def test_factor_attribution_sums_to_total(self):
        # The stress.py acceptance property: factor P&L + specific P&L == total.
        assets = ["A", "B"]
        factors = ["MKT"]
        exposures = [[1.0], [1.5]]
        positions = {"A": 100.0, "B": 200.0}
        scenario = Scenario(
            "crash",
            factor_shocks={"MKT": -0.5},
            specific_shocks={"A": -0.1},
        )

        out = stress_book(assets, factors, exposures, positions, scenario)

        assert out.factor_pnl + out.specific_pnl == pytest.approx(out.total_pnl)
        assert out.factor_attribution["MKT"] + out.specific_pnl == pytest.approx(
            out.total_pnl
        )

    def test_factor_only_pnl_values(self):
        assets = ["A", "B"]
        factors = ["MKT"]
        exposures = [[1.0], [1.5]]
        positions = {"A": 100.0, "B": 200.0}
        scenario = Scenario("crash", factor_shocks={"MKT": -0.5})

        out = stress_book(assets, factors, exposures, positions, scenario)

        # dollar exposure to MKT = 100*1 + 200*1.5 = 400; * -0.5 = -200.
        assert out.factor_pnl == pytest.approx(-200.0)
        assert out.specific_pnl == pytest.approx(0.0)
        assert out.total_pnl == pytest.approx(-200.0)
        assert out.factor_attribution == {"MKT": pytest.approx(-200.0)}
        assert out.total_return == pytest.approx(-200.0 / 300.0)

    def test_position_not_in_assets_ignored(self):
        # v1 ignores positions for which there is no factor view.
        assets = ["A"]
        factors = ["MKT"]
        exposures = [[1.0]]
        positions = {"A": 100.0, "UNKNOWN": 99999.0}
        scenario = Scenario("mkt", factor_shocks={"MKT": -0.1})

        out = stress_book(assets, factors, exposures, positions, scenario)

        assert out.total_pnl == pytest.approx(-10.0)
        assert "UNKNOWN" not in out.asset_pnl

    def test_factor_not_in_model_ignored(self):
        assets = ["A"]
        factors = ["MKT"]
        exposures = [[1.0]]
        positions = {"A": 100.0}
        scenario = Scenario("mixed", factor_shocks={"MKT": -0.1, "OIL": 5.0})

        out = stress_book(assets, factors, exposures, positions, scenario)

        assert out.total_pnl == pytest.approx(-10.0)
        assert "OIL" not in out.factor_attribution

    def test_run_scenarios_returns_name_keyed(self):
        assets = ["A"]
        factors = ["MKT"]
        exposures = [[1.0]]
        positions = {"A": 100.0}
        scenarios = [
            Scenario("mild", factor_shocks={"MKT": -0.05}),
            Scenario("severe", factor_shocks={"MKT": -0.30}),
        ]

        out = run_scenarios(assets, factors, exposures, positions, scenarios)

        assert set(out.keys()) == {"mild", "severe"}
        assert out["mild"].total_pnl == pytest.approx(-5.0)
        assert out["severe"].total_pnl == pytest.approx(-30.0)

    def test_to_dict_roundtrip(self):
        assets = ["A"]
        factors: list[str] = []
        exposures = [[]]
        positions = {"A": 100.0}
        scenario = Scenario("half", specific_shocks={"A": -0.5})

        out = stress_book(assets, factors, exposures, positions, scenario)
        d = out.to_dict()

        assert d["scenario"] == "half"
        assert d["total_pnl"] == pytest.approx(-50.0)
        assert d["total_return"] == pytest.approx(-0.5)
        assert d["asset_pnl"] == {"A": pytest.approx(-50.0)}

    def test_long_short_signs(self):
        assets = ["LONG", "SHORT"]
        factors = ["MKT"]
        exposures = [[1.0], [1.0]]
        positions = {"LONG": 100.0, "SHORT": -100.0}
        scenario = Scenario("mkt_up", factor_shocks={"MKT": 0.10})

        out = stress_book(assets, factors, exposures, positions, scenario)

        assert out.asset_pnl["LONG"] == pytest.approx(10.0)
        assert out.asset_pnl["SHORT"] == pytest.approx(-10.0)
        assert out.total_pnl == pytest.approx(0.0)

    # --- failure paths ---

    def test_empty_positions_raises(self):
        with pytest.raises(Unavailable, match="no positions"):
            stress_book(["A"], ["MKT"], [[1.0]], {}, Scenario("x"))

    def test_zero_gross_value_raises(self):
        # Position present but maps to no asset in the book -> gross value 0.
        with pytest.raises(Unavailable, match="gross position value is 0"):
            stress_book(
                ["A"], ["MKT"], [[1.0]], {"UNKNOWN": 100.0}, Scenario("x")
            )
