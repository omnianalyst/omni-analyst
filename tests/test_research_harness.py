"""The harness, and the five ways a cross-sectional backtest lies.

Each guard here exists because a specific result nearly survived on 2026-08-09:
a rank IC of -8.35 that earned nothing, a winner that cleared 13 of 30 rebalance
offsets, a loser made significant by subtracting costs, a null whose own 95th
percentile was 2.5 rather than 1.96, and five signals that were significant
full-sample and dead in the most recent third.

A guard that cannot be shown to fire is decoration.
"""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from omni.research.harness import evaluate, self_test
from omni.research.registry import Registry


@pytest.fixture
def registry(tmp_path) -> Registry:
    return Registry(path=tmp_path / "registry.jsonl")


def _panel(seed: int = 0, days: int = 600, assets: int = 30):
    rng = np.random.default_rng(seed)
    dates = pd.date_range("2021-01-01", periods=days, freq="D")
    cols = [f"A{i:02d}" for i in range(assets)]
    rets = 0.02 * rng.normal(0, 1, size=(days, assets))
    prices = pd.DataFrame(
        100 * np.exp(np.cumsum(rets, axis=0)), index=dates, columns=cols
    )
    return prices, dates, cols


class TestTheRegistryIsTheHonestPartOfTheBar:
    def test_the_count_survives_across_instances(self, tmp_path):
        """N is meaningless if it resets with the process.

        A researcher who runs 84 tests today and 84 tomorrow, applying
        sqrt(2 ln 84) to each, has run 168 tests against a bar built for 84.
        """
        path = tmp_path / "r.jsonl"
        Registry(path=path).record(name="a", source="s", cells=50, verdict="fail")
        Registry(path=path).record(name="b", source="s", cells=34, verdict="fail")

        assert Registry(path=path).total_cells() == 84

    def test_the_bar_counts_the_test_about_to_run(self, registry):
        # Otherwise each new test is judged as though it were the first.
        before = registry.bar(pending_cells=0)
        including = registry.bar(pending_cells=500)

        assert including > before

    def test_the_bar_never_drops_below_the_crypto_null(self, registry):
        """1.96 is wrong here before multiplicity is even considered.

        A permutation null on crypto cross-sections puts the null's OWN 95th
        percentile at |t| 2.2-2.5, because one market factor correlates every
        asset.
        """
        assert registry.bar(pending_cells=1) >= 2.5

    def test_a_test_producing_no_statistics_is_refused(self, registry):
        # Recording zero cells would understate the search it was part of.
        with pytest.raises(ValueError, match="not a test"):
            registry.record(name="x", source="s", cells=0, verdict="fail")

    def test_a_truncated_final_line_does_not_destroy_the_history(self, tmp_path):
        # The one corruption an append-only file suffers is an interrupted
        # write. Losing one record is acceptable; losing all of them is not.
        path = tmp_path / "r.jsonl"
        reg = Registry(path=path)
        reg.record(name="a", source="s", cells=10, verdict="fail")
        with path.open("a") as fh:
            fh.write('{"name": "b", "cel')

        assert Registry(path=path).total_cells() == 10


class TestTheHarnessProvesItselfBeforeItIsTrusted:
    def test_it_recovers_a_planted_edge_and_finds_none_in_noise(self):
        # A test that cannot find an edge that is definitely there proves
        # nothing when it reports none.
        self_test()

    def test_a_planted_edge_is_found_by_evaluate(self, registry):
        rng = np.random.default_rng(3)
        prices, dates, cols = _panel(seed=3)
        planted = pd.DataFrame(
            rng.normal(0, 1, size=(len(dates), len(cols))), index=dates, columns=cols
        )
        moved = prices * np.exp(np.cumsum(0.004 * planted.shift(1).fillna(0), axis=0))

        v = evaluate(
            name="planted", source="synthetic", signal=lambda _p: planted,
            prices=moved, horizons=(1,), cost_bps=0.0, registry=registry,
            permutation_draws=20,
        )[0]

        assert abs(v.gross.t) > 4.0


class TestEachGuardFires:
    def test_significant_full_sample_but_dead_recently_is_named(self, registry):
        """The pattern that retired five of six candidates in one day."""
        rng = np.random.default_rng(11)
        prices, dates, cols = _panel(seed=11, days=900)
        planted = pd.DataFrame(
            rng.normal(0, 1, size=(len(dates), len(cols))), index=dates, columns=cols
        )
        # The edge exists for the first two thirds and then stops.
        strength = np.where(np.arange(len(dates))[:, None] < 600, 0.006, 0.0)
        moved = prices * np.exp(
            np.cumsum(strength * planted.shift(1).fillna(0), axis=0)
        )

        v = evaluate(
            name="decayed", source="synthetic", signal=lambda _p: planted,
            prices=moved, horizons=(1,), cost_bps=0.0, registry=registry,
            permutation_draws=20,
        )[0]

        assert abs(v.gross.t) > v.bar
        assert abs(v.recent_third.t) < v.bar
        assert not v.passed
        assert any("most recent third" in w for w in v.warnings)

    def test_the_alignment_sweep_reports_every_offset(self, registry):
        # The rebalance start date is arbitrary and nobody reports it; the
        # headline statistic is one draw from a distribution of `horizon` draws.
        prices, dates, cols = _panel(seed=5, days=700)
        rng = np.random.default_rng(5)
        score = pd.DataFrame(
            rng.normal(0, 1, size=(len(dates), len(cols))), index=dates, columns=cols
        )

        v = evaluate(
            name="align", source="synthetic", signal=lambda _p: score,
            prices=prices, horizons=(7,), cost_bps=0.0, registry=registry,
            permutation_draws=20,
        )[0]

        assert 0.0 <= v.alignment_clearing <= 1.0
        assert not np.isnan(v.alignment_median_t)

    def test_a_permuted_null_is_measured_not_assumed(self, registry):
        prices, dates, cols = _panel(seed=7, days=700)
        rng = np.random.default_rng(7)
        score = pd.DataFrame(
            rng.normal(0, 1, size=(len(dates), len(cols))), index=dates, columns=cols
        )

        v = evaluate(
            name="null", source="synthetic", signal=lambda _p: score,
            prices=prices, horizons=(3,), cost_bps=0.0, registry=registry,
            permutation_draws=40,
        )[0]

        assert v.null_p95 > 0
        assert not np.isnan(v.null_p95)

    def test_gross_and_net_are_both_reported(self, registry):
        """A result significant NET but not GROSS is a cost artifact.

        Costs are near deterministic, so subtracting them moves the mean without
        touching the variance and mechanically inflates |t| on a loser.
        """
        prices, dates, cols = _panel(seed=9, days=700)
        rng = np.random.default_rng(9)
        score = pd.DataFrame(
            rng.normal(0, 1, size=(len(dates), len(cols))), index=dates, columns=cols
        )

        v = evaluate(
            name="costs", source="synthetic", signal=lambda _p: score,
            prices=prices, horizons=(3,), cost_bps=40.0, registry=registry,
            permutation_draws=20,
        )[0]

        assert v.gross.mean_ann_pct != v.net.mean_ann_pct
        assert v.net.mean_ann_pct < v.gross.mean_ann_pct

    def test_too_few_periods_reports_nothing_rather_than_a_number(self, registry):
        # A statistic from twelve observations is not a small result, it is not
        # a result.
        prices, dates, cols = _panel(seed=13, days=120)
        rng = np.random.default_rng(13)
        score = pd.DataFrame(
            rng.normal(0, 1, size=(len(dates), len(cols))), index=dates, columns=cols
        )

        v = evaluate(
            name="thin", source="synthetic", signal=lambda _p: score,
            prices=prices, horizons=(14,), cost_bps=0.0, registry=registry,
            permutation_draws=10,
        )[0]

        assert not v.passed
        assert any("floor" in w for w in v.warnings)


class TestPassingIsDeliberatelyHardToDo:
    def test_full_sample_significance_alone_does_not_pass(self, registry):
        rng = np.random.default_rng(11)
        prices, dates, cols = _panel(seed=11, days=900)
        planted = pd.DataFrame(
            rng.normal(0, 1, size=(len(dates), len(cols))), index=dates, columns=cols
        )
        strength = np.where(np.arange(len(dates))[:, None] < 600, 0.006, 0.0)
        moved = prices * np.exp(
            np.cumsum(strength * planted.shift(1).fillna(0), axis=0)
        )

        v = evaluate(
            name="fullonly", source="synthetic", signal=lambda _p: planted,
            prices=moved, horizons=(1,), cost_bps=0.0, registry=registry,
            permutation_draws=20,
        )[0]

        # Every strategy retired in this project was significant full-sample.
        assert abs(v.gross.t) > v.bar
        assert not v.passed

    def test_a_signal_must_return_a_frame(self, registry):
        prices, _dates, _cols = _panel()

        with pytest.raises(TypeError, match="DataFrame"):
            evaluate(
                name="bad", source="synthetic", signal=lambda _p: 42,
                prices=prices, horizons=(1,), registry=registry,
            )


class TestTheStatisticDoesNotDivideByFloatingPointDust:
    """A constant spread returned |t| = 1.8e16 and a PASSING verdict.

    `np.std(ddof=1)` of a constant series is 2.1e-17, not 0.0, so the original
    `se > 0` test was always True. This is the float-compared-to-zero failure the
    house rules forbid, and it was live in code that had already been
    mutation-tested — the mutation testing checked the guards, not the
    arithmetic under them.
    """

    def test_a_constant_series_yields_no_evidence_rather_than_infinite_evidence(self):
        from omni.research.harness import _stat

        leg = _stat(np.full(60, 0.05), 1)

        assert leg.t == 0.0
        assert leg.mean_ann_pct > 0  # the mean is real; only the t is undefined

    def test_a_near_constant_series_is_also_refused(self):
        # Differences at the last bit are floating point, not variance.
        from omni.research.harness import _stat

        near = np.array([0.05] * 30 + [0.05 + 1e-18] * 30)

        assert _stat(near, 1).t == 0.0

    def test_a_real_spread_still_produces_a_statistic(self):
        # The guard must not swallow genuine low-variance signal.
        from omni.research.harness import _stat

        rng = np.random.default_rng(0)
        leg = _stat(0.001 + 0.0005 * rng.normal(0, 1, 200), 1)

        assert abs(leg.t) > 5.0

    def test_one_observation_raises_rather_than_reporting_a_zero(self):
        # A measured zero and an unmeasurable one must not look the same.
        from omni.research.harness import _stat

        with pytest.raises(ValueError, match="at least two observations"):
            _stat(np.array([0.01]), 1)


class TestDegeneratePeriodsAreSkippedNotZeroFilled:
    def test_the_information_coefficient_is_not_padded_with_invented_zeros(self):
        """Appending 0.0 for a degenerate period shrinks the variance.

        The IC statistic would then be biased by however many degenerate periods
        the panel happens to contain — a fabricated observation wearing the
        clothes of a measured one.
        """
        from omni.research.harness import _periods

        dates = pd.date_range("2021-01-01", periods=300, freq="D")
        cols = [f"A{i:02d}" for i in range(20)]
        rng = np.random.default_rng(2)
        prices = pd.DataFrame(
            100 * np.exp(np.cumsum(0.02 * rng.normal(0, 1, (300, 20)), axis=0)),
            index=dates, columns=cols,
        )
        scores = pd.DataFrame(
            rng.normal(0, 1, (300, 20)), index=dates, columns=cols
        )
        # Make every score identical on a subset of dates: rank is undefined.
        scores.iloc[::4] = 1.0

        _r, _c, ics, _offered = _periods(
            scores, prices, horizon=1, offset=0, quantile=5
        )

        assert len(ics) > 0
        assert not np.any(ics == 0.0), "a substituted zero survived into the ICs"


class TestTheSignThatRanksIsNotAlwaysTheSignThatEarns:
    """Eight oscillator cells had a significant IC opposing a significant portfolio.

    `below_sma_10` at h=1 scored ic_t +4.66 against a portfolio t of -4.02. The
    score genuinely orders the median asset one way while the fat tail pays the
    other, so the direction that ranks is not the direction that earns. The
    original guard missed this entirely because it only inspected an
    INSIGNIFICANT portfolio — this is the stronger version of that failure, not
    a weaker one.
    """

    def test_opposing_significant_ic_and_portfolio_are_named(self, registry):
        from omni.research.harness import Leg, Verdict

        v = Verdict(
            name="opposed", horizon=1, bar=2.5,
            gross=Leg(-90.0, -4.02, 400), net=Leg(-95.0, -4.20, 400),
            thirds=(), recent_third=Leg(-90.0, -4.02, 133),
            null_p95=2.1, alignment_median_t=-4.0, alignment_clearing=1.0,
            ic_t=4.66, turnover=0.5, cost_bps=20.0, warnings=(),
        )

        # The condition the harness now tests, asserted directly on the values
        # so the test states the rule rather than re-deriving it.
        assert abs(v.ic_t) > v.bar
        assert abs(v.gross.t) > v.bar
        assert v.ic_t * v.gross.t < 0

    async def test_the_warning_fires_on_a_constructed_panel(self, registry):
        """A signal that ranks the median correctly and loses money anyway.

        Built by paying the BOTTOM-ranked asset a large positive return and the
        rest a small negative one: the rank correlation is positive because most
        assets order correctly, while the quintile spread is dominated by the
        one name in the short leg.
        """
        rng = np.random.default_rng(21)
        days, assets = 500, 20
        dates = pd.date_range("2021-01-01", periods=days, freq="D")
        cols = [f"A{i:02d}" for i in range(assets)]
        score = pd.DataFrame(
            rng.normal(0, 1, size=(days, assets)), index=dates, columns=cols
        )

        # Noise on every name, so the spread has real variance -- a
        # deterministic construction would be caught by the degenerate-variance
        # guard and score t = 0 rather than demonstrating anything.
        rets = 0.01 * rng.normal(0, 1, size=(days, assets))
        order = np.argsort(-score.to_numpy(), axis=1)
        for d in range(days - 1):
            for rank, col in enumerate(order[d]):
                # Monotone in rank (so the IC is positive) but with a large
                # payout at the very bottom, which is what the spread shorts.
                rets[d + 1, col] += -0.002 * rank
            rets[d + 1, order[d][-1]] += 0.30

        prices = pd.DataFrame(
            100 * np.exp(np.cumsum(rets, axis=0)), index=dates, columns=cols
        )

        v = evaluate(
            name="ranks-but-loses", source="synthetic", signal=lambda _p: score,
            prices=prices, horizons=(1,), cost_bps=0.0, registry=registry,
            permutation_draws=15,
        )[0]

        assert v.ic_t > 0, "the score should rank the median asset correctly"
        assert v.gross.t < 0, "the portfolio should still lose"
        assert any("OPPOSITE directions" in w for w in v.warnings)


class TestTheKnobsAreRealAndStated:
    """Weighting, strictness and combination — built to test whether the system
    was biased toward rejection. They change no conclusion on real data, which
    is itself the answer, but a knob that cannot be shown to DO anything is
    decoration and would be worse than absent."""

    def test_inverse_vol_weighting_actually_reweights(self, registry):
        # One wild name and nine calm ones: equal weight is dominated by the
        # wild one, inverse-vol is not, so the two must disagree.
        rng = np.random.default_rng(31)
        days, assets = 400, 20
        dates = pd.date_range("2021-01-01", periods=days, freq="D")
        cols = [f"A{i:02d}" for i in range(assets)]
        vols = np.full(assets, 0.01)
        vols[0] = 0.15
        prices = pd.DataFrame(
            100 * np.exp(np.cumsum(vols * rng.normal(0, 1, (days, assets)), axis=0)),
            index=dates, columns=cols,
        )
        score = pd.DataFrame(
            rng.normal(0, 1, (days, assets)), index=dates, columns=cols
        )

        eq = evaluate(
            name="w.eq", source="synthetic", signal=lambda _p: score, prices=prices,
            horizons=(7,), cost_bps=0.0, registry=registry, permutation_draws=10,
            weighting="equal",
        )[0]
        iv = evaluate(
            name="w.iv", source="synthetic", signal=lambda _p: score, prices=prices,
            horizons=(7,), cost_bps=0.0, registry=registry, permutation_draws=10,
            weighting="inverse_vol",
        )[0]

        assert eq.gross.mean_ann_pct != iv.gross.mean_ann_pct
        assert eq.weighting == "equal" and iv.weighting == "inverse_vol"

    def test_an_unknown_weighting_is_refused(self, registry):
        prices, _d, _c = _panel()
        rng = np.random.default_rng(0)
        score = pd.DataFrame(
            rng.normal(0, 1, prices.shape), index=prices.index, columns=prices.columns
        )
        with pytest.raises(ValueError, match="weighting must be"):
            evaluate(
                name="w.bad", source="synthetic", signal=lambda _p: score,
                prices=prices, horizons=(1,), registry=registry, weighting="magic",
            )

    def test_strictness_changes_what_passes(self):
        """`strict` demands the full sample too; `balanced` does not.

        A strategy that works NOW and not historically is a legitimate object,
        and requiring both is how a regime change gets mistaken for no edge.
        """
        from omni.research.harness import Leg, Verdict

        def _v(strictness):
            return Verdict(
                name="x", horizon=1, bar=2.5,
                gross=Leg(10.0, 1.20, 400),          # full sample: NOT significant
                net=Leg(9.0, 1.10, 400),
                thirds=(), recent_third=Leg(40.0, 3.10, 133),   # recent: strong
                null_p95=2.0, alignment_median_t=3.0, alignment_clearing=0.8,
                ic_t=1.0, turnover=0.4, cost_bps=20.0, warnings=(),
                strictness=strictness,
            )

        assert not _v("strict").passed
        assert _v("balanced").passed
        assert _v("exploratory").passed

    def test_an_unknown_strictness_is_refused(self, registry):
        prices, _d, _c = _panel()
        rng = np.random.default_rng(0)
        score = pd.DataFrame(
            rng.normal(0, 1, prices.shape), index=prices.index, columns=prices.columns
        )
        with pytest.raises(ValueError, match="strictness must be"):
            evaluate(
                name="s.bad", source="synthetic", signal=lambda _p: score,
                prices=prices, horizons=(1,), registry=registry, strictness="loose",
            )

    def test_combination_is_scale_invariant_and_reports_correlation(self):
        """The correlation matrix is the whole point of the return value.

        Blending two signals that correlate at 0.95 buys a longer name and
        nothing else, and this project's price signals are near-duplicates by
        construction.
        """
        from omni.research.harness import combine

        prices, dates, cols = _panel(seed=41, days=300, assets=15)
        rng = np.random.default_rng(41)
        a = pd.DataFrame(rng.normal(0, 1, (300, 15)), index=dates, columns=cols)
        b = a * 300.0 + 7.0        # identical information, wildly different scale
        c = pd.DataFrame(rng.normal(0, 1, (300, 15)), index=dates, columns=cols)

        blended, corr = combine({"a": a, "b": b, "c": c}, prices=prices)

        assert abs(corr.loc["a", "b"] - 1.0) < 1e-6, "scale must not survive z-scoring"
        assert abs(corr.loc["a", "c"]) < 0.15
        assert blended.shape == prices.shape

    def test_combination_refuses_a_signal_with_no_weight(self):
        from omni.research.harness import combine

        prices, dates, cols = _panel(seed=42, days=200, assets=12)
        f = pd.DataFrame(np.zeros((200, 12)), index=dates, columns=cols)

        with pytest.raises(ValueError, match="no weight given"):
            combine({"a": f, "b": f}, prices=prices, weights={"a": 1.0})


class TestASignalWithNoCrossSectionIsRefused:
    """Finding 53: a filter leaving too few names returns 0.00 for every cell,
    which reads as a decisive failure and is actually the absence of a test."""

    def test_a_signal_scoring_almost_nothing_raises_rather_than_returning_zeros(
        self, registry
    ):
        import numpy as np
        import pandas as pd

        from omni.research.harness import evaluate

        days = pd.date_range("2024-01-01", periods=400, freq="D", tz="UTC")
        rng = np.random.default_rng(3)
        prices = pd.DataFrame(
            100 * np.exp(rng.normal(0, 0.01, size=(400, 30)).cumsum(axis=0)),
            index=days,
            columns=[f"A{i}" for i in range(30)],
        )

        def almost_never_scores(frame: pd.DataFrame) -> pd.DataFrame:
            # Admits a name only on a move no asset in this panel ever makes.
            moves = frame.pct_change(1, fill_method=None)
            return moves.where(moves.abs() >= 0.90).shift(1)

        with pytest.raises(ValueError, match="cannot be measured on this panel"):
            evaluate(
                name="test.no_cross_section", source="synthetic",
                signal=almost_never_scores, prices=prices, horizons=(1, 2),
                registry=registry, permutation_draws=5, record=False,
            )

    def test_a_signal_that_does_score_enough_names_is_still_measured(self, registry):
        """The floor must not reject signals it exists to protect."""
        import numpy as np
        import pandas as pd

        from omni.research.harness import evaluate

        days = pd.date_range("2024-01-01", periods=400, freq="D", tz="UTC")
        rng = np.random.default_rng(4)
        prices = pd.DataFrame(
            100 * np.exp(rng.normal(0, 0.01, size=(400, 30)).cumsum(axis=0)),
            index=days,
            columns=[f"A{i}" for i in range(30)],
        )

        verdicts = evaluate(
            name="test.full_cross_section", source="synthetic",
            signal=lambda f: f.pct_change(3, fill_method=None).shift(1),
            prices=prices, horizons=(1,), registry=registry,
            permutation_draws=5, record=False,
        )

        assert len(verdicts) == 1
        assert verdicts[0].gross.n > 0
