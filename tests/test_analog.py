"""Analog windows: similarity scoring and honest forward returns.

What these tests defend:

* similarity follows the published regime weights (Sahm 0.4, curve 0.3,
  LEI 0.3) -- not a hand-tuned metric;
* CPI refines but cannot decide: without CPI the three recession gauges
  still carry the full score;
* non-matching gauges are named in `missed`, never silently dropped;
* a window with no forward data reports None, not zero -- zero is a return,
  None is "don't know";
* the current month cannot be its own analog.
"""

from omni.conviction.analog import MacroState, analog_windows, similarity


def _state(curve=False, sahm=False, lei=False, cpi=2.5) -> MacroState:
    return MacroState(curve, sahm, lei, cpi)


def test_similarity_uses_the_regime_weights() -> None:
    # All three gauges match + CPI within tolerance -> 1.0
    score, matched, missed = similarity(_state(), _state())
    assert score == 1.0
    assert matched == ["yield_curve", "sahm", "lei", "cpi"]
    assert missed == []

    # Now is stressed (all three triggered), then was calm: nothing matches,
    # CPI differs by more than a point too -> 0.0
    score, matched, missed = similarity(
        _state(curve=True, sahm=True, lei=True, cpi=5.0), _state(cpi=2.5)
    )
    assert score == 0.0
    assert set(missed) == {"yield_curve", "sahm", "lei", "cpi"}

    # Now calm, then stressed: same zero, the comparison is symmetric
    score, _, _ = similarity(_state(cpi=2.5), _state(curve=True, sahm=True, lei=True, cpi=5.0))
    assert score == 0.0


def test_partial_matches_carry_only_their_weight() -> None:
    # Curve and LEI are calm on both sides (False == False matches, 0.3+0.3);
    # Sahm triggers on both (+0.4); CPI within a point (+0.2) -> 1.0.
    score, matched, missed = similarity(
        _state(sahm=True, cpi=3.0), _state(sahm=True, cpi=2.5)
    )
    assert score == 1.0
    assert matched == ["yield_curve", "sahm", "lei", "cpi"]
    assert missed == []

    # A single true disagreement (curve only): 1.0 - 0.3, CPI absent entirely.
    score, matched, missed = similarity(
        _state(curve=True, cpi=None), _state(cpi=None)
    )
    assert score == 0.7
    assert matched == ["sahm", "lei"]
    assert missed == ["yield_curve"]


def test_cpi_is_a_refiner_not_a_decider() -> None:
    # Three gauges match, CPI unmeasurable in the past month: the recession
    # gauges carry the score alone -- no penalty for absent data.
    score, matched, _ = similarity(_state(), _state(cpi=None))
    assert score == 1.0
    assert matched == ["yield_curve", "sahm", "lei"]


def test_forward_returns_none_is_preserved_not_zeroed() -> None:
    now = _state(curve=True, sahm=True, lei=True, cpi=6.0)
    history = [
        ("1973-10", _state(curve=True, sahm=True, lei=True, cpi=7.0)),
        ("2008-09", _state(curve=True, sahm=True, lei=True, cpi=4.9)),
    ]
    forward = {
        "1973-10": {"gold": 73.5, "stocks": None},  # stocks series absent
        # 2008-09 has no forward row at all
    }

    windows = analog_windows(now, history, forward)

    by_month = {w.month: w for w in windows}
    assert by_month["1973-10"].forward["gold"] == 73.5
    assert by_month["1973-10"].forward["stocks"] is None
    # missing month -> empty forward dict, not fabricated zeros
    assert by_month["2008-09"].forward == {}


def test_the_current_month_is_not_its_own_analog() -> None:
    now = _state(curve=True, sahm=True)
    history = [("2026-08", now), ("2000-09", now)]

    windows = analog_windows(now, history, {}, current_month="2026-08")

    assert [w.month for w in windows] == ["2000-09"]


def test_below_floor_windows_are_refused() -> None:
    now = _state(curve=True, sahm=True, lei=True, cpi=2.5)
    history = [("1995-06", _state(cpi=2.4))]  # only CPI matches: 0.2

    windows = analog_windows(now, history, {}, min_similarity=0.7)

    assert windows == []


def test_limit_orders_by_similarity_then_date() -> None:
    now = _state()
    perfect = _state()
    history = [
        ("1987-08", perfect),
        ("1999-12", perfect),
        ("2018-11", _state(cpi=2.6)),
    ]
    windows = analog_windows(now, history, {}, limit=2)
    assert len(windows) == 2
    assert [w.month for w in windows] == ["1987-08", "1999-12"]
