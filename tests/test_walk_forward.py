"""Walk-forward validation: prove the harness separates a real edge from overfit.

The two load-bearing cases: (1) a signal that is genuinely monotonic in
confidence holds out-of-sample -- the forward rate stays high; (2) a signal
that calibrates in-sample by luck collapses forward -- the test rate falls
toward chance. If the harness could not tell these apart it would be useless.
"""

from datetime import datetime, timedelta

from omni.conviction.walk_forward import (
    PredictionRow,
    walk_forward,
)

_BASE = datetime(2026, 1, 1)
CUTOFF = _BASE + timedelta(days=50)


def _row(method, confidence, direction, hit, resolved_at):
    outcome = "upper" if (direction == "up" and hit) else (
        "lower" if (direction == "down" and hit) else "lower" if direction == "up" else "upper"
    )
    return PredictionRow(method, confidence, direction, outcome, resolved_at)


def test_real_monotonic_edge_holds_out_of_sample():
    # High-confidence predictions hit 80%, low-confidence hit 20%, in BOTH
    # halves. This is a real edge. The forward rate must clear the 0.6 target.
    rows = []
    for day in range(100):
        ts = _BASE + timedelta(days=day)
        # two high-confidence (hit 80%), two low-confidence (hit 20%)
        for conf, p_hit in ((0.85, 0.8), (0.25, 0.2)):
            hit_high = (day % 5 != 0)  # 4 of 5 hit
            hit_low = (day % 5 == 0)  # 1 of 5 hit
            rows.append(_row("real", 0.85, "up", hit_high, ts))
            rows.append(_row("real", 0.25, "up", hit_low, ts))

    res = walk_forward(rows, cutoff=CUTOFF)
    assert len(res) == 1
    r = res[0]
    assert r.threshold is not None
    assert r.test_hit_rate is not None
    assert r.holds_out_of_sample is True
    # Forward rate close to in-sample -- the edge is real, not overfit.
    assert abs(r.test_hit_rate - r.in_sample_hit_rate) < 0.1


def test_in_sample_overfit_collapses_forward():
    # The training half calibrates perfectly (high conf hits, low conf misses),
    # but the test half is pure coin-flip regardless of confidence. A threshold
    # derived from training must NOT hold forward -- the edge was overfit.
    rows = []
    for day in range(100):
        ts = _BASE + timedelta(days=day)
        before = day < 50
        if before:
            # Training: high conf always hits, low conf always misses -> perfect calibration.
            rows.append(_row("overfit", 0.85, "up", True, ts))
            rows.append(_row("overfit", 0.25, "up", False, ts))
        else:
            # Test: 50/50 regardless of confidence -> no real edge.
            hit = (day % 2 == 0)
            rows.append(_row("overfit", 0.85, "up", hit, ts))
            rows.append(_row("overfit", 0.25, "up", hit, ts))

    res = walk_forward(rows, cutoff=CUTOFF)
    r = res[0]
    assert r.threshold is not None
    # Threshold derived from training is 0.8 (the high-conf bucket hit 100%).
    assert r.threshold == 0.8
    assert r.test_hit_rate is not None
    # Forward rate ~0.5: the overfit edge collapsed.
    assert r.holds_out_of_sample is False
    assert r.test_hit_rate < 0.6


def test_method_with_no_forward_predictions_is_omitted():
    rows = [_row("onlypast", 0.9, "up", True, _BASE)]
    res = walk_forward(rows, cutoff=CUTOFF)
    assert res == []


def test_uncalibrated_method_surfaced_rate_is_none():
    # Training has too few resolved (< 10) to derive a threshold: test_hit_rate
    # is None (unknown), which is NOT the same as a failed edge.
    rows = [
        _row("thin", 0.8, "up", True, _BASE + timedelta(days=60)),
        _row("thin", 0.8, "up", True, _BASE + timedelta(days=61)),
    ]
    res = walk_forward(rows, cutoff=CUTOFF)
    assert len(res) == 1
    assert res[0].test_hit_rate is None
    assert res[0].holds_out_of_sample is None
