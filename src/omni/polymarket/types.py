"""Value types for Polymarket ingestion and Stage A calibration.

Bitemporal by design. Every observation carries the moment it was observed
(`observed_at`) and the moment the underlying event became knowable
(`event_date`), mirroring the claim invariants in `AGENTS.md`. A single
`as_of` cannot tell "what the price was" from "when we knew it"; a backtest
that confuses the two scores itself against information it would not have had.

All probabilities are `float` on the wire because `calibration_with_benchmark`
reads `float(p.confidence)` and `float(b.market_probability)`. They are coerced
through `float()` exactly once at construction and validated to `[0, 1]`, so a
NaN-bearing JSON blob from Gamma cannot reach the calibration buckets.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any


def _finite_unit_interval(name: str, value: Any) -> float:
    """Coerce to float and refuse NaN/inf/out-of-range in one place.

    `value <= 0` and similar comparisons are *false* against NaN, so a
    validity check written as a comparison silently passes NaN through to a
    confidently-labelled bucket. The check has to come before any ordering.
    """
    try:
        f = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{name} is not a number: {value!r}") from exc
    if not math.isfinite(f):
        raise ValueError(f"{name} must be finite, got {f}")
    if not 0.0 <= f <= 1.0:
        raise ValueError(f"{name} must be in [0, 1], got {f}")
    return f


def _aware(name: str, ts: datetime) -> datetime:
    if ts.tzinfo is None:
        raise ValueError(
            f"{name} is naive; a timestamp without a zone cannot be ordered "
            f"against a cutoff or a knowledge_date"
        )
    return ts.astimezone(UTC)


@dataclass(frozen=True)
class ResolvedMarket:
    """A Polymarket that has resolved, as the historical record reports it.

    `resolved_yes` is the truth the backtest scores against. It is set from
    Gamma's `outcomes`/`outcomePrices` at resolution: the outcome whose price
    reached `1.0` is the winner. If two outcomes resolve at 1.0, or none do,
    the parser refuses to construct one — a market whose resolution is
    ambiguous cannot be scored, and silently picking a side would manufacture
    ground truth.

    `yes_token_id` is the CLOB token the YES share trades under. Gamma returns
    `clobTokenIds` as a stringified JSON array; the parser picks element 0 as
    YES by Polymarket convention and leaves the NO id in `no_token_id` for the
    rare strategy that needs both legs.
    """

    condition_id: str
    question: str
    category: str
    resolved_yes: bool
    resolution_date: datetime
    created_at: datetime
    yes_token_id: str | None = None
    no_token_id: str | None = None
    neg_risk: bool = False
    slug: str = ""
    volume: float = 0.0

    def __post_init__(self) -> None:
        if not self.condition_id.strip():
            raise ValueError("condition_id must be non-empty")
        if not self.question.strip():
            raise ValueError("question must be non-empty")
        if not self.category.strip():
            raise ValueError("category must be non-empty")
        if self.volume < 0:
            raise ValueError(f"volume must not be negative: {self.volume}")
        object.__setattr__(self, "created_at", _aware("created_at", self.created_at))
        object.__setattr__(
            self, "resolution_date", _aware("resolution_date", self.resolution_date)
        )
        if self.resolution_date < self.created_at:
            raise ValueError(
                f"market {self.condition_id}: resolution_date "
                f"{self.resolution_date} precedes created_at {self.created_at}; "
                f"a market that resolved before it opened is a parsing error"
            )


@dataclass(frozen=True)
class MarketPricePoint:
    """One observed YES price for one market at one moment.

    `at` is when the price was observable (the bar's timestamp on a price
    series), not when we recorded it. A backtest scoring a cutoff against
    "what was knowable then" must use the bar time; using the recording time
    would license a future sample as the cutoff's benchmark.
    """

    at: datetime
    yes_price: float

    def __post_init__(self) -> None:
        object.__setattr__(self, "at", _aware("at", self.at))
        object.__setattr__(self, "yes_price", _finite_unit_interval("yes_price", self.yes_price))


@dataclass(frozen=True)
class MarketAtCutoff:
    """A resolved market with its cutoff-anchored benchmark and evidence.

    `cutoff` is `resolution_date - horizon`. The benchmark probability is the
    YES price sampled nearest to cutoff (within `tolerance`); if no sample
    falls within tolerance, the harness excludes the market rather than
    approximate. `documents` are the textual evidence available *before*
    cutoff; the harness enforces `document.at < cutoff` and refuses anything
    later.
    """

    market: ResolvedMarket
    cutoff: datetime
    market_probability: float
    documents: tuple[Document, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "cutoff", _aware("cutoff", self.cutoff))
        object.__setattr__(
            self, "market_probability", _finite_unit_interval("market_probability", self.market_probability)
        )
        if self.cutoff <= self.market.created_at:
            raise ValueError(
                f"cutoff {self.cutoff} is at or before created_at "
                f"{self.market.created_at}; the market did not exist yet"
            )
        if self.cutoff >= self.market.resolution_date:
            raise ValueError(
                f"cutoff {self.cutoff} is at or after resolution_date "
                f"{self.market.resolution_date}; the backtest would score "
                f"itself against information that includes the outcome"
            )
        for doc in self.documents:
            if doc.at >= self.cutoff:
                raise ValueError(
                    f"document {doc.source!r} at {doc.at} is not before cutoff "
                    f"{self.cutoff}; a post-cutoff document is lookahead bias"
                )


@dataclass(frozen=True)
class Document:
    """A piece of textual evidence, strictly pre-cutoff."""

    at: datetime
    source: str
    text: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "at", _aware("at", self.at))
        if not self.source.strip():
            raise ValueError("document source must be non-empty")
        if not self.text.strip():
            raise ValueError("document text must be non-empty")


@dataclass(frozen=True)
class Estimation:
    """One LLM P(yes) estimate, already mapped to the calibration shape.

    `direction` follows the convention in `calibration/__init__.py`: `UP` is a
    YES bet, `DOWN` is a NO bet. `confidence` is the bin centre on [0, 1]; the
    decile edges line up with `calibration_with_benchmark`'s bucketing.

    `chosen_bin` is retained verbatim because the bin width is the
    elicitation's resolution and a report that showed only `confidence` would
    hide it. `raw_choice` is the exact string the model returned, kept for the
    same reason an audit needs the original token, not its parsed value.
    """

    chosen_bin: float
    direction: str
    confidence: float
    raw_choice: str
    market_id: str
    cutoff: datetime

    def __post_init__(self) -> None:
        object.__setattr__(self, "chosen_bin", _finite_unit_interval("chosen_bin", self.chosen_bin))
        object.__setattr__(self, "confidence", _finite_unit_interval("confidence", self.confidence))
        if self.direction not in ("up", "down"):
            raise ValueError(
                f"direction must be 'up' or 'down', got {self.direction!r}; "
                f"neutral has no analogue on a binary market"
            )
        object.__setattr__(self, "cutoff", _aware("cutoff", self.cutoff))
