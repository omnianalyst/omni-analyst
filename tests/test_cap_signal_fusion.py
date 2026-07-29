"""Behaviour tests for the signal-fusion capabilities.

v1's only test for signal_fusion was an empty-table router test
(`backend/tests/unit/test_signal_fusion_router.py`) -- it exercises the HTTP
layer against fresh Postgres tables and never constructs a signal vector. There
is therefore no numeric oracle to copy: these constructed-input tests are the
oracle, as for the regime port.

Every assertion is on a constructed series with a known answer. Every failure
path (single signal, constant signal, mismatched length, lag window longer than
the series) raises `Unavailable` -- v1 substituted a neutral zero, a zero
correlation, or an assumed lag in each of these cases, which is how a
covered-looking-but-empty network enters the store.
"""

import numpy as np
import pytest

from omni.capabilities.signal_fusion import (
    NormalizationMethod,
    convergence,
    conviction,
    cross_correlation_at_lag,
    independence_votes,
    lead_lag,
    normalize,
    z_score,
)
from omni.ingest.protocol import Unavailable

# ---------------------------------------------------------------------------
# z_score -- required outcome: mean 0, unit std (population)
# ---------------------------------------------------------------------------


class TestZScore:
    def test_global_zscore_has_mean_zero_and_unit_std(self):
        rng = np.random.RandomState(7)
        x = rng.normal(50.0, 3.0, size=500)
        z = z_score(x)
        assert float(np.mean(z)) == pytest.approx(0.0, abs=1e-12)
        assert float(np.std(z)) == pytest.approx(1.0, abs=1e-12)

    def test_zscore_is_shift_and_scale_invariant(self):
        # z-score of (a*x + b) == z-score of x.
        x = np.array([1.0, 2.0, 3.0, 4.0, 5.0, 6.0, 7.0, 8.0])
        assert np.allclose(z_score(100.0 * x + 9.0), z_score(x))

    def test_known_small_series_values(self):
        # x=[2,4,4,2]: mean=3, population std=1 -> z = [ -1, 1, 1, -1 ].
        z = z_score([2.0, 4.0, 4.0, 2.0])
        assert np.allclose(z, [-1.0, 1.0, 1.0, -1.0])
        assert float(np.std(z)) == pytest.approx(1.0)


# ---------------------------------------------------------------------------
# normalize -- faithful dispatch of v1's seven methods
# ---------------------------------------------------------------------------


class TestNormalize:
    def test_identity_returns_input(self):
        x = np.array([0.1, -0.2, 0.3])
        assert np.allclose(normalize(x, NormalizationMethod.IDENTITY), x)

    def test_sign(self):
        x = np.array([-0.4, 0.0, 0.5])
        assert np.allclose(normalize(x, NormalizationMethod.SIGN), [-1.0, 0.0, 1.0])

    def test_tanh(self):
        x = np.array([0.0, 1.0])
        assert np.allclose(normalize(x, NormalizationMethod.TANH), np.tanh(x))

    def test_min_max_maps_native_range_to_minus_one_one(self):
        # values spanning the native range map to [-1, +1].
        x = np.array([0.0, 50.0, 100.0])
        out = normalize(x, NormalizationMethod.MIN_MAX, native_range=(0.0, 100.0))
        assert np.allclose(out, [-1.0, 0.0, 1.0])

    def test_inverted_flips_sign(self):
        x = np.array([0.0, 50.0, 100.0])
        up = normalize(x, NormalizationMethod.MIN_MAX, native_range=(0.0, 100.0))
        down = normalize(x, NormalizationMethod.MIN_MAX, native_range=(0.0, 100.0), inverted=True)
        assert np.allclose(down, -up)

    def test_zscore_method_is_rolling_and_clipped_bit_for_bit(self):
        # v1's registry Z_SCORE is a *rolling* z divided by 3 and clipped to
        # [-1, 1] -- NOT a unit-std z-score (that is `z_score()` above). The
        # warmup indices use an expanding window, so only full-window indices
        # settle. These expected values are v1's formula applied by hand to
        # [1..8] with window=4: rolling mean/std (population), std<1e-8 guard
        # (none triggered), (x-mean)/std, then clip(/3, -1, 1).
        x = np.array([1.0, 2.0, 3.0, 4.0, 5.0, 6.0, 7.0, 8.0])
        out = normalize(x, NormalizationMethod.Z_SCORE, window=4)
        expected = np.array(
            [-1.0, -0.3333333, 0.2041241, 0.4472136, 0.4472136, 0.4472136, 0.4472136, 0.4472136]
        )
        assert np.allclose(out, expected, atol=1e-6)
        assert out.min() >= -1.0 and out.max() <= 1.0

    def test_percentile_of_monotonic_series(self):
        # Strictly rising series: each point is the max of its trailing window,
        # so its rolling percentile is 1.0 -> mapped to +1.
        x = np.array([1.0, 2.0, 3.0, 4.0, 5.0])
        out = normalize(x, NormalizationMethod.PERCENTILE, window=len(x))
        # First point: only itself in the window -> percentile 1.0 -> +1.0.
        assert out[0] == pytest.approx(1.0)
        assert out[-1] == pytest.approx(1.0)
        # Non-monotonic input discriminates from a constant +1 stub: in [3,1,2]
        # the middle point is the window min (percentile 0.5 -> 0.0) and the
        # last is below the max (2 of 3 -> 1/3).
        nm = normalize([3.0, 1.0, 2.0], NormalizationMethod.PERCENTILE, window=3)
        assert nm[1] == pytest.approx(0.0)
        assert nm[2] == pytest.approx(1.0 / 3.0)

    def test_rank_of_monotonic_series(self):
        x = np.array([1.0, 2.0, 3.0, 4.0, 5.0])
        out = normalize(x, NormalizationMethod.RANK, window=len(x))
        # Final point is the largest in a full window of 5 -> rank index 4 ->
        # 4 / (5-1) * 2 - 1 = 1.0.
        assert out[-1] == pytest.approx(1.0)
        # Non-monotonic input discriminates from a constant +1 stub: in [3,1,2]
        # with an expanding window, index 1 is the min of [3,1] (rank 0 -> -1.0)
        # and index 2 is the median of [3,1,2] (rank 1 of 3 -> 0.0).
        nm = normalize([3.0, 1.0, 2.0], NormalizationMethod.RANK, window=3)
        assert nm[2] == pytest.approx(0.0)
        assert nm[1] == pytest.approx(-1.0)


# ---------------------------------------------------------------------------
# convergence -- required outcome: maximal / minimal alignment, direction sign
# ---------------------------------------------------------------------------


class TestConvergence:
    def test_two_identical_signals_have_maximal_alignment(self):
        # Two equal positive signals: std 0 -> alignment 1.0; direction is the
        # shared value.
        res = convergence({"a": 0.6, "b": 0.6})
        assert res.alignment == pytest.approx(1.0)
        assert res.direction == pytest.approx(0.6)

    def test_signal_and_its_negation_have_minimal_alignment(self):
        # +0.8 and -0.8: std ~ 0.8 > 0 -> 1 - std clamped to >= 0; for two
        # values std = |0.8 - 0| = 0.8 (population), so alignment = 0.2...
        # but the direction cancels to 0. For maximal spread use +-1.0:
        res = convergence({"a": 1.0, "b": -1.0})
        assert res.alignment == 0.0  # std=1.0 -> 1-1 = 0
        assert res.direction == pytest.approx(0.0)
        assert res.alignment < convergence({"a": 0.6, "b": 0.6}).alignment

    def test_direction_sign_follows_the_majority(self):
        res = convergence({"a": 0.8, "b": 0.7, "c": -0.2})
        assert res.direction > 0.0
        assert res.bullish == 2
        assert res.bearish == 1
        assert res.neutral == 0

    def test_weights_scale_direction(self):
        # Equal-weight mean of {0.5, -0.5} is 0; pinning all weight on the
        # positive signal drives direction toward +0.5.
        eq = convergence({"a": 0.5, "b": -0.5})
        weighted = convergence({"a": 0.5, "b": -0.5}, weights={"a": 1.0, "b": 0.0})
        assert eq.direction == pytest.approx(0.0)
        assert weighted.direction == pytest.approx(0.5)

    def test_divergences_flag_opposed_pairs(self):
        res = convergence({"a": 0.9, "b": -0.9, "c": 0.1})
        pairs = {(d.signal_a, d.signal_b) for d in res.divergences}
        assert ("a", "b") in pairs
        for d in res.divergences:
            assert abs(d.delta) > 1.0

    def test_independence_votes_dedup_proxy_against_its_root(self):
        # A root present alongside its own proxy: together they cast one vote,
        # split 0.5 / 0.5. An independent signal keeps its full vote.
        votes = independence_votes(
            ["root", "proxy", "indep"],
            proxy_of={"proxy": ["root"]},
        )
        assert votes["root"] == pytest.approx(0.5)
        assert votes["proxy"] == pytest.approx(0.5)
        assert votes["indep"] == pytest.approx(1.0)

    def test_independence_votes_group_proxies_of_an_absent_root(self):
        # Two proxies of the same missing root share one vote between them.
        votes = independence_votes(
            ["p1", "p2"],
            proxy_of={"p1": ["missing"], "p2": ["missing"]},
        )
        assert votes["p1"] == pytest.approx(0.5)
        assert votes["p2"] == pytest.approx(0.5)

    def test_conviction_formula(self):
        # v1: 0.6*alignment + 0.2*participation + 0.2*|direction|
        v = conviction(1.0, 0.5, participation=1.0)
        assert v == pytest.approx(0.6 + 0.2 + 0.1)

    def test_conviction_requires_participation(self):
        with pytest.raises(TypeError):
            conviction(1.0, 0.5)  # type: ignore[call-arg]

    def test_conviction_rejects_out_of_range_participation(self):
        with pytest.raises(Unavailable):
            conviction(1.0, 0.5, participation=1.5)


# ---------------------------------------------------------------------------
# lead-lag -- required outcome: recover an exact applied shift, correct sign
# ---------------------------------------------------------------------------


class TestLeadLag:
    def test_recover_exact_positive_shift(self):
        # a leads b by `k`: b[i] = a[i-k]. v1's lag>0 branch pairs a[:-k] with
        # b[k:], which are identical -> correlation 1.0 at lag +k.
        rng = np.random.RandomState(11)
        a = rng.normal(size=120)
        k = 5
        b = np.concatenate([np.full(k, a[0]), a[:-k]])
        res = lead_lag(a, b, max_lag=15)
        assert res.lag == k
        assert res.correlation == pytest.approx(1.0, abs=1e-9)
        assert res.significance == pytest.approx(1.0)

    def test_sign_convention_positive_means_a_leads_b(self):
        # Same construction as above: a leads b -> positive lag.
        rng = np.random.RandomState(12)
        a = rng.normal(size=120)
        k = 4
        b = np.concatenate([np.full(k, a[0]), a[:-k]])
        res = lead_lag(a, b, max_lag=15)
        assert res.lag > 0
        assert res.lag == k

    def test_negative_shift_when_b_leads_a(self):
        # Reverse roles: b leads a by k -> the recovered lag is negative.
        rng = np.random.RandomState(13)
        b = rng.normal(size=120)
        k = 6
        a = np.concatenate([np.full(k, b[0]), b[:-k]])
        res = lead_lag(a, b, max_lag=15)
        assert res.lag < 0
        assert res.lag == -k

    def test_cross_correlation_at_lag_zero_of_identical_series_is_one(self):
        x = np.array([1.0, 2.0, 3.0, 4.0, 5.0, 4.0, 3.0, 2.0, 1.0])
        assert cross_correlation_at_lag(x, x, 0) == pytest.approx(1.0)

    def test_cross_correlation_at_lag_exceeding_series_raises(self):
        x = np.arange(10.0)
        with pytest.raises(Unavailable, match=">= series length"):
            cross_correlation_at_lag(x, x, lag=10)


# ---------------------------------------------------------------------------
# Failure paths -- each raises Unavailable, never returns a default
# ---------------------------------------------------------------------------


class TestFailurePaths:
    def test_zscore_constant_signal_raises_not_zeros(self):
        # v1 returned zeros here; the work order requires a raise.
        with pytest.raises(Unavailable, match="zero variance"):
            z_score([5.0] * 40)

    def test_zscore_empty_input_raises(self):
        with pytest.raises(Unavailable, match="nothing to normalise"):
            z_score([])

    def test_normalize_zscore_window_below_two_raises(self):
        with pytest.raises(Unavailable, match="window >= 2"):
            normalize([1.0, 2.0, 3.0, 4.0], NormalizationMethod.Z_SCORE, window=1)

    def test_normalize_zscore_constant_signal_raises(self):
        with pytest.raises(Unavailable, match="constant signal"):
            normalize([3.0] * 20, NormalizationMethod.Z_SCORE, window=5)

    def test_normalize_min_max_zero_span_raises(self):
        with pytest.raises(Unavailable, match="zero span"):
            normalize([1.0, 2.0, 3.0], NormalizationMethod.MIN_MAX, native_range=(5.0, 5.0))

    def test_normalize_min_max_inverted_range_raises(self):
        # F3: v1's bare `span < 1e-8` (no abs) treated an inverted native_range
        # (max < min) as invalid; the port's abs() let it through and silently
        # sign-flipped the mapping. Refuse, matching v1.
        with pytest.raises(Unavailable, match="zero span"):
            normalize([0.0, 50.0, 100.0], NormalizationMethod.MIN_MAX, native_range=(100.0, 0.0))

    def test_normalize_empty_raises(self):
        with pytest.raises(Unavailable, match="nothing to normalise"):
            normalize([], NormalizationMethod.IDENTITY)

    def test_convergence_single_signal_raises(self):
        with pytest.raises(Unavailable, match="need >= 2"):
            convergence({"a": 0.5})

    def test_convergence_no_signals_raises(self):
        with pytest.raises(Unavailable, match="need >= 2"):
            convergence({})

    def test_convergence_zero_total_weight_raises(self):
        with pytest.raises(Unavailable, match="sum to zero"):
            convergence({"a": 0.5, "b": -0.5}, weights={"a": 0.0, "b": 0.0})

    def test_leadlag_mismatched_length_raises(self):
        with pytest.raises(Unavailable, match="mismatched lengths"):
            lead_lag([1.0, 2.0, 3.0], [1.0, 2.0], max_lag=1)

    def test_leadlag_window_longer_than_series_raises(self):
        x = np.arange(40.0)
        with pytest.raises(Unavailable, match=">= series length"):
            lead_lag(x, x, max_lag=40)

    def test_leadlag_constant_series_raises(self):
        const = [2.0] * 60
        with pytest.raises(Unavailable, match="constant series"):
            lead_lag(const, const, max_lag=10)

    def test_leadlag_too_few_points_raises(self):
        a = [1.0, 2.0, 3.0]
        b = [3.0, 2.0, 1.0]
        with pytest.raises(Unavailable, match="overlapping points"):
            lead_lag(a, b, max_lag=2)

    def test_leadlag_pure_noise_near_n_refuses_not_fabricated_edge(self):
        # F1: with max_lag near n, the extreme lags overlap 2-3 points, where a
        # Pearson correlation is always ~+-1.0. v1 had an n_eff < 10 sample-size
        # floor that refused such edges; the port dropped it and returned
        # |correlation| = 1.0 fabricated from pure noise. The floor must refuse.
        rng = np.random.RandomState(7)
        a = rng.normal(size=40)
        b = rng.normal(size=40)
        with pytest.raises(Unavailable, match="overlapping points"):
            lead_lag(a, b, max_lag=38)

    def test_leadlag_max_lag_n_minus_one_raises_at_guard_not_mid_scan(self):
        # F2: max_lag = n - 1 used to slip the `max_lag >= n` guard and crash
        # mid-scan with a misleading "constant slice" message (1-point slices at
        # the extreme lag). It must raise at the guard, blaming max_lag.
        rng = np.random.RandomState(5)
        a = rng.normal(size=40)
        b = rng.normal(size=40)
        with pytest.raises(Unavailable, match="max_lag"):
            lead_lag(a, b, max_lag=39)

    def test_cross_correlation_mismatched_length_raises(self):
        with pytest.raises(Unavailable, match="mismatched lengths"):
            cross_correlation_at_lag([1.0, 2.0], [1.0, 2.0, 3.0], lag=0)
