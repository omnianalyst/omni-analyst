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
    from omni.capabilities import (
        attribution,
        fixed_income,
        fundamentals,
        macro,
        news,
        portfolio,
        portfolio_risk,
    )

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

    # --- Portfolio construction, factor risk and sizing (capabilities/portfolio.py) ---
    # These run over return / covariance / equity series whose licence the
    # descriptor cannot see, so they carry touches_byo exactly like the
    # price-consuming fundamentals caps above. produces is empty for every one:
    # the closed claim_type enum has no home for weights, risk decompositions,
    # factor loadings or position sizes, and inventing one is a schema bug. The
    # two pure-scalar sizers (Kelly, meta-label) take caller-asserted edge /
    # probability inputs rather than bar-derived data, so they are not marked
    # touches_byo -- their output is a private sizing decision, never coverage.
    for cap in (
        _bind(
            "portfolio.optimize_weights",
            "Long-only portfolio weights from an asset covariance: HRP "
            "(default), equal-risk-contribution risk parity, or long-only "
            "minimum variance. Method chosen by name; cap / prior_weights / "
            "turnover_penalty apply uniformly.",
            fn=portfolio.optimize_weights,
            consumes=("price_snapshot",),
            touches_byo=True,
            provenance=(
                "HRP / risk parity / min variance are deterministic "
                "allocation procedures over a supplied covariance, not return "
                "forecasts; they assume no distribution. A singular / non-PD "
                "covariance raises rather than falling back to equal weight."
            ),
        ),
        _bind(
            "portfolio.vol_target_weights",
            "Inverse-volatility weights scaled so the portfolio hits a target "
            "annual vol, capped at a maximum leverage.",
            fn=portfolio.vol_target_weights,
            consumes=("price_snapshot",),
            touches_byo=True,
            provenance=(
                "Inverse-vol weighting assumes equal correlation across "
                "assets; an optional corr_matrix overrides that. No asset "
                "with a positive volatility raises rather than yielding a "
                "silent zero-weight vector."
            ),
        ),
        _bind(
            "portfolio.risk_contributions",
            "Normalised per-asset risk contributions RC_i = w_i (Sigma w)_i / "
            "(w' Sigma w) for supplied weights and covariance.",
            fn=portfolio.risk_contributions,
            consumes=("price_snapshot",),
            touches_byo=True,
        ),
        _bind(
            "portfolio.fit_factor_risk_model",
            "Multi-factor risk model: per-asset OLS time-series regression of "
            "returns on factors, the implied asset covariance "
            "B Sigma_f B' + diag(specific), and the factor-vs-specific "
            "variance split.",
            fn=portfolio.fit_factor_risk_model,
            consumes=("price_snapshot",),
            touches_byo=True,
            provenance=(
                "Model output, not a measurement: OLS exposures and a "
                "regression-derived covariance. Assets without enough "
                "overlap to regress are treated as fully idiosyncratic "
                "rather than dropped."
            ),
        ),
        _bind(
            "portfolio.atr_position_size",
            "Clenow/Turtle ATR position size: units at risk = "
            "risk_fraction * equity / (ATR * point_value).",
            fn=portfolio.atr_position_size,
            touches_byo=True,
            provenance=(
                "The position count inherits the licence of the ATR / price "
                "scalars the caller supplies, which are bar-derived; "
                "risk_fraction is a declared policy constant (~0.1-0.2%)."
            ),
        ),
        _bind(
            "portfolio.fractional_kelly",
            "Half-Kelly (default) fraction of capital to risk, f = fraction * "
            "(edge / odds), capped. Full Kelly is ruinous if the edge is "
            "overestimated, so a hard cap is applied.",
            fn=portfolio.fractional_kelly,
            provenance=(
                "Pure scalar formula over caller-asserted edge / odds; it "
                "consumes no bars, so it is not touches_byo. Its output is a "
                "private sizing decision and never coverage."
            ),
        ),
        _bind(
            "portfolio.meta_label_size",
            "Map a meta-label (secondary-model) probability to a [0, max_size] "
            "position multiplier: 0 below threshold, scaling linearly to "
            "max_size at probability 1.0.",
            fn=portfolio.meta_label_size,
            provenance=(
                "Pure scalar mapping over a caller-supplied probability; it "
                "consumes no bars. The threshold / max_size are policy "
                "constants, not estimates."
            ),
        ),
        _bind(
            "portfolio.drawdown_breaker",
            "Active drawdown circuit breaker: scale a desired position size "
            "down -- or halt it -- based on the current drawdown of the "
            "equity curve. The curve is required (no fabricated 0.0 drawdown).",
            fn=portfolio.drawdown_breaker,
            consumes=("price_snapshot",),
            touches_byo=True,
            provenance=(
                "A risk gate, not a forecast. v1's pipeline hardcoded "
                "drawdown = 0.0 which disabled this breaker; the equity "
                "curve is mandatory here so the gate cannot be silently "
                "turned off."
            ),
        ),
    ):
        registry.add(cap)

    # --- Portfolio risk: VaR / CVaR / beta / correlation / stress ----------
    # (capabilities/portfolio_risk.py)
    # Every one runs over return series (or, for stress_book, exposures /
    # positions derived from them), so all inherit the price licence and are
    # touches_byo. The claim_type enum has no home for a VaR number, a beta or
    # a stress P&L, so produces is empty for all of them.
    for cap in (
        _bind(
            "portfolio_risk.calculate_var",
            "Value at Risk by three methods: historical percentile, "
            "parametric (normal) and a Monte-Carlo draw, at a chosen "
            "confidence level. Optional portfolio_value converts the loss "
            "to a currency amount.",
            fn=portfolio_risk.calculate_var,
            consumes=("price_snapshot",),
            touches_byo=True,
            provenance=(
                "The historical and parametric legs are deterministic over "
                "the supplied sample. The Monte-Carlo leg is a MODEL OUTPUT: "
                "it draws from a normal fit to the sample moments, so it "
                "systematically under-prices fat-tailed risk. Treat it as a "
                "what-if under a stated distribution, not a measurement. "
                "`seed` makes the draw reproducible."
            ),
        ),
        _bind(
            "portfolio_risk.calculate_cvar",
            "Conditional VaR / Expected Shortfall: the mean of the returns at "
            "or below the historical VaR threshold.",
            fn=portfolio_risk.calculate_cvar,
            consumes=("price_snapshot",),
            touches_byo=True,
        ),
        _bind(
            "portfolio_risk.calculate_beta",
            "Beta of an asset's returns against a benchmark: "
            "cov(asset, bench) / var(bench), both at ddof=1 so beta(x, x) == 1.",
            fn=portfolio_risk.calculate_beta,
            consumes=("price_snapshot",),
            touches_byo=True,
            provenance=(
                "A sample covariance ratio over the two supplied series; it "
                "raises on length mismatch, <2 paired observations or zero "
                "benchmark variance rather than returning a default 1.0."
            ),
        ),
        _bind(
            "portfolio_risk.calculate_correlation_matrix",
            "Pearson correlation matrix across the supplied return series, "
            "with the pairs whose abs(corr) exceeds a threshold and the mean "
            "upper-triangular correlation.",
            fn=portfolio_risk.calculate_correlation_matrix,
            consumes=("price_snapshot",),
            touches_byo=True,
        ),
        _bind(
            "portfolio_risk.stress_book",
            "Reprice a book of positions under a named stress scenario via a "
            "linear factor model: shocked return = exposures . factor_shocks "
            "(+ specific_shocks), with exact factor / specific P&L split.",
            fn=portfolio_risk.stress_book,
            touches_byo=True,
            provenance=(
                "The P&L is linear in the supplied exposures and the "
                "declared scenario shocks -- a what-if over a stated factor "
                "model, not an observed outcome. The scenario is caller-"
                "constructed (factor_shocks / specific_shocks), never "
                "fabricated."
            ),
        ),
    ):
        registry.add(cap)

    # --- Performance attribution (capabilities/attribution.py) ---
    # All consume return panels (asset / factor / benchmark series), so all
    # are touches_byo. The regression-based decompositions are model outputs
    # (OLS with an intercept); their additivity (factors + specific == total)
    # is asserted inside the implementation, which is the property worth
    # declaring in provenance.
    for cap in (
        _bind(
            "attribution.regress_factor_exposures",
            "OLS time-series regression of an asset's returns on a factor "
            "panel: per-factor betas, intercept, R-squared and residuals.",
            fn=attribution.regress_factor_exposures,
            consumes=("price_snapshot",),
            touches_byo=True,
            provenance=(
                "Model output, not a measurement: OLS exposures over the "
                "aligned window. Raises on rank-deficient / ill-conditioned "
                "design, no date overlap, or a zero-variance asset (R-squared "
                "undefined) rather than returning a fabricated fit."
            ),
        ),
        _bind(
            "attribution.attribute_returns",
            "Decompose portfolio returns into per-factor contributions plus a "
            "specific return. contribution[factor] = beta * sum(factor); "
            "specific = n * intercept. Factors + specific == total, exactly.",
            fn=attribution.attribute_returns,
            consumes=("price_snapshot",),
            touches_byo=True,
            provenance=(
                "Regression-based decomposition (OLS with intercept, so "
                "residuals are zero-mean and the split is additive to "
                "floating tolerance). v1's mean-exposure heuristic did not "
                "sum exactly and is not reproduced."
            ),
        ),
        _bind(
            "attribution.market_model_attribution",
            "Single-factor (market-model) performance attribution against a "
            "benchmark: annualised alpha, beta, R-squared, total / factor / "
            "specific return, tracking error and information ratio.",
            fn=attribution.market_model_attribution,
            consumes=("price_snapshot",),
            touches_byo=True,
            provenance=(
                "Single-factor OLS attribution. A zero tracking error "
                "(portfolio identical to benchmark) makes the information "
                "ratio undefined and raises, rather than returning the "
                "fabricated 0 of v1's handler."
            ),
        ),
        _bind(
            "attribution.holding_contributions",
            "Per-holding contribution to total portfolio return for a "
            "static-weight book: contribution_i = weight_i * sum(returns_i). "
            "Contributions sum to the portfolio total to floating tolerance.",
            fn=attribution.holding_contributions,
            consumes=("price_snapshot",),
            touches_byo=True,
        ),
    ):
        registry.add(cap)

    # --- Fixed income analytics (capabilities/fixed_income.py) ---
    # These compute over a Bond's price / yield and / or a YieldCurve built
    # from prices. Whether the result is shareable depends on the licence of
    # those inputs, which the descriptor cannot see -- so all are touches_byo,
    # exactly like detect.manipulation. The closed enum has no claim type for
    # a bond price, yield, duration, spread or curve, so produces is empty.
    for cap in (
        _bind(
            "fixed_income.calculate_price",
            "Clean price of a bond from its cash-flow schedule, discounted "
            "either at the bond's YTM (annual compounding) or at a supplied "
            "zero curve (continuous).",
            fn=fixed_income.calculate_price,
            touches_byo=True,
        ),
        _bind(
            "fixed_income.calculate_yield",
            "Yield to maturity of a bond from its price, solved by Brent "
            "search over the cash-flow schedule.",
            fn=fixed_income.calculate_yield,
            touches_byo=True,
            provenance=(
                "The YTM is the constant rate that reprices the bond to its "
                "price; a solve that fails to converge in the search bracket "
                "raises rather than returning v1's fabricated 0.05."
            ),
        ),
        _bind(
            "fixed_income.calculate_duration",
            "Interest-rate sensitivity suite: Macaulay, modified, effective, "
            "dollar duration and key-rate durations, at a chosen yield shock.",
            fn=fixed_income.calculate_duration,
            touches_byo=True,
            provenance=(
                "Effective duration is the central-difference of the price "
                "function; v1 dropped the leading negative sign and produced "
                "negative duration for vanilla bonds, which is fixed here."
            ),
        ),
        _bind(
            "fixed_income.calculate_convexity",
            "Second-order price/yield curvature of a bond from its "
            "cash-flow schedule (closed form, no finite-difference noise).",
            fn=fixed_income.calculate_convexity,
            touches_byo=True,
        ),
        _bind(
            "fixed_income.calculate_z_spread",
            "The constant spread over the risk-free zero curve that reprices "
            "the bond to its observed price, solved by Brent search.",
            fn=fixed_income.calculate_z_spread,
            touches_byo=True,
        ),
        _bind(
            "fixed_income.calculate_spread_duration",
            "Price sensitivity to a 1bp change in the bond's Z-spread.",
            fn=fixed_income.calculate_spread_duration,
            touches_byo=True,
        ),
        _bind(
            "fixed_income.calculate_credit_metrics",
            "Credit metrics derived from the Z-spread: implied default "
            "probability (hazard rate * time-to-maturity), expected loss, "
            "DV01 and credit01. The recovery rate is a required argument.",
            fn=fixed_income.calculate_credit_metrics,
            touches_byo=True,
            provenance=(
                "Model output, not a measurement: implied_default_probability "
                "uses the spread/(1-recovery) hazard-rate approximation, "
                "which can exceed 1.0 cumulatively for long tenors. The "
                "recovery rate is caller-owned -- there is no universal "
                "default, and v1's silent 0.4 is gone."
            ),
        ),
        _bind(
            "fixed_income.calculate_total_return",
            "Horizon total return on a bond: price return, coupon income and "
            "reinvestment income over a holding period at an assumed ending "
            "yield and reinvestment rate.",
            fn=fixed_income.calculate_total_return,
            touches_byo=True,
            provenance=(
                "A what-if over declared ending_yield / reinvestment_rate "
                "assumptions, not an observed holding-period return."
            ),
        ),
        _bind(
            "fixed_income.analyze_credit_migration",
            "Price impact of a rating migration under a transition matrix and "
            "a per-rating spread table: per-rating probability, spread change, "
            "price impact and the expected / downgrade probabilities.",
            fn=fixed_income.analyze_credit_migration,
            touches_byo=True,
            provenance=(
                "Scenario output over a caller-supplied transition matrix "
                "and spread table; price impact uses the modified-duration "
                "approximation (-duration * spread_change)."
            ),
        ),
        _bind(
            "fixed_income.fit_nelson_siegel",
            "Fit Nelson-Siegel beta0/beta1/beta2/lambda to a set of market "
            "tenors and yields by L-BFGS-B least squares.",
            fn=fixed_income.fit_nelson_siegel,
            touches_byo=True,
            provenance=(
                "Parametric model fit; lambda is ill-conditioned and the fit "
                "is judged on yield reproduction at the input tenors, not on "
                "recovering a true lambda."
            ),
        ),
        _bind(
            "fixed_income.build_yield_curve",
            "Bootstrap a YieldCurve from a set of zero-coupon bonds by "
            "fitting Nelson-Siegel to their maturities and yields.",
            fn=fixed_income.build_yield_curve,
            touches_byo=True,
            provenance=(
                "A parametric (Nelson-Siegel) curve, not a bootstrapped "
                "piecewise curve; only zero-coupon bonds contribute."
            ),
        ),
    ):
        registry.add(cap)

    return registry
