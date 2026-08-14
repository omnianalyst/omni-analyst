"""Allocation rules over ETFs, for the forward shadow book.

`docs/ETF_PORTFOLIO_EXPERIMENT.md` tested a different question and answered it
no: selecting *constituents* inside a sector beat the sector ETF in 3 of 9
sectors, median excess CAGR -2.70%. This module does not retry that. It
allocates *across* ETFs, which is a question that experiment never asked.

Each rule returns the weights and the measurements behind them, and decides
nothing about when to act -- `shadow_book.record_decision` stamps the session
the weights apply to, and refuses one that is not in the future. Keeping the
rule ignorant of the calendar is what stops a rule from ever being the thing
that backdates a decision.

**Every rule refuses rather than degrades.** A rule that quietly drops a name
with a short history changes its own universe, and the recorded universe is the
only evidence of what it was choosing from.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any

import numpy as np
import pandas as pd

from omni.research.etf_replication import price_quality_scores

TRADING_DAYS = 252

# The history each rule needs before it will produce weights. 127 sessions is
# what `price_quality_scores` requires for its six-month momentum; the others
# use less but are held to the same floor so that three books starting on the
# same day are choosing from the same amount of evidence.
MIN_HISTORY_SESSIONS = 127

# Annualised volatility below which a name is not inverse-volatility weighted.
# `1 / sigma` is unbounded as sigma approaches zero, so a near-constant series
# -- a fund that stopped printing, a provider repeating its last mark -- takes
# essentially the whole book. The guard is a tolerance on the standard
# deviation rather than a comparison against zero: `np.std` of a constant 0.05
# series returns ~1e-17, not 0.0, so `if sigma == 0` never fires and the
# division proceeds on noise. Five modules in this repository have shipped
# fabricated output through exactly that hole.
MIN_ANNUAL_VOLATILITY = 0.005


class AllocationRefused(Exception):
    """No weights were produced, and the reason is stated."""


@dataclass(frozen=True)
class Allocation:
    book: str
    rule_version: str
    universe: list[str]
    weights: dict[str, float]
    inputs: dict[str, Any]
    benchmark: str


def _eligible(panel: pd.DataFrame, universe: list[str]) -> pd.DataFrame:
    missing = [s for s in universe if s not in panel.columns]
    if missing:
        raise AllocationRefused(
            f"the panel has no column for {', '.join(sorted(missing))}. The rule "
            f"was asked to choose from a universe the data does not cover, and "
            f"allocating over the remainder would silently be a different rule"
        )
    history = panel.loc[:, universe].sort_index()
    if len(history) < MIN_HISTORY_SESSIONS:
        raise AllocationRefused(
            f"{len(history)} sessions of history, against the "
            f"{MIN_HISTORY_SESSIONS} every rule here requires"
        )
    window = history.tail(MIN_HISTORY_SESSIONS)
    incomplete = sorted(window.columns[window.isna().any()])
    if incomplete:
        raise AllocationRefused(
            f"incomplete history for {', '.join(incomplete)} over the last "
            f"{MIN_HISTORY_SESSIONS} sessions. Dropping them would change the "
            f"universe this decision records having chosen from"
        )
    return window


def equal_weight(
    panel: pd.DataFrame,
    universe: list[str],
    *,
    benchmark: str,
    book: str = "etf_equal_weight_sectors",
) -> Allocation:
    """One over n. The baseline every other rule here has to beat.

    It is in the shadow book rather than assumed because the constituent
    experiment's one broad finding was that equal weighting beat its sector ETF
    on CAGR in 7 of 9 sectors while worsening drawdown in 6 of 9 -- a result
    good enough to be worth a forward record and not good enough to act on.
    """
    window = _eligible(panel, universe)
    names = list(window.columns)
    weight = 1.0 / len(names)
    return Allocation(
        book=book,
        rule_version="equal_weight/v1",
        universe=names,
        weights=dict.fromkeys(names, weight),
        inputs={
            "n": len(names),
            "history_sessions": len(window),
            "rule": "1/n over every name with complete history",
        },
        benchmark=benchmark,
    )


def top_measured(
    panel: pd.DataFrame,
    universe: list[str],
    *,
    benchmark: str,
    top_n: int = 3,
    book: str = "etf_top_measured_sectors",
) -> Allocation:
    """The n highest-scoring ETFs, equally weighted.

    Reuses `price_quality_scores` rather than restating it: the backtest and the
    forward record must rank on the same rule, or a disagreement between them
    would be read as evidence about the market.
    """
    if top_n < 2:
        raise AllocationRefused(
            f"top_n is {top_n}; a one-name book is a bet on a single sector, not "
            f"an allocation, and its result would say nothing about the rule"
        )
    window = _eligible(panel, universe)
    scores = price_quality_scores(window)
    if len(scores) < top_n:
        raise AllocationRefused(
            f"{len(scores)} names scored, against the {top_n} this rule selects. "
            f"Selecting fewer would quietly change the concentration the record "
            f"is meant to measure"
        )
    selected = list(scores.index[:top_n])
    weight = 1.0 / len(selected)
    return Allocation(
        book=book,
        rule_version=f"top_measured/v1/n={top_n}",
        universe=list(window.columns),
        weights=dict.fromkeys(selected, weight),
        inputs={
            "scores": {k: round(float(v), 6) for k, v in scores.items()},
            "selected": selected,
            "top_n": top_n,
            "history_sessions": len(window),
        },
        benchmark=benchmark,
    )


def risk_balanced(
    panel: pd.DataFrame,
    universe: list[str],
    *,
    benchmark: str,
    lookback_sessions: int = 63,
    book: str = "etf_risk_balanced_sectors",
) -> Allocation:
    """Inverse-volatility weights: each name contributes similar risk.

    This is not risk parity -- it ignores correlation, so the contributions are
    only equal when the names are equally correlated, which sector ETFs are not.
    Naming it `risk_balanced` rather than `risk_parity` keeps that difference
    visible in the book's own label.
    """
    window = _eligible(panel, universe)
    daily = window.pct_change(fill_method=None).tail(lookback_sessions)
    sigma = daily.std(ddof=1) * math.sqrt(TRADING_DAYS)

    unusable = sorted(sigma.index[~np.isfinite(sigma)])
    if unusable:
        raise AllocationRefused(
            f"volatility is not finite for {', '.join(unusable)}; every "
            f"comparison against NaN is false, so an unguarded weighting would "
            f"pass it through and produce a confident NaN book"
        )

    too_still = sorted(sigma.index[sigma < MIN_ANNUAL_VOLATILITY])
    if too_still:
        raise AllocationRefused(
            f"{', '.join(too_still)} measured under {MIN_ANNUAL_VOLATILITY:.1%} "
            f"annualised volatility over {lookback_sessions} sessions. 1/sigma "
            f"is unbounded there, so a fund that stopped printing would take "
            f"essentially the whole book"
        )

    inverse = 1.0 / sigma
    weights = inverse / inverse.sum()
    return Allocation(
        book=book,
        rule_version=f"risk_balanced/v1/lookback={lookback_sessions}",
        universe=list(window.columns),
        weights={k: float(v) for k, v in weights.items()},
        inputs={
            "annualised_volatility": {
                k: round(float(v), 6) for k, v in sigma.items()
            },
            "lookback_sessions": lookback_sessions,
            "measure": "inverse annualised volatility, correlation ignored",
        },
        benchmark=benchmark,
    )


RULES = {
    "etf_equal_weight_sectors": equal_weight,
    "etf_top_measured_sectors": top_measured,
    "etf_risk_balanced_sectors": risk_balanced,
}


__all__ = [
    "MIN_ANNUAL_VOLATILITY",
    "MIN_HISTORY_SESSIONS",
    "RULES",
    "Allocation",
    "AllocationRefused",
    "equal_weight",
    "risk_balanced",
    "top_measured",
]
