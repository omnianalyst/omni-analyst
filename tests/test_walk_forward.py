"""Walk-forward validation: prove the harness separates a real edge from overfit.

The two load-bearing cases: (1) a signal that is genuinely monotonic in
confidence holds out-of-sample -- the forward rate stays high; (2) a signal
that calibrates in-sample by luck collapses forward -- the test rate falls
toward chance. If the harness could not tell these apart it would be useless.
"""

from datetime import UTC, datetime, timedelta

from omni.conviction.walk_forward import (
    PredictionRow,
    walk_forward,
)

_BASE = datetime(2026, 1, 1, tzinfo=UTC)
CUTOFF = _BASE + timedelta(days=50)


def _row(method, confidence, direction, hit, resolved_at):
    outcome = "upper" if (direction == "up" and hit) else (
        "lower" if (direction == "down" and hit) else "lower" if direction == "up" else "upper"
    )
    return PredictionRow(method, confidence, direction, outcome, resolved_at)


def test_real_monotonic_edge_holds_out_of_sample():
    # Calls stated at 0.65 hit 80%, low-confidence hit 20%, in BOTH halves.
    # This is a real edge: the hit rate beats what the barriers alone imply.
    # (The fixture used to state 0.85 for an 80% hit rate -- a call that hits
    # LESS often than a random walk reaches its own target, which is no edge
    # at all and is exactly what the gate must not qualify.)
    rows = []
    for day in range(100):
        ts = _BASE + timedelta(days=day)
        # two high-confidence (hit 80%), two low-confidence (hit 20%)
        for conf, p_hit in ((0.85, 0.8), (0.25, 0.2)):
            hit_high = (day % 5 != 0)  # 4 of 5 hit
            hit_low = (day % 5 == 0)  # 1 of 5 hit
            rows.append(_row("real", 0.65, "up", hit_high, ts))
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


def test_a_hit_rate_equal_to_its_own_confidence_is_not_an_edge():
    # 0.85 stated, 80% realised, in both halves: barrier geometry, not skill.
    rows = []
    for day in range(100):
        ts = _BASE + timedelta(days=day)
        rows.append(_row("geometry", 0.85, "up", day % 5 != 0, ts))
    r = walk_forward(rows, cutoff=CUTOFF)[0]
    assert r.threshold is None
    assert r.test_surfaced == 0


def test_training_calls_whose_label_window_straddles_the_cutoff_are_purged():
    # Every training call is a hit, but each one's label window runs past the
    # cutoff: it shares its price path with the test calls, so none may train.
    rows = []
    for day in range(40, 50):
        made = _BASE + timedelta(days=day)
        rows.append(PredictionRow(
            "straddle", 0.65, "up", "upper", made + timedelta(hours=1),
            created_at=made, horizon_ends_at=made + timedelta(days=30),
        ))
    for day in range(50, 60):
        made = _BASE + timedelta(days=day)
        rows.append(PredictionRow(
            "straddle", 0.65, "up", "upper", made + timedelta(hours=1),
            created_at=made, horizon_ends_at=made + timedelta(days=1),
        ))
    r = walk_forward(rows, cutoff=CUTOFF)[0]
    assert r.train_resolved == 0
    assert r.threshold is None


def test_the_split_is_on_the_decision_date_not_the_resolution_date():
    # Called before the cutoff, resolved after it: under a resolved_at split
    # this is a TEST row. It is neither -- not test (decided before the
    # cutoff) and not training (unresolved at the cutoff).
    made = CUTOFF - timedelta(days=1)
    late = PredictionRow(
        "decision", 0.65, "up", "upper", CUTOFF + timedelta(days=5),
        created_at=made, horizon_ends_at=CUTOFF + timedelta(days=10),
    )
    after = PredictionRow(
        "decision", 0.65, "up", "upper", CUTOFF + timedelta(days=6),
        created_at=CUTOFF + timedelta(days=5),
        horizon_ends_at=CUTOFF + timedelta(days=7),
    )
    r = walk_forward([late, after], cutoff=CUTOFF)[0]
    assert r.train_resolved == 0
    assert r.test_resolved == 1
