"""Allocation across ETFs, replayed.

The cadence is what these tests exist for. A rule at three cadences produces
three plausible equity curves, and the difference between them is entirely in
how often the weights were restated -- so a `threshold` implementation that
quietly rebalanced every session, or a `static` one that rebalanced quarterly,
would produce numbers nobody could tell were wrong. The rebalance count is
therefore asserted exactly, not merely as an ordering.
"""

import numpy as np
import pandas as pd
import pytest

from omni.research.allocation import AllocationRefused, equal_weight, risk_balanced
from omni.research.etf_allocation import (
    DRIFT_THRESHOLD,
    QUARTERLY_SESSIONS,
    run_allocation_experiment,
)

SESSIONS = 400
INDEX = pd.bdate_range("2024-08-05", periods=SESSIONS)
UNIVERSE = ["XLK", "XLE", "XLV"]


def _panel(drifts: dict[str, float], *, benchmark_drift: float = 0.0004):
    columns = {
        name: 100.0 * np.cumprod(np.full(SESSIONS, 1.0 + drift))
        for name, drift in drifts.items()
    }
    columns["SPY"] = 100.0 * np.cumprod(np.full(SESSIONS, 1.0 + benchmark_drift))
    return pd.DataFrame(columns, index=INDEX)


def _result(experiment, book, cadence):
    matches = [r for r in experiment.results if r.book == book and r.cadence == cadence]
    assert len(matches) == 1, f"{book}/{cadence} appears {len(matches)} times"
    return matches[0]


class TestCadence:
    def test_static_decides_once_and_never_again(self):
        """A rule that cannot beat its own first decision is paying turnover for
        nothing, so the floor has to actually be a floor."""
        panel = _panel({"XLK": 0.001, "XLE": 0.0005, "XLV": 0.0002})

        experiment = run_allocation_experiment(
            panel, {"equal_weight": equal_weight},
            universe=UNIVERSE, benchmark="SPY", cadences=("static",),
        )

        result = _result(experiment, "equal_weight", "static")
        assert result.rebalances == 1
        assert result.turnover == pytest.approx(1.0)

    def test_quarterly_restates_every_sixty_three_sessions(self):
        panel = _panel({"XLK": 0.001, "XLE": 0.0005, "XLV": 0.0002})

        experiment = run_allocation_experiment(
            panel, {"equal_weight": equal_weight},
            universe=UNIVERSE, benchmark="SPY", cadences=("quarterly",),
        )

        result = _result(experiment, "equal_weight", "quarterly")
        expected = 1 + (result.sessions - 1) // QUARTERLY_SESSIONS
        assert result.rebalances == expected
        assert result.rebalances > 1

    def test_threshold_holds_still_when_the_book_does_not_drift(self):
        """Every name moving identically means the weights never leave target.
        A threshold rule that rebalanced anyway would charge turnover for a
        trade with nothing to correct -- and would look identical to a correct
        one on the equity curve, because the trade is a no-op."""
        panel = _panel({"XLK": 0.0005, "XLE": 0.0005, "XLV": 0.0005})

        experiment = run_allocation_experiment(
            panel, {"equal_weight": equal_weight},
            universe=UNIVERSE, benchmark="SPY", cadences=("threshold",),
        )

        result = _result(experiment, "equal_weight", "threshold")
        assert result.rebalances == 1
        assert result.turnover == pytest.approx(1.0)

    def test_threshold_restates_when_drift_is_large_and_less_often_than_quarterly(self):
        panel = _panel({"XLK": 0.004, "XLE": -0.002, "XLV": 0.0})

        experiment = run_allocation_experiment(
            panel, {"equal_weight": equal_weight},
            universe=UNIVERSE, benchmark="SPY", cadences=("threshold", "quarterly"),
        )

        threshold = _result(experiment, "equal_weight", "threshold")
        assert threshold.rebalances > 1, (
            "a book pulled apart by a 0.6%/day spread must cross a "
            f"{DRIFT_THRESHOLD} drift threshold"
        )
        assert threshold.turnover > 1.0


class TestTheBaseline:
    def test_the_baseline_is_the_benchmark_bought_once(self):
        panel = _panel({"XLK": 0.001, "XLE": 0.0005, "XLV": 0.0002})

        experiment = run_allocation_experiment(
            panel, {"equal_weight": equal_weight},
            universe=UNIVERSE, benchmark="SPY", cost_bps=0.0,
        )

        # 0.04%/session compounded over the post-warmup window, entered free.
        sessions = experiment.baseline.sessions
        expected = (1.0004 ** sessions) - 1.0
        assert experiment.baseline.total_return_pct == pytest.approx(
            expected * 100.0, rel=1e-6
        )
        assert experiment.baseline.rebalances == 1

    def test_excess_is_measured_against_that_baseline_not_against_zero(self):
        """A rule that loses to buy-and-hold while making money must report a
        negative excess. Reporting its own return as the result is how a losing
        allocation reads as a winning one."""
        panel = _panel(
            {"XLK": 0.0001, "XLE": 0.0001, "XLV": 0.0001}, benchmark_drift=0.002
        )

        experiment = run_allocation_experiment(
            panel, {"equal_weight": equal_weight},
            universe=UNIVERSE, benchmark="SPY", cadences=("static",), cost_bps=0.0,
        )

        result = _result(experiment, "equal_weight", "static")
        assert result.cagr_pct > 0.0
        assert result.excess_cagr_pct < 0.0


class TestCostAndRefusal:
    def test_turnover_is_charged_at_the_stated_rate(self):
        flat = _panel({"XLK": 0.0, "XLE": 0.0, "XLV": 0.0}, benchmark_drift=0.0)

        free = run_allocation_experiment(
            flat, {"equal_weight": equal_weight},
            universe=UNIVERSE, benchmark="SPY", cadences=("static",), cost_bps=0.0,
        )
        charged = run_allocation_experiment(
            flat, {"equal_weight": equal_weight},
            universe=UNIVERSE, benchmark="SPY", cadences=("static",), cost_bps=100.0,
        )

        assert _result(free, "equal_weight", "static").total_return_pct == pytest.approx(
            0.0, abs=1e-9
        )
        assert _result(
            charged, "equal_weight", "static"
        ).total_return_pct == pytest.approx(-1.0, abs=1e-6)

    def test_a_hole_in_the_panel_refuses_the_whole_experiment(self):
        panel = _panel({"XLK": 0.001, "XLE": 0.0005, "XLV": 0.0002})
        panel.iloc[300, panel.columns.get_loc("XLE")] = np.nan

        with pytest.raises(AllocationRefused, match="incomplete history"):
            run_allocation_experiment(
                panel, {"equal_weight": equal_weight},
                universe=UNIVERSE, benchmark="SPY",
            )

    def test_a_missing_benchmark_refuses_rather_than_scoring_against_nothing(self):
        panel = _panel({"XLK": 0.001, "XLE": 0.0005, "XLV": 0.0002}).drop(columns=["SPY"])

        with pytest.raises(AllocationRefused, match="no column for SPY"):
            run_allocation_experiment(
                panel, {"equal_weight": equal_weight},
                universe=UNIVERSE, benchmark="SPY",
            )

    def test_a_rule_that_refuses_is_reported_rather_than_dropped(self):
        """A book missing from the results with no reason reads as a rule that
        was never asked for."""
        panel = _panel({"XLK": 0.0005, "XLE": 0.0005, "XLV": 0.0005})
        panel["XLV"] = 100.0

        experiment = run_allocation_experiment(
            panel, {"risk_balanced": risk_balanced},
            universe=UNIVERSE, benchmark="SPY", cadences=("static",),
        )

        assert experiment.results == []
        assert any("risk_balanced/static refused" in w for w in experiment.warnings)

    def test_the_limitations_are_carried_on_every_result(self):
        panel = _panel({"XLK": 0.001, "XLE": 0.0005, "XLV": 0.0002})

        experiment = run_allocation_experiment(
            panel, {"equal_weight": equal_weight},
            universe=UNIVERSE, benchmark="SPY",
        )

        joined = " ".join(experiment.warnings)
        assert "not a capital-allocation gate" in joined
        assert "holdout" in joined


class TestCashAndLongFlat:
    """A rule whose weights sum below 1.0 holds the remainder in cash."""

    def _half_invested(self, calls):
        from omni.research.allocation import Allocation

        def rule(history, universe, *, benchmark):
            calls.append(len(history))
            return Allocation(
                book="half", rule_version="half/v1", universe=list(universe),
                weights={"XLK": 0.5}, inputs={}, benchmark=benchmark,
            )

        return rule

    def test_cash_is_held_rather_than_renormalised_away(self):
        # Renormalising the invested names to 1.0 after the first session made
        # this book earn nearly all of XLK's return instead of half of it.
        panel = _panel({"XLK": 0.001, "XLE": 0.0, "XLV": 0.0})
        calls: list[int] = []
        experiment = run_allocation_experiment(
            panel, {"half": self._half_invested(calls)},
            universe=UNIVERSE, benchmark="SPY", cadences=("static",), cost_bps=0.0,
        )
        result = _result(experiment, "half", "static")
        sessions = result.sessions
        expected = 0.5 * (1.001**sessions) + 0.5 - 1.0
        assert result.total_return_pct == pytest.approx(expected * 100.0, rel=1e-9)
        assert 0.0 < result.cash_weight_mean < 0.5
        assert any("cash earned 0%" in w for w in experiment.warnings)

    def test_cash_earns_the_stated_rate_and_sharpe_is_measured_over_it(self):
        panel = _panel({"XLK": 0.0, "XLE": 0.0, "XLV": 0.0}, benchmark_drift=0.0)
        calls: list[int] = []
        experiment = run_allocation_experiment(
            panel, {"half": self._half_invested(calls)},
            universe=UNIVERSE, benchmark="SPY", cadences=("static",), cost_bps=0.0,
            cash_rate_annual=0.05,
        )
        result = _result(experiment, "half", "static")
        years = result.sessions / 252
        # Half the book compounds at 5%; the flat half stays at 0.5.
        assert result.total_return_pct == pytest.approx(
            (0.5 * 1.05**years + 0.5 - 1.0) * 100.0, rel=1e-6
        )
        assert experiment.cash_rate_annual_pct == pytest.approx(5.0)
        assert not any("cash earned 0%" in w for w in experiment.warnings)

    def test_an_all_cash_static_book_decides_once_and_stays_flat(self):
        from omni.research.allocation import Allocation

        calls: list[int] = []

        def flat(history, universe, *, benchmark):
            calls.append(len(history))
            return Allocation(
                book="flat", rule_version="flat/v1", universe=list(universe),
                weights={}, inputs={}, benchmark=benchmark,
            )

        panel = _panel({"XLK": 0.001, "XLE": 0.0005, "XLV": 0.0002})
        experiment = run_allocation_experiment(
            panel, {"flat": flat},
            universe=UNIVERSE, benchmark="SPY", cadences=("static",), cost_bps=0.0,
        )
        result = _result(experiment, "flat", "static")
        assert len(calls) == 1
        assert result.rebalances == 1
        assert result.total_return_pct == pytest.approx(0.0, abs=1e-12)

    def test_tsmom_252_is_replayed_instead_of_refused_on_the_short_warmup(self):
        """It needs 253 sessions and used to be asked after 128, so it refused
        on its first decision and the book never appeared in any result."""
        from omni.research.allocation import tsmom_252

        panel = _panel({"XLK": 0.001, "XLE": -0.0005, "XLV": 0.0002})
        experiment = run_allocation_experiment(
            panel, {"tsmom": tsmom_252, "equal_weight": equal_weight},
            universe=UNIVERSE, benchmark="SPY", cadences=("static",), cost_bps=0.0,
        )
        assert not any("tsmom/static refused" in w for w in experiment.warnings)
        tsmom = _result(experiment, "tsmom", "static")
        equal = _result(experiment, "equal_weight", "static")
        # One window for every book and the baseline.
        assert experiment.warmup_sessions == 252
        assert tsmom.sessions == equal.sessions == experiment.baseline.sessions
        assert tsmom.sessions == SESSIONS - 252 - 1
        # Long the two rising sectors, flat the falling one.
        assert tsmom.total_return_pct > equal.total_return_pct


class TestSharpeDifference:
    def _noise(self, seed, mean=0.0, n=500):
        rng = np.random.default_rng(seed)
        return pd.Series(
            rng.normal(mean, 0.01, n), index=pd.bdate_range("2024-01-02", periods=n)
        )

    def test_two_draws_from_one_distribution_are_not_distinguishable(self):
        from omni.research.etf_allocation import sharpe_difference_p_value

        p = sharpe_difference_p_value(self._noise(1), self._noise(2))
        assert p is not None and p > 0.10

    def test_a_series_that_beats_its_pair_every_session_is_distinguishable(self):
        from omni.research.etf_allocation import sharpe_difference_p_value

        base = self._noise(1)
        p = sharpe_difference_p_value(base + 0.002, base)
        assert p is not None and p < 0.01

    def test_too_short_or_constant_is_untestable_not_insignificant(self):
        from omni.research.etf_allocation import sharpe_difference_p_value

        short = self._noise(1, n=30)
        assert sharpe_difference_p_value(short, short * 0.5) is None
        base = self._noise(1)
        constant = pd.Series(0.001, index=base.index)
        assert sharpe_difference_p_value(constant, base) is None

    def test_every_result_carries_a_p_value_and_the_multiplicity_is_stated(self):
        panel = _panel({"XLK": 0.001, "XLE": 0.0005, "XLV": 0.0002})
        experiment = run_allocation_experiment(
            panel, {"equal_weight": equal_weight},
            universe=UNIVERSE, benchmark="SPY",
        )
        assert experiment.baseline.excess_sharpe_p_value is None
        assert len(experiment.results) == 3
        assert any("Bonferroni" in w and "0.0167" in w for w in experiment.warnings)
