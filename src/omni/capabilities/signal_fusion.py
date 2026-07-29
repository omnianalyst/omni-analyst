"""Signal fusion: normalisation, convergence, lead-lag.

Ported from v1 `app/services/signal_fusion/` -- the three engines that operate
on already-collected signal values:

- `normalization_engine.py` -- putting heterogeneous signals on one scale.
- `convergence_engine.py`   -- agreement across independent signals.
- `lead_lag_analyzer.py`    -- which signal moves first.

What carried, and how:

- The seven normalisation methods (IDENTITY / SIGN / Z_SCORE / PERCENTILE /
  MIN_MAX / TANH / RANK) are ported via `normalize()`, bit-for-bit with v1's
  `_apply_normalization` including the rolling helpers and the z-score clip.
- Convergence math: weighted direction (`np.average`), alignment
  (`1 - population std`, clamped), bull/bear/neutral counts, and pairwise
  divergences. The independence-vote de-duplication that stops a source and its
  proxies double-counting is ported as a pure `independence_votes()` helper --
  it was the one genuinely good idea in v1's convergence path and is the same
  honesty `Capability.is_proxy` / `proxy_of` carries in the v2 registry.
- Lead-lag cross-correlation: `cross_correlation_at_lag()` is the building
  block (one lag, one correlation); `lead_lag()` scans the lag range and
  returns the lag maximising `|correlation|`, with v1's t-stat significance.

What did NOT carry, and why:

- Every SQLAlchemy/`AsyncSession` path (`store_normalized`, `store_snapshot`,
  `compute_convergence_vector`, `get_signal_freshness`, `compute_history`).
  PORTING.md drops framework tangle; these are pure functions.
- `SignalRegistry` is not imported. It is already harvested into
  `src/omni/capability/registry.py` as `Capability` (with `is_proxy` /
  `proxy_of`), and a second registry is how this project got two incompatible
  ones last time. Method/window/native-range/inversion that v1 read off a
  `SignalDefinition` are explicit arguments here; proxy metadata is passed into
  `independence_votes()` by the caller who owns the registry.
- The regime weight table (`REGIME_WEIGHT_ADJUSTMENTS`) read `sig.category`
  from the registry. Category-aware weighting belongs to whatever wires
  capabilities into a plan, not to convergence arithmetic. Weights are an
  explicit input to `convergence()`.
- v1's `conviction = 0.6*alignment + 0.2*participation + 0.2*|direction|` is
  preserved as a helper, but `participation` is a *coverage* concept (what
  fraction of the independent scope is present). Folding it into a fusion
  number double-counts the coverage layer, so it is an explicit, non-defaulted
  argument to `conviction()` -- never fabricated.

Defaults removed (raise `Unavailable` instead, per the work order):

- v1's Z_SCORE returned `np.zeros_like(values)` when `window < 2` -- a neutral
  score fabricated from insufficient data. Raises.
- v1's Z_SCORE substituted `rolling_std = 1.0` wherever std was ~0, which turns
  a globally constant signal into a row of zeros (looks neutral, is undefined).
  A constant input raises; the local numerical guard is kept bit-for-bit for
  series that merely contain a flat stretch.
- v1's MIN_MAX returned zeros when `native_range` was ~0. Raises.
- v1's convergence returned `None` for fewer than 3 signals (silent), and fell
  back to uniform weights when total weight was 0 (a default). Both raise.
- v1's lead-lag returned `None` for `<30` overlapping points or low
  significance, and substituted `corr = 0.0` whenever a lag exceeded the series
  or a slice was constant. The structural failures raise; significance is
  reported, not used to silently drop an edge.

`edge_gate.py` and `pattern_miner.py` were read and judged rather than ported.
See `_orchestrator/reports/J4.md` for that comparison; it is worth more than
the ports would have been.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from enum import Enum

import numpy as np

from omni.ingest.protocol import Unavailable


class NormalizationMethod(str, Enum):
    Z_SCORE = "z_score"
    PERCENTILE = "percentile"
    MIN_MAX = "min_max"
    SIGN = "sign"
    TANH = "tanh"
    IDENTITY = "identity"
    RANK = "rank"


# v1's chosen thresholds, carried as named constants. None are tuned here and
# none are invented.
_ZSCORE_CLIP = 3.0  # v1 divides the rolling z by 3 then clips to [-1, 1]
_STD_EPS = 1e-8
_BULL_THRESHOLD = 0.15  # v1 counts a signal bullish at v > 0.15
_BEAR_THRESHOLD = -0.15
_DIVERGENCE_THRESHOLD = 1.0  # v1 reports a pair when |va - vb| > 1.0
_MAX_DIVERGENCES = 5
_MIN_OVERLAP = 30  # v1 drops a lead-lag pair below 30 shared observations
_SIGNIFICANCE_SCALE = 2.5  # v1: significance = min(1, |t| / 2.5)


# ---------------------------------------------------------------------------
# Normalisation
# ---------------------------------------------------------------------------


def _require_nonempty(values: np.ndarray) -> None:
    if values.size == 0:
        raise Unavailable("empty signal; nothing to normalise")


def z_score(values: Sequence[float]) -> np.ndarray:
    """Textbook global z-score: `(x - mean) / population_std`.

    The result has mean exactly 0 and standard deviation exactly 1 (population,
    ddof=0, matching v1's `np.std`). This is what "normalising to z-scores"
    means; v1's registry `Z_SCORE` method is a different, rolling-and-clipped
    transform -- see `normalize(method=NormalizationMethod.Z_SCORE)`.

    A constant signal has no scale, so its z-score is undefined: raises
    `Unavailable` rather than returning zeros.
    """
    arr = np.asarray(values, dtype=float)
    _require_nonempty(arr)
    sigma = float(np.std(arr))
    if sigma < _STD_EPS:
        raise Unavailable("constant signal; z-score is undefined (zero variance)")
    return (arr - float(np.mean(arr))) / sigma


def _rolling_mean(values: np.ndarray, window: int) -> np.ndarray:
    if len(values) < window:
        return np.full_like(values, float(np.mean(values)))
    cumsum = np.cumsum(np.insert(values, 0, 0))
    result = np.empty_like(values, dtype=float)
    full_rolling = (cumsum[window:] - cumsum[:-window]) / window
    result[: window - 1] = values[: window - 1]
    result[window - 1 :] = full_rolling
    mean = float(np.mean(values[:window]))
    result[: window - 1] = mean
    return result


def _rolling_std(values: np.ndarray, window: int) -> np.ndarray:
    result = np.empty_like(values, dtype=float)
    if len(values) < window:
        return np.full_like(values, float(np.std(values)))
    for i in range(min(window - 1, len(values))):
        result[i] = float(np.std(values[: max(i + 1, 2)]))
    for i in range(window - 1, len(values)):
        result[i] = float(np.std(values[i - window + 1 : i + 1]))
    return result


def normalize(
    values: Sequence[float],
    method: NormalizationMethod,
    *,
    window: int | None = None,
    native_range: tuple[float, float] | None = None,
    inverted: bool = False,
) -> np.ndarray:
    """Port of v1's `NormalizationEngine._apply_normalization`, dispatch verbatim.

    `window` is required for the rolling methods (Z_SCORE / PERCENTILE / RANK);
    `native_range` is required for MIN_MAX. `inverted` flips the sign of the
    result, exactly as v1's `sig.inverted` did.

    Deviations from v1, all per the work order: Z_SCORE raises on `window < 2`
    and on a globally constant input (v1 returned zeros); MIN_MAX raises on a
    zero native range (v1 returned zeros). The rolling-std numerical guard is
    kept bit-for-bit for series that merely contain a flat stretch.
    """
    arr = np.asarray(values, dtype=float)
    _require_nonempty(arr)

    if method is NormalizationMethod.IDENTITY:
        out = arr
    elif method is NormalizationMethod.SIGN:
        out = np.sign(arr)
    elif method is NormalizationMethod.TANH:
        out = np.tanh(arr)
    elif method is NormalizationMethod.Z_SCORE:
        if window is None or window < 2:
            raise Unavailable(f"z_score needs window >= 2, got {window}")
        if float(np.ptp(arr)) == 0.0:
            raise Unavailable("constant signal; rolling z-score is undefined")
        w = min(window, len(arr))
        rmean = _rolling_mean(arr, w)
        rstd = _rolling_std(arr, w)
        rstd = np.where(rstd < _STD_EPS, 1.0, rstd)
        z = (arr - rmean) / rstd
        out = np.clip(z / _ZSCORE_CLIP, -1.0, 1.0)
    elif method is NormalizationMethod.PERCENTILE:
        if window is None or window < 1:
            raise Unavailable(f"percentile needs window >= 1, got {window}")
        w = min(window, len(arr))
        result = np.zeros_like(arr)
        for i in range(len(arr)):
            start = max(0, i - w + 1)
            window_vals = arr[start : i + 1]
            result[i] = np.mean(window_vals <= arr[i])
        out = result * 2.0 - 1.0
    elif method is NormalizationMethod.RANK:
        if window is None or window < 1:
            raise Unavailable(f"rank needs window >= 1, got {window}")
        w = min(window, len(arr))
        result = np.zeros_like(arr)
        for i in range(len(arr)):
            start = max(0, i - w + 1)
            window_vals = arr[start : i + 1]
            rank = np.searchsorted(np.sort(window_vals), arr[i])
            result[i] = rank / max(len(window_vals) - 1, 1) * 2.0 - 1.0
        out = result
    elif method is NormalizationMethod.MIN_MAX:
        if native_range is None:
            raise Unavailable("min_max requires native_range")
        lo, hi = native_range
        span = hi - lo
        if abs(span) < _STD_EPS:
            raise Unavailable("min_max native_range has zero span; transform is undefined")
        out = ((arr - lo) / span) * 2.0 - 1.0
    else:  # pragma: no cover - exhaustive enum
        raise Unavailable(f"unknown normalisation method: {method!r}")

    if inverted:
        out = -out
    return out


# ---------------------------------------------------------------------------
# Convergence
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Divergence:
    signal_a: str
    signal_b: str
    delta: float


@dataclass(frozen=True)
class Convergence:
    direction: float
    alignment: float
    bullish: int
    bearish: int
    neutral: int
    divergences: tuple[Divergence, ...]


def alignment(values: Sequence[float]) -> float:
    """Agreement across signals: `1 - population_std`, clamped to [0, 1].

    Identical signals have std 0 -> alignment 1.0 (maximal). Signals spread
    across the band have std >= 1 -> alignment 0.0 (minimal). Uses population
    std (ddof=0), matching v1's `np.std(values)`.
    """
    arr = np.asarray(values, dtype=float)
    _require_nonempty(arr)
    a = 1.0 - float(np.std(arr))
    return max(0.0, min(1.0, a))


def direction(values: Sequence[float], weights: Sequence[float] | None = None) -> float:
    """Weighted mean of signal values. Equal weights when `weights` is None."""
    arr = np.asarray(values, dtype=float)
    _require_nonempty(arr)
    if weights is None:
        return float(np.mean(arr))
    return float(np.average(arr, weights=np.asarray(weights, dtype=float)))


def _find_divergences(
    signal_values: Mapping[str, float],
    max_results: int = _MAX_DIVERGENCES,
) -> list[Divergence]:
    items = list(signal_values.items())
    found: list[Divergence] = []
    for i in range(len(items)):
        for j in range(i + 1, len(items)):
            sa, va = items[i]
            sb, vb = items[j]
            delta = va - vb
            if abs(delta) > _DIVERGENCE_THRESHOLD:
                found.append(Divergence(sa, sb, delta))
    found.sort(key=lambda d: abs(d.delta), reverse=True)
    return found[:max_results]


def independence_votes(
    signal_ids: Sequence[str],
    proxy_of: Mapping[str, Sequence[str]],
) -> dict[str, float]:
    """Fractional vote per signal so a source and its proxies don't double-count.

    Ported from v1's `ConvergenceEngine._independence_votes` (the live
    `bucket_of` / `buckets` path; v1 also built a `groups` dict it never read,
    which is dropped here). Signals whose proxy root is present merge into that
    root's bucket; proxies of an absent shared root merge together; independent
    signals keep a full vote. Each bucket's members split one unit vote evenly.

    `proxy_of` maps a proxy signal id to the underlying root ids it is derived
    from -- the same relationship `Capability.proxy_of` already carries. The
    caller, who owns the registry, supplies it; this function stays pure.
    """
    present = set(signal_ids)
    bucket_of: dict[str, str] = {}
    for sid in signal_ids:
        roots = proxy_of.get(sid)
        if roots:
            present_root = next((r for r in roots if r in present), None)
            bucket_of[sid] = present_root if present_root else "|".join(sorted(roots))
        else:
            bucket_of[sid] = sid

    buckets: dict[str, list[str]] = {}
    for sid, bucket in bucket_of.items():
        buckets.setdefault(bucket, []).append(sid)

    votes: dict[str, float] = {}
    for members in buckets.values():
        share = 1.0 / len(members)
        for sid in members:
            votes[sid] = share
    return votes


def convergence(
    signal_values: Mapping[str, float],
    *,
    weights: Mapping[str, float] | None = None,
    votes: Mapping[str, float] | None = None,
) -> Convergence:
    """Fuse one date's signal vector into a directional reading.

    `signal_values` are already on a common [-1, +1] scale (via `normalize`).
    `weights` scales each signal (e.g. by reliability); `votes` sets how many
    independent votes each casts (use `independence_votes(...)` to de-duplicate
    proxies, as v1 did). When either is omitted, every present signal counts
    once -- no absent signal is invented.

    Raises `Unavailable` when fewer than two signals are present (agreement is
    undefined for one) or when the supplied weights sum to zero (v1 silently
    fell back to uniform weights here, treating dead signals as equally live).
    """
    if len(signal_values) < 2:
        raise Unavailable(f"convergence undefined for {len(signal_values)} signal(s); need >= 2")

    sids = list(signal_values.keys())
    vals = list(signal_values.values())

    weight_arr: list[float] | None = None
    if weights is not None:
        weight_arr = [float(weights[s]) for s in sids]
        if sum(weight_arr) <= 0.0:
            raise Unavailable("weights sum to zero; no signal carries weight")

    d = direction(vals, weight_arr)
    al = alignment(vals)

    vote_of = votes if votes is not None else {s: 1.0 for s in sids}
    bull = sum(vote_of[s] for s, v in zip(sids, vals) if v > _BULL_THRESHOLD)
    bear = sum(vote_of[s] for s, v in zip(sids, vals) if v < _BEAR_THRESHOLD)
    bull_i = round(bull)
    bear_i = round(bear)
    neutral_i = max(0, len(vals) - bull_i - bear_i)

    divergences = tuple(_find_divergences(signal_values))
    return Convergence(
        direction=d,
        alignment=al,
        bullish=bull_i,
        bearish=bear_i,
        neutral=neutral_i,
        divergences=divergences,
    )


def conviction(
    alignment_value: float,
    direction_value: float,
    *,
    participation: float,
) -> float:
    """v1's conviction assembly: `0.6*alignment + 0.2*participation + 0.2*|dir|`.

    `participation` is the fraction of the independent signal scope that is
    present -- a coverage concept the caller supplies from the coverage layer.
    It is required, not defaulted: inventing a breadth number is exactly the
    kind of substitution this port exists to remove. v1 baked this into the
    convergence result using a registry-wide denominator; v2 keeps fusion
    arithmetic and coverage scope separate.
    """
    if not 0.0 <= participation <= 1.0:
        raise Unavailable(f"participation must be in [0, 1], got {participation}")
    return 0.6 * alignment_value + 0.2 * participation + 0.2 * abs(direction_value)


# ---------------------------------------------------------------------------
# Lead-lag
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class LeadLag:
    lag: int
    correlation: float
    significance: float


def cross_correlation_at_lag(a: Sequence[float], b: Sequence[float], lag: int) -> float:
    """Pearson correlation of `a` and `b` at a single shift.

    Sign convention (carried from v1's `_compute_cross_correlation`):
    a positive `lag` means **`a` leads `b`**: it pairs `a[:-lag]` with
    `b[lag:]` -- `a`'s past with `b`'s future. A negative `lag` means `b`
    leads `a`. `lag == 0` is the unshifted correlation.

    Raises `Unavailable` on mismatched lengths, a lag the series cannot support,
    or a constant slice (correlation undefined). v1 substituted `0.0` in each of
    these cases -- an assumed correlation, which is the failure mode this port
    removes.
    """
    arr_a = np.asarray(a, dtype=float)
    arr_b = np.asarray(b, dtype=float)
    if arr_a.shape[0] != arr_b.shape[0]:
        raise Unavailable(f"mismatched lengths: {arr_a.shape[0]} vs {arr_b.shape[0]}")
    n = arr_a.shape[0]
    if n < 2:
        raise Unavailable(f"need >= 2 points, got {n}")

    if lag == 0:
        x, y = arr_a, arr_b
    elif lag > 0:
        if n <= lag:
            raise Unavailable(f"lag {lag} >= series length {n}")
        x, y = arr_a[:-lag], arr_b[lag:]
    else:
        k = -lag
        if n <= k:
            raise Unavailable(f"lag {lag} >= series length {n}")
        x, y = arr_a[k:], arr_b[:-k]

    if float(np.std(x)) < 1e-12 or float(np.std(y)) < 1e-12:
        raise Unavailable("constant slice; correlation undefined")
    corr = float(np.corrcoef(x, y)[0, 1])
    if np.isnan(corr):
        raise Unavailable("correlation is NaN")
    return corr


def lead_lag(a: Sequence[float], b: Sequence[float], *, max_lag: int) -> LeadLag:
    """Find the shift maximising `|correlation|` and report its significance.

    Scans `lag` in `[-max_lag, max_lag]`. Returns the best lag (positive =>
    `a` leads `b`), its correlation, and v1's significance
    (`min(1, |t| / 2.5)` with `t = r * sqrt(n_eff - 2) / sqrt(1 - r^2)`).

    Raises `Unavailable` on mismatched lengths, `max_lag >= series length` (the
    scan would query lags the series cannot support), a constant series, or
    fewer than `_MIN_OVERLAP` points. v1 returned `None` for the last two and
    substituted `0.0` for uncomputable lags; both fed silence into the graph.
    Significance is reported, not used to silently drop an edge -- deciding
    whether a weak edge is worth keeping is the caller's job.
    """
    arr_a = np.asarray(a, dtype=float)
    arr_b = np.asarray(b, dtype=float)
    if arr_a.shape[0] != arr_b.shape[0]:
        raise Unavailable(f"mismatched lengths: {arr_a.shape[0]} vs {arr_b.shape[0]}")
    n = arr_a.shape[0]
    if max_lag >= n:
        raise Unavailable(f"max_lag {max_lag} >= series length {n}")
    if n < 2:
        raise Unavailable(f"need >= 2 points, got {n}")
    if n < _MIN_OVERLAP:
        raise Unavailable(f"need >= {_MIN_OVERLAP} overlapping points, got {n}")
    if float(np.std(arr_a)) < 1e-12 or float(np.std(arr_b)) < 1e-12:
        raise Unavailable("constant series; lead-lag undefined")

    best_lag = 0
    best_corr = 0.0
    for lag in range(-max_lag, max_lag + 1):
        corr = cross_correlation_at_lag(arr_a, arr_b, lag)
        if abs(corr) > abs(best_corr):
            best_corr = corr
            best_lag = lag

    n_eff = n - abs(best_lag)
    if n_eff >= 3:
        t_stat = best_corr * (n_eff - 2) ** 0.5 / (1 - best_corr**2 + 1e-10) ** 0.5
        significance = min(1.0, abs(t_stat) / _SIGNIFICANCE_SCALE)
    else:
        significance = 0.0
    return LeadLag(lag=best_lag, correlation=best_corr, significance=significance)
