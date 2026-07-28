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


def _bucket(low, n, hits, claim_type="manipulation_signal", method="detect"):
    return Calibration(
        claim_type=claim_type, method=method,
        bucket_low=low, bucket_high=round(low + 0.1, 2), n=n, hits=hits,
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
        buckets = [_bucket(0.5, 20, 8), _bucket(0.7, 20, 14), _bucket(0.9, 20, 19)]
        assert calibrated_threshold(buckets, target_hit_rate=0.6) == pytest.approx(0.7)

    def test_a_stricter_target_raises_the_threshold(self):
        buckets = [_bucket(0.5, 20, 8), _bucket(0.7, 20, 14), _bucket(0.9, 20, 19)]
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
        return [_bucket(0.7, 40, 32), _bucket(0.8, 40, 34)]

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
        return [_bucket(0.7, 40, 32), _bucket(0.8, 40, 34)]

    def test_a_qualifying_candidate_is_surfaced_with_its_track_record(self, calibrated):
        v = assess(_candidate(confidence=0.85), calibrated)
        assert v.surfaced
        assert v.calibrated_hit_rate == pytest.approx(0.85)
        assert "85%" in v.detail

    def test_the_threshold_is_reported_so_the_decision_can_be_checked(self, calibrated):
        assert assess(_candidate(), calibrated).threshold == pytest.approx(0.7)


class TestRateLimiting:
    @pytest.fixture
    def calibrated(self):
        return [_bucket(0.7, 40, 32), _bucket(0.8, 40, 38)]

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
        assert kept[0].calibrated_hit_rate == pytest.approx(0.95)

    def test_refused_candidates_never_survive_the_cap(self, calibrated):
        vs = [
            assess(_candidate(confidence=0.85), calibrated),
            assess(_candidate(confidence=0.85, falsifiable=False), calibrated),
        ]
        assert len(rate_limit_by_conviction(vs, max_surfaced=10)) == 1
