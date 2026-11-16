"""The conviction gate. Every test here is about refusing to speak."""

from uuid import uuid4

import pytest

from omni.conviction.gate import (
    MIN_RESOLVED_FOR_CALIBRATION,
    Calibration,
    Candidate,
    Refusal,
    assess,
    calibrated_threshold,
    rate_limit_by_conviction,
)

_MIDPOINT = object()


def _bucket(low, n, hits, claim_type="manipulation_signal", method="detect",
            mean_confidence=_MIDPOINT):
    # The bucket's mean stated confidence is its zero-edge hit rate (confidence
    # is the driftless first-passage probability of the call's own barriers),
    # and the gate requires a bucket to beat it. Midpoint unless a test says
    # otherwise.
    if mean_confidence is _MIDPOINT:
        mean_confidence = round(low + 0.05, 2)
    return Calibration(
        claim_type=claim_type, method=method,
        bucket_low=low, bucket_high=round(low + 0.1, 2), n=n, hits=hits,
        mean_confidence=mean_confidence,
    )


def _candidate(confidence=0.85, **kw):
    kw.setdefault("searched_for_disconfirming", True)
    kw.setdefault("falsifiable", True)
    return Candidate(
        claim_id=uuid4(), claim_type="manipulation_signal", method="detect",
        confidence=confidence, **kw,
    )


class TestCalibrationFloor:
    def test_a_thin_bucket_reports_no_hit_rate(self):
        assert _bucket(0.8, n=MIN_RESOLVED_FOR_CALIBRATION - 1, hits=9).hit_rate is None

    def test_a_bucket_at_the_floor_reports_one(self):
        b = _bucket(0.8, n=MIN_RESOLVED_FOR_CALIBRATION, hits=8)
        assert b.hit_rate == pytest.approx(0.8)

    def test_no_threshold_can_be_derived_without_evidence(self):
        assert calibrated_threshold([_bucket(0.9, n=3, hits=3)],
                                    target_hit_rate=0.6) is None

    def test_the_threshold_is_the_lowest_bucket_that_clears_the_target(self):
        buckets = [_bucket(0.5, 200, 80), _bucket(0.7, 200, 180), _bucket(0.9, 200, 199)]
        assert calibrated_threshold(buckets, target_hit_rate=0.6) == pytest.approx(0.7)

    def test_a_stricter_target_raises_the_threshold(self):
        buckets = [_bucket(0.5, 200, 80), _bucket(0.7, 200, 180), _bucket(0.9, 200, 199)]
        assert calibrated_threshold(buckets, target_hit_rate=0.9) == pytest.approx(0.9)


class TestSilenceIsValid:
    def test_an_uncalibrated_class_is_never_surfaced_however_confident(self):
        """The failure this whole module exists to prevent."""
        v = assess(_candidate(confidence=0.99), [_bucket(0.9, n=2, hits=2)])
        assert not v.surfaced
        assert v.refusal is Refusal.UNCALIBRATED

    def test_a_class_with_no_history_at_all_is_never_surfaced(self):
        assert assess(_candidate(), []).refusal is Refusal.UNCALIBRATED

    def test_calibration_for_a_different_class_does_not_qualify_this_one(self):
        other = [_bucket(0.7, 50, 45, claim_type="price_snapshot")]
        assert assess(_candidate(), other).refusal is Refusal.UNCALIBRATED

    def test_calibration_for_a_different_method_does_not_qualify_either(self):
        other = [_bucket(0.7, 50, 45, method="some_other_pipeline")]
        assert assess(_candidate(), other).refusal is Refusal.UNCALIBRATED


class TestEvidenceRequirements:
    @pytest.fixture
    def calibrated(self):
        return [_bucket(0.7, 400, 360), _bucket(0.8, 400, 380)]

    def test_a_one_sided_finding_is_advocacy_and_is_refused(self, calibrated):
        v = assess(
            _candidate(searched_for_disconfirming=False, supporting=("a", "b")),
            calibrated,
        )
        assert not v.surfaced
        assert v.refusal is Refusal.NO_DISCONFIRMING_SEARCH

    def test_searching_and_finding_nothing_disconfirming_is_acceptable(self, calibrated):
        """The requirement is that the search happened, not that it found
        something. Demanding a counter-argument would invent one."""
        v = assess(
            _candidate(searched_for_disconfirming=True, disconfirming=()), calibrated
        )
        assert v.surfaced

    def test_an_unscoreable_finding_is_refused(self, calibrated):
        v = assess(_candidate(falsifiable=False), calibrated)
        assert not v.surfaced
        assert v.refusal is Refusal.NOT_FALSIFIABLE

    def test_below_the_derived_threshold_is_refused(self, calibrated):
        v = assess(_candidate(confidence=0.5), calibrated)
        assert not v.surfaced
        assert v.refusal is Refusal.BELOW_THRESHOLD
        assert v.threshold == pytest.approx(0.7)


class TestSurfacing:
    @pytest.fixture
    def calibrated(self):
        return [_bucket(0.7, 400, 360), _bucket(0.8, 400, 380)]

    def test_a_qualifying_candidate_is_surfaced_with_its_track_record(self, calibrated):
        v = assess(_candidate(confidence=0.85), calibrated)
        assert v.surfaced
        assert v.calibrated_hit_rate == pytest.approx(0.95)
        assert "95%" in v.detail
        # The record is stated against what it had to beat, never alone.
        assert v.zero_edge_rate == pytest.approx(0.85)
        assert "85%" in v.detail
        assert v.hit_rate_lower_bound == pytest.approx(0.9241, abs=1e-3)

    def test_the_threshold_is_reported_so_the_decision_can_be_checked(self, calibrated):
        assert assess(_candidate(), calibrated).threshold == pytest.approx(0.7)


class TestRateLimiting:
    @pytest.fixture
    def calibrated(self):
        return [_bucket(0.7, 400, 360), _bucket(0.8, 400, 392)]

    def test_nothing_is_manufactured_to_fill_a_quota(self, calibrated):
        """A quiet week is a healthy outcome."""
        refused = [assess(_candidate(confidence=0.1), calibrated) for _ in range(5)]
        assert rate_limit_by_conviction(refused, max_surfaced=3) == []

    def test_fewer_qualifying_than_the_cap_returns_fewer(self, calibrated):
        vs = [assess(_candidate(confidence=0.85), calibrated)]
        assert len(rate_limit_by_conviction(vs, max_surfaced=5)) == 1

    def test_the_strongest_survive_the_cap(self, calibrated):
        vs = [
            assess(_candidate(confidence=0.75), calibrated),
            assess(_candidate(confidence=0.85), calibrated),
        ]
        kept = rate_limit_by_conviction(vs, max_surfaced=1)
        assert len(kept) == 1
        assert kept[0].calibrated_hit_rate == pytest.approx(0.98)

    def test_refused_candidates_never_survive_the_cap(self, calibrated):
        vs = [
            assess(_candidate(confidence=0.85), calibrated),
            assess(_candidate(confidence=0.85, falsifiable=False), calibrated),
        ]
        assert len(rate_limit_by_conviction(vs, max_surfaced=10)) == 1


class TestGeometryIsNotSkill:
    """Confidence is the driftless first-passage probability of a call's own
    barriers, so with no edge the hit rate EQUALS the confidence. A bucket
    clearing the target says nothing until it also beats itself."""

    def test_a_bucket_hitting_exactly_its_own_confidence_never_qualifies(self):
        # Target two units away, stop eight: a random walk hits it 80% of the
        # time. A thousand resolved calls at 80% is a large, clean, worthless
        # record -- and the old gate surfaced it as "resolved correctly 80%".
        zero_edge = [_bucket(0.8, 1000, 800, mean_confidence=0.80)]
        assert calibrated_threshold(zero_edge, target_hit_rate=0.6) is None
        v = assess(_candidate(confidence=0.85), zero_edge)
        assert not v.surfaced
        # Measured and found wanting, which is not "too few predictions".
        assert v.refusal is Refusal.NO_DEMONSTRATED_EDGE

    def test_six_of_ten_is_not_a_calibrated_bucket(self):
        # P(>=6/10 | p=0.5) is 0.377; with five buckets one clears by chance
        # 91% of the time. The Wilson lower bound of 6/10 is 0.31.
        lucky = [_bucket(0.5, 10, 6)]
        assert lucky[0].hit_rate == pytest.approx(0.6)
        assert calibrated_threshold(lucky, target_hit_rate=0.6) is None

    def test_a_bucket_with_no_known_zero_edge_rate_cannot_qualify(self):
        unknown = [_bucket(0.7, 400, 390, mean_confidence=None)]
        assert calibrated_threshold(unknown, target_hit_rate=0.6) is None

    def test_a_candidate_is_gated_on_its_own_bucket_not_the_lowest_qualifier(self):
        # 0.7 qualifies; 0.8 was measured and FAILED; 0.9 was never measured.
        # "Everything above the lowest qualifying bucket" surfaced all three.
        buckets = [_bucket(0.7, 400, 360), _bucket(0.8, 400, 200), _bucket(0.9, 3, 3)]
        assert assess(_candidate(confidence=0.75), buckets).surfaced
        for confidence in (0.85, 0.95):
            v = assess(_candidate(confidence=confidence), buckets)
            assert not v.surfaced
            assert v.refusal is Refusal.BELOW_THRESHOLD

    def test_confidence_of_exactly_one_belongs_to_the_top_bucket(self):
        buckets = [_bucket(0.9, 400, 399)]
        assert assess(_candidate(confidence=1.0), buckets).surfaced

    def test_ten_of_ten_does_not_outrank_a_long_record(self):
        buckets = [_bucket(0.5, 1000, 990), _bucket(0.6, 10, 10)]
        long_record = assess(_candidate(confidence=0.55), buckets)
        ten_for_ten = assess(_candidate(confidence=0.65), buckets)
        assert long_record.surfaced and ten_for_ten.surfaced
        assert ten_for_ten.calibrated_hit_rate > long_record.calibrated_hit_rate
        kept = rate_limit_by_conviction([ten_for_ten, long_record], max_surfaced=1)
        assert kept == [long_record]


class TestASweepWinnerIsDeflated:
    """Backfilling k variants and surfacing the best is k tests."""

    def test_the_lower_bound_tightens_with_the_number_of_variants_tried(self):
        from omni.conviction.gate import z_for_trials

        assert z_for_trials(1) == pytest.approx(1.959964, abs=1e-6)
        # 0.05 / 4 two-sided: z = 2.4977
        assert z_for_trials(4) == pytest.approx(2.4977, abs=1e-4)
        with pytest.raises(ValueError):
            z_for_trials(0)

    def test_a_bucket_that_clears_alone_fails_as_one_of_twenty(self):
        # 150 of 200 in the 0.6 bucket (mean 0.65): lower bound 0.686 alone.
        alone = Calibration(
            claim_type="c", method="m.w20", bucket_low=0.6, bucket_high=0.7,
            n=200, hits=150, mean_confidence=0.65,
        )
        swept = Calibration(
            claim_type="c", method="m.w20", bucket_low=0.6, bucket_high=0.7,
            n=200, hits=150, mean_confidence=0.65, trials=20,
        )
        assert alone.shows_edge(target_hit_rate=0.6)
        assert swept.hit_rate_lower_bound < alone.hit_rate_lower_bound
        assert not swept.shows_edge(target_hit_rate=0.6)
