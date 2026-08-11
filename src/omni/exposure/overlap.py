"""Portfolio exposure analysis: overlap, concentration, bucket coverage.

The exposure tool answers two questions the surface of a portfolio hides:

1. **Concentration** -- if you hold VTI and QQQ, you own AAPL and MSFT twice.
   This module sums effective company weight across all ETF positions so the
   number you see is the number you own, not the per-fund number that hides
   the duplication.

2. **Overlap** -- how much of one ETF is duplicated by another. A portfolio
   that looks diversified (three different ETFs) but whose holdings overlap
   80% is one position wearing three labels.

Both are computed from the same input: a set of ETF positions, each carrying
its portfolio allocation and its holdings (ticker, weight). No I/O -- this is
a pure function over in-memory data. The DB query that populates these
structures lives in ``query.py``; the API endpoint composes the two.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from decimal import Decimal

__all__ = [
    "ConcentrationFlag",
    "ETFPosition",
    "ExposureResult",
    "Holding",
    "OverlapPair",
    "analyze",
]


def _D(value: Decimal | float | str) -> Decimal:
    return value if isinstance(value, Decimal) else Decimal(str(value))


@dataclass(frozen=True)
class Holding:
    """One constituent inside an ETF, at its fund weight (0.0 - 1.0)."""

    ticker: str
    weight: Decimal


@dataclass(frozen=True)
class ETFPosition:
    """One ETF in the portfolio, at its allocation and current holdings."""

    symbol: str
    bucket: str
    allocation: Decimal
    holdings: tuple[Holding, ...]


@dataclass(frozen=True)
class ConcentrationFlag:
    """A company whose effective portfolio weight exceeds the threshold."""

    ticker: str
    total_weight: Decimal
    source_etfs: tuple[str, ...]


@dataclass(frozen=True)
class OverlapPair:
    """How much two ETFs duplicate each other.

    ``shared_weight`` is the sum of the smaller holding weight for each ticker
    both funds hold -- the portion of fund A that fund B also covers. An
    overlap of 0.60 means 60% of A's holdings by weight are also in B.
    """

    etf_a: str
    etf_b: str
    shared_weight: Decimal


@dataclass(frozen=True)
class ExposureResult:
    concentration: tuple[ConcentrationFlag, ...]
    overlaps: tuple[OverlapPair, ...]
    bucket_exposure: tuple[tuple[str, Decimal], ...]
    top_holdings: tuple[tuple[str, Decimal], ...]


_ZERO = Decimal(0)


def analyze(
    positions: tuple[ETFPosition, ...] | list[ETFPosition],
    *,
    concentration_threshold: Decimal = Decimal("0.05"),
    overlap_threshold: Decimal = Decimal("0.25"),
) -> ExposureResult:
    """Compute effective exposure across a portfolio of ETF positions.

    ``concentration_threshold`` flags any single company whose summed weight
    across all ETFs exceeds this fraction of the portfolio. ``overlap_threshold``
    filters the overlap matrix to pairs whose duplication is material -- a
    5%% cross-holding between two unrelated ETFs is noise, not a red flag.

    Both thresholds are structural guardrails, not predictions: they state
    "this is the point where a human should look," not "this will lose money."
    """
    positions = tuple(positions)
    if not positions:
        return ExposureResult(
            concentration=(),
            overlaps=(),
            bucket_exposure=(),
            top_holdings=(),
        )

    # --- Effective company weight across the portfolio ---
    #
    # If VTI is 60% of the portfolio and AAPL is 6% of VTI, then AAPL is
    # 3.6% of the portfolio through VTI alone. If QQQ is 20% and AAPL is 12%
    # of QQQ, AAPL adds another 2.4% for a total of 6.0%. That total is the
    # number that matters, and it is almost always larger than any single
    # fund's holding sheet suggests.
    effective: dict[str, Decimal] = defaultdict(lambda: _ZERO)
    sources: dict[str, set[str]] = defaultdict(set)

    for pos in positions:
        for holding in pos.holdings:
            contributed = holding.weight * pos.allocation
            if contributed <= _ZERO:
                continue
            effective[holding.ticker] += contributed
            sources[holding.ticker].add(pos.symbol)

    concentration = tuple(
        ConcentrationFlag(
            ticker=ticker,
            total_weight=weight,
            source_etfs=tuple(sorted(sources[ticker])),
        )
        for ticker, weight in sorted(
            effective.items(), key=lambda kv: kv[1], reverse=True
        )
        if weight >= concentration_threshold
    )

    top_holdings = tuple(
        (ticker, weight)
        for ticker, weight in sorted(
            effective.items(), key=lambda kv: kv[1], reverse=True
        )
        if weight > _ZERO
    )

    # --- Pairwise overlap ---
    #
    # For each pair of ETFs, the overlap is the sum of the smaller weight for
    # each shared ticker. If A holds AAPL at 6% and B holds AAPL at 12%, the
    # overlap contribution is 6% -- B covers everything A does for that name.
    # Summed across all shared names, this is "what fraction of A does B
    # duplicate."
    overlaps: list[OverlapPair] = []
    for i, a in enumerate(positions):
        a_holdings = {h.ticker: h.weight for h in a.holdings}
        for b in positions[i + 1 :]:
            b_holdings = {h.ticker: h.weight for h in b.holdings}
            shared_tickers = set(a_holdings) & set(b_holdings)
            if not shared_tickers:
                continue
            shared = sum(
                (min(a_holdings[t], b_holdings[t]) for t in shared_tickers),
                _ZERO,
            )
            if shared >= overlap_threshold:
                overlaps.append(
                    OverlapPair(
                        etf_a=a.symbol,
                        etf_b=b.symbol,
                        shared_weight=shared,
                    )
                )

    # --- Bucket exposure ---
    #
    # Which risk regime does the portfolio actually own? Summing allocation by
    # bucket reveals if the "diversified" portfolio is actually 90% growth.
    bucket: dict[str, Decimal] = defaultdict(lambda: _ZERO)
    for pos in positions:
        bucket[pos.bucket] += pos.allocation
    bucket_exposure = tuple(
        sorted(bucket.items(), key=lambda kv: kv[1], reverse=True)
    )

    return ExposureResult(
        concentration=tuple(concentration),
        overlaps=tuple(overlaps),
        bucket_exposure=bucket_exposure,
        top_holdings=top_holdings,
    )
