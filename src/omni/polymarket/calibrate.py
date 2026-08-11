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
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta

import httpx

from omni.calibration import Benchmark, Direction, Outcome
from omni.calibration.report import BenchmarkCalibrationBucket, calibration_with_benchmark
from omni.ingest.protocol import Unavailable
from omni.llm.protocol import LanguageModel
from omni.polymarket.estimator import estimate, estimation_id
from omni.polymarket.gamma import fetch_price_history
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
    slippage or fill risk — that is Stage B.)
    """

    method_buckets: dict[str, list[BenchmarkCalibrationBucket]]
    n_estimated: int
    exclusions: list[Exclusion] = field(default_factory=list)
    brier_score: float | None = None
    market_brier_score: float | None = None
    log_loss: float | None = None
    method: str = "polymarket_llm_v1"

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


async def run_stage_a(
    model: LanguageModel,
    snapshots: Sequence[MarketAtCutoff],
    *,
    method: str = "polymarket_llm_v1",
) -> StageAReport:
    """Run the LLM over each snapshot and bucket the results.

    A snapshot whose LLM call raises `Unavailable` or `UnusableCompletion` is
    recorded as an exclusion with the refusal's `reason`. The calibration
    function still gets the remaining predictions; a single refusal does not
    abort the run.
    """
    predictions: list[_Prediction] = []
    benchmarks: dict[str, Benchmark] = {}
    p_yes_estimates: list[float] = []
    p_yes_markets: list[float] = []
    outcomes: list[bool] = []
    exclusions: list[Exclusion] = []

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

    buckets = calibration_with_benchmark(predictions, benchmarks)
    return StageAReport(
        method_buckets=buckets,
        n_estimated=len(predictions),
        exclusions=exclusions,
        brier_score=_brier(p_yes_estimates, outcomes),
        market_brier_score=_brier(p_yes_markets, outcomes),
        log_loss=_log_loss(p_yes_estimates, outcomes),
        method=method,
    )


__all__ = [
    "DEFAULT_HORIZON",
    "DEFAULT_TOLERANCE",
    "DocumentProvider",
    "Exclusion",
    "StageAReport",
    "prepare_snapshot",
    "run_stage_a",
]
