"""Stage A calibration harness.

The flow: resolved markets -> cutoff snapshot -> LLM estimation -> bucketed
calibration vs market benchmark. The calibration function we feed is the
existing `omni.calibration.report.calibration_with_benchmark`, used unchanged;
we build its inputs and read its output.

**Non-invasive by construction.** Nothing here writes to the `prediction`
table, opens a venue, or imports from `conviction/`, `trading/`, `venue/`,
or `llm/`. The `LanguageModel` is the only seam and it is passed in.

**Exclusions are surfaced, not hidden.** A market we cannot honestly score
(no price sample near cutoff, an upstream outage, a model refusal) is returned
in the report's `exclusions` list with a reason. The counts in
`method_buckets` are over the markets we *did* estimate; a Stage A run that
quietly skipped half its sample would read as more calibrated than it was.
"""

from __future__ import annotations

import math
import random
from collections.abc import Awaitable, Callable, Hashable, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta

import httpx

from omni.calibration import Benchmark, Direction, Outcome
from omni.calibration.report import BenchmarkCalibrationBucket, calibration_with_benchmark
from omni.ingest.protocol import Unavailable
from omni.llm.protocol import LanguageModel
from omni.polymarket.estimator import estimate, estimation_id
from omni.polymarket.gamma import fetch_price_history
from omni.polymarket.pnl import (
    DEFAULT_FEE_RATES,
    DEFAULT_HALF_SPREAD,
    Fill,
    PnLSummary,
    side_and_entry,
    summarise,
)
from omni.polymarket.types import Document, Estimation, MarketAtCutoff, ResolvedMarket

DEFAULT_HORIZON = timedelta(days=7)
DEFAULT_TOLERANCE = timedelta(hours=6)
_EPS = 1e-6

DocumentProvider = Callable[
    [httpx.AsyncClient, ResolvedMarket, datetime],
    Awaitable[tuple[Document, ...]],
]


@dataclass(frozen=True)
class _Prediction:
    """The Prediction-shaped object `calibration_with_benchmark` reads.

    Local and pure — never persisted. Fields are exactly those the
    calibration function inspects via `getattr`, no more, so adding a column
    to the real `prediction` table cannot drift this stub out of sync.
    """

    id: str
    method: str
    confidence: float
    direction: Direction
    outcome: Outcome


@dataclass(frozen=True)
class Exclusion:
    market: ResolvedMarket
    reason: str

    def __post_init__(self) -> None:
        if not self.reason.strip():
            raise ValueError("exclusion reason must be non-empty")


@dataclass(frozen=True)
class StageAReport:
    """What Stage A produces: bucketed calibration, summary scores, and the
    markets it refused to score.

    `brier_score` and `log_loss` are mean scores over the estimated set; lower
    is better for both. `market_brier_score` is the same metric applied to the
    market's pre-cutoff price — the freely available crowd estimate. The
    single comparison `brier_score - market_brier_score` is what Stage A
    exists to measure: a negative number says our estimate beat the market on
    raw accuracy. (It says nothing yet about whether the edge survives fees,
    slippage or fill risk — that is what `pnl_summary` is for.)

    `pnl_summary` is the dollar view: what trading each prediction at the
    market's cutoff price (taker by default: V2 fee curve plus half the
    spread) would have earned, on the side of the disagreement,
    filtering to trades where `|llm_prob - market_prob| >= pnl_threshold`.
    `None` means no threshold-crossing trades — the model agreed with the
    market on everything, so there was nothing to trade.

    `trade_pairs` is the in-memory record of `(Estimation, MarketAtCutoff)`
    for each successful LLM call. Not persisted — lives only as long as the
    report. Used by `sweep_thresholds()` to apply multiple P&L thresholds to
    the same LLM outputs without re-calling the model.
    """

    method_buckets: dict[str, list[BenchmarkCalibrationBucket]]
    n_estimated: int
    exclusions: list[Exclusion] = field(default_factory=list)
    brier_score: float | None = None
    market_brier_score: float | None = None
    log_loss: float | None = None
    method: str = "polymarket_llm_v1"
    pnl_summary: PnLSummary | None = None
    trade_pairs: list = field(default_factory=list)
    # A 95% interval on `brier_edge` from a paired bootstrap that resamples
    # whole event clusters. `None` when fewer than `_MIN_BOOTSTRAP_CLUSTERS`
    # clusters were scored -- an interval over two clusters is not one.
    brier_edge_interval: tuple[float, float] | None = None
    brier_edge_clusters: int = 0

    @property
    def brier_edge_significant(self) -> bool | None:
        """Whether the edge's interval excludes zero. `None` when no interval
        could be measured; a point estimate alone is never called significant."""
        if self.brier_edge_interval is None:
            return None
        low, high = self.brier_edge_interval
        return high < 0.0 or low > 0.0

    @property
    def n_excluded(self) -> int:
        return len(self.exclusions)

    @property
    def brier_edge(self) -> float | None:
        if self.brier_score is None or self.market_brier_score is None:
            return None
        return self.brier_score - self.market_brier_score


def _outcome_of(resolved_yes: bool) -> Outcome:
    return Outcome.UPPER if resolved_yes else Outcome.LOWER


def _p_yes_of(est: Estimation) -> float:
    return est.confidence if est.direction == "up" else 1.0 - est.confidence


def _brier(p_yes: Sequence[float], outcomes: Sequence[bool]) -> float | None:
    if len(p_yes) != len(outcomes) or not p_yes:
        return None
    total = sum((p - (1.0 if y else 0.0)) ** 2 for p, y in zip(p_yes, outcomes, strict=True))
    return total / len(p_yes)


def _log_loss(p_yes: Sequence[float], outcomes: Sequence[bool]) -> float | None:
    if len(p_yes) != len(outcomes) or not p_yes:
        return None
    total = 0.0
    for p, y in zip(p_yes, outcomes, strict=True):
        p_clamped = min(max(p, _EPS), 1.0 - _EPS)
        actual = 1.0 if y else 0.0
        total -= actual * math.log(p_clamped) + (1.0 - actual) * math.log(1.0 - p_clamped)
    return total / len(p_yes)


_MIN_BOOTSTRAP_CLUSTERS = 10
_BOOTSTRAP_DRAWS = 2000


def default_event_cluster(market: ResolvedMarket) -> Hashable:
    """The cluster a market's outcome is assumed to share.

    `ResolvedMarket` carries no event id, so this is a stated proxy: markets in
    one category resolving on the same UTC day are treated as one event (the
    strikes of one price ladder, the candidates of one election). It errs
    toward merging, which widens the interval -- the safe direction. Pass a
    real event key as `cluster_key` when one is available.
    """
    return (market.category, market.resolution_date.date().isoformat())


def paired_brier_edge_interval(
    p_model: Sequence[float],
    p_market: Sequence[float],
    outcomes: Sequence[bool],
    clusters: Sequence[Hashable],
    *,
    draws: int = _BOOTSTRAP_DRAWS,
    seed: int = 0,
    level: float = 0.95,
) -> tuple[float, float] | None:
    """Percentile interval on mean(model Brier - market Brier), clustered.

    Paired: each market contributes the DIFFERENCE of its two squared errors,
    so the shared outcome noise cancels. Clustered: whole clusters are
    resampled with replacement, because markets from one event resolve
    together and treating them as independent understates the interval.

    Returns `None` with fewer than `_MIN_BOOTSTRAP_CLUSTERS` distinct clusters.
    """
    n = len(outcomes)
    if not (len(p_model) == len(p_market) == len(clusters) == n) or n == 0:
        return None
    by_cluster: dict[Hashable, list[float]] = {}
    for pm, pk, y, c in zip(p_model, p_market, outcomes, clusters, strict=True):
        actual = 1.0 if y else 0.0
        by_cluster.setdefault(c, []).append((pm - actual) ** 2 - (pk - actual) ** 2)
    groups = list(by_cluster.values())
    if len(groups) < _MIN_BOOTSTRAP_CLUSTERS:
        return None
    rng = random.Random(seed)
    k = len(groups)
    means: list[float] = []
    for _ in range(draws):
        total = 0.0
        count = 0
        for _ in range(k):
            g = groups[rng.randrange(k)]
            total += sum(g)
            count += len(g)
        means.append(total / count)
    means.sort()
    alpha = (1.0 - level) / 2.0
    lo = means[math.floor(alpha * (draws - 1))]
    hi = means[math.ceil((1.0 - alpha) * (draws - 1))]
    return lo, hi


def _benchmark_for(snap: MarketAtCutoff, est_id: str) -> Benchmark:
    return Benchmark(market_probability=float(snap.market_probability))


async def prepare_snapshot(
    client: httpx.AsyncClient,
    market: ResolvedMarket,
    *,
    horizon: timedelta = DEFAULT_HORIZON,
    tolerance: timedelta = DEFAULT_TOLERANCE,
    document_provider: DocumentProvider | None = None,
) -> MarketAtCutoff | Exclusion:
    """Build a `MarketAtCutoff` for one resolved market, or an `Exclusion`.

    Exclusion cases (all honest refusals, not silent skips):
    - no `yes_token_id` (cannot query price history)
    - no price sample within `tolerance` of cutoff (would approximate)
    - the document provider raised (treated as no evidence)

    `document_provider` defaults to a stub that returns the market question
    itself as a single pre-cutoff document. The real evidence surface is a
    separate concern and is plugged in by the caller.
    """
    cutoff = market.resolution_date - horizon
    if cutoff <= market.created_at:
        cutoff = market.created_at + (market.resolution_date - market.created_at) / 2

    if market.yes_token_id is None:
        return Exclusion(market=market, reason="no yes_token_id; cannot price history")

    try:
        history = await fetch_price_history(
            client,
            token_id=market.yes_token_id,
            start=cutoff - tolerance,
            end=cutoff + tolerance,
        )
    except Unavailable as exc:
        return Exclusion(market=market, reason=f"price history unavailable: {exc}")

    tolerance_seconds = tolerance.total_seconds()
    within = [p for p in history if abs((p.at - cutoff).total_seconds()) <= tolerance_seconds]
    if not within:
        return Exclusion(
            market=market,
            reason=f"no price sample within {tolerance} of cutoff {cutoff.isoformat()}",
        )

    nearest = min(within, key=lambda p: abs((p.at - cutoff).total_seconds()))
    market_probability = float(nearest.yes_price)

    try:
        if document_provider is None:
            docs: tuple[Document, ...] = (
                Document(
                    at=market.created_at,
                    source=f"polymarket:market:{market.condition_id}",
                    text=market.question,
                ),
            )
        else:
            docs = await document_provider(client, market, cutoff)
    except Unavailable as exc:
        return Exclusion(market=market, reason=f"document provider unavailable: {exc}")

    try:
        return MarketAtCutoff(
            market=market,
            cutoff=cutoff,
            market_probability=market_probability,
            documents=docs,
        )
    except ValueError as exc:
        return Exclusion(market=market, reason=f"snapshot invalid: {exc}")


def _backtest_pnl(
    trades: list[tuple[Estimation, MarketAtCutoff]],
    *,
    threshold: float,
    size_usd: float,
    taker: bool,
    half_spread: float = DEFAULT_HALF_SPREAD,
) -> PnLSummary:
    """Dollar view of Stage A's predictions: what trading each one at the
    market's cutoff price would have earned, filtered to threshold-crossing
    disagreements.

    **The side is the side of the disagreement.** YES is bought when the
    model's P(yes) is ABOVE the market's, NO when it is below
    (`pnl.side_and_entry`). An earlier version chose the side from whether the
    model sat above 0.5, so a model at 0.65 against a market at 0.85 bought
    YES at 0.85 -- a trade its own estimate valued at -0.20 a share.

    Each trade is sized at `size_usd` of capital deployed, not equal shares.
    A taker pays the fee curve AND crosses half the spread: the entry price is
    the cutoff mid for the chosen side plus `half_spread`. `taker=False` is the
    explicit best case -- a resting order assumed to fill at mid for free --
    and charges neither. Fee rate is per-category, derived via
    `news.derive_category` against the documented Polymarket V2 rates
    (`pnl.DEFAULT_FEE_RATES`).

    `threshold` is the minimum `|llm_prob - market_prob|` required to open a
    trade.
    """
    from omni.polymarket.news import derive_category

    fills: list[Fill] = []
    for est, snap in trades:
        llm_p_yes = _p_yes_of(est)
        edge = abs(llm_p_yes - snap.market_probability)
        if edge < threshold:
            continue
        side = side_and_entry(
            model_p_yes=llm_p_yes,
            market_p_yes=snap.market_probability,
            half_spread=half_spread if taker else 0.0,
        )
        if side is None:
            continue
        direction, entry_price = side
        size_shares = size_usd / entry_price
        category = derive_category(snap.market)
        fee_rate = DEFAULT_FEE_RATES.get(category, 0.05)
        fills.append(
            Fill(
                direction=direction,
                entry_price=entry_price,
                size_shares=size_shares,
                outcome_yes=snap.market.resolved_yes,
                fee_rate=fee_rate,
                taker=taker,
            )
        )
    return summarise(fills)


async def run_stage_a(
    model: LanguageModel,
    snapshots: Sequence[MarketAtCutoff],
    *,
    method: str = "polymarket_llm_v1",
    pnl_threshold: float | None = None,
    pnl_size_usd: float = 5.0,
    pnl_taker: bool = True,
    pnl_half_spread: float = DEFAULT_HALF_SPREAD,
    cluster_key: Callable[[ResolvedMarket], Hashable] = default_event_cluster,
    bootstrap_seed: int = 0,
) -> StageAReport:
    """Run the LLM over each snapshot and bucket the results.

    A snapshot whose LLM call raises `Unavailable` or `UnusableCompletion` is
    recorded as an exclusion with the refusal's `reason`. The calibration
    function still gets the remaining predictions; a single refusal does not
    abort the run.

    If `pnl_threshold` is supplied (e.g. 0.05), the report also carries a
    `pnl_summary` computed by `_backtest_pnl`. Pass `None` to skip the
    dollar view (calibration only). The dollar view defaults to TAKER
    execution -- fee curve plus `pnl_half_spread` -- because a free fill at the
    historical mid is the best case, not the expected one; pass
    `pnl_taker=False` to read that best case deliberately.

    `brier_edge_interval` is a paired bootstrap over event clusters
    (`cluster_key`); see `paired_brier_edge_interval`.
    """
    predictions: list[_Prediction] = []
    benchmarks: dict[str, Benchmark] = {}
    p_yes_estimates: list[float] = []
    p_yes_markets: list[float] = []
    outcomes: list[bool] = []
    exclusions: list[Exclusion] = []
    trade_pairs: list[tuple[Estimation, MarketAtCutoff]] = []

    for snap in snapshots:
        try:
            est = await estimate(model, snap)
        except Unavailable as exc:
            exclusions.append(Exclusion(market=snap.market, reason=f"LLM unavailable: {exc}"))
            continue
        est_id = estimation_id(est)
        predictions.append(
            _Prediction(
                id=est_id,
                method=method,
                confidence=float(est.confidence),
                direction=Direction(est.direction),
                outcome=_outcome_of(snap.market.resolved_yes),
            )
        )
        benchmarks[est_id] = _benchmark_for(snap, est_id)
        p_yes_estimates.append(_p_yes_of(est))
        p_yes_markets.append(float(snap.market_probability))
        outcomes.append(snap.market.resolved_yes)
        trade_pairs.append((est, snap))

    buckets = calibration_with_benchmark(predictions, benchmarks)
    pnl_summary = (
        _backtest_pnl(
            trade_pairs,
            threshold=pnl_threshold,
            size_usd=pnl_size_usd,
            taker=pnl_taker,
            half_spread=pnl_half_spread,
        )
        if pnl_threshold is not None
        else None
    )
    clusters = [cluster_key(snap.market) for _est, snap in trade_pairs]
    interval = paired_brier_edge_interval(
        p_yes_estimates, p_yes_markets, outcomes, clusters, seed=bootstrap_seed
    )
    return StageAReport(
        method_buckets=buckets,
        n_estimated=len(predictions),
        exclusions=exclusions,
        brier_score=_brier(p_yes_estimates, outcomes),
        market_brier_score=_brier(p_yes_markets, outcomes),
        log_loss=_log_loss(p_yes_estimates, outcomes),
        method=method,
        pnl_summary=pnl_summary,
        trade_pairs=trade_pairs,
        brier_edge_interval=interval,
        brier_edge_clusters=len(set(clusters)),
    )


@dataclass(frozen=True)
class ThresholdSweep:
    """A P&L-vs-threshold curve, and the threshold it would pick -- labelled.

    `best_in_sample` is the threshold with the highest net P&L ON THE SAME
    TRADES the curve was drawn from. It is a description of this sample, not an
    estimate of what that threshold earns next: the maximum of several noisy
    numbers is biased upward by construction. `in_sample` is always True and
    exists so a consumer cannot print the pick without the label. To estimate
    the pick's forward P&L, choose on one set of trades and score on another
    (`sweep_thresholds_split`).
    """

    by_threshold: dict[float, PnLSummary]
    best_in_sample: float | None
    in_sample: bool = True


def _best(by_threshold: dict[float, PnLSummary]) -> float | None:
    traded = {t: s for t, s in by_threshold.items() if s.n_closed > 0}
    if not traded:
        return None
    return max(traded, key=lambda t: traded[t].net_pnl)


def sweep_thresholds(
    trade_pairs: list,
    *,
    thresholds: tuple[float, ...],
    size_usd: float = 5.0,
    taker: bool = True,
    half_spread: float = DEFAULT_HALF_SPREAD,
) -> ThresholdSweep:
    """Apply multiple P&L thresholds to the same LLM outputs.

    The expensive thing is the LLM call; the threshold is a post-LLM filter.
    This helper exists so a single Stage A run can produce a P&L-vs-threshold
    curve without re-calling the model per threshold. The pick it reports is
    in-sample and says so; see `ThresholdSweep`.
    """
    by_threshold = {
        t: _backtest_pnl(
            trade_pairs, threshold=t, size_usd=size_usd, taker=taker,
            half_spread=half_spread,
        )
        for t in thresholds
    }
    return ThresholdSweep(by_threshold=by_threshold, best_in_sample=_best(by_threshold))


def sweep_thresholds_split(
    trade_pairs: list,
    *,
    thresholds: tuple[float, ...],
    size_usd: float = 5.0,
    taker: bool = True,
    half_spread: float = DEFAULT_HALF_SPREAD,
) -> tuple[float | None, PnLSummary | None]:
    """Pick the threshold on the earlier half, score it on the later half.

    Trades are ordered by cutoff, the first half selects, the second half is
    the only number reported. Returns `(None, None)` when the selection half
    traded nothing.
    """
    ordered = sorted(trade_pairs, key=lambda pair: pair[1].cutoff)
    cut = len(ordered) // 2
    select, holdout = ordered[:cut], ordered[cut:]
    pick = sweep_thresholds(
        select, thresholds=thresholds, size_usd=size_usd, taker=taker,
        half_spread=half_spread,
    ).best_in_sample
    if pick is None:
        return None, None
    return pick, _backtest_pnl(
        holdout, threshold=pick, size_usd=size_usd, taker=taker,
        half_spread=half_spread,
    )


__all__ = [
    "DEFAULT_HORIZON",
    "DEFAULT_TOLERANCE",
    "DocumentProvider",
    "Exclusion",
    "StageAReport",
    "ThresholdSweep",
    "default_event_cluster",
    "paired_brier_edge_interval",
    "prepare_snapshot",
    "run_stage_a",
    "sweep_thresholds",
    "sweep_thresholds_split",
]
