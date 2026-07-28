"""Bind the extracted v1 analysis into the capability registry.

`builtin.py` binds v2's own adapters -- the sources that fetch and emit claim
drafts. The modules under `omni.capabilities/` (plural) hold the real analysis
maths lifted out of v1 by work orders E1-E3: macro indicators, fundamental and
portfolio analytics, and news/sentiment scoring. Nothing there is registered,
so the planner cannot discover any of it; a capability the planner cannot
discover may as well not exist.

This file binds those extracted functions as capabilities without modifying
them. Each function becomes either a claim producer (its output maps to a
claim_type already in the schema enum) or an analysis step (it consumes claims
and returns a computed result, so `produces` is empty). No claim type is
invented: the enum in migrations/001_core_schema.sql and 003_domains_and_graph.sql
is closed, and `CLAIM_TYPES` below mirrors it so a drift is caught.

Licence classification follows the work order: macro from FRED is shareable;
news and sentiment from commercial APIs are not. Fundamentals are sourced from
SEC EDGAR (redistributable), so they are shareable; portfolio analytics that
run over price/return series inherit the licence of the bars they consume, so
they are marked `touches_byo` exactly like `detect.manipulation` in builtin.py.
"""

from __future__ import annotations

import inspect
from collections.abc import Callable
from typing import Any

from omni.capability.registry import (
    Callability,
    Capability,
    Maturity,
    Registry,
)

# The closed claim_type enum, mirroring migrations/001_core_schema.sql and
# migrations/003_domains_and_graph.sql. A capability declaring anything outside
# this set is a schema bug; the tests check against this.
CLAIM_TYPES = frozenset(
    {
        "price_snapshot",
        "fundamental_metric",
        "filing_event",
        "macro_series_point",
        "news_event",
        "manipulation_signal",
        "perception_news",
        "perception_macro",
        "perception_social",
        "perception_positioning",
        "perception_divergence",
        "onchain_flow",
        "onchain_tvl",
        "onchain_supply",
    }
)


def _bind(
    name: str,
    description: str,
    *,
    fn: Callable[..., Any],
    consumes: tuple[str, ...] = (),
    produces: tuple[str, ...] = (),
    entity_kinds: tuple[str, ...] = (),
    touches_byo: bool = False,
    cost: float = 0.1,
    provenance: str = "",
    is_proxy: bool = False,
    proxy_of: tuple[str, ...] = (),
) -> Capability:
    """Wrap an extracted function in a Capability whose `call` runs it.

    Sync and async functions are both accepted; the wrapper awaits the latter.
    Maturity is WIRED only because every function bound here has a behaviour
    assertion in tests/test_cap_*.py -- the one thing the work order requires
    for that grade.
    """
    if inspect.iscoroutinefunction(fn):

        async def call(*args: Any, **kwargs: Any) -> Any:
            return await fn(*args, **kwargs)

    else:

        async def call(*args: Any, **kwargs: Any) -> Any:
            return fn(*args, **kwargs)

    return Capability(
        name=name,
        description=description,
        consumes=consumes,
        produces=produces,
        entity_kinds=entity_kinds,
        touches_byo=touches_byo,
        is_proxy=is_proxy,
        proxy_of=proxy_of,
        provenance=provenance,
        cost=cost,
        maturity=Maturity.WIRED,
        callability=Callability.YES,
        origin=f"{fn.__module__}.{fn.__qualname__}",
        call=call,
    )


def build_extracted_registry() -> Registry:
    from omni.capabilities import fundamentals, macro, news

    registry = Registry()

    # --- Macro (FRED-sourced; redistributable) ---
    # Macro analytics consume macro_series_point claims and emit derived
    # indicators. None of the derived outputs has a home in the closed enum, so
    # every one is an analysis step with produces empty.
    for cap in (
        _bind(
            "macro.sahm_rule",
            "Sahm recession signal: the 3-month average unemployment rate "
            "minus the prior 12-month low. Triggers at >=0.5.",
            fn=macro.sahm_rule,
            consumes=("macro_series_point",),
            provenance=(
                "The 0.5 trigger is a policy-fixed constant, not a calibrated "
                "threshold. A deterministic transform with no uncertainty band."
            ),
        ),
        _bind(
            "macro.yield_curve_inversion",
            "2Y/10Y treasury spread over a trailing window: current spread, "
            "inversion flag and 90-day days-inverted count.",
            fn=macro.yield_curve_inversion,
            consumes=("macro_series_point",),
        ),
        _bind(
            "macro.recession_probability",
            "Composite recession probability from yield-curve inversion, the "
            "Sahm signal and LEI direction. Fixed 0.3/0.4/0.3 weighting.",
            fn=macro.recession_probability,
            consumes=("macro_series_point",),
            provenance=(
                "Weights are policy-fixed, not calibrated. A deterministic "
                "composite; the conviction gate must not treat its output as a "
                "calibrated probability without resolved history."
            ),
        ),
        _bind(
            "macro.inflation_measures",
            "CPI inflation: YoY (13-observation), month-over-month annualised "
            "and 3-month annualised, plus a short-horizon trend.",
            fn=macro.inflation_measures,
            consumes=("macro_series_point",),
            provenance=(
                "The 13-observation YoY window assumes monthly frequency; a "
                "quarterly series would silently mis-annualise."
            ),
        ),
        _bind(
            "macro.pce_inflation",
            "PCE inflation YoY versus a target (default 2.0), with distance "
            "from target.",
            fn=macro.pce_inflation,
            consumes=("macro_series_point",),
        ),
        _bind(
            "macro.inflation_expectations",
            "5-year and 10-year inflation expectations, the 5y5y forward and "
            "an anchoring check around 2.0.",
            fn=macro.inflation_expectations,
            consumes=("macro_series_point",),
        ),
        _bind(
            "macro.labor_market_tightness",
            "Labor-market tightness score (1/unemployment * normalised job "
            "growth) mapped to tight/balanced/loose with wage-pressure bands.",
            fn=macro.labor_market_tightness,
            consumes=("macro_series_point",),
            provenance=(
                "Deterministic transform with no uncertainty band; the bands "
                "are declared thresholds, not estimates."
            ),
        ),
        _bind(
            "macro.taylor_rule",
            "Taylor Rule policy rate: r* + pi + alpha*(pi - target) + "
            "beta*output_gap, with standard default coefficients.",
            fn=macro.taylor_rule,
            consumes=("macro_series_point",),
        ),
        _bind(
            "macro.taylor_rule_variant",
            "Fully-parameterised Taylor Rule variant: caller supplies target, "
            "r*, alpha and beta.",
            fn=macro.taylor_rule_variant,
            consumes=("macro_series_point",),
        ),
        _bind(
            "macro.assess_policy_implications",
            "Policy read from a Taylor rate versus the current rate: stance, "
            "the rate adjustment implied and the risks that arise.",
            fn=macro.assess_policy_implications,
            consumes=("macro_series_point",),
        ),
        _bind(
            "macro.assess_scenario_impact",
            "Average and peak per-variable impact of a scenario forecast "
            "against a baseline forecast.",
            fn=macro.assess_scenario_impact,
            consumes=("macro_series_point",),
        ),
    ):
        registry.add(cap)

    # --- Fundamentals and portfolio analytics ---
    # financial_ratios / dcf_valuation / peer_comparison consume
    # fundamental_metric claims (EDGAR-sourced, redistributable). The portfolio
    # analytics consume price/return series, whose licence the descriptor
    # cannot see, so they carry touches_byo exactly like detect.manipulation.
    for cap in (
        _bind(
            "fundamentals.financial_ratios",
            "Per-share and margin ratios (P/E, PEG, P/B, ROE, ROA, "
            "debt-to-equity, current/quick, free cash flow, dividend yield) "
            "from an as-reported fundamentals snapshot.",
            fn=fundamentals.financial_ratios,
            consumes=("fundamental_metric",),
            entity_kinds=("company",),
            provenance=(
                "Derived from as-reported fundamentals; a computed ratio, not "
                "a primary data point, so it is left out of the "
                "fundamental_metric producers rather than conflated with them."
            ),
        ),
        _bind(
            "fundamentals.dcf_valuation",
            "Discounted-cash-flow fair value: projected FCFs, terminal value "
            "and the equity bridge to per-share value and upside.",
            fn=fundamentals.dcf_valuation,
            consumes=("fundamental_metric",),
            entity_kinds=("company",),
            provenance=(
                "Model output, not a measured value; depends on declared "
                "assumptions (growth, discount, terminal rate)."
            ),
        ),
        _bind(
            "fundamentals.peer_comparison",
            "Rank a company's metrics against supplied peers: peer averages, "
            "per-metric percentile rank and relative valuation versus peers.",
            fn=fundamentals.peer_comparison,
            consumes=("fundamental_metric",),
            entity_kinds=("company",),
        ),
        _bind(
            "fundamentals.portfolio_returns",
            "Portfolio absolute, percentage and annualised return, volatility "
            "and Sharpe from transactions, total value and a daily-return "
            "series.",
            fn=fundamentals.portfolio_returns,
            consumes=("price_snapshot",),
            touches_byo=True,
            provenance=(
                "Risk-free rate is a hardcoded 2.0 here, inconsistent with "
                "risk_metrics' 4.5; a single risk-free source should feed both."
            ),
        ),
        _bind(
            "fundamentals.risk_metrics",
            "Historical VaR (95/99), conditional VaR, annualised std, downside "
            "deviation, Sortino, Calmar and (optionally) beta versus a "
            "benchmark.",
            fn=fundamentals.risk_metrics,
            consumes=("price_snapshot",),
            touches_byo=True,
            provenance=(
                "portfolio_beta is null when no usable benchmark is supplied "
                "(never fabricated as 1.0). No Monte-Carlo fallback on short "
                "history; it raises instead."
            ),
        ),
        _bind(
            "fundamentals.stress_tests",
            "Scenario impact on a portfolio value: market crash, rate rise and "
            "currency depreciation.",
            fn=fundamentals.stress_tests,
            provenance=(
                "Declared stress assumptions (-20/-5/-3%), not measured "
                "outcomes; hypothetical, so never aggregate into shared "
                "coverage as if observed."
            ),
        ),
        _bind(
            "fundamentals.correlation_matrix",
            "Pairwise return correlation across symbols over supplied "
            "return series.",
            fn=fundamentals.correlation_matrix,
            consumes=("price_snapshot",),
            touches_byo=True,
            provenance=(
                "Blends return series across symbols; if any input came from "
                "a byo_only credential the result is audience-scoped to that "
                "owner and must not fold into shared coverage."
            ),
        ),
        _bind(
            "fundamentals.benchmark_comparison",
            "Benchmark return and portfolio alpha from a benchmark close "
            "series and the portfolio's return.",
            fn=fundamentals.benchmark_comparison,
            consumes=("price_snapshot",),
            touches_byo=True,
        ),
    ):
        registry.add(cap)

    # --- News and sentiment ---
    # News/sentiment comes from commercial APIs, so these are touches_byo.
    # aggregate_market_sentiment and the StockTwits scorer yield sentiment
    # readings that map to the perception_* claim types already in the enum, so
    # they are producers; the rest are analysis steps.
    for cap in (
        _bind(
            "news.aggregate_market_sentiment",
            "Aggregate per-sentiment counts into a single market read: "
            "overall bullish/bearish/neutral, a breakdown and a weighted "
            "score.",
            fn=news.aggregate_market_sentiment,
            consumes=("news_event",),
            produces=("perception_news",),
            touches_byo=True,
        ),
        _bind(
            "news.score_portfolio_impact",
            "Score a news item's portfolio impact from its sentiment, "
            "confidence and the affected tickers.",
            fn=news.score_portfolio_impact,
            consumes=("news_event",),
            touches_byo=True,
        ),
        _bind(
            "news.score_stocktwits_messages",
            "Score StockTwits messages into a sentiment reading using the "
            "provider's own Bullish/Bearish/Neutral tags.",
            fn=news.score_stocktwits_messages,
            produces=("perception_social",),
            touches_byo=True,
            provenance=(
                "Reads provider-supplied tags; no inferred polarity (the "
                "TextBlob-based Reddit/Twitter scorers were refused, not "
                "ported)."
            ),
        ),
        _bind(
            "news.stocktwits_sentiment",
            "Fetch and score StockTwits sentiment for a symbol via an "
            "injectable fetcher.",
            fn=news.stocktwits_sentiment,
            produces=("perception_social",),
            touches_byo=True,
            cost=1.0,
            provenance=(
                "Reads provider-supplied tags; no inferred polarity. Does "
                "network IO through fetch_fn, unlike the pure-compute caps."
            ),
        ),
    ):
        registry.add(cap)

    return registry
