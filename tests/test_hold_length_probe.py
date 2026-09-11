"""Tests for the hold-length probe simulation.

The simulation is the part that matters: a vectorised walk over a funding panel
that ranks, selects, and sums realised carry. These tests feed small synthetic
panels where the answer is knowable by hand and check the arithmetic.

The mutation check: swap the `nlargest` for `nsmallest`, or the hold window sum
for a mean, or the cost annualisation for a flat subtraction, and at least one
assertion below fails.
"""

from __future__ import annotations

import sys
from decimal import Decimal
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from ops.hold_length_probe import _sample_statistics, simulate


def _panel(rates: dict[str, list[float]], n_settlements: int) -> pd.DataFrame:
    """Build a funding panel from per-asset rate sequences.

    Shorter sequences are right-aligned (the asset joins partway through).
    """
    data = {}
    for asset, seq in rates.items():
        col = [np.nan] * n_settlements
        offset = n_settlements - len(seq)
        for i, val in enumerate(seq):
            col[offset + i] = val
        data[asset] = col
    idx = pd.date_range("2024-01-01", periods=n_settlements, freq="h", tz="UTC")
    return pd.DataFrame(data, index=idx)


class TestSimulate:
    def test_zero_funding_produces_negative_cost_only(self):
        n = 500
        panel = _panel({"BTC": [0.0] * n, "ETH": [0.0] * n}, n)
        r = simulate(panel, hold_days=7, settlement_hours=1, lookback_days=1, enter_rank=1, cost_bps=Decimal(28))

        assert r["n_periods"] > 0
        # No funding, only cost drag: -(28/100) * (365/7) = -14.6 %/yr
        expected_cost = 28.0 / 100 * (365.0 / 7)
        assert r["mean_pct_yr"] == pytest.approx(-expected_cost, abs=0.01)

    def test_constant_positive_funding_beats_cost_at_long_hold(self):
        rate = 0.0001  # per settlement
        n = 500
        panel = _panel({"BTC": [rate] * n}, n)
        r = simulate(
            panel, hold_days=14, settlement_hours=1, lookback_days=1, enter_rank=1, cost_bps=Decimal(10)
        )
        # Annualised funding: 0.0001 * 8760 * 100 = 87.6 %/yr
        # Cost: 10/100 * 365/14 = 2.607 %/yr
        # Net: ~85.0 %/yr
        assert r["mean_pct_yr"] == pytest.approx(85.0, abs=0.1)

    def test_ranks_by_trailing_funding_not_forward(self):
        # BTC pays more in the lookback window, ETH pays more during the hold.
        # The selector should pick BTC (higher trailing), and the realised
        # return reflects ETH's rate during the hold -- except the selector
        # already committed to BTC.
        n = 200
        btc = [0.0001] * 200
        eth = [0.0] * 100 + [0.0002] * 100
        panel = _panel({"BTC": btc, "ETH": eth}, n)
        r = simulate(
            panel, hold_days=3, settlement_hours=1, lookback_days=2, enter_rank=1, cost_bps=Decimal(0)
        )
        # The lookback at t=48 (2*24) sees BTC at 0.0001 > ETH at 0.0.
        # Selector picks BTC. During hold, BTC pays 0.0001/settlement.
        # Annualised: 0.0001 * 8760 * 100 = 87.6 %/yr
        assert r["mean_pct_yr"] == pytest.approx(87.6, abs=0.5)

    def test_insufficient_data_returns_zero_periods(self):
        panel = _panel({"BTC": [0.01] * 10}, 10)
        r = simulate(panel, hold_days=42, settlement_hours=1, lookback_days=7, enter_rank=1)
        assert r["n_periods"] == 0
        assert r["mean_pct_yr"] is None

    def test_empty_panel_returns_zero_periods(self):
        r = simulate(pd.DataFrame(), hold_days=42, settlement_hours=1, lookback_days=7, enter_rank=1)
        assert r["n_periods"] == 0

    def test_enter_rank_two_averages_top_two(self):
        n = 500
        btc = [0.0002] * n
        eth = [0.0001] * n
        sol = [0.0] * n
        panel = _panel({"BTC": btc, "ETH": eth, "SOL": sol}, n)
        r = simulate(
            panel, hold_days=7, settlement_hours=1, lookback_days=1, enter_rank=2, cost_bps=Decimal(0)
        )
        # Top 2 are BTC (0.0002) and ETH (0.0001). Average = 0.00015.
        # Annualised: 0.00015 * 8760 * 100 = 131.4 %/yr
        assert r["mean_pct_yr"] == pytest.approx(131.4, abs=0.5)

    def test_longer_hold_reduces_cost_drag(self):
        rate = 0.00005
        n = 2000
        panel = _panel({"BTC": [rate] * n}, n)

        r_short = simulate(
            panel, hold_days=7, settlement_hours=1, lookback_days=1, enter_rank=1, cost_bps=Decimal(28)
        )
        r_long = simulate(
            panel, hold_days=42, settlement_hours=1, lookback_days=1, enter_rank=1, cost_bps=Decimal(28)
        )

        # Both have the same gross (constant funding), so the difference is
        # purely the cost amortisation. Longer hold => less drag => higher net.
        assert r_long["mean_pct_yr"] > r_short["mean_pct_yr"]

        cost_short = 28.0 / 100 * (365.0 / 7)
        cost_long = 28.0 / 100 * (365.0 / 42)
        diff_expected = cost_short - cost_long
        diff_actual = r_long["mean_pct_yr"] - r_short["mean_pct_yr"]
        assert diff_actual == pytest.approx(diff_expected, abs=0.01)

    def test_a_panel_with_missing_measurements_is_refused(self):
        """U26: an all-NaN asset column used to be silently dropped from
        selection; a fabricated zero mean was the alternative. Both are
        inventions -- the panel itself is refused."""
        n = 500
        panel = _panel({"BTC": [0.0001] * n, "ETH": [np.nan] * n}, n)
        with pytest.raises(ValueError, match="missing or non-finite"):
            simulate(
                panel, hold_days=7, settlement_hours=1, lookback_days=1,
                enter_rank=2, cost_bps=Decimal(0),
            )


class TestTheGridAndStatisticsAreHonest:
    """U26: settlement counts are not days, undefined statistics are withheld,
    and the final exactly-complete window is measured."""

    def test_a_gapped_grid_is_refused(self):
        n = 500
        panel = _panel({"BTC": [0.0001] * n}, n)
        gapped = panel.drop(panel.index[100:103])
        with pytest.raises(ValueError, match="missing or irregular"):
            simulate(
                gapped, hold_days=7, settlement_hours=1, lookback_days=1,
                enter_rank=1,
            )

    def test_a_constant_series_has_no_t_statistic(self):
        n = 500
        panel = _panel({"BTC": [0.0001] * n}, n)
        r = simulate(
            panel, hold_days=7, settlement_hours=1, lookback_days=1,
            enter_rank=1, cost_bps=Decimal(0),
        )
        assert r["n_periods"] > 0
        assert r["t_stat"] is None
        assert r["recent_third_t"] is None
        assert r["mean_pct_yr"] == pytest.approx(87.6, abs=0.1)

    def test_an_eight_hour_venue_uses_eight_hours(self):
        n = 600
        idx = pd.date_range("2024-01-01", periods=n, freq="8h", tz="UTC")
        panel = pd.DataFrame({"BTC": [0.0003] * n}, index=idx)
        r = simulate(
            panel, hold_days=7, settlement_hours=8, lookback_days=1,
            enter_rank=1, cost_bps=Decimal(0),
        )
        # 0.0003 per 8h settlement x 3/day x 365 x 100 = 32.85 %/yr
        assert r["mean_pct_yr"] == pytest.approx(32.85, abs=0.1)

    def test_the_final_exactly_complete_window_is_measured(self):
        # lookback 1 day (24 rows) + 3 windows of 7 days = 240 rows exactly.
        n = 24 + 3 * 24 * 7
        panel = _panel({"BTC": [0.0001] * n}, n)
        r = simulate(
            panel, hold_days=7, settlement_hours=1, lookback_days=1,
            enter_rank=1, cost_bps=Decimal(0),
        )
        assert r["n_periods"] == 3

    def test_sample_statistics_withhold_undefined_values(self):
        constant = np.full(5, 0.01)
        mean, _std, t = _sample_statistics(constant)
        assert mean == 0.01
        assert t is None
        singleton = np.array([0.01])
        assert _sample_statistics(singleton) == (0.01, None, None)
        varying = np.array([0.01, 0.02, 0.03, 0.04])
        mean, _std, t = _sample_statistics(varying)
        assert mean == pytest.approx(0.025)
        assert t is not None and t != 0.0
