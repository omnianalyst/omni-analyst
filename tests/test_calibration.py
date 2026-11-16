from dataclasses import dataclass

import pytest

from omni.calibration import Benchmark, Direction, Outcome
from omni.calibration.report import calibration_with_benchmark


@dataclass
class _Pred:
    id: int
    method: str = "momentum"
    confidence: float = 0.65
    direction: Direction = Direction.UP
    outcome: Outcome = Outcome.PENDING


def _bucket_for(buckets, low: float):
    return next(b for b in buckets if b.bucket_low == low)


class TestCalibrationPendingExcluded:
    def test_a_pending_prediction_produces_no_methods(self):
        result = calibration_with_benchmark([_Pred(id=1)], {})
        assert result == {}

    def test_pending_is_dropped_even_among_resolved(self):
        pending = _Pred(id=1, outcome=Outcome.PENDING)
        resolved = _Pred(id=2, outcome=Outcome.UPPER)
        result = calibration_with_benchmark([pending, resolved], {})
        assert "momentum" in result
        assert sum(b.n for b in result["momentum"]) == 1


class TestCalibrationSuppressionBelowMinBucketN:
    def test_bucket_under_threshold_suppresses_hit_rate_and_mean_confidence(self):
        preds = [
            _Pred(id=i, confidence=0.65, outcome=Outcome.UPPER) for i in range(5)
        ]
        result = calibration_with_benchmark(preds, {})
        bucket = _bucket_for(result["momentum"], 0.6)
        assert bucket.n == 5
        assert bucket.benchmarked_n == 0
        assert bucket.hit_rate is None
        assert bucket.mean_confidence is None


class TestCalibrationAtThreshold:
    def test_bucket_at_threshold_reports_hit_rate_and_mean_confidence(self):
        preds = [
            _Pred(id=i, confidence=0.65, outcome=Outcome.UPPER) for i in range(10)
        ]
        result = calibration_with_benchmark(preds, {})
        bucket = _bucket_for(result["momentum"], 0.6)
        assert bucket.n == 10
        assert bucket.hit_rate == pytest.approx(1.0)
        assert bucket.mean_confidence == pytest.approx(0.65, rel=1e-6)


class TestCalibrationUnbenchmarked:
    def test_unbenchmarked_bucket_has_zero_benchmarked_n_and_null_market_mean(self):
        preds = [
            _Pred(id=i, confidence=0.65, outcome=Outcome.UPPER) for i in range(10)
        ]
        result = calibration_with_benchmark(preds, {})
        bucket = _bucket_for(result["momentum"], 0.6)
        assert bucket.n == 10
        assert bucket.benchmarked_n == 0
        assert bucket.market_mean_probability is None
        assert bucket.hit_rate == pytest.approx(1.0)


class TestCalibrationWithBenchmarks:
    def test_mixed_benchmarked_and_unbenchmarked_reports_all_columns(self):
        benchmarked = [_Pred(id=i, confidence=0.65, outcome=Outcome.UPPER) for i in range(10)]
        unbenchmarked = [
            _Pred(id=100 + i, confidence=0.65, outcome=Outcome.UPPER) for i in range(10)
        ]
        benchmarks = {str(p.id): Benchmark(market_probability=0.70) for p in benchmarked}
        result = calibration_with_benchmark(benchmarked + unbenchmarked, benchmarks)
        bucket = _bucket_for(result["momentum"], 0.6)
        assert bucket.n == 20
        assert bucket.benchmarked_n == 10
        assert bucket.hit_rate == pytest.approx(1.0)
        assert bucket.mean_confidence == pytest.approx(0.65, rel=1e-6)
        assert bucket.market_mean_probability == pytest.approx(0.70, rel=1e-6)

    def test_benchmarked_n_below_threshold_nulls_market_mean_but_keeps_count(self):
        benchmarked = [_Pred(id=i, confidence=0.65, outcome=Outcome.UPPER) for i in range(2)]
        unbenchmarked = [
            _Pred(id=100 + i, confidence=0.65, outcome=Outcome.UPPER) for i in range(8)
        ]
        benchmarks = {str(p.id): Benchmark(market_probability=0.70) for p in benchmarked}
        result = calibration_with_benchmark(benchmarked + unbenchmarked, benchmarks)
        bucket = _bucket_for(result["momentum"], 0.6)
        assert bucket.n == 10
        assert bucket.benchmarked_n == 2
        assert bucket.market_mean_probability is None
        assert bucket.hit_rate == pytest.approx(1.0)


class TestCalibrationTopBucketEdgeCase:
    def test_confidence_of_one_lands_in_the_top_bucket(self):
        preds = [
            _Pred(id=i, confidence=1.0, outcome=Outcome.UPPER) for i in range(10)
        ]
        result = calibration_with_benchmark(preds, {})
        top = _bucket_for(result["momentum"], 0.9)
        assert top.n == 10
        assert top.hit_rate == pytest.approx(1.0)


class TestLikeForLikeAndProperScores:
    def test_the_benchmarked_subset_is_reported_on_its_own(self):
        """The benchmarked ten all hit; the unbenchmarked ten all miss.

        `hit_rate` over all twenty is 0.5 and the market mean is over the ten.
        Set side by side those describe two samples. The like-for-like columns
        describe the one the market number was taken over.
        """
        benchmarked = [_Pred(id=i, confidence=0.62, outcome=Outcome.UPPER) for i in range(10)]
        unbenchmarked = [
            _Pred(id=100 + i, confidence=0.68, outcome=Outcome.LOWER) for i in range(10)
        ]
        benchmarks = {str(p.id): Benchmark(market_probability=0.70) for p in benchmarked}
        bucket = _bucket_for(
            calibration_with_benchmark(benchmarked + unbenchmarked, benchmarks)["momentum"],
            0.6,
        )
        assert bucket.hit_rate == pytest.approx(0.5)
        assert bucket.mean_confidence == pytest.approx(0.65)
        assert bucket.benchmarked_hit_rate == pytest.approx(1.0)
        assert bucket.benchmarked_mean_confidence == pytest.approx(0.62)
        assert bucket.benchmarked_brier == pytest.approx((0.62 - 1.0) ** 2)

    def test_the_hit_rate_carries_a_wilson_interval(self):
        preds = [
            _Pred(id=i, confidence=0.65,
                  outcome=Outcome.UPPER if i < 7 else Outcome.LOWER)
            for i in range(10)
        ]
        bucket = _bucket_for(calibration_with_benchmark(preds, {})["momentum"], 0.6)
        low, high = bucket.hit_rate_interval
        assert low == pytest.approx(0.3968, abs=1e-4)
        assert high == pytest.approx(0.8922, abs=1e-4)

    def test_brier_is_the_mean_squared_error_of_stated_confidence(self):
        preds = [
            _Pred(id=i, confidence=0.65,
                  outcome=Outcome.UPPER if i < 7 else Outcome.LOWER)
            for i in range(10)
        ]
        bucket = _bucket_for(calibration_with_benchmark(preds, {})["momentum"], 0.6)
        assert bucket.brier == pytest.approx((7 * 0.35**2 + 3 * 0.65**2) / 10)

    def test_a_thin_bucket_reports_no_interval_or_score(self):
        preds = [_Pred(id=i, confidence=0.65, outcome=Outcome.UPPER) for i in range(3)]
        bucket = _bucket_for(calibration_with_benchmark(preds, {})["momentum"], 0.6)
        assert bucket.hit_rate_interval is None
        assert bucket.brier is None
        assert bucket.benchmarked_hit_rate is None
