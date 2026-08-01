"""Behaviour tests for the consensus comparison / trend capabilities.

Extracted from two inline v1 handlers (``/consensus/compare`` and
``/consensus/historical/{symbol}``) that had no test of the comparative /
trend maths itself -- v1's router tests mocked the engine and asserted the
HTTP shape. These tests are the oracle for the extracted pure functions, so
every assertion is on a hand-computed value for a known input, and every
default-substitution v1 made on missing input (an empty comparison, an empty
or score-less history) has a test that it raises ``Unavailable``.
"""

from __future__ import annotations

import math

import pytest

from omni.capabilities.consensus import compare_consensus, consensus_trend
from omni.ingest.protocol import Unavailable

# --------------------------------------------------------------------------- #
# compare_consensus
# --------------------------------------------------------------------------- #


def test_compare_ranks_by_composite_score_descending():
    out = compare_consensus(
        {
            "RANKA": {"composite_score": 0.7, "signal": "buy", "confidence": 0.9},
            "RANKB": {"composite_score": 0.3, "signal": "sell", "confidence": 0.4},
        }
    )
    assert out["rankings"]["RANKA"]["rank"] == 1
    assert out["rankings"]["RANKA"]["score"] == 0.7
    assert out["rankings"]["RANKB"]["rank"] == 2
    assert out["rankings"]["RANKB"]["score"] == 0.3
    # The signal / confidence the handler forwarded are passed through.
    assert out["rankings"]["RANKA"]["signal"] == "buy"
    assert out["rankings"]["RANKA"]["confidence"] == 0.9


def test_compare_overall_leader_insight_names_the_top_symbol():
    out = compare_consensus({"A": {"composite_score": 0.8}, "B": {"composite_score": 0.2}})
    assert "A shows the strongest overall consensus score" in out["insights"]


def test_compare_factor_leader_insight_only_above_threshold():
    out = compare_consensus(
        {
            "A": {
                "composite_score": 0.5,
                "factor_scores": {
                    "technical": {"score": 0.8, "signal": "buy"},
                    "fundamental": {"score": 0.4, "signal": "hold"},
                },
            },
            "B": {
                "composite_score": 0.5,
                "factor_scores": {
                    "technical": {"score": 0.2, "signal": "sell"},
                    "fundamental": {"score": 0.6, "signal": "buy"},
                },
            },
        }
    )
    # A leads technical (0.8 > 0.5 threshold) -> insight; B leads fundamental
    # (0.6 > 0.5) -> insight. Both clear the threshold.
    assert "A leads in technical" in out["insights"]
    assert "B leads in fundamental" in out["insights"]


def test_compare_factor_leader_below_threshold_emits_no_factor_insight():
    out = compare_consensus(
        {
            "A": {
                "composite_score": 0.5,
                "factor_scores": {"technical": {"score": 0.45}},
            },
            "B": {
                "composite_score": 0.5,
                "factor_scores": {"technical": {"score": 0.3}},
            },
        }
    )
    # Overall leader insight present, but the technical leader (0.45) is below
    # the 0.5 threshold so no factor-leader insight is emitted.
    assert any("strongest overall" in s for s in out["insights"])
    assert not any("leads in" in s for s in out["insights"])


def test_compare_factor_comparison_covers_union_of_factors():
    out = compare_consensus(
        {
            "A": {"composite_score": 0.5, "factor_scores": {"x": {"score": 0.1}}},
            "B": {
                "composite_score": 0.5,
                "factor_scores": {"x": {"score": 0.2}, "y": {"score": 0.3}},
            },
        }
    )
    assert set(out["factor_comparison"]) == {"x", "y"}
    # A has no 'y' factor -> absent from that column (v1 skipped symbols
    # missing the factor).
    assert "A" not in out["factor_comparison"]["y"]
    assert out["factor_comparison"]["x"]["B"]["score"] == 0.2


def test_compare_single_analysis_ranks_it_first():
    out = compare_consensus({"SOLO": {"composite_score": 0.6}})
    assert out["rankings"]["SOLO"]["rank"] == 1


def test_compare_empty_raises():
    with pytest.raises(Unavailable):
        compare_consensus({})


def test_compare_missing_composite_score_raises():
    with pytest.raises(Unavailable):
        compare_consensus({"X": {"signal": "buy"}})


# --------------------------------------------------------------------------- #
# consensus_trend
# --------------------------------------------------------------------------- #


def test_trend_ascending_scores_report_positive_change():
    out = consensus_trend(
        [
            {"composite_score": 0.2, "signal": "hold"},
            {"composite_score": 0.5, "signal": "buy"},
            {"composite_score": 0.8, "signal": "strong_buy"},
        ]
    )
    assert out["data_points"] == 3
    assert out["score_change"] == round(0.8 - 0.2, 3)
    assert out["min_score"] == 0.2
    assert out["max_score"] == 0.8
    assert out["avg_score"] == round((0.2 + 0.5 + 0.8) / 3, 3)
    assert out["current_score"] == 0.8
    assert out["current_signal"] == "strong_buy"


def test_trend_descending_scores_report_negative_change():
    out = consensus_trend(
        [{"composite_score": 0.9}, {"composite_score": 0.4}, {"composite_score": 0.1}]
    )
    assert out["score_change"] == round(0.1 - 0.9, 3)
    assert out["score_change"] < 0


def test_trend_single_observation_change_is_zero():
    out = consensus_trend([{"composite_score": 0.42}])
    assert out["data_points"] == 1
    assert out["score_change"] == 0.0
    assert out["min_score"] == out["max_score"] == out["avg_score"] == 0.42
    assert out["current_score"] == 0.42


def test_trend_current_signal_none_when_absent():
    out = consensus_trend([{"composite_score": 0.5}, {"composite_score": 0.6}])
    assert out["current_signal"] is None


def test_trend_empty_raises_rather_than_fabricating_zeros():
    # v1 returned a full zero-filled trends dict ("hold" signal, all zeros)
    # on empty history -- a flat trend over data that did not exist.
    with pytest.raises(Unavailable):
        consensus_trend([])


def test_trend_snapshot_missing_score_raises():
    with pytest.raises(Unavailable):
        consensus_trend([{"composite_score": 0.5}, {"signal": "buy"}])


def test_trend_rounds_to_three_decimals():
    out = consensus_trend([{"composite_score": 1 / 3}, {"composite_score": 2 / 3}])
    assert out["avg_score"] == round(1.0 / 2, 3)
    assert out["min_score"] == round(1 / 3, 3)
    # Confirm rounding actually happened: the raw 1/3 is 0.3333... and must
    # differ from the rounded 0.333. (The pre-existing ``!= math.inf`` below
    # cannot fail for any plausible implementation and is kept only because
    # the no-delete rule forbids removing it; this inequality is the real
    # discrimination.)
    assert out["min_score"] != 1 / 3
    assert out["min_score"] != math.inf


def test_compare_factor_leader_at_exact_threshold_emits_no_insight():
    # v1 used a strict ``> 0.5`` (software/.../consensus.py:714); a score
    # exactly at the threshold is NOT a leader. Pins the boundary so the
    # comparison cannot silently drift to ``>=``. The existing above/below
    # tests use 0.6/0.8 and 0.45, which pass under both ``>`` and ``>=`` and
    # therefore could not settle this.
    out = compare_consensus(
        {
            "A": {
                "composite_score": 0.5,
                "factor_scores": {"technical": {"score": 0.5}},
            },
            "B": {
                "composite_score": 0.5,
                "factor_scores": {"technical": {"score": 0.3}},
            },
        }
    )
    assert not any("leads in" in s for s in out["insights"])


def test_trend_current_and_change_are_positional_so_ordering_matters():
    # The docstring contracts ``history`` as oldest-first. The function has no
    # timestamp field to sort on (it consumes only composite_score + optional
    # signal), so "current" is the LAST element by position and "change" is
    # last-minus-first. Reversing the input therefore flips the sign of
    # score_change and changes current_score. This pins the positional
    # semantics so a future "helpful" refactor that re-sorts, picks max, or
    # switches to max-minus-min would break here rather than silently produce
    # a different trend from the same numbers.
    ascending = [
        {"composite_score": 0.2},
        {"composite_score": 0.5},
        {"composite_score": 0.8, "signal": "buy"},
    ]
    out_asc = consensus_trend(ascending)
    assert out_asc["score_change"] == round(0.8 - 0.2, 3)
    assert out_asc["current_score"] == 0.8
    assert out_asc["current_signal"] == "buy"

    # Same three points, reversed -- a violated oldest-first precondition.
    out_rev = consensus_trend(list(reversed(ascending)))
    assert out_rev["score_change"] == round(0.2 - 0.8, 3)
    assert out_rev["current_score"] == 0.2
    assert out_rev["current_signal"] is None
