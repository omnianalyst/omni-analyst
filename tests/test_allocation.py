"""Allocation rules for the forward shadow book.

The tests here are built so that a rule cannot pass by accident. Equal weight,
inverse volatility and score selection all produce plausible-looking weight
vectors that sum to one, so `assert weights.sum() == 1` distinguishes none of
them -- and this project has already found an optimiser no test could tell apart
from equal weight. Each rule is therefore given a panel where its answer is
forced and the other rules' answers are different.
"""

import math
import sys
from datetime import date, timedelta
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from omni.research.allocation import (
    MIN_ANNUAL_VOLATILITY,
    MIN_HISTORY_SESSIONS,
    AllocationRefused,
    equal_weight,
    risk_balanced,
    top_measured,
)
from ops.shadow_book_record import next_session

SESSIONS = 200
INDEX = pd.bdate_range("2025-01-01", periods=SESSIONS)


def _alternating(amplitude: float) -> np.ndarray:
    """A series whose daily return alternates +a, -a.

    Deterministic, so the annualised volatility is a known function of `a` and
    two assets built this way have volatilities in exactly the ratio of their
    amplitudes -- which is what makes the inverse-volatility assertion below an
    equality rather than an ordering.
    """
    steps = np.array([amplitude if i % 2 == 0 else -amplitude for i in range(SESSIONS)])
    return 100.0 * np.cumprod(1.0 + steps)


def _trending(daily: float) -> np.ndarray:
    return 100.0 * np.cumprod(np.full(SESSIONS, 1.0 + daily))


class TestEqualWeight:
    def test_every_name_gets_one_over_n(self):
        panel = pd.DataFrame(
            {n: _trending(0.0004) for n in ("XLK", "XLE", "XLV", "XLF")}, index=INDEX
        )

        allocation = equal_weight(panel, list(panel.columns), benchmark="SPY")

        assert allocation.weights == {
            "XLK": 0.25, "XLE": 0.25, "XLV": 0.25, "XLF": 0.25,
        }
        assert sum(allocation.weights.values()) == pytest.approx(1.0)
        assert allocation.universe == ["XLK", "XLE", "XLV", "XLF"]

    def test_weights_ignore_how_the_names_performed(self):
        """The rule's whole content is that it does not look at returns. A rule
        that tilted toward the winner would still sum to one and still hold
        every name."""
        panel = pd.DataFrame(
            {"XLK": _trending(0.003), "XLE": _trending(-0.002)}, index=INDEX
        )

        allocation = equal_weight(panel, ["XLK", "XLE"], benchmark="SPY")

        assert allocation.weights["XLK"] == pytest.approx(allocation.weights["XLE"])


class TestTopMeasured:
    def test_it_selects_the_highest_scoring_names_and_holds_nothing_else(self):
        """Three strong trends and two weak ones, so the selection is forced."""
        panel = pd.DataFrame(
            {
                "XLK": _trending(0.004),
                "XLE": _trending(0.003),
                "XLV": _trending(0.002),
                "XLF": _trending(-0.002),
                "XLU": _trending(-0.003),
            },
            index=INDEX,
        )

        allocation = top_measured(panel, list(panel.columns), benchmark="SPY", top_n=3)

        assert set(allocation.weights) == {"XLK", "XLE", "XLV"}
        assert all(w == pytest.approx(1 / 3) for w in allocation.weights.values())
        assert "XLF" not in allocation.weights
        assert "XLU" not in allocation.weights

    def test_the_universe_records_everything_it_chose_from(self):
        """A name that scored badly and a name that was never offered are
        different facts, and only one of them is evidence about the rule."""
        panel = pd.DataFrame(
            {
                "XLK": _trending(0.004),
                "XLE": _trending(0.003),
                "XLV": _trending(0.002),
                "XLF": _trending(-0.002),
            },
            index=INDEX,
        )

        allocation = top_measured(panel, list(panel.columns), benchmark="SPY", top_n=2)

        assert set(allocation.universe) == {"XLK", "XLE", "XLV", "XLF"}
        assert set(allocation.inputs["scores"]) == set(allocation.universe)
        assert len(allocation.weights) == 2

    def test_a_one_name_book_is_refused(self):
        panel = pd.DataFrame({"XLK": _trending(0.001)}, index=INDEX)

        with pytest.raises(AllocationRefused, match="single sector"):
            top_measured(panel, ["XLK"], benchmark="SPY", top_n=1)

    def test_asking_for_more_names_than_scored_is_refused(self):
        panel = pd.DataFrame(
            {"XLK": _trending(0.002), "XLE": _trending(0.001)}, index=INDEX
        )

        with pytest.raises(AllocationRefused, match="against the 3"):
            top_measured(panel, ["XLK", "XLE"], benchmark="SPY", top_n=3)


class TestRiskBalanced:
    def test_weights_are_inversely_proportional_to_volatility(self):
        """The quiet name gets exactly twice the weight of the name that moves
        twice as much. An equal-weight implementation gives 0.5/0.5 and a
        weight-by-volatility implementation inverts the answer; both would pass
        an ordering assertion on one of the two, and neither passes this.
        """
        panel = pd.DataFrame(
            {"QUIET": _alternating(0.01), "LOUD": _alternating(0.02)}, index=INDEX
        )

        allocation = risk_balanced(panel, ["QUIET", "LOUD"], benchmark="SPY")

        assert allocation.weights["QUIET"] == pytest.approx(2 / 3, abs=1e-3)
        assert allocation.weights["LOUD"] == pytest.approx(1 / 3, abs=1e-3)
        assert sum(allocation.weights.values()) == pytest.approx(1.0)

    def test_the_measured_volatilities_are_recorded(self):
        panel = pd.DataFrame(
            {"QUIET": _alternating(0.01), "LOUD": _alternating(0.02)}, index=INDEX
        )

        allocation = risk_balanced(panel, ["QUIET", "LOUD"], benchmark="SPY")

        measured = allocation.inputs["annualised_volatility"]
        assert measured["LOUD"] == pytest.approx(2 * measured["QUIET"], rel=1e-3)
        assert measured["QUIET"] == pytest.approx(0.01 * math.sqrt(252), rel=0.02)

    def test_a_near_constant_series_is_refused_rather_than_taking_the_book(self):
        """The float-equality trap, in the one place it would be most costly.

        This series moves by 1e-9 a day, so its standard deviation is tiny and
        emphatically not 0.0 -- `if sigma == 0` never fires. Left unguarded,
        1/sigma hands it ~99.99% of the book, and the result is a confident,
        plausible-looking allocation into a fund that has effectively stopped
        printing.
        """
        panel = pd.DataFrame(
            {"NORMAL": _alternating(0.01), "STUCK": _alternating(1e-9)}, index=INDEX
        )

        stuck_sigma = (
            panel["STUCK"].pct_change().tail(63).std(ddof=1) * math.sqrt(252)
        )
        assert stuck_sigma > 0.0, "the series must not be exactly constant"
        assert stuck_sigma < MIN_ANNUAL_VOLATILITY

        with pytest.raises(AllocationRefused, match="annualised volatility"):
            risk_balanced(panel, ["NORMAL", "STUCK"], benchmark="SPY")

    def test_an_exactly_constant_series_is_refused_too(self):
        panel = pd.DataFrame(
            {"NORMAL": _alternating(0.01), "FLAT": np.full(SESSIONS, 100.0)},
            index=INDEX,
        )

        with pytest.raises(AllocationRefused, match="annualised volatility"):
            risk_balanced(panel, ["NORMAL", "FLAT"], benchmark="SPY")


class TestEveryRuleRefusesRatherThanDegrades:
    @pytest.mark.parametrize(
        "rule", [equal_weight, top_measured, risk_balanced], ids=lambda f: f.__name__
    )
    def test_a_name_missing_from_the_panel_is_refused(self, rule):
        panel = pd.DataFrame(
            {"XLK": _trending(0.001), "XLE": _trending(0.001)}, index=INDEX
        )

        with pytest.raises(AllocationRefused, match="no column for GLD"):
            rule(panel, ["XLK", "XLE", "GLD"], benchmark="SPY")

    @pytest.mark.parametrize(
        "rule", [equal_weight, top_measured, risk_balanced], ids=lambda f: f.__name__
    )
    def test_short_history_is_refused(self, rule):
        short = pd.DataFrame(
            {
                "XLK": _trending(0.001)[: MIN_HISTORY_SESSIONS - 1],
                "XLE": _trending(0.001)[: MIN_HISTORY_SESSIONS - 1],
                "XLV": _trending(0.001)[: MIN_HISTORY_SESSIONS - 1],
            },
            index=INDEX[: MIN_HISTORY_SESSIONS - 1],
        )

        with pytest.raises(AllocationRefused, match="sessions of history"):
            rule(short, ["XLK", "XLE", "XLV"], benchmark="SPY")

    @pytest.mark.parametrize(
        "rule", [equal_weight, top_measured, risk_balanced], ids=lambda f: f.__name__
    )
    def test_a_hole_in_the_window_is_refused_not_dropped(self, rule):
        """Dropping the name would change the universe the decision records
        having chosen from, which is the only evidence of what it saw."""
        values = _trending(0.001).copy()
        values[-10] = np.nan
        panel = pd.DataFrame(
            {"XLK": _trending(0.002), "XLE": _trending(0.0015), "XLV": values},
            index=INDEX,
        )

        with pytest.raises(AllocationRefused, match="incomplete history for XLV"):
            rule(panel, ["XLK", "XLE", "XLV"], benchmark="SPY")


class TestTheNextSessionADecisionCanApplyTo:
    """`next_session` decides what a run stamps on the row, so it is the one
    place a stale panel could produce a decision about a session that has
    already happened."""

    def test_the_next_business_day_after_the_last_close(self):
        # Thursday close -> Friday.
        assert next_session(
            date(2026, 8, 13), today=date(2026, 8, 13)
        ) == date(2026, 8, 14)

    def test_a_friday_close_skips_the_weekend(self):
        assert next_session(
            date(2026, 8, 14), today=date(2026, 8, 14)
        ) == date(2026, 8, 17)

    def test_a_stale_panel_cannot_stamp_a_session_already_past(self):
        """The failure this guard exists for: a panel that stopped updating a
        week ago would otherwise date today's decision to last Tuesday, which is
        a decision recorded after its own outcome."""
        stale = date(2026, 8, 3)
        today = date(2026, 8, 13)

        chosen = next_session(stale, today=today)

        assert chosen > today
        assert chosen == date(2026, 8, 14)

    def test_the_chosen_session_is_never_a_weekend(self):
        for offset in range(14):
            day = date(2026, 8, 1) + timedelta(days=offset)
            assert next_session(day, today=day).weekday() < 5
