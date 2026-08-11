"""Pure tests for the fee curve and P&L math.

Numbers are pinned against hand-computed expectations. The fee formula is
the one piece of Polymarket-specific arithmetic the whole strategy rests on;
if a test here fails, the P&L is wrong everywhere downstream.
"""


import pytest

from omni.polymarket.pnl import (
    Fill,
    fee_for,
    size_for_notional,
    summarise,
)


class TestFeeCurve:
    def test_fee_peaks_at_half(self):
        peak = fee_for(100, 0.5, fee_rate=0.05)
        edge = fee_for(100, 0.05, fee_rate=0.05)
        assert peak > edge * 5  # roughly 25x by the p(1-p) curve

    def test_fee_zero_at_extremes(self):
        assert fee_for(100, 0.0) == 0.0
        assert fee_for(100, 1.0) == 0.0

    def test_fee_scales_with_size(self):
        assert fee_for(200, 0.5) == 2 * fee_for(100, 0.5)

    def test_maker_fee_zero(self):
        assert fee_for(1000, 0.5, taker=False) == 0.0

    def test_known_value_pinned(self):
        # 100 shares * 0.05 rate * 0.5 * 0.5 = 1.25 USD
        assert fee_for(100, 0.5, fee_rate=0.05) == pytest.approx(1.25)

    def test_negative_size_refused(self):
        with pytest.raises(ValueError):
            fee_for(-1, 0.5)

    def test_out_of_range_price_refused(self):
        with pytest.raises(ValueError):
            fee_for(100, 1.5)


class TestSizeForNotional:
    def test_known_split(self):
        # $5 at p=0.5 buys 10 shares
        assert size_for_notional(5.0, 0.5) == pytest.approx(10.0)

    def test_cheap_price_more_shares(self):
        assert size_for_notional(5.0, 0.1) == pytest.approx(50.0)


class TestFillMath:
    def test_yes_winner_pnl(self):
        # Bought 10 YES shares at 0.4 ($4 cost). YES wins => $10 payoff.
        f = Fill(direction="YES", entry_price=0.4, size_shares=10.0,
                 outcome_yes=True, fee_rate=0.0, taker=False)
        assert f.cost == pytest.approx(4.0)
        assert f.payoff == pytest.approx(10.0)
        assert f.gross_pnl == pytest.approx(6.0)
        assert f.fee_pnl == 0.0
        assert f.net_pnl == pytest.approx(6.0)
        assert f.roi_pct == pytest.approx(150.0)

    def test_yes_loser_pnl(self):
        # Bought 10 YES shares at 0.4. NO wins => $0 payoff.
        f = Fill(direction="YES", entry_price=0.4, size_shares=10.0,
                 outcome_yes=False, fee_rate=0.0, taker=False)
        assert f.payoff == 0.0
        assert f.gross_pnl == pytest.approx(-4.0)
        assert f.roi_pct == pytest.approx(-100.0)

    def test_no_winner_pnl(self):
        # Bought 10 NO shares at 0.6 ($6 cost). NO wins => $10 payoff.
        f = Fill(direction="NO", entry_price=0.6, size_shares=10.0,
                 outcome_yes=False, fee_rate=0.0, taker=False)
        assert f.payoff == pytest.approx(10.0)
        assert f.gross_pnl == pytest.approx(4.0)

    def test_taker_fee_applied(self):
        # Same YES win, but with taker fee at 0.05 rate.
        f = Fill(direction="YES", entry_price=0.5, size_shares=10.0,
                 outcome_yes=True, fee_rate=0.05, taker=True)
        # fee = 10 * 0.05 * 0.5 * 0.5 = 0.125
        assert f.fee_pnl == pytest.approx(-0.125)
        assert f.net_pnl < f.gross_pnl

    def test_maker_fee_zero(self):
        f = Fill(direction="YES", entry_price=0.5, size_shares=10.0,
                 outcome_yes=True, fee_rate=0.05, taker=False)
        assert f.fee_pnl == 0.0

    def test_bad_direction_refused(self):
        with pytest.raises(ValueError):
            Fill(direction="MAYBE", entry_price=0.5, size_shares=10.0,
                 outcome_yes=True, fee_rate=0.0, taker=False)

    def test_nan_entry_refused(self):
        with pytest.raises(ValueError):
            Fill(direction="YES", entry_price=float("nan"), size_shares=10.0,
                 outcome_yes=True, fee_rate=0.0, taker=False)


class TestSummarise:
    def _fill(self, gross, cost=5.0, net=None):
        if net is None:
            net = gross
        class _F:
            self_gross = gross
            self_cost = cost
            self_net = net
            @property
            def gross_pnl(self): return gross
            @property
            def fee_pnl(self): return net - gross
            @property
            def net_pnl(self): return net
            @property
            def cost(self): return cost
            @property
            def roi_pct(self): return (net / cost * 100) if cost else 0
        return _F()

    def test_empty_returns_zeros(self):
        s = summarise([])
        assert s.n_closed == 0
        assert s.win_rate is None
        assert s.avg_roi_pct is None

    def test_simple_two_trades(self):
        fills = [
            Fill(direction="YES", entry_price=0.5, size_shares=10.0,
                 outcome_yes=True, fee_rate=0.0, taker=False),  # +5
            Fill(direction="YES", entry_price=0.5, size_shares=10.0,
                 outcome_yes=False, fee_rate=0.0, taker=False),  # -5
        ]
        s = summarise(fills)
        assert s.n_closed == 2
        assert s.n_wins == 1
        assert s.win_rate == pytest.approx(0.5)
        assert s.net_pnl == pytest.approx(0.0)
        assert s.avg_roi_pct == pytest.approx(0.0)

    def test_drawdown_calculation(self):
        # Win $10, then lose $5, then lose $5. Peak = 10, trough = 0.
        fills = [
            Fill(direction="YES", entry_price=0.5, size_shares=20.0,
                 outcome_yes=True, fee_rate=0.0, taker=False),  # +10
            Fill(direction="YES", entry_price=0.5, size_shares=10.0,
                 outcome_yes=False, fee_rate=0.0, taker=False),  # -5
            Fill(direction="YES", entry_price=0.5, size_shares=10.0,
                 outcome_yes=False, fee_rate=0.0, taker=False),  # -5
        ]
        s = summarise(fills)
        assert s.worst_drawdown_usd == pytest.approx(10.0)
