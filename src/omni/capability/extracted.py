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
invented: the claim_type enum declared across the migration files is closed,
and `CLAIM_TYPES` below mirrors it so a drift is caught.

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

# The closed claim_type enum, mirroring the claim_type values declared
# across all migration files (001_core_schema.sql, 003_domains_and_graph.sql,
# 010_yield_curve_signal.sql, ...). A capability declaring anything outside
# this set is a schema bug; test_claim_types_frozenset_mirrors_the_migration_enum
# asserts this set matches the migrations so the next addition fails loudly.
CLAIM_TYPES = frozenset(
    {
        "price_snapshot",
        "fundamental_metric",
        "filing_event",
        "macro_series_point",
        # 064: long descriptive sleeve series (gold, T-bills, 10y yield) --
        # current values, never to be mistaken for point-in-time vintages.
        "sleeve_history_point",
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
        "yield_curve_signal",
        "sahm_rule_signal",
        "inflation_signal",
        "output_gap_signal",
        "lei_signal",
        "regime_assessment",
        "sector_score",
        # 035: crypto derivatives. Keyless public venue data, so `allowed` --
        # unlike every crypto price feed, these accumulate as shared coverage.
        "funding_rate",
        "open_interest",
        "liquidation_event",
        "basis",
        # 037: crypto's redistributable fundamentals tier -- the EDGAR
        # counterpart. Fees are what users paid; revenue is the protocol's
        # own share, and conflating them misprices P/F by whatever goes to LPs.
        "protocol_revenue",
        "protocol_fees",
        "stablecoin_supply",
        "chain_tvl",
        # 036: microstructure. Feeds the cost model's spread estimate with a
        # measured number instead of a configured constant.
        "orderbook_snapshot",
        "trade_tape",
        # 041/042: the claim IS the convergence -- its provenance is the set
        # of independent claim families that agreed. A dial is an editorial
        # parameter stored bitemporally so a change is a new claim rather than a
        # silent rewrite of what a backtest sees.
        "convergence",
        "dial",
        # 050: ETF/fund holdings from issuer disclosures. The exposure tool maps
        # these to compute overlap and concentration across the portfolio.
        "holding",
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
        backtest,
        crossasset,
        execution_analytics,
        fixed_income,
        fundamentals,
        indicators,
        macro,
        microstructure,
        news,
        options,
        portfolio,
        portfolio_risk,
        regime,
        risk,
        signal_fusion,
        volatility,
    )

    registry = Registry()

    # --- Macro (FRED-sourced; redistributable) ---
    # The raw-series macros (sahm_rule, yield_curve_inversion, inflation_*) run
    # over macro_series_point claims and emit derived indicators. The composites
    # (recession_probability, assess_policy_implications, assess_scenario_impact,
    # taylor_rule_variant) take already-computed sub-indicators or caller-built
    # structures instead, so their consumes is empty -- see their notes. None of
    # the derived outputs has a home in the closed enum, so every one is an
    # analysis step with produces empty.
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
            consumes=(),
            provenance=(
                "Consumes the OUTPUTS of sibling capabilities (a yield-curve "
                "inversion flag, a Sahm trigger, LEI labels), not "
                "macro_series_point claims; a planner needs an inter-step "
                "value-passing layer to assemble them. Weights are policy-"
                "fixed, not calibrated. A deterministic composite; the "
                "conviction gate must not treat its output as a calibrated "
                "probability without resolved history."
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
            consumes=(),
            provenance=(
                "Six caller-supplied scalars; four (target, r_star, alpha, "
                "beta) are policy constants with no claim source. Consumes no "
                "macro_series_point claims, so a planner cannot assemble the "
                "args from the claim store alone."
            ),
        ),
        _bind(
            "macro.assess_policy_implications",
            "Policy read from a Taylor rate versus the current rate: stance, "
            "the rate adjustment implied and the risks that arise.",
            fn=macro.assess_policy_implications,
            consumes=(),
            provenance=(
                "taylor_rate is the output of macro.taylor_rule and stance is "
                "a caller-supplied label, not macro_series_point claims; a "
                "planner needs an inter-step value-passing layer to assemble "
                "them."
            ),
        ),
        _bind(
            "macro.assess_scenario_impact",
            "Average and peak per-variable impact of a scenario forecast "
            "against a baseline forecast.",
            fn=macro.assess_scenario_impact,
            consumes=(),
            provenance=(
                "Takes two caller-built nested forecast dicts (baseline and "
                "scenario), not macro_series_point claims; nothing in the "
                "claim store yields these structures, so a planner cannot "
                "assemble the args from claims alone."
            ),
        ),
    ):
        registry.add(cap)

    # --- Fundamentals and portfolio analytics ---
    # financial_ratios and dcf_valuation take a required current_price (a
    # price_snapshot, produced only by the byo_only polygon/coingecko feeds)
    # alongside EDGAR fundamentals, so both inherit the price licence.
    # peer_comparison ranks price-derived ratios (pe/pb/peg) carried in
    # comparison_data, so it inherits the price licence too. stress_tests
    # scales a bar-derived NAV. The remaining portfolio analytics consume
    # price/return series whose licence the descriptor cannot see, so every
    # one carries touches_byo exactly like detect.manipulation.
    for cap in (
        _bind(
            "fundamentals.financial_ratios",
            "Per-share and margin ratios (P/E, PEG, P/B, ROE, ROA, "
            "debt-to-equity, current/quick, free cash flow, dividend yield) "
            "from an as-reported fundamentals snapshot.",
            fn=fundamentals.financial_ratios,
            consumes=("fundamental_metric", "price_snapshot"),
            entity_kinds=("company",),
            touches_byo=True,
            provenance=(
                "pe/pb ratios and dividend yield divide by current_price, a "
                "price_snapshot produced only by the byo_only polygon/coingecko "
                "feeds, so the output inherits that licence. A computed ratio, "
                "not a primary data point, so it is left out of the "
                "fundamental_metric producers rather than conflated with them."
            ),
        ),
        _bind(
            "fundamentals.dcf_valuation",
            "Discounted-cash-flow fair value: projected FCFs, terminal value "
            "and the equity bridge to per-share value and upside.",
            fn=fundamentals.dcf_valuation,
            consumes=("fundamental_metric", "price_snapshot"),
            entity_kinds=("company",),
            touches_byo=True,
            provenance=(
                "upside_percentage is a function of current_price and the "
                "output returns it; current_price is a price_snapshot produced "
                "only by the byo_only polygon/coingecko feeds, so the result "
                "inherits that licence. Model output, not a measured value; "
                "depends on declared assumptions (growth, discount, terminal "
                "rate)."
            ),
        ),
        _bind(
            "fundamentals.peer_comparison",
            "Rank a company's metrics against supplied peers: peer averages, "
            "per-metric percentile rank and relative valuation versus peers.",
            fn=fundamentals.peer_comparison,
            consumes=("fundamental_metric", "price_snapshot"),
            entity_kinds=("company",),
            touches_byo=True,
            provenance=(
                "comparison_data carries price-derived ratios (pe/pb/peg/"
                "dividend_yield) and relative_valuation is computed from them. "
                "Their provenance is opaque to the descriptor, so the output "
                "inherits the price licence -- the same convention as "
                "portfolio.atr_position_size."
            ),
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
            touches_byo=True,
            provenance=(
                "total_value is a bar-derived NAV (positions marked at market "
                "prices), so the dollar P&L inherits the price licence -- the "
                "same convention as portfolio.atr_position_size. Declared "
                "stress assumptions (-20/-5/-3%), not measured outcomes; "
                "hypothetical, so never aggregate into shared coverage as if "
                "observed."
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
    # aggregate_market_sentiment / score_portfolio_impact take sentiment inputs
    # whose provenance the descriptor cannot see. The only news_event producer
    # wired today is rss (allowed), but a byo_only news provider (news_api)
    # exists in the catalog, so the safe direction is touches_byo=True: over-
    # exclude from shared plans rather than risk leaking a commercial feed once
    # one is wired. The StockTwits scorers are genuinely byo -- StockTwits is
    # commercial and perception_social has no shareable producer.
    # aggregate_market_sentiment and the StockTwits scorers yield perception_*
    # readings, so they produce; score_portfolio_impact is an analysis step.
    for cap in (
        _bind(
            "news.aggregate_market_sentiment",
            "Aggregate per-sentiment counts into a single market read: "
            "overall bullish/bearish/neutral, a per-sentiment breakdown and "
            "total article count.",
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

    # --- Options pricing and chain analytics (capabilities/options.py) ---
    # Every function inherits the licence of the price / quote data it
    # consumes (spot prices, option market prices, volumes, OI), so all are
    # touches_byo exactly like detect.manipulation. The closed enum has no
    # claim type for an option price, Greek, implied vol or chain metric, so
    # produces is empty for all of them.
    for cap in (
        _bind(
            "options.black_scholes",
            "Black-Scholes-Merton price and Greeks for a European option. "
            "All inputs are required positional arguments.",
            fn=options.black_scholes,
            consumes=("price_snapshot",),
            touches_byo=True,
            provenance=(
                "Model output, not a measurement: assumes European exercise, "
                "continuous trading, and constant r/sigma/q. T == 0 returns "
                "intrinsic (the correct limit); T < 0 or sigma <= 0 raises."
            ),
        ),
        _bind(
            "options.implied_volatility",
            "Implied volatility from a market price by Newton-Raphson with "
            "Brent fallback, or None on non-convergence.",
            fn=options.implied_volatility,
            consumes=("price_snapshot",),
            touches_byo=True,
            provenance=(
                "Model-derived: the constant vol that reprices BSM to the "
                "observed price. Returns None -- never the last iterate -- "
                "when the solver fails, the price is below intrinsic, or "
                "T <= 0."
            ),
        ),
        _bind(
            "options.monte_carlo",
            "Monte Carlo option price under geometric Brownian motion with "
            "an explicit seed for reproducibility.",
            fn=options.monte_carlo,
            consumes=("price_snapshot",),
            touches_byo=True,
            provenance=(
                "Model output, not a measurement: the discounted expectation "
                "of terminal payoff under risk-neutral GBM, estimated by "
                "Monte Carlo. Carries sampling noise quantified by std_error "
                "and the 95% confidence interval."
            ),
        ),
        _bind(
            "options.build_volatility_surface",
            "Implied-volatility grid for market_prices at strikes x "
            "expiries, via the BSM IV solver. Non-convergent cells are NaN.",
            fn=options.build_volatility_surface,
            consumes=("price_snapshot",),
            touches_byo=True,
            provenance=(
                "Model-derived surface: each cell is the IV that reprices "
                "BSM to the observed market price. Cells where the solver "
                "cannot converge or the price is non-positive are NaN, "
                "never a fabricated number."
            ),
        ),
        _bind(
            "options.put_call_ratio",
            "Volume put/call ratio over an option chain, with sentiment "
            "band.",
            fn=options.put_call_ratio,
            consumes=("price_snapshot",),
            touches_byo=True,
        ),
        _bind(
            "options.max_pain",
            "Max-pain strike -- the strike at which total option-holder "
            "payoff is minimised -- from open interest across the chain.",
            fn=options.max_pain,
            consumes=("price_snapshot",),
            touches_byo=True,
        ),
        _bind(
            "options.detect_unusual_activity",
            "Flag contracts whose volume exceeds twice their open interest.",
            fn=options.detect_unusual_activity,
            consumes=("price_snapshot",),
            touches_byo=True,
        ),
        _bind(
            "options.put_call_parity_errors",
            "Put-call parity violations across paired call/put contracts "
            "with complete bid/ask quotes.",
            fn=options.put_call_parity_errors,
            consumes=("price_snapshot",),
            touches_byo=True,
        ),
    ):
        registry.add(cap)

    # --- Realised volatility estimators (capabilities/volatility.py) ---
    # All consume price or OHLC bar data and inherit the price feed
    # licence, so all are touches_byo. No estimator output has a home in
    # the closed claim_type enum, so produces is empty for all.
    for cap in (
        _bind(
            "volatility.close_to_close",
            "Annualised close-to-close volatility: log-return population "
            "std over a trailing window.",
            fn=volatility.close_to_close,
            consumes=("price_snapshot",),
            touches_byo=True,
            provenance=(
                "Deterministic estimator, not a forecast. Uses log returns "
                "(not simple) so the sqrt(T) annualisation invariance holds. "
                "Zero-variance windows raise rather than returning 0."
            ),
        ),
        _bind(
            "volatility.ewma",
            "Annualised EWMA (RiskMetrics) volatility over a trailing "
            "window.",
            fn=volatility.ewma,
            consumes=("price_snapshot",),
            touches_byo=True,
            provenance=(
                "Normalised exponential weights on squared zero-mean log "
                "returns. The lambda decay (default 0.94) is a declared "
                "constant, not calibrated."
            ),
        ),
        _bind(
            "volatility.parkinson",
            "Annualised Parkinson (1980) high-low volatility from OHLC "
            "bars.",
            fn=volatility.parkinson,
            consumes=("price_snapshot",),
            touches_byo=True,
            provenance=(
                "Intraday-range estimator; more efficient than close-to-"
                "close but assumes zero drift. An addition from the "
                "standard closed form, not a v1 port."
            ),
        ),
        _bind(
            "volatility.garman_klass",
            "Annualised Garman-Klass (1980) OHLC volatility.",
            fn=volatility.garman_klass,
            consumes=("price_snapshot",),
            touches_byo=True,
            provenance=(
                "OHLC estimator using close-open in addition to high-low. "
                "Negative per-bar variance (corrupt OHLC) raises rather "
                "than being swallowed."
            ),
        ),
        _bind(
            "volatility.rogers_satchell",
            "Annualised Rogers-Satchell (1991) OHLC volatility.",
            fn=volatility.rogers_satchell,
            consumes=("price_snapshot",),
            touches_byo=True,
            provenance=(
                "OHLC estimator bias-corrected for drift; does not assume "
                "zero mean, unlike Parkinson."
            ),
        ),
        _bind(
            "volatility.volatility_of_volatility",
            "Annualised population std of a series of volatility readings.",
            fn=volatility.volatility_of_volatility,
            consumes=("price_snapshot",),
            touches_byo=True,
            provenance=(
                "Second-order statistic over caller-supplied vol readings; "
                "the readings inherit the licence of whatever produced them."
            ),
        ),
    ):
        registry.add(cap)

    # --- Market regime detection (capabilities/regime.py) ---
    # All consume return series (derived from prices) so all are
    # touches_byo. The closed enum has no claim type for a regime label or
    # rolling-vol series, so produces is empty. detect_regime_changes is the
    # exception on consumes: it takes a label sequence, not price claims.
    for cap in (
        _bind(
            "regime.realised_volatility",
            "Trailing rolling population std of returns over the full "
            "series.",
            fn=regime.realised_volatility,
            consumes=("price_snapshot",),
            touches_byo=True,
        ),
        _bind(
            "regime.volatility_regime_path",
            "Per-bar volatility regime label (quiet / transition / "
            "volatile) banded against the rolling-vol median.",
            fn=regime.volatility_regime_path,
            consumes=("price_snapshot",),
            touches_byo=True,
            provenance=(
                "The 1.3x / 2.0x median multipliers are declared constants, "
                "not calibrated thresholds. A deterministic classifier with "
                "no uncertainty band."
            ),
        ),
        _bind(
            "regime.classify_volatility",
            "Classify the current bar's volatility regime against the "
            "rolling-vol median.",
            fn=regime.classify_volatility,
            consumes=("price_snapshot",),
            touches_byo=True,
            provenance=(
                "Threshold-classified against the series median; the 1.3x / "
                "2.0x multipliers are declared constants, not calibrated."
            ),
        ),
        _bind(
            "regime.classify_trend",
            "Classify the current trend regime via MA crossover on "
            "returns: uptrend / downtrend / neutral.",
            fn=regime.classify_trend,
            consumes=("price_snapshot",),
            touches_byo=True,
            provenance=(
                "The 20 / 60 MA windows are declared constants, not "
                "optimised. A deterministic crossover rule."
            ),
        ),
        _bind(
            "regime.detect_regime_changes",
            "Indices where consecutive regime labels differ.",
            fn=regime.detect_regime_changes,
            touches_byo=True,
            provenance=(
                "Pure structural transform on a label sequence; introduces "
                "no lag of its own. The output inherits the licence of the "
                "regime labels it processes."
            ),
        ),
    ):
        registry.add(cap)

    # --- Technical indicators (capabilities/indicators.py) ---
    # All consume price and/or volume series, so all are touches_byo. The
    # closed enum has no claim type for a moving average, oscillator or
    # cumulative-volume reading, so produces is empty.
    for cap in (
        _bind(
            "indicators.sma",
            "Simple moving average; None in the first period - 1 positions.",
            fn=indicators.sma,
            consumes=("price_snapshot",),
            touches_byo=True,
        ),
        _bind(
            "indicators.ema",
            "Exponential moving average seeded with the SMA of the first "
            "period values.",
            fn=indicators.ema,
            consumes=("price_snapshot",),
            touches_byo=True,
        ),
        _bind(
            "indicators.rsi",
            "Relative Strength Index via Wilder's smoothing.",
            fn=indicators.rsi,
            consumes=("price_snapshot",),
            touches_byo=True,
        ),
        _bind(
            "indicators.macd",
            "MACD line, signal line and histogram from fast / slow / "
            "signal EMA periods.",
            fn=indicators.macd,
            consumes=("price_snapshot",),
            touches_byo=True,
        ),
        _bind(
            "indicators.bollinger_bands",
            "Bollinger bands with population std; zero-variance windows "
            "collapse to a zero-width band.",
            fn=indicators.bollinger_bands,
            consumes=("price_snapshot",),
            touches_byo=True,
        ),
        _bind(
            "indicators.stochastic",
            "Stochastic oscillator %K and %D (SMA of %K).",
            fn=indicators.stochastic,
            consumes=("price_snapshot",),
            touches_byo=True,
        ),
        _bind(
            "indicators.atr",
            "Average True Range via Wilder's smoothing.",
            fn=indicators.atr,
            consumes=("price_snapshot",),
            touches_byo=True,
        ),
        _bind(
            "indicators.vwap",
            "Cumulative volume-weighted average price using typical price.",
            fn=indicators.vwap,
            consumes=("price_snapshot",),
            touches_byo=True,
        ),
        _bind(
            "indicators.obv",
            "On-balance volume: cumulative signed volume by price "
            "direction.",
            fn=indicators.obv,
            consumes=("price_snapshot",),
            touches_byo=True,
        ),
    ):
        registry.add(cap)

    # --- Market microstructure (capabilities/microstructure.py) ----------
    # All three compute over trade prints and quotes (price / volume market
    # data), so all inherit the price-feed licence and are touches_byo exactly
    # like detect.manipulation. The closed enum has no claim type for an
    # effective spread, a Kyle lambda or a VPIN reading, so produces is empty.
    for cap in (
        _bind(
            "microstructure.effective_spread",
            "Effective, realised and price-improvement spreads over the last "
            "100 trades matched to their prevailing quotes, at a configurable "
            "realised-spread horizon.",
            fn=microstructure.effective_spread,
            consumes=("price_snapshot",),
            touches_byo=True,
            provenance=(
                "Means over the trades that contributed each measure, so "
                "realised_spread may reflect a subset. Fewer than 5 trades or "
                "quotes, or no derivable realised observation, raises rather "
                "than returning v1's fabricated 0.0."
            ),
        ),
        _bind(
            "microstructure.kyle_lambda",
            "Kyle's lambda: the OLS slope of price-change on tick-rule signed "
            "order flow, scaled by 1e4.",
            fn=microstructure.kyle_lambda,
            consumes=("price_snapshot",),
            touches_byo=True,
            provenance=(
                "A regression-derived price-impact coefficient, not a "
                "measurement; signed by the tick rule (the first trade is "
                "treated as a buy). Fewer than 10 trades, a flat price series, "
                "or zero-variance signed volume raises rather than returning "
                "0.0."
            ),
        ),
        _bind(
            "microstructure.order_flow_toxicity",
            "Volume-synchronised probability of informed trading (VPIN): the "
            "mean absolute buy/sell volume imbalance across volume buckets.",
            fn=microstructure.order_flow_toxicity,
            consumes=("price_snapshot",),
            touches_byo=True,
            provenance=(
                "A [0, 1] imbalance ratio over volume buckets; trade direction "
                "uses the VPIN tick rule (a non-decreasing tick is a buy). "
                "Fewer than 50 trades or 5 completed buckets raises rather "
                "than returning 0.0."
            ),
        ),
    ):
        registry.add(cap)

    # --- Execution analytics / post-trade TCA (capabilities/execution_analytics.py) ---
    # These measure executions that have already happened. The slippage and
    # shortfall caps consume fill prices and benchmark price/volume bars, all
    # price-derived, so they are touches_byo exactly like detect.manipulation.
    # slippage_summary / identify_outliers take already-computed scalar series
    # whose licence the descriptor cannot see, so they are touches_byo too --
    # their output is audience-scoped to whoever owned the underlying fills.
    # The closed enum has no claim type for a slippage, shortfall or impact
    # figure, so produces is empty for all.
    for cap in (
        _bind(
            "execution.implementation_shortfall",
            "Perold/Wagner implementation shortfall decomposed into delay, "
            "trading and opportunity cost in bps, exactly additive in the "
            "decision price as the single numeraire.",
            fn=execution_analytics.implementation_shortfall,
            consumes=("price_snapshot",),
            touches_byo=True,
            provenance=(
                "decision_price is a required argument independent of the "
                "fills; shortfall against the first fill's own price is "
                "definitionally zero and is the classic fake. The three "
                "components sum to the total to floating tolerance "
                "(check_additivity runs inside the call)."
            ),
        ),
        _bind(
            "execution.benchmark_slippage",
            "Fill VWAP versus a supplied benchmark price (arrival / decision / "
            "close), in bps, signed for side.",
            fn=execution_analytics.benchmark_slippage_bps,
            consumes=("price_snapshot",),
            touches_byo=True,
        ),
        _bind(
            "execution.vwap_slippage",
            "Fill VWAP versus the interval VWAP built from a benchmark "
            "price/volume window, in bps.",
            fn=execution_analytics.vwap_slippage_bps,
            consumes=("price_snapshot",),
            touches_byo=True,
            provenance=(
                "The benchmark VWAP is computed from the supplied bars; a "
                "zero-volume window raises rather than dividing by a smuggled-"
                "in scalar. Fills outside the window raise."
            ),
        ),
        _bind(
            "execution.slippage_summary",
            "Mean, median and standard deviation of a slippage series in bps.",
            fn=execution_analytics.slippage_summary,
            touches_byo=True,
            provenance=(
                "A descriptive statistic over caller-supplied slippage "
                "values; the values inherit the licence of the executions "
                "they were computed from. An empty series raises."
            ),
        ),
        _bind(
            "execution.identify_outliers",
            "Indices whose value exceeds z_threshold standard deviations from "
            "the mean (default |z| > 2).",
            fn=execution_analytics.identify_outliers,
            touches_byo=True,
            provenance=(
                "z-score flagging over a caller-supplied series; a constant "
                "series returns [] via the exact np.ptp == 0 guard rather "
                "than fabricating z-scores from floating-point residue."
            ),
        ),
    ):
        registry.add(cap)

    # --- Signal fusion: normalisation, convergence, lead-lag --------------
    # (capabilities/signal_fusion.py)
    # These run over already-collected signal / return scalars the caller
    # supplies. Those values are downstream of perception_* claims (news and
    # social are byo_only) or of price-derived return series, and the fusion
    # arithmetic cannot see which, so every one is touches_byo -- a fused or
    # normalised reading must never be assumed shareable. The closed enum has
    # no claim type for a normalised signal, a convergence reading, a
    # conviction score or a lead-lag result, so produces is empty. None of the
    # inputs is a typed claim (the functions are signal-vector agnostic), so
    # consumes is empty too.
    for cap in (
        _bind(
            "signal_fusion.normalize",
            "Put a signal series on a common [-1, +1] scale: one of identity "
            "/ sign / z-score / percentile / min-max / tanh / rank, with "
            "optional inversion.",
            fn=signal_fusion.normalize,
            touches_byo=True,
            provenance=(
                "A deterministic transform, not a measurement. The rolling "
                "z-score is a z-score against a stated reference window, "
                "clipped to [-1, 1]; a globally constant input raises rather "
                "than yielding v1's fabricated zeros."
            ),
        ),
        _bind(
            "signal_fusion.convergence",
            "Fuse one date's already-normalised signal vector into a "
            "directional reading: weighted direction, alignment, bull / bear "
            "/ neutral counts and the largest pairwise divergences.",
            fn=signal_fusion.convergence,
            touches_byo=True,
            provenance=(
                "Weighted mean and 1-std agreement over the supplied signals; "
                "fewer than two signals, or weights summing to zero, raise "
                "rather than falling back to uniform weights. No absent "
                "signal is invented."
            ),
        ),
        _bind(
            "signal_fusion.conviction",
            "Assemble a conviction score from alignment, direction and a "
            "caller-supplied participation (coverage) fraction: "
            "0.6*alignment + 0.2*participation + 0.2*|direction|.",
            fn=signal_fusion.conviction,
            touches_byo=True,
            provenance=(
                "v1's fixed-weight assembly; the weights are declared "
                "constants, not calibrated. participation is a required "
                "coverage input -- inventing a breadth number is the "
                "substitution this port exists to remove."
            ),
        ),
        _bind(
            "signal_fusion.lead_lag",
            "Find the shift maximising |correlation| between two series over "
            "[-max_lag, max_lag], with v1's t-stat significance.",
            fn=signal_fusion.lead_lag,
            touches_byo=True,
            provenance=(
                "Cross-correlation scan; the best lag is refused when its "
                "effective overlap falls below the sample-size floor (a two-"
                "point correlation is noise, not an edge). Significance is "
                "reported, not used to silently drop an adequately-sampled "
                "lag."
            ),
        ),
    ):
        registry.add(cap)

    # --- Market / macro / systemic risk (capabilities/risk.py) -------------
    # v1's integrated_risk_analyzer: breadth, concentration, options skew,
    # growth, credit, correlation breakdown and geopolitical risk, plus the
    # weighted overall composite. Prefix is `market_risk.` -- NOT `risk.`
    # (which shares the "risk" token with portfolio_risk and a planner could
    # read as a superset of it) and not `portfolio_risk.` (taken: that module
    # scores a POSITION book -- VaR/CVaR/beta/stress). market_risk scores the
    # MARKET environment, the standard finance counterpart to position risk.
    #
    # Licence split. The macro inputs (growth) come from FRED (allowed) and are
    # shareable; everything else derives from prices (quotes, market caps,
    # option IVs, breadth, return series) or from news whose producer could be
    # byo, so each entry is classified individually below. No output has a home
    # in the closed claim_type enum, so produces is empty for all: these are
    # analysis steps, not claim writers.
    #
    # Four overlaps with already-registered capabilities were deliberately left
    # UNREGISTERED rather than entered as peers -- registering a second entry
    # that computes the same quantity differently is the two-incompatible-
    # registries failure this project shipped once. They are spelled out, with
    # the divergence, in the operator's archived research notes:
    # estimate_recession_probability (vs macro.recession_probability),
    # analyze_volatility_regime (vs regime.classify_volatility / volatility.*),
    # analyze_yield_curve_risk (vs macro.yield_curve_inversion) and
    # analyze_inflation_risk (vs macro.inflation_*). The single-chain / single-
    # step leaf helpers (calculate_put_call_skew, calculate_depth_score,
    # calculate_top_concentration, classify_risk_level, find_extreme_skew,
    # calculate_correlation_stability, identify_correlation_clusters,
    # identify_geopolitical_hotspots, identify_scenario_triggers,
    # identify_concentrated_sectors) are sub-computations or static catalogs,
    # not orchestrator entry points, and are likewise left out.
    for cap in (
        _bind(
            "market_risk.liquidity_risk",
            "Wide-spread ratio across a set of (bid, ask) quotes, scaled to a "
            "0-100 liquidity-stress score.",
            fn=risk.analyze_liquidity_risk,
            touches_byo=True,
            is_proxy=True,
            proxy_of=("liquidity",),
            provenance=(
                "Quotes are bid/ask market data (price_snapshot-derived, "
                "byo_only feeds). The wide-spread ratio is a PROXY for "
                "liquidity stress, not a direct depth measurement. A zero bid "
                "or no quotes raises Unavailable rather than v1's neutral score."
            ),
        ),
        _bind(
            "market_risk.concentration_risk",
            "Herfindahl-Hirschman market concentration from supplied market "
            "caps, scaled to 0-100, with the top-5 share.",
            fn=risk.analyze_concentration_risk,
            touches_byo=True,
            provenance=(
                "Market caps are price_snapshot-derived (byo_only feeds); the "
                "HHI is a deterministic transform of them. No market caps "
                "raises Unavailable rather than v1's neutral 50."
            ),
        ),
        _bind(
            "market_risk.options_skew",
            "Average put-call skew across supplied skew observations, mapped "
            "to a 0-100 fear/complacency score.",
            fn=risk.analyze_options_skew,
            touches_byo=True,
            is_proxy=True,
            proxy_of=("positioning",),
            provenance=(
                "Consumes already-computed skew values -- each the OTM-put "
                "minus OTM-call IV of one chain, derived from option prices "
                "(byo_only). A planner must assemble the skews first (the "
                "single-chain calculate_put_call_skew helper is deliberately "
                "unregistered), so consumes is empty: a skew float is not a "
                "claim. None observations are dropped; all-None raises."
            ),
        ),
        _bind(
            "market_risk.breadth",
            "Market-breadth score from advance/decline ratio, percent above "
            "the 50/200-day MAs and new highs/lows.",
            fn=risk.analyze_market_breadth,
            touches_byo=True,
            is_proxy=True,
            proxy_of=("participation",),
            provenance=(
                "Every input is a breadth statistic derived from cross-section "
                "price series (byo_only); a planner must assemble them first, "
                "so consumes is empty. The score is a PROXY for market "
                "participation, not a direct measurement. All inputs are "
                "required -- v1 defaulted each to a neutral value."
            ),
        ),
        _bind(
            "market_risk.growth_risk",
            "Growth risk from a GDP nowcast and employment. The output carries "
            "a growth_score_recession_heuristic field that is a DISTINCT "
            "GDP/unemployment heuristic, NOT the same quantity as "
            "macro.recession_probability's yield-curve/Sahm/LEI composite.",
            fn=risk.analyze_growth_risk,
            consumes=("macro_series_point",),
            provenance=(
                "Inputs (gdp_growth, unemployment, job_growth) are "
                "macro_series_point series sourced from FRED (allowed), with no "
                "price input, so the result is shareable. The "
                "growth_score_recession_heuristic field is risk.py's "
                "GDP/unemployment heuristic (0.15 base plus band adds) -- a "
                "diagnostic for why the growth score moved, NOT a second "
                "estimate of macro.recession_probability's calibrated-seeming "
                "composite. The two use different inputs and a different model "
                "and disagree on the same state (0.85 vs 1.0 -- see QM M2 / "
                "N6); do not average, compare or substitute them. The field "
                "name intentionally contains neither 'probability' nor "
                "'recession_probability' so a consumer cannot read it as "
                "interchangeable with macro.recession_probability's `probability` "
                "output key. Score thresholds are policy-fixed, not calibrated."
            ),
        ),
        _bind(
            "market_risk.credit_risk",
            "Credit-spread risk against historical-average anchors (IG 120, "
            "HY 450 bps): a numeric stress score in {40, 60, 80} via 1.2x and "
            "1.5x multipliers of the anchors, plus a spread_widening flag.",
            fn=risk.analyze_credit_risk,
            touches_byo=True,
            provenance=(
                "IG/HY spreads are caller-supplied; their source is opaque to "
                "the descriptor (FRED publishes some spread series, allowed, "
                "but the same spreads are commonly observed from bond prices -- "
                "byo_only). Safe direction is touches_byo: over-exclude from "
                "shared plans rather than leak. The 120/450 anchors and 1.2x/"
                "1.5x bands are calibration constants, not input defaults."
            ),
        ),
        _bind(
            "market_risk.correlation_risks",
            "Mean pairwise correlation, its 60-day rolling stability, a "
            "breakdown flag and single-link correlation clusters across "
            "supplied per-symbol return series.",
            fn=risk.analyze_correlation_risks,
            consumes=("price_snapshot",),
            touches_byo=True,
            provenance=(
                "Returns are price_snapshot-derived (byo_only); the caller owns "
                "the price->return conversion. Distinct from "
                "portfolio_risk.calculate_correlation_matrix, which returns the "
                "matrix and high-corr pairs: this adds the rolling STABILITY "
                "and the breakdown/cluster risk view over time. Fewer than two "
                "symbols raises rather than v1's neutral 50; stability is 0.0 "
                "(not fabricated) when fewer than one 60-day window is "
                "observable."
            ),
        ),
        _bind(
            "market_risk.geopolitical_risks",
            "Geopolitical risk score from risk-keyword density in article "
            "titles, plus the matched regional hotspots.",
            fn=risk.analyze_geopolitical_risks,
            consumes=("news_event",),
            touches_byo=True,
            provenance=(
                "Articles are news_event claims; the only wired producer today "
                "is rss (allowed), but a byo_only news provider (news_api) "
                "exists in the catalog, so the safe direction is touches_byo -- "
                "matching news.aggregate_market_sentiment. A keyword-labelling "
                "step over titles, not a measurement; at least one article is "
                "required."
            ),
        ),
        _bind(
            "market_risk.overall_risk_score",
            "Weighted overall risk score and black-swan probability from five "
            "sibling risk scores (market / economic / sentiment / correlation / "
            "geopolitical).",
            fn=risk.calculate_overall_risk_score,
            touches_byo=True,
            provenance=(
                "A COMPOSITE of five sibling-capability outputs, not claims: a "
                "planner needs an inter-step value-passing layer to assemble "
                "them, so consumes is empty (the same shape as the macro "
                "composites L1 corrected -- never point consumes at a claim the "
                "planner cannot satisfy). Weights are policy-fixed "
                "(0.30/0.25/0.15/0.15/0.15), NOT calibrated, so the conviction "
                "gate must not treat the output as a calibrated probability. "
                "touches_byo because the market/sentiment/correlation/"
                "geopolitical sub-scores are themselves byo, so the blend "
                "inherits the most restrictive licence of its inputs. "
                "black_swan_prob is a policy formula, not an estimate."
            ),
        ),
    ):
        registry.add(cap)

    # --- Backtest validation: multiple-testing correction + the no-leakage
    # backtester (capabilities/backtest.py) -------------------------------
    # The project's defence against fooling itself: deflated Sharpe,
    # probability of backtest overfitting, a causal point-in-time backtester
    # and a leakage probe. Every one runs over a strategy return series or a
    # price series (returns are price_snapshot-derived; the only price
    # producers are the byo_only polygon/coingecko feeds), so all are
    # touches_byo exactly like detect.manipulation. The closed enum has no
    # claim type for a Sharpe credibility report, a PBO reading, a backtest
    # equity curve or a leakage verdict, so produces is empty for all: these
    # are analysis steps, not claim writers.
    #
    # The leaf Sharpe helpers (sharpe_ratio, probabilistic_sharpe_ratio,
    # expected_max_sharpe, deflated_sharpe_ratio) and forward_returns are
    # deliberately NOT registered: they take pre-computed scalars or are
    # inputs to evaluate_strategy_sharpe / backtest_signal, and registering
    # them alongside the composites is the "forty near-identical maths
    # functions" the work order warns against -- see _orchestrator/reports/N4.md.
    # No already-registered capability computes a deflated Sharpe, a PBO or a
    # no-look-ahead backtest; fundamentals.portfolio_returns emits a
    # descriptive annualised Sharpe (hardcoded 2.0 risk-free) but no
    # multiple-testing correction, so there is no duplicate to collapse.
    for cap in (
        _bind(
            "backtest.evaluate_strategy_sharpe",
            "End-to-end Sharpe credibility report for a strategy return "
            "series: annualised Sharpe, per-period Sharpe, skew, kurtosis, "
            "the Probabilistic Sharpe Ratio (vs 0) and the Deflated Sharpe "
            "Ratio (vs the expected max over n_trials), with an is_credible "
            "gate at DSR > 0.95.",
            fn=backtest.evaluate_strategy_sharpe,
            consumes=("price_snapshot",),
            touches_byo=True,
            provenance=(
                "Model output, not a measurement: PSR/DSR are probabilities "
                "under the stated skew/kurtosis-adjusted distribution, not "
                "observed frequencies. sr_variance defaults to 1/n_obs (the "
                "Sharpe estimator's sampling variance under the null) when "
                "the caller omits it; n_trials is caller-asserted. consumes "
                "price_snapshot because a returns series is price-derived -- a "
                "planner assembles it from price_snapshot claims via the "
                "price->return transform (the convention every returns-"
                "consuming cap in this file follows). A constant or too-short "
                "series yields NaN fields and is_credible=False rather than a "
                "fabricated verdict."
            ),
        ),
        _bind(
            "backtest.probability_of_backtest_overfitting",
            "Probability of Backtest Overfitting via Combinatorially-"
            "Symmetric Cross-Validation: the fraction of train/test splits "
            "where the in-sample-best strategy ranks below the median out-of-"
            "sample, over a (T, N) per-strategy performance matrix.",
            fn=backtest.probability_of_backtest_overfitting,
            consumes=("price_snapshot",),
            touches_byo=True,
            is_proxy=True,
            proxy_of=("out_of_sample_degradation",),
            provenance=(
                "A PROXY for out-of-sample degradation: PBO near 0.5+ means "
                "the in-sample selection rank does not carry out of sample, "
                "not that the live strategy will lose money. Model output "
                "over a caller-supplied performance matrix (per-strategy "
                "returns, price_snapshot-derived). T < n_groups raises "
                "Unavailable rather than silently rewriting n_groups to a "
                "different granularity -- the v1 substitution this port "
                "exists to remove."
            ),
        ),
        _bind(
            "backtest.backtest_signal",
            "Point-in-time single-asset backtest of a position signal against "
            "a price series: the signal is lagged (>=1 bar) before applying to "
            "forward returns, so a signal known at t can only earn the t->t+h "
            "return. Returns per-bar strategy returns and the cumulative "
            "equity curve, net of a proportional cost_per_turn.",
            fn=backtest.backtest_signal,
            consumes=("price_snapshot",),
            touches_byo=True,
            provenance=(
                "The no-look-ahead invariant is enforced by shifting the "
                "signal forward one bar; there is no code path by which a "
                "same-bar signal touches a same-bar return. `prices` is a "
                "price_snapshot series (polygon/coingecko, byo_only), so the "
                "resulting returns/equity inherit that licence. `signal` is a "
                "caller-supplied position series, not a claim -- a planner "
                "needs a signal-producing step to assemble it. lag < 1 raises "
                "rather than allowing look-ahead."
            ),
        ),
        _bind(
            "backtest.leakage_probe",
            "Demonstrate that the backtester forbids look-ahead: build a "
            "perfect-foresight signal (the future return sign) and verify the "
            "causal backtester (lag>=1) fails to reproduce the impossible "
            "look-ahead PnL. Returns the naive, causal and leak_prevented "
            "verdict.",
            fn=backtest.leakage_probe,
            consumes=("price_snapshot",),
            touches_byo=True,
            provenance=(
                "A diagnostic self-test of the no-leakage invariant, not a "
                "strategy result. `prices` is a price_snapshot series "
                "(byo_only); the probe's totals inherit that licence. "
                "leak_prevented is True iff the causal total is dramatically "
                "below the perfect-foresight total -- a backtest that can see "
                "the future is worse than none."
            ),
        ),
    ):
        registry.add(cap)

    # --- Cross-asset relationships and signal edge (capabilities/crossasset.py) -
    # Two families lifted from v1: the cross-asset engine (correlations, RORO,
    # sector rotation, cycle phase, intermarket divergences) and the edge-metric
    # statistics (information coefficient, quantile spread, hit rate). Every one
    # runs over price-derived series (returns, sector prices) or over an
    # already-computed structure built from them (a correlation dict, a set of
    # leader labels, a signal/forward-return panel) whose licence the descriptor
    # cannot see, so every one is touches_byo -- the same convention as
    # detect.manipulation and regime.detect_regime_changes. The closed
    # claim_type enum has no home for an IC, a quantile spread, a RORO score, a
    # cycle label or a correlation-regime divergence, so produces is empty for
    # all: these are analysis steps, not claim writers.
    #
    # NB detect_divergences is NOT a perception_divergence producer.
    # perception_divergence (migration 004: "derived; only as fresh as its
    # inputs") already has a dedicated producer -- perception.divergence in
    # capability/derived.py, CONSUMES (perception_macro, fundamental_metric) --
    # which derives a perception-vs-fundamentals split on one entity. That is a
    # contradictory-source finding. detect_divergences instead flags asset-pair
    # correlations breaking hardcoded historical norms (SPY/VIX -0.7, GLD/TLT
    # 0.3, SPY/HYG 0.6): a market-structure regime signal over many assets. The
    # shared word "divergence" must not conflate them; mapping one to the other
    # would be the two-incompatible-registries failure. See _orchestrator/
    # reports/N5.md.
    #
    # Matrix overlap: cross_asset_correlations computes its correlation matrix
    # with np.corrcoef over aligned return series -- the same estimator as
    # fundamentals.correlation_matrix and portfolio_risk.calculate_correlation_
    # matrix. It is not a second matrix-only entry; its value is the divergence
    # read (vs fixed intermarket norms) and the short-vs-long correlation_shifts,
    # neither available elsewhere. Divergence vs fundamentals: a min-observations
    # guard (>=4 symbols and >=20 returns per symbol here, vs no such guard in
    # fundamentals) plus the cross-asset-specific post-processing. See N5.
    for cap in (
        _bind(
            "crossasset.infer_cycle_phase",
            "Economic cycle phase (early / mid / late / recession / unknown) "
            "from the set of leading sector names, via fixed sector buckets.",
            fn=crossasset.infer_cycle_phase,
            touches_byo=True,
            is_proxy=True,
            proxy_of=("economic_cycle",),
            provenance=(
                "Deterministic set-intersection classifier; the four sector "
                "buckets and the priority tie-break are declared constants, "
                "not estimates. consumes is empty because `leaders` is a set "
                "of sector-name labels -- the output of a ranking step over "
                "sector prices -- not a claim the store can supply; a planner "
                "needs an inter-step value-passing layer (e.g. "
                "sector_rotation's ranking) to assemble it. touches_byo "
                "because those labels inherit the price licence of the sector "
                "series they were ranked from. A cycle label is a PROXY for "
                "the economic regime, not a measurement of it."
            ),
        ),
        _bind(
            "crossasset.detect_divergences",
            "Flag asset pairs whose current correlation breaks a hardcoded "
            "historical norm (SPY/VIX -0.7, GLD/TLT 0.3, SPY/HYG 0.6) by more "
            "than 0.3, with a high/moderate significance band.",
            fn=crossasset.detect_divergences,
            touches_byo=True,
            provenance=(
                "consumes is empty: the input is an already-computed "
                "{symbol: {symbol: corr}} dict, not a claim; a planner must "
                "assemble it from a correlation matrix (e.g. "
                "crossasset.cross_asset_correlations or "
                "fundamentals.correlation_matrix output). The norms and the "
                "0.3 / 0.5 thresholds are declared constants. touches_byo "
                "because the corr dict is built from return series (price-"
                "derived, byo_only). NOT a perception_divergence producer -- "
                "see the section note above and N5; that claim type is a "
                "derived perception-vs-fundamentals finding with its own "
                "producer."
            ),
        ),
        _bind(
            "crossasset.cross_asset_correlations",
            "Rolling correlation matrix across asset classes, plus intermarket "
            "divergences (vs fixed norms) and short-vs-long correlation shifts "
            "(breakdown / spike). Needs >=4 symbols and >=20 returns per symbol.",
            fn=crossasset.cross_asset_correlations,
            consumes=("price_snapshot",),
            touches_byo=True,
            provenance=(
                "The matrix is np.corrcoef over aligned return series -- the "
                "same estimator as fundamentals.correlation_matrix and "
                "portfolio_risk.calculate_correlation_matrix; this entry's "
                "distinct value is the divergence read and the "
                "correlation_shifts (see N5 for the overlap). Returns are "
                "price_snapshot-derived (byo_only). <4 symbols or <20 returns "
                "per symbol raises Unavailable rather than the empty matrix v1 "
                "returned. The norms and the 0.25 shift threshold are declared "
                "constants."
            ),
        ),
        _bind(
            "crossasset.roro_indicator",
            "Risk-On/Risk-Off composite in [-1, +1] from VIX direction (30%), "
            "credit-spread proxy HYG-vs-TLT (25%), dollar strength UUP (20%) "
            "and small-cap breadth IWM-vs-SPY (25%), with a 5-band "
            "classification.",
            fn=crossasset.roro_indicator,
            consumes=("price_snapshot",),
            touches_byo=True,
            is_proxy=True,
            proxy_of=("risk_appetite",),
            provenance=(
                "Fixed-weight composite; the 0.30/0.25/0.20/0.25 weights and "
                "the classification bands are declared constants, not "
                "calibrated. Each component contributes only when every symbol "
                "it needs has enough history -- a short series is skipped, not "
                "fabricated as 0 (v1's defect, fixed). The composite is a PROXY "
                "for latent market risk appetite, not a measurement of it."
            ),
        ),
        _bind(
            "crossasset.sector_rotation",
            "Sector rotation: per-sector 5d/20d momentum, top-3 / bottom-3 "
            "ranked sectors and the inferred economic cycle phase. Needs >=20 "
            "positive prices per sector.",
            fn=crossasset.sector_rotation,
            consumes=("price_snapshot",),
            touches_byo=True,
            provenance=(
                "Momentum is 0.6*ret_20d + 0.4*ret_5d (declared weights); "
                "cycle phase delegates to infer_cycle_phase's fixed sector "
                "buckets. Sector prices are price_snapshot-derived (byo_only). "
                "A sector with <20 positive prices is skipped; none qualifying "
                "raises Unavailable rather than v1's 'unknown' phase."
            ),
        ),
        _bind(
            "crossasset.information_coefficient",
            "Cross-sectional information coefficient: per-date Spearman/Pearson "
            "correlation of a signal with forward returns across assets, "
            "summarized into mean IC, its IR, t-stat and significance.",
            fn=crossasset.information_coefficient,
            consumes=("price_snapshot",),
            touches_byo=True,
            is_proxy=True,
            proxy_of=("predictive_power",),
            provenance=(
                "A correlation of the signal against a chosen forward-return "
                "horizon under a stated method (Spearman default) -- the "
                "horizon and method are assumptions, not measurements. Dates "
                "with <5 assets (min_cross_section) are dropped; is_significant "
                "needs >=12 periods. forward_return is price_snapshot-derived "
                "(byo_only); the signal column's provenance is opaque to the "
                "descriptor. An IC is a PROXY for predictive power, not a "
                "measurement of it. (The pure-noise-looks-significant defect "
                "is handled by the n/overlap correction in time_series_ic; "
                "cross-sectional ICs are independent across dates, so it does "
                "not apply here.)"
            ),
        ),
        _bind(
            "crossasset.time_series_ic",
            "Time-series information coefficient for a single signal series "
            "against an aligned forward-return series, with optional rolling "
            "window and an overlap-corrected significance test.",
            fn=crossasset.time_series_ic,
            consumes=("price_snapshot",),
            touches_byo=True,
            is_proxy=True,
            proxy_of=("predictive_power",),
            provenance=(
                "Single-series IC; the forward horizon and method are "
                "assumptions. When overlap > 1 (overlapping forward returns) "
                "the t-stat uses an effective sample size n/overlap so "
                "autocorrelated returns cannot manufacture significance -- the "
                "pure-noise-looks-significant defect. forward_return is "
                "price_snapshot-derived (byo_only). <3 paired observations "
                "yields an empty IC (NaN), never a fabricated number. An IC is "
                "a PROXY for predictive power, not a measurement of it."
            ),
        ),
        _bind(
            "crossasset.quantile_analysis",
            "Quantile (decile) spread of a cross-sectional signal: per-quantile "
            "mean forward returns, top-minus-bottom spread, monotonicity and "
            "the annualized long/short Sharpe.",
            fn=crossasset.quantile_analysis,
            consumes=("price_snapshot",),
            touches_byo=True,
            is_proxy=True,
            proxy_of=("predictive_power",),
            provenance=(
                "Sorts assets into n_quantiles by signal rank each date. "
                "Monotonicity (Spearman of quantile rank vs mean return) is "
                "the signal-quality check a top-minus-bottom gap alone misses. "
                "forward_return is price_snapshot-derived (byo_only). A sub-"
                "computation of evaluate_signal, exposed separately for a "
                "planner that wants only the quantile lens. An edge proxy, not "
                "a measurement of predictive power."
            ),
        ),
        _bind(
            "crossasset.evaluate_signal",
            "Full edge report for one cross-sectional signal: IC, quantile "
            "spread, directional hit rate and a plain-language verdict "
            "(INSUFFICIENT DATA / NO MEASURABLE EDGE / WEAK..STRONG EDGE).",
            fn=crossasset.evaluate_signal,
            consumes=("price_snapshot",),
            touches_byo=True,
            is_proxy=True,
            proxy_of=("predictive_power",),
            provenance=(
                "Composite of information_coefficient, quantile_analysis and "
                "hit_rate; quantiles are skipped automatically when the cross-"
                "section is too thin to bucket. The verdict's WEAK/MODERATE/"
                "STRONG bands (<0.02 / <0.05 / else) are declared constants, "
                "not calibrated. forward_return is price_snapshot-derived "
                "(byo_only). A PROXY for predictive power; the verdict is a "
                "policy-banded read of the IC, not a calibrated probability."
            ),
        ),
    ):
        registry.add(cap)

    return registry
