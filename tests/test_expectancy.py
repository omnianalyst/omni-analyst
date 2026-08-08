"""Realised expectancy, and the sample properties that make a good number lie.

Every figure asserted here was hand-derived and is written out, so a test
failure says which arithmetic changed rather than that an output moved.

The case this module exists for: `trend.sma` on crypto resolved 424 predictions
at a 34.2% hit rate -- an interval entirely below a coin flip -- while earning
+29.2 bps per trade on a 4.32:1 payoff. The gate barred on hit rate and refused
a profitable strategy. `test_a_losing_hit_rate_can_be_a_winning_strategy`
reproduces that shape in miniature.
"""

from __future__ import annotations

import dataclasses
from decimal import Decimal

import pytest

from omni.trading.expectancy import Expectancy, ResolvedTrade, compute


def _trade(
    *,
    entity: str = "BTC",
    direction: str = "up",
    outcome: str = "upper",
    entry: str = "100",
    upper: str = "104",
    lower: str = "99",
    horizon: str = "d1",
) -> ResolvedTrade:
    return ResolvedTrade(
        entity_key=entity,
        direction=direction,
        outcome=outcome,
        entry_price=Decimal(entry),
        upper_barrier=Decimal(upper),
        lower_barrier=Decimal(lower),
        horizon_key=horizon,
    )


class TestPnlSign:
    """The pairing of direction and outcome decides sign, never the outcome."""

    def test_a_long_hitting_the_upper_barrier_earns_the_target(self):
        # (104 - 100) / 100 = 400 bps
        assert _trade(direction="up", outcome="upper").pnl_bps == Decimal(400)

    def test_a_long_hitting_the_lower_barrier_pays_the_stop(self):
        # (100 - 99) / 100 = 100 bps, lost
        assert _trade(direction="up", outcome="lower").pnl_bps == Decimal(-100)

    def test_a_short_hitting_the_lower_barrier_earns_the_stop_distance(self):
        assert _trade(direction="down", outcome="lower").pnl_bps == Decimal(100)

    def test_a_short_hitting_the_upper_barrier_pays_the_target_distance(self):
        assert _trade(direction="down", outcome="upper").pnl_bps == Decimal(-400)

    def test_the_same_outcome_is_a_win_for_one_side_and_a_loss_for_the_other(self):
        """`lower` alone carries no sign. Reading it as one inverts half the book."""
        long_ = _trade(direction="up", outcome="lower").pnl_bps
        short = _trade(direction="down", outcome="lower").pnl_bps
        assert long_ < 0 < short

    def test_a_neutral_call_earns_nothing_at_either_barrier(self):
        assert _trade(direction="neutral", outcome="upper").pnl_bps == Decimal(0)
        assert _trade(direction="neutral", outcome="lower").pnl_bps == Decimal(0)


class TestAssumedPnl:
    def test_an_expiry_is_assumed_not_measured(self):
        trade = _trade(outcome="expiry")
        assert trade.is_assumed
        assert trade.pnl_bps == Decimal(0)

    def test_a_barrier_outcome_is_measured(self):
        assert not _trade(outcome="upper").is_assumed

    def test_the_assumed_share_is_reported_not_hidden(self):
        # 3 expiries in 4 -- the caller must be able to see that three quarters
        # of this result is a number nobody observed.
        trades = [_trade(outcome="expiry") for _ in range(3)] + [_trade()]
        result = compute(trades)
        assert result.assumed_n == 3
        assert result.assumed_share == Decimal(3) / Decimal(4)


class TestEffectiveSample:
    def test_correlated_assets_on_one_date_are_not_nine_observations(self):
        # Nine assets resolving on one horizon date. The raw count says 9; the
        # conservative reading says 1, because crypto assets move together.
        trades = [_trade(entity=f"A{i}", horizon="d1") for i in range(9)]
        result = compute(trades)
        assert result.n == 9
        assert result.effective_n == 1

    def test_distinct_horizons_raise_the_effective_sample(self):
        trades = [_trade(entity="BTC", horizon=f"d{i}") for i in range(9)]
        assert compute(trades).effective_n == 9

    def test_effective_n_never_exceeds_n(self):
        trades = [_trade(horizon=f"d{i}") for i in range(3)]
        result = compute(trades)
        assert result.effective_n <= result.n


class TestConcentration:
    def test_one_name_carrying_everything_reads_as_one(self):
        trades = [_trade(entity="BTC", outcome="upper")] + [
            _trade(entity=f"X{i}", outcome="expiry") for i in range(4)
        ]
        assert compute(trades).concentration == Decimal(1)

    def test_an_even_spread_reads_low(self):
        trades = [_trade(entity=f"X{i}", outcome="upper") for i in range(4)]
        assert compute(trades).concentration == Decimal(1) / Decimal(4)

    def test_a_big_winner_against_a_big_loser_is_still_concentrated(self):
        """Absolute contribution, so cancellation does not read as diversity.

        A book carried by one huge winner and one huge loser is not diversified
        just because the pooled mean looks moderate -- it is two bets.
        """
        trades = [
            _trade(entity="WIN", direction="up", outcome="upper"),
            _trade(entity="LOSE", direction="down", outcome="upper"),
            _trade(entity="FLAT", outcome="expiry"),
        ]
        assert compute(trades).concentration > Decimal("0.4")

    def test_positive_entities_counts_names_not_trades(self):
        trades = [
            _trade(entity="A", outcome="upper"),
            _trade(entity="B", outcome="lower"),
            _trade(entity="B", outcome="lower"),
        ]
        result = compute(trades)
        assert result.positive_entities == 1
        assert len(result.per_entity) == 2


class TestExpectancyItself:
    def test_a_losing_hit_rate_can_be_a_winning_strategy(self):
        """The defect this module exists to fix, in miniature.

        One win at +400 and two losses at -100 is a 33% hit rate -- below any
        plausible target -- and earns +66.7 bps per trade. Barring on hit rate
        refuses it; barring on expectancy does not.
        """
        trades = [
            _trade(direction="up", outcome="upper", horizon="d1"),
            _trade(direction="up", outcome="lower", horizon="d2"),
            _trade(direction="up", outcome="lower", horizon="d3"),
        ]
        result = compute(trades)
        hit_rate = Decimal(1) / Decimal(3)
        assert hit_rate < Decimal("0.5")
        # (400 - 100 - 100) / 3 = 66.666...
        assert result.gross_bps == Decimal(200) / Decimal(3)
        assert result.gross_bps > 0

    def test_a_winning_hit_rate_can_be_a_losing_strategy(self):
        """The mirror, and the reason the old gate was unsafe in both directions.

        Two wins at +100 and one loss at -400 is a 67% hit rate -- comfortably
        past a 0.6 target -- and loses 66.7 bps per trade.
        """
        trades = [
            _trade(direction="down", outcome="lower", horizon="d1"),
            _trade(direction="down", outcome="lower", horizon="d2"),
            _trade(direction="down", outcome="upper", horizon="d3"),
        ]
        result = compute(trades)
        hit_rate = Decimal(2) / Decimal(3)
        assert hit_rate > Decimal("0.6")
        assert result.gross_bps == Decimal(-200) / Decimal(3)
        assert result.gross_bps < 0

    def test_net_subtracts_the_round_trip(self):
        trades = [_trade(direction="up", outcome="upper")]
        result = compute(trades)
        assert result.gross_bps == Decimal(400)
        assert result.net_bps(Decimal(20)) == Decimal(380)

    def test_a_negative_round_trip_cost_is_refused(self):
        with pytest.raises(ValueError, match="cannot be a credit"):
            compute([_trade()]).net_bps(Decimal(-5))

    def test_pooling_is_per_trade_not_per_entity(self):
        """One asset with many predictions must not be levelled with a rare one.

        Weighting per entity would make a single BTC prediction equal in the
        pool to fifty ETH ones.
        """
        trades = [_trade(entity="A", outcome="upper", horizon=f"d{i}") for i in range(9)]
        trades.append(_trade(entity="B", direction="up", outcome="lower", horizon="dx"))
        result = compute(trades)
        # (9 * 400 - 100) / 10 = 350, not the entity mean of (400 + -100)/2 = 150
        assert result.gross_bps == Decimal(350)


class TestEmpty:
    def test_no_trades_yields_a_zeroed_result_rather_than_raising(self):
        result = compute([])
        assert result.n == 0
        assert result.effective_n == 0
        assert result.gross_bps == Decimal(0)
        assert result.concentration == Decimal(0)
        assert result.assumed_share == Decimal(0)


class TestConstruction:
    def test_barriers_must_straddle_entry(self):
        with pytest.raises(ValueError, match="must straddle"):
            _trade(entry="100", upper="99", lower="98")

    def test_a_pending_prediction_has_no_realised_pnl(self):
        with pytest.raises(ValueError, match="pending"):
            _trade(outcome="pending")

    def test_a_non_positive_entry_is_refused(self):
        with pytest.raises(ValueError, match="entry_price"):
            _trade(entry="0", upper="1", lower="-1")


def test_expectancy_is_frozen():
    """Frozen so a caller cannot adjust a measured result after reading it."""
    result = compute([_trade()])
    assert isinstance(result, Expectancy)
    with pytest.raises(dataclasses.FrozenInstanceError):
        result.n = 5  # type: ignore[misc]
