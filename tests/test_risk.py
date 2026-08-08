"""The risk engine, tested for the thing that makes it worth having: refusal.

A permissive risk check passes almost any test written the obvious way, because
"the good trade was allowed" is satisfied by `return allowed`. So the weight
here sits on pairs: a scenario that must be refused next to the smallest
variation of it that must be allowed. Neither assertion means much alone; a
trivially-permissive engine fails the first and a trivially-refusing engine
fails the second, and only an engine that actually computes the limit passes
both.

Three properties get the most attention because they are the ones a plausible
implementation gets wrong:

- **Unknown is not zero.** A correlation that was never measured has to count
  against the concentration limit. An implementation that reads a missing pair
  as 0.0 passes every correlation test written with explicit correlations and
  still lets a portfolio become one bet in six tickers.
- **Exposure is measured after the intent, against existing positions.** An
  engine that scores the intent's notional in isolation blocks the sell that
  flattens a too-large long and waves through the buy that doubles it.
- **Every optional argument omitted must refuse.** This is the only test that
  can catch the whole class of bug where a caller forgets to wire an input and
  the engine silently treats the absence as "fine".
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from omni.portfolio.risk import (
    RiskLimits,
    RiskRefusal,
    RiskVerdict,
    check,
)
from omni.venue.protocol import MarketType, Position, Side, TradeIntent

NOW = datetime(2026, 3, 1, 12, 0, tzinfo=UTC)
FRESH = NOW - timedelta(seconds=30)


@dataclass(frozen=True)
class FakeState:
    """The structural minimum `check` reads: a NAV and a list of positions.

    Deliberately not `portfolio.state.PortfolioState` -- risk must be callable
    against a reconstructed backtest snapshot as readily as against live state,
    and a test that could only be written against the concrete class would be
    evidence the coupling had crept back in.
    """

    nav: Decimal
    positions: tuple[Position, ...] = ()


def _position(
    symbol: str,
    quantity: str,
    entry: str,
    *,
    venue: str = "paper",
) -> Position:
    return Position(
        venue=venue,
        symbol=symbol,
        market_type=MarketType.SPOT,
        quantity=Decimal(quantity),
        average_entry=Decimal(entry),
        as_of=NOW,
    )


def _intent(
    *,
    symbol: str = "BTC/USD",
    side: Side = Side.BUY,
    quantity: str = "1",
    price: str = "5000",
) -> TradeIntent:
    return TradeIntent(
        venue="paper",
        symbol=symbol,
        side=side,
        market_type=MarketType.SPOT,
        quantity=Decimal(quantity),
        reference_price=Decimal(price),
    )


def _limits(**overrides) -> RiskLimits:
    base = {
        "max_position_pct_nav": Decimal("0.10"),
        "max_gross_exposure_pct_nav": Decimal("0.50"),
        "max_net_exposure_pct_nav": Decimal("0.30"),
        "max_positions": 5,
        "max_correlated_exposure_pct_nav": Decimal("0.20"),
        "correlation_threshold": Decimal("0.60"),
        "min_notional": Decimal(100),
        "max_notional": Decimal(50_000),
        "daily_loss_limit_pct_nav": Decimal("0.02"),
        "max_drawdown_pct": Decimal("0.10"),
        "max_data_age": timedelta(minutes=5),
    }
    return RiskLimits(**{**base, **overrides})


def _check(intent=None, state=None, limits=None, **overrides):
    """A fully-specified, clean call. Tests override exactly what they test.

    Every optional argument is supplied here on purpose: a test that varies one
    input against a clean baseline is only meaningful if the baseline is
    allowed, and the baseline is only allowed if nothing else is missing.
    """
    kwargs = {
        "correlations": {},
        "data_as_of": FRESH,
        "now": NOW,
        "realised_pnl_today": Decimal(0),
        "peak_nav": Decimal(100_000),
        "halted": False,
        "reconciled": True,
    }
    kwargs.update(overrides)
    return check(
        intent if intent is not None else _intent(),
        state if state is not None else FakeState(nav=Decimal(100_000)),
        limits if limits is not None else _limits(),
        **kwargs,
    )


# --- the baseline must be permitted, or every refusal test below is vacuous ---


def test_a_clean_fully_specified_intent_is_allowed():
    verdict = _check()
    assert verdict.allowed is True
    assert verdict.refusals == ()
    assert bool(verdict) is True


# --- fail closed ---


def test_every_optional_argument_omitted_refuses():
    """The headline property: absence of input is never absence of risk.

    Nothing about this intent is wrong. It is 5% of NAV against a portfolio
    holding nothing, well inside every limit configured. It is refused purely
    because the caller did not say how old the data was, what today's P&L was,
    or where the high-water mark sits -- which is the correct answer to "should
    I trade on facts I do not have".
    """
    verdict = check(
        _intent(),
        FakeState(nav=Decimal(100_000)),
        _limits(),
    )

    assert verdict.allowed is False
    assert not verdict
    assert RiskRefusal.STALE_DATA in verdict.refusals
    assert RiskRefusal.DAILY_PNL_UNKNOWN in verdict.refusals
    assert RiskRefusal.PEAK_NAV_UNKNOWN in verdict.refusals


def test_no_limits_configured_refuses():
    verdict = check(
        _intent(),
        FakeState(nav=Decimal(100_000)),
        None,
        data_as_of=FRESH,
        now=NOW,
        realised_pnl_today=Decimal(0),
        peak_nav=Decimal(100_000),
    )

    assert verdict.allowed is False
    assert RiskRefusal.NO_LIMITS_CONFIGURED in verdict.refusals


def test_no_state_available_refuses():
    verdict = check(
        _intent(),
        None,
        _limits(),
        data_as_of=FRESH,
        now=NOW,
        realised_pnl_today=Decimal(0),
        peak_nav=Decimal(100_000),
    )

    assert verdict.allowed is False
    assert RiskRefusal.NO_STATE_AVAILABLE in verdict.refusals


def test_halt_refuses_an_otherwise_perfect_intent():
    baseline = _check()
    assert baseline.allowed is True

    verdict = _check(halted=True)

    assert verdict.allowed is False
    assert verdict.refusals == (RiskRefusal.TRADING_HALTED,)


def test_reconciliation_divergence_refuses_an_otherwise_perfect_intent():
    verdict = _check(reconciled=False)

    assert verdict.allowed is False
    assert verdict.refusals == (RiskRefusal.RECONCILIATION_DIVERGENCE,)


def test_zero_nav_refuses_because_a_fraction_of_nothing_bounds_nothing():
    verdict = _check(state=FakeState(nav=Decimal(0)))

    assert verdict.allowed is False
    assert RiskRefusal.NO_STATE_AVAILABLE in verdict.refusals


def test_negative_nav_refuses():
    verdict = _check(state=FakeState(nav=Decimal(-1)))

    assert verdict.allowed is False
    assert RiskRefusal.NO_STATE_AVAILABLE in verdict.refusals


def test_nan_nav_refuses_rather_than_raising_a_decimal_error():
    """Every ordering comparison against a Decimal NaN raises, not returns.

    So `nav <= 0` on a NaN NAV does not fall through to "NAV is fine", it
    raises `InvalidOperation` from inside a limit check. Screening for it turns
    a stack trace at 3am into a named refusal.
    """
    verdict = _check(state=FakeState(nav=Decimal("NaN")))

    assert verdict.allowed is False
    assert RiskRefusal.NO_STATE_AVAILABLE in verdict.refusals


def test_non_finite_position_quantity_refuses():
    state = FakeState(
        nav=Decimal(100_000),
        positions=(_position("ETH/USD", "NaN", "3000"),),
    )

    verdict = _check(state=state)

    assert verdict.allowed is False
    assert RiskRefusal.NO_STATE_AVAILABLE in verdict.refusals
    assert "ETH/USD" in verdict.detail


# --- freshness ---


def test_missing_data_timestamp_is_stale():
    verdict = _check(data_as_of=None)

    assert verdict.allowed is False
    assert RiskRefusal.STALE_DATA in verdict.refusals


def test_data_older_than_the_tolerance_is_stale_and_inside_it_is_not():
    stale = _check(data_as_of=NOW - timedelta(minutes=6))
    fresh = _check(data_as_of=NOW - timedelta(minutes=4))

    assert RiskRefusal.STALE_DATA in stale.refusals
    assert fresh.allowed is True


def test_data_stamped_in_the_future_is_refused():
    verdict = _check(data_as_of=NOW + timedelta(minutes=1))

    assert verdict.allowed is False
    assert RiskRefusal.STALE_DATA in verdict.refusals


def test_naive_and_aware_timestamps_cannot_be_aged_so_the_data_is_stale():
    verdict = _check(data_as_of=FRESH.replace(tzinfo=None))

    assert verdict.allowed is False
    assert RiskRefusal.STALE_DATA in verdict.refusals


# --- size and exposure ---


def test_position_too_large_names_that_refusal_and_a_smaller_one_passes():
    too_big = _check(intent=_intent(quantity="3"))
    ok = _check(intent=_intent(quantity="2"))

    assert RiskRefusal.POSITION_TOO_LARGE in too_big.refusals
    assert ok.allowed is True


def test_size_is_measured_on_the_resulting_position_not_the_intent_alone():
    """An add-to-a-winner is the trade the limit exists to stop.

    Both intents below are 5% of NAV in isolation and both would pass an engine
    that looked only at the order. Adding to an existing 6% long takes the
    position to 11% against a 10% cap; the same order in a name held flat does
    not.
    """
    held = FakeState(
        nav=Decimal(100_000),
        positions=(_position("BTC/USD", "1.2", "5000"),),
    )
    flat = FakeState(nav=Decimal(100_000))

    adding = _check(intent=_intent(quantity="1"), state=held)
    opening = _check(intent=_intent(quantity="1"), state=flat)

    assert RiskRefusal.POSITION_TOO_LARGE in adding.refusals
    assert opening.allowed is True


def test_a_sell_that_reduces_an_oversized_long_is_not_blocked_by_the_size_limit():
    """The same arithmetic, run the other way -- de-risking must stay possible.

    An engine that scores every intent as fresh exposure refuses this, which is
    the limit working backwards: it would trap a position above its cap.
    """
    held = FakeState(
        nav=Decimal(100_000),
        positions=(_position("BTC/USD", "2.4", "5000"),),
    )

    verdict = _check(intent=_intent(side=Side.SELL, quantity="1"), state=held)

    assert RiskRefusal.POSITION_TOO_LARGE not in verdict.refusals


def test_gross_exposure_counts_shorts_while_net_exposure_nets_them():
    """The two limits must not be the same number wearing different names.

    This book is long 20k of ETH and short 20k of SOL. Gross is 45k after a 5k
    buy; net is 5k. A 40% gross cap is breached and a 30% net cap is not, and an
    implementation that computed one from the other could not produce that.
    """
    state = FakeState(
        nav=Decimal(100_000),
        positions=(
            _position("ETH/USD", "5", "4000"),
            _position("SOL/USD", "-200", "100"),
        ),
    )

    verdict = _check(
        state=state,
        limits=_limits(
            max_gross_exposure_pct_nav=Decimal("0.40"),
            max_net_exposure_pct_nav=Decimal("0.30"),
            max_correlated_exposure_pct_nav=Decimal(1),
        ),
        correlations={
            ("BTC/USD", "ETH/USD"): Decimal("0.1"),
            ("BTC/USD", "SOL/USD"): Decimal("0.1"),
        },
    )

    assert RiskRefusal.GROSS_EXPOSURE_EXCEEDED in verdict.refusals
    assert RiskRefusal.NET_EXPOSURE_EXCEEDED not in verdict.refusals


def test_net_exposure_is_breached_by_a_one_sided_book_that_clears_gross():
    """The mirror of the test above, so neither limit can be a stub.

    Long 20k ETH and long 20k SOL: gross 45k clears a 60% cap, net 45k breaches
    a 30% one.
    """
    state = FakeState(
        nav=Decimal(100_000),
        positions=(
            _position("ETH/USD", "5", "4000"),
            _position("SOL/USD", "200", "100"),
        ),
    )

    verdict = _check(
        state=state,
        limits=_limits(
            max_gross_exposure_pct_nav=Decimal("0.60"),
            max_net_exposure_pct_nav=Decimal("0.30"),
            max_correlated_exposure_pct_nav=Decimal(1),
        ),
        correlations={
            ("BTC/USD", "ETH/USD"): Decimal("0.1"),
            ("BTC/USD", "SOL/USD"): Decimal("0.1"),
        },
    )

    assert RiskRefusal.NET_EXPOSURE_EXCEEDED in verdict.refusals
    assert RiskRefusal.GROSS_EXPOSURE_EXCEEDED not in verdict.refusals


def test_net_exposure_limit_is_two_sided():
    """A 45k *short* book is as far from flat as a 45k long one."""
    state = FakeState(
        nav=Decimal(100_000),
        positions=(
            _position("ETH/USD", "-10", "4000"),
            _position("SOL/USD", "-100", "100"),
        ),
    )

    verdict = _check(
        intent=_intent(side=Side.SELL),
        state=state,
        limits=_limits(
            max_gross_exposure_pct_nav=Decimal("0.60"),
            max_net_exposure_pct_nav=Decimal("0.30"),
            max_correlated_exposure_pct_nav=Decimal(1),
        ),
        correlations={
            ("BTC/USD", "ETH/USD"): Decimal("0.1"),
            ("BTC/USD", "SOL/USD"): Decimal("0.1"),
        },
    )

    assert RiskRefusal.NET_EXPOSURE_EXCEEDED in verdict.refusals


# --- position count ---


def test_opening_one_position_too_many_is_refused():
    state = FakeState(
        nav=Decimal(100_000),
        positions=(
            _position("ETH/USD", "1", "3000"),
            _position("SOL/USD", "10", "100"),
        ),
    )

    verdict = _check(
        state=state,
        limits=_limits(max_positions=2, max_correlated_exposure_pct_nav=Decimal(1)),
        correlations={
            ("BTC/USD", "ETH/USD"): Decimal("0.1"),
            ("BTC/USD", "SOL/USD"): Decimal("0.1"),
        },
    )

    assert RiskRefusal.TOO_MANY_POSITIONS in verdict.refusals


def test_adding_to_a_held_symbol_does_not_open_a_new_slot():
    """Position *count* is by symbol, not by order.

    At the cap, the engine must still permit trading the names already held --
    otherwise hitting `max_positions` freezes the book rather than closing it
    to new names.
    """
    state = FakeState(
        nav=Decimal(100_000),
        positions=(
            _position("BTC/USD", "0.5", "5000"),
            _position("ETH/USD", "1", "3000"),
        ),
    )

    verdict = _check(
        intent=_intent(quantity="1"),
        state=state,
        limits=_limits(
            max_positions=2,
            max_position_pct_nav=Decimal("0.10"),
            max_correlated_exposure_pct_nav=Decimal(1),
        ),
        correlations={("BTC/USD", "ETH/USD"): Decimal("0.1")},
    )

    assert RiskRefusal.TOO_MANY_POSITIONS not in verdict.refusals


def test_a_closed_position_does_not_occupy_a_slot():
    state = FakeState(
        nav=Decimal(100_000),
        positions=(
            _position("ETH/USD", "0", "0"),
            _position("SOL/USD", "10", "100"),
        ),
    )

    verdict = _check(
        state=state,
        limits=_limits(max_positions=2, max_correlated_exposure_pct_nav=Decimal(1)),
        correlations={("BTC/USD", "SOL/USD"): Decimal("0.1")},
    )

    assert RiskRefusal.TOO_MANY_POSITIONS not in verdict.refusals


def test_a_book_of_shorts_still_fills_the_position_ceiling():
    """A short occupies a slot. The count is of open names, not of longs.

    Every other count test above holds only longs, so counting `quantity > 0`
    rather than `quantity != 0` passes all of them. Under that rule a short-only
    book reports zero open positions and can never reach the ceiling at all --
    the limit is deleted for exactly the book that is hardest to unwind.

    Refused at a cap of 2 and allowed at 3, so neither a permissive engine nor a
    uniformly-refusing one satisfies both halves, and the detail pins the count
    at 3 rather than merely at "too many".
    """
    state = FakeState(
        nav=Decimal(100_000),
        positions=(
            _position("ETH/USD", "-1", "3000"),
            _position("SOL/USD", "-10", "100"),
        ),
    )
    correlations = {
        ("BTC/USD", "ETH/USD"): Decimal("0.1"),
        ("BTC/USD", "SOL/USD"): Decimal("0.1"),
    }

    at_the_cap = _check(
        intent=_intent(side=Side.SELL),
        state=state,
        limits=_limits(max_positions=2, max_correlated_exposure_pct_nav=Decimal(1)),
        correlations=correlations,
    )
    inside_it = _check(
        intent=_intent(side=Side.SELL),
        state=state,
        limits=_limits(max_positions=3, max_correlated_exposure_pct_nav=Decimal(1)),
        correlations=correlations,
    )

    assert RiskRefusal.TOO_MANY_POSITIONS in at_the_cap.refusals
    assert "3 open positions" in at_the_cap.detail
    assert inside_it.allowed is True


# --- correlation, and the missing-pair rule ---


def _correlation_state() -> FakeState:
    return FakeState(
        nav=Decimal(100_000),
        positions=(_position("ETH/USD", "2", "3000"),),
    )


def _correlation_limits() -> RiskLimits:
    return _limits(
        max_position_pct_nav=Decimal("0.15"),
        max_correlated_exposure_pct_nav=Decimal("0.15"),
        correlation_threshold=Decimal("0.60"),
    )


def test_correlated_exposure_is_refused_and_uncorrelated_exposure_is_not():
    """One pair of numbers, two answers, decided only by the correlation.

    10k of BTC alongside 6k of ETH is 16k against a 15k concentration cap when
    the two move together, and 10k when they do not. Same book, same order.
    """
    correlated = _check(
        intent=_intent(quantity="2"),
        state=_correlation_state(),
        limits=_correlation_limits(),
        correlations={("BTC/USD", "ETH/USD"): Decimal("0.85")},
    )
    independent = _check(
        intent=_intent(quantity="2"),
        state=_correlation_state(),
        limits=_correlation_limits(),
        correlations={("BTC/USD", "ETH/USD"): Decimal("0.10")},
    )

    assert RiskRefusal.CORRELATION_LIMIT_EXCEEDED in correlated.refusals
    assert independent.allowed is True


def test_an_unmeasured_pair_is_assumed_correlated():
    """The rule that stops a portfolio becoming one bet in six tickers.

    An implementation reading a missing pair as 0.0 passes every test written
    with explicit correlations and fails only this one -- and in production it
    would clear an entire book of un-analysed names as mutually independent.
    """
    empty_map = _check(
        intent=_intent(quantity="2"),
        state=_correlation_state(),
        limits=_correlation_limits(),
        correlations={},
    )
    no_map = _check(
        intent=_intent(quantity="2"),
        state=_correlation_state(),
        limits=_correlation_limits(),
        correlations=None,
    )
    wrong_pair_only = _check(
        intent=_intent(quantity="2"),
        state=_correlation_state(),
        limits=_correlation_limits(),
        correlations={("SOL/USD", "DOGE/USD"): Decimal("0.1")},
    )

    for verdict in (empty_map, no_map, wrong_pair_only):
        assert RiskRefusal.CORRELATION_LIMIT_EXCEEDED in verdict.refusals
        assert "unmeasured" in verdict.detail


def test_correlation_lookup_is_symmetric():
    """rho(a, b) == rho(b, a), so key order must not decide a refusal."""
    verdict = _check(
        intent=_intent(quantity="2"),
        state=_correlation_state(),
        limits=_correlation_limits(),
        correlations={("ETH/USD", "BTC/USD"): Decimal("0.10")},
    )

    assert verdict.allowed is True


def test_a_strongly_anticorrelated_name_counts_toward_concentration():
    """Long BTC against short ETH at rho -0.9 is one bet, not two.

    Summing absolute notionals cannot see which way each leg points, so reading
    -0.9 as "uncorrelated" would let the concentration limit be bypassed by
    inverting a leg.
    """
    verdict = _check(
        intent=_intent(quantity="2"),
        state=_correlation_state(),
        limits=_correlation_limits(),
        correlations={("BTC/USD", "ETH/USD"): Decimal("-0.90")},
    )

    assert RiskRefusal.CORRELATION_LIMIT_EXCEEDED in verdict.refusals


@pytest.mark.parametrize("rho", ["NaN", "1.5", "-2"])
def test_an_impossible_correlation_is_treated_as_unmeasured(rho: str):
    """A rho outside [-1, 1] or a NaN is a broken estimate, not a low one.

    Passing it through would be worse than a missing pair, because a NaN
    compares false against the threshold and would silently read as
    "uncorrelated".
    """
    verdict = _check(
        intent=_intent(quantity="2"),
        state=_correlation_state(),
        limits=_correlation_limits(),
        correlations={("BTC/USD", "ETH/USD"): Decimal(rho)},
    )

    assert RiskRefusal.CORRELATION_LIMIT_EXCEEDED in verdict.refusals


# --- notional band ---


def test_notional_below_the_minimum_is_refused():
    verdict = _check(intent=_intent(quantity="0.01", price="5000"))

    assert RiskRefusal.BELOW_MIN_NOTIONAL in verdict.refusals


def test_notional_above_the_maximum_is_refused():
    verdict = _check(
        intent=_intent(quantity="12", price="5000"),
        state=FakeState(nav=Decimal(10_000_000)),
        peak_nav=Decimal(10_000_000),
    )

    assert RiskRefusal.ABOVE_MAX_NOTIONAL in verdict.refusals
    assert RiskRefusal.POSITION_TOO_LARGE not in verdict.refusals


# --- kill switches ---


def test_daily_loss_beyond_the_limit_halts_and_a_smaller_loss_does_not():
    breached = _check(realised_pnl_today=Decimal(-2001))
    inside = _check(realised_pnl_today=Decimal(-1999))

    assert RiskRefusal.DAILY_LOSS_LIMIT_HIT in breached.refusals
    assert inside.allowed is True


def test_a_profitable_day_never_trips_the_loss_limit():
    verdict = _check(realised_pnl_today=Decimal(50_000))

    assert verdict.allowed is True


def test_unknown_daily_pnl_is_refused_rather_than_assumed_flat():
    verdict = _check(realised_pnl_today=None)

    assert verdict.allowed is False
    assert RiskRefusal.DAILY_PNL_UNKNOWN in verdict.refusals
    assert RiskRefusal.DAILY_LOSS_LIMIT_HIT not in verdict.refusals


def test_nan_daily_pnl_is_refused():
    verdict = _check(realised_pnl_today=Decimal("NaN"))

    assert verdict.allowed is False
    assert RiskRefusal.DAILY_PNL_UNKNOWN in verdict.refusals


def test_drawdown_beyond_the_kill_switch_halts_and_a_shallower_one_does_not():
    state = FakeState(nav=Decimal(89_000))
    shallow = FakeState(nav=Decimal(91_000))

    deep_verdict = _check(state=state, peak_nav=Decimal(100_000))
    shallow_verdict = _check(state=shallow, peak_nav=Decimal(100_000))

    assert RiskRefusal.MAX_DRAWDOWN_HIT in deep_verdict.refusals
    assert RiskRefusal.MAX_DRAWDOWN_HIT not in shallow_verdict.refusals


def test_unknown_peak_nav_is_refused_rather_than_assumed_to_be_current_nav():
    verdict = _check(peak_nav=None)

    assert verdict.allowed is False
    assert RiskRefusal.PEAK_NAV_UNKNOWN in verdict.refusals
    assert RiskRefusal.MAX_DRAWDOWN_HIT not in verdict.refusals


def test_a_non_positive_peak_nav_is_not_a_high_water_mark():
    verdict = _check(peak_nav=Decimal(0))

    assert verdict.allowed is False
    assert RiskRefusal.PEAK_NAV_UNKNOWN in verdict.refusals


# --- every reason at once ---


def test_all_applicable_refusals_are_reported_together():
    """No short-circuit: an operator shown one reason widens one limit.

    Shown all of them, they see the intent is wrong rather than the limits.
    """
    state = FakeState(
        nav=Decimal(100_000),
        positions=(_position("ETH/USD", "10", "3000"),),
    )

    verdict = _check(
        intent=_intent(quantity="20", price="5000"),
        state=state,
        data_as_of=None,
        reconciled=False,
        realised_pnl_today=Decimal(-9_000),
        peak_nav=Decimal(200_000),
        correlations={},
    )

    assert set(verdict.refusals) >= {
        RiskRefusal.RECONCILIATION_DIVERGENCE,
        RiskRefusal.STALE_DATA,
        RiskRefusal.ABOVE_MAX_NOTIONAL,
        RiskRefusal.POSITION_TOO_LARGE,
        RiskRefusal.GROSS_EXPOSURE_EXCEEDED,
        RiskRefusal.NET_EXPOSURE_EXCEEDED,
        RiskRefusal.CORRELATION_LIMIT_EXCEEDED,
        RiskRefusal.DAILY_LOSS_LIMIT_HIT,
        RiskRefusal.MAX_DRAWDOWN_HIT,
    }
    assert len(verdict.refusals) == len(set(verdict.refusals))


# --- limits validate themselves ---


@pytest.mark.parametrize(
    "field",
    [
        "max_position_pct_nav",
        "max_gross_exposure_pct_nav",
        "max_net_exposure_pct_nav",
        "max_correlated_exposure_pct_nav",
        "correlation_threshold",
        "daily_loss_limit_pct_nav",
        "max_drawdown_pct",
    ],
)
def test_a_zero_fraction_limit_is_rejected_not_read_as_unlimited(field: str):
    """A zero here would delete a safety check by reading as `x > 0` always.

    Every fraction is checked individually because one un-validated field is
    one disabled limit, and the field that gets forgotten is the one that
    matters.
    """
    with pytest.raises(ValueError, match="disabled safety check"):
        _limits(**{field: Decimal(0)})


@pytest.mark.parametrize(
    "field",
    [
        "max_position_pct_nav",
        "max_gross_exposure_pct_nav",
        "max_net_exposure_pct_nav",
        "max_correlated_exposure_pct_nav",
        "correlation_threshold",
        "daily_loss_limit_pct_nav",
        "max_drawdown_pct",
    ],
)
def test_a_negative_fraction_limit_is_rejected(field: str):
    with pytest.raises(ValueError):
        _limits(**{field: Decimal("-0.1")})


@pytest.mark.parametrize(
    "field",
    [
        "max_position_pct_nav",
        "max_gross_exposure_pct_nav",
        "max_net_exposure_pct_nav",
        "max_correlated_exposure_pct_nav",
        "correlation_threshold",
        "daily_loss_limit_pct_nav",
        "max_drawdown_pct",
    ],
)
def test_a_fraction_limit_above_one_is_rejected(field: str):
    with pytest.raises(ValueError, match="must not exceed 1"):
        _limits(**{field: Decimal("1.01")})


def test_a_nan_limit_is_rejected():
    with pytest.raises(ValueError, match="finite"):
        _limits(max_position_pct_nav=Decimal("NaN"))


def test_max_positions_below_one_is_rejected():
    with pytest.raises(ValueError, match="at least 1"):
        _limits(max_positions=0)


def test_non_positive_max_data_age_is_rejected():
    with pytest.raises(ValueError, match="max_data_age"):
        _limits(max_data_age=timedelta(0))


def test_an_inverted_notional_band_is_rejected():
    with pytest.raises(ValueError, match="must exceed min_notional"):
        _limits(min_notional=Decimal(1000), max_notional=Decimal(100))


def test_a_zero_min_notional_is_rejected():
    with pytest.raises(ValueError, match="min_notional"):
        _limits(min_notional=Decimal(0))


# --- the verdict cannot lie about itself ---


def test_an_allowed_verdict_cannot_carry_refusals():
    with pytest.raises(ValueError, match="cannot carry refusals"):
        RiskVerdict(
            allowed=True,
            refusals=(RiskRefusal.STALE_DATA,),
            detail="contradiction",
        )


def test_a_refusal_must_name_a_reason():
    with pytest.raises(ValueError, match="must name its reason"):
        RiskVerdict(allowed=False, refusals=(), detail="because")


def test_the_verdict_is_truthy_only_when_allowed():
    assert bool(_check()) is True
    assert bool(_check(halted=True)) is False


def test_a_refusal_explains_itself_in_the_detail():
    verdict = _check(intent=_intent(quantity="3"))

    assert RiskRefusal.POSITION_TOO_LARGE in verdict.refusals
    assert "15000" in verdict.detail
    assert "10000" in verdict.detail


# --- Regressions from the Wave 1 adversarial verification -------------------


class TestReconciliationIsNotAssumed:
    """An unrun reconciliation must not read as a passed one.

    The module gave `data_as_of`, `realised_pnl_today` and `peak_nav` each a
    None sentinel and its own refusal, precisely so an unevaluated safety check
    could not read as cleared -- then defaulted `reconciled` to True, which is
    the same class of bug the other three were written to avoid. A caller that
    simply never wires reconciliation was permitted outright.
    """

    def test_omitting_reconciliation_refuses(self):
        verdict = check(
            _intent(),
            FakeState(nav=Decimal(100_000)),
            _limits(),
            correlations={},
            data_as_of=FRESH,
            now=NOW,
            realised_pnl_today=Decimal(0),
            peak_nav=Decimal(100_000),
        )
        assert not verdict.allowed
        assert RiskRefusal.RECONCILIATION_UNKNOWN in verdict.refusals

    def test_unknown_is_distinguished_from_diverged(self):
        # They call for different operator responses: one is "wire the check",
        # the other is "stop, the book is wrong".
        assert RiskRefusal.RECONCILIATION_UNKNOWN in _check(
            reconciled=None
        ).refusals
        assert RiskRefusal.RECONCILIATION_DIVERGENCE in _check(
            reconciled=False
        ).refusals

    def test_an_explicit_pass_still_allows(self):
        assert _check(reconciled=True).allowed


class TestGrossDoesNotNetAcrossVenues:
    """Gross is capital deployed; net is direction. They are not one formula.

    A long on one venue against a short on another is flat directionally and
    fully exposed operationally -- two liquidations, two custodians. The engine
    previously netted the intent's own symbol across venues before adding it to
    a gross built per row for every other symbol, so the same book scored
    differently depending on which name happened to be traded.
    """

    def _hedged(self, symbol: str) -> FakeState:
        return FakeState(
            nav=Decimal(100_000),
            positions=(
                _position(symbol, "5", "5000", venue="a"),
                _position(symbol, "-5", "5000", venue="b"),
            ),
        )

    def test_a_cross_venue_hedge_in_the_traded_name_counts_toward_gross(self):
        verdict = _check(
            intent=_intent(symbol="BTC/USD", quantity="1"),
            state=self._hedged("BTC/USD"),
            limits=_limits(max_gross_exposure_pct_nav=Decimal("0.50")),
        )
        assert RiskRefusal.GROSS_EXPOSURE_EXCEEDED in verdict.refusals

    def test_the_same_book_in_another_name_scores_identically(self):
        # The bug: these two disagreed. A hedge in the traded name vanished
        # from gross while the identical hedge in any other name counted.
        limits = _limits(max_gross_exposure_pct_nav=Decimal("0.50"))
        traded = _check(
            intent=_intent(symbol="BTC/USD", quantity="1"),
            state=self._hedged("BTC/USD"),
            limits=limits,
        )
        untraded = _check(
            intent=_intent(symbol="BTC/USD", quantity="1"),
            state=self._hedged("ETH/USD"),
            limits=limits,
        )
        traded_breached = RiskRefusal.GROSS_EXPOSURE_EXCEEDED in traded.refusals
        untraded_breached = RiskRefusal.GROSS_EXPOSURE_EXCEEDED in untraded.refusals
        assert traded_breached is untraded_breached is True

    def test_a_delta_neutral_carry_is_still_flat_on_net(self):
        # The carry producer's book must not trip the NET limit: that is the
        # strategy working, not a concentration. Gross is raised enough to
        # isolate the net assertion.
        verdict = _check(
            intent=_intent(symbol="BTC/USD", quantity="1"),
            state=self._hedged("BTC/USD"),
            limits=_limits(
                max_gross_exposure_pct_nav=Decimal("0.60"),
                max_net_exposure_pct_nav=Decimal("0.30"),
            ),
        )
        assert RiskRefusal.NET_EXPOSURE_EXCEEDED not in verdict.refusals
        assert RiskRefusal.GROSS_EXPOSURE_EXCEEDED not in verdict.refusals

    def test_selling_into_a_long_on_the_same_venue_reduces_gross(self):
        state = FakeState(
            nav=Decimal(100_000),
            positions=(_position("BTC/USD", "8", "5000", venue="paper"),),
        )
        limits = _limits(
            max_gross_exposure_pct_nav=Decimal("0.35"),
            max_position_pct_nav=Decimal(1),
        )
        # 8 BTC @ 5000 = 40_000, above the 35_000 cap. Selling 2 on the SAME
        # venue leaves 30_000 and must clear.
        reducing = _check(
            intent=_intent(side=Side.SELL, quantity="2"), state=state, limits=limits
        )
        assert RiskRefusal.GROSS_EXPOSURE_EXCEEDED not in reducing.refusals

    def test_selling_the_same_size_on_another_venue_opens_a_second_leg(self):
        state = FakeState(
            nav=Decimal(100_000),
            positions=(_position("BTC/USD", "8", "5000", venue="a"),),
        )
        limits = _limits(
            max_gross_exposure_pct_nav=Decimal("0.35"),
            max_position_pct_nav=Decimal(1),
        )
        # Same symbol, same size, different venue: 40_000 + 10_000 = 50_000
        # of deployed capital, not 30_000. Netting by symbol would score this
        # identically to the reducing case above.
        opening = _check(
            intent=_intent(side=Side.SELL, quantity="2"), state=state, limits=limits
        )
        assert RiskRefusal.GROSS_EXPOSURE_EXCEEDED in opening.refusals


class TestMarksAreKeyedByVenueNotSymbol:
    """The defect `state.py` carried, in the layer that enforces the limits.

    `risk.py` kept marks in a symbol-keyed dict while the book it valued was
    keyed per (venue, symbol), so the venue component was destructured away at
    the lookup and both legs of a cross-venue position were valued at whichever
    price landed in the dict first.

    It matters here more than in `state.py`. There a wrong mark misreports an
    unrealised P&L; here it decides whether an intent is REFUSED. A basis book
    priced at one venue for both legs understates gross exposure, so the cap
    stops binding on exactly the strategy `basis.crossvenue` exists to run.

    Both tests set the cap BETWEEN the two answers, so a collapsed valuation
    passes and a correct one refuses. Neither is satisfied by an engine that
    merely refuses a lot.
    """

    NAV = Decimal(200_000)
    # 10 @ 5000 at venue a, 10 @ 6000 at venue b.
    #   correct   gross = 50_000 + 60_000 = 110_000
    #   collapsed gross = 50_000 + 50_000 = 100_000   (both marked at 5000)
    CAP = Decimal("0.525")  # 105_000, strictly between the two

    def _cross_venue(self) -> FakeState:
        return FakeState(
            nav=self.NAV,
            positions=(
                _position("BTC/USD", "10", "5000", venue="a"),
                _position("BTC/USD", "10", "6000", venue="b"),
            ),
        )

    def _limits(self) -> RiskLimits:
        return _limits(
            max_gross_exposure_pct_nav=self.CAP,
            max_position_pct_nav=Decimal(1),
            max_net_exposure_pct_nav=Decimal(1),
            max_positions=10,
            max_correlated_exposure_pct_nav=Decimal(1),
        )

    def test_each_venue_is_valued_at_its_own_entry(self):
        verdict = _check(
            intent=_intent(symbol="ETH/USD", quantity="1", price="200"),
            state=self._cross_venue(),
            limits=self._limits(),
        )
        assert RiskRefusal.GROSS_EXPOSURE_EXCEEDED in verdict.refusals

    def test_an_intents_price_does_not_reprice_the_other_venues_leg(self):
        # Trading BTC at venue 'a' for 5000 must not re-price venue 'b''s leg,
        # carried at 6000. Under the defect the intent's reference overwrote the
        # single symbol key and both legs became 5000.
        verdict = _check(
            intent=_intent(symbol="BTC/USD", quantity="1", price="5000"),
            state=self._cross_venue(),
            limits=self._limits(),
        )
        assert RiskRefusal.GROSS_EXPOSURE_EXCEEDED in verdict.refusals
