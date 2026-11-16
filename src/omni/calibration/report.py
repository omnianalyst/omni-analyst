"""Calibration report with the market benchmark column (K-03).

P-02's ``prediction_ledger.calibration`` reports two numbers per confidence
bucket: stated confidence and realised hit rate. This module produces the
*same* bucketing with two extra columns drawn from the benchmark store:

- ``market_mean_probability`` — the mean market-implied probability across
  the benchmarked predictions that landed in this bucket.
- ``benchmarked_n``           — how many of the bucket's predictions carry a
  benchmark (always ≤ ``n``).

Reported side by side, the four numbers answer the only question that
justifies the prediction-markets surface in an OSS analysis project: *did we
add anything over a freely available crowd estimate?* "Our model said 65%,
the market said 71%, we were right 58% of the time" is a far richer
statement than hit rate alone.

The function is pure (no DB, no Redis, no IO) so it can be tested with
synthetic inputs and reused by any caller. ``benchmarks`` is keyed by the
``str`` form of ``Prediction.id`` — the same shape ``list_benchmarks``
returns.

Small-sample suppression mirrors P-02's rule: a bucket with fewer than
``min_bucket_n`` resolved predictions reports ``hit_rate`` and
``mean_confidence`` as ``None``, and a bucket with fewer than
``min_bucket_n`` *benchmarked* resolved predictions additionally reports
``market_mean_probability`` as ``None``. ``benchmarked_n`` is always present
so a consumer can see whether the missing market number is "no benchmarks"
or "too few to state a mean".

This module does not import the prediction-ledger service or model: it
operates on whatever ``Prediction``-shaped objects the caller passes, as
long as they expose ``method``, ``confidence`` (a number), ``direction``
and ``outcome`` (with ``.value`` members comparable to
``"pending" / "upper" / "lower" / "expiry"``). It is therefore safe to add
columns to ``Prediction`` without touching this file.
"""
from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from omni.calibration import Benchmark, Direction, Outcome


@dataclass
class BenchmarkCalibrationBucket:
    bucket_low: float
    bucket_high: float
    n: int
    hit_rate: float | None
    mean_confidence: float | None
    benchmarked_n: int
    market_mean_probability: float | None
    # The 95% Wilson interval on `hit_rate`. A rate without one invites reading
    # 7 of 10 as 70%; the interval for that is [0.40, 0.89].
    hit_rate_interval: tuple[float, float] | None = None
    # Mean squared error of the stated confidence against the 0/1 outcome over
    # the bucket. A proper score: it is minimised by stating the true rate, so
    # it cannot be improved by hedging or by overclaiming.
    brier: float | None = None
    # The model's numbers over the BENCHMARKED subset only -- the same
    # predictions `market_mean_probability` is taken over. `hit_rate` and
    # `mean_confidence` cover all `n`; set beside a market mean over a subset
    # they compare two different samples, and a difference between them is as
    # likely to be which predictions happened to have a market as anything
    # about skill. These three are the like-for-like comparison.
    benchmarked_hit_rate: float | None = None
    benchmarked_mean_confidence: float | None = None
    benchmarked_brier: float | None = None


_Z_95 = 1.959963984540054


def _wilson(hits: int, n: int) -> tuple[float, float]:
    p = hits / n
    z_sq = _Z_95 * _Z_95
    denominator = 1.0 + z_sq / n
    centre = (p + z_sq / (2 * n)) / denominator
    half = (_Z_95 / denominator) * (p * (1.0 - p) / n + z_sq / (4 * n * n)) ** 0.5
    return max(0.0, centre - half), min(1.0, centre + half)


def _brier(rows: Sequence) -> float:
    return sum(
        (float(p.confidence) - (1.0 if _hit(p.direction, p.outcome) else 0.0)) ** 2
        for p in rows
    ) / len(rows)


def calibration_with_benchmark(
    predictions: Sequence,
    benchmarks: Mapping[str, Benchmark],
    *,
    n_buckets: int = 10,
    min_bucket_n: int = 10,
) -> dict[str, list[BenchmarkCalibrationBucket]]:
    """Bucket resolved predictions by stated confidence, per ``method``.

    Returns ``{method: [BenchmarkCalibrationBucket, ...]}`` sorted by bucket
    low edge, with one bucket per decile regardless of whether anything
    landed in it (so consumers can render a complete reliability curve).

    Parameters
    ----------
    predictions
        Any sequence of ``Prediction``-shaped rows. Pending predictions are
        excluded from every bucket (calibration only counts resolved rows);
        they are silently dropped, matching P-02's behaviour.
    benchmarks
        Map of ``str(prediction.id) -> Benchmark``. Predictions whose id is
        not in the map are counted in ``n`` but not in ``benchmarked_n``.
    n_buckets
        Number of equal-width confidence buckets on ``[0, 1]``. Default 10
        (deciles).
    min_bucket_n
        Smallest ``n`` for which a per-bucket hit rate / mean confidence is
        reported. Mirrors the P-02 ``MIN_BUCKET_N`` rule. The same threshold
        is applied to ``benchmarked_n`` before reporting
        ``market_mean_probability``.
    """
    if n_buckets < 1:
        raise ValueError("n_buckets must be >= 1")
    if min_bucket_n < 1:
        raise ValueError("min_bucket_n must be >= 1")

    by_method: dict[str, list] = {}
    for p in predictions:
        outcome = getattr(p, "outcome", None)
        if outcome is None or outcome == Outcome.PENDING:
            continue
        method = getattr(p, "method", None)
        if method is None:
            continue
        by_method.setdefault(method, []).append(p)

    edges = [(i / n_buckets, (i + 1) / n_buckets) for i in range(n_buckets)]
    out: dict[str, list[BenchmarkCalibrationBucket]] = {}
    for method, preds in by_method.items():
        buckets: list[BenchmarkCalibrationBucket] = []
        for i, (low, high) in enumerate(edges):
            is_top = i == n_buckets - 1
            in_bucket = [
                p for p in preds
                if low <= float(p.confidence) < high
                or (is_top and float(p.confidence) == 1.0)
            ]
            n = len(in_bucket)
            hits = sum(1 for p in in_bucket if _hit(p.direction, p.outcome))

            benchmarked_preds = [p for p in in_bucket if str(p.id) in benchmarks]
            benchmarked = [benchmarks[str(p.id)] for p in benchmarked_preds]
            benchmarked_n = len(benchmarked)
            market_probs = [float(b.market_probability) for b in benchmarked]

            if n >= min_bucket_n:
                hit_rate: float | None = hits / n
                mean_conf: float | None = (
                    sum(float(p.confidence) for p in in_bucket) / n
                )
                interval: tuple[float, float] | None = _wilson(hits, n)
                brier: float | None = _brier(in_bucket)
            else:
                hit_rate = None
                mean_conf = None
                interval = None
                brier = None

            if benchmarked_n >= min_bucket_n:
                market_mean: float | None = sum(market_probs) / benchmarked_n
                b_hits = sum(
                    1 for p in benchmarked_preds if _hit(p.direction, p.outcome)
                )
                b_hit_rate: float | None = b_hits / benchmarked_n
                b_mean_conf: float | None = (
                    sum(float(p.confidence) for p in benchmarked_preds)
                    / benchmarked_n
                )
                b_brier: float | None = _brier(benchmarked_preds)
            else:
                market_mean = None
                b_hit_rate = None
                b_mean_conf = None
                b_brier = None

            buckets.append(
                BenchmarkCalibrationBucket(
                    bucket_low=low,
                    bucket_high=high,
                    n=n,
                    hit_rate=hit_rate,
                    mean_confidence=mean_conf,
                    benchmarked_n=benchmarked_n,
                    market_mean_probability=market_mean,
                    hit_rate_interval=interval,
                    brier=brier,
                    benchmarked_hit_rate=b_hit_rate,
                    benchmarked_mean_confidence=b_mean_conf,
                    benchmarked_brier=b_brier,
                )
            )
        out[method] = buckets
    return out


def _hit(direction: Direction, outcome: Outcome) -> bool:
    """Same direction-vs-outcome hit rule as ``prediction_ledger._hit``.

    Duplicated here on purpose: importing the helper from the ledger service
    would couple this pure module to a service that may evolve independently.
    The rule itself is small and stable.
    """
    if direction == Direction.UP:
        return outcome == Outcome.UPPER
    if direction == Direction.DOWN:
        return outcome == Outcome.LOWER
    if direction == Direction.NEUTRAL:
        return outcome == Outcome.EXPIRY
    return False
