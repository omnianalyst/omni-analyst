"""Macroeconomic analysis as pure capabilities.

Ported from v1 `app/services/macroeconomic/fed_data_service.py` and
`app/services/macroeconomic/economic_modeling.py`. Only the computation was
lifted; every fetch, cache, DB session, FRED HTTP call, aiohttp session and
router is gone. The data each function needs arrives as a plain argument -- a
list of values for a series, or the already-computed sub-indicators a composite
needs. FRED ingestion itself already lives at `omni.ingest.fred`.

Where v1 substituted a default on missing input -- a 0 change_percent when the
previous value was zero, an empty forecast dict on fewer than three points, a
"stable" trend on a zero mean, a 2.0 nowcast when the model was empty -- this
module raises `Unavailable` instead. The census found 44 fabrications in the
predecessor by that exact failure mode; a capability that always returns a
number is how hallucinated coverage enters the store.

Entry points (the composite analyses the orchestrator calls) are async. The
leaf mathematical helpers are sync because they do no IO.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

import numpy as np

from omni.ingest.protocol import Unavailable


def calculate_trend(values: Sequence[float]) -> str:
    if len(values) < 2:
        return "insufficient_data"

    x = np.arange(len(values))
    slope = np.polyfit(x, values, 1)[0]

    mean_val = np.mean(values)
    # `np.isclose`, not `== 0`: a near-zero mean from a series centred on zero
    # with float noise (mean ~1e-17, not 0.0) would otherwise divide a near-zero
    # slope by it and classify the noise as rising/falling sharply. A flat or
    # zero-centred series is "stable" -- the honest classification, not a guess.
    if np.isclose(mean_val, 0.0):
        return "stable"

    normalized_slope = slope / mean_val

    if normalized_slope > 0.02:
        return "rising_sharply"
    elif normalized_slope > 0.005:
        return "rising"
    elif normalized_slope < -0.02:
        return "falling_sharply"
    elif normalized_slope < -0.005:
        return "falling"
    else:
        return "stable"


def generate_simple_forecast(values: Sequence[float], periods: int = 3) -> dict[str, float]:
    if len(values) < 3:
        raise Unavailable("fewer than 3 values; cannot forecast")

    alpha = 0.3
    s = values[0]
    for val in values[1:]:
        s = alpha * val + (1 - alpha) * s

    return {f"period_{i}": s for i in range(1, periods + 1)}


def calculate_percentile(value: float, all_values: Sequence[float]) -> float:
    if not all_values:
        raise Unavailable("no values to compute a percentile against")

    return (np.sum(np.array(all_values) <= value) / len(all_values)) * 100


def period_change_percent(current: float, previous: float) -> float:
    if previous == 0:
        raise Unavailable("previous value is zero; percentage change undefined")
    return ((current - previous) / previous) * 100


def historical_context(current: float, all_values: Sequence[float]) -> dict[str, Any]:
    if not all_values:
        raise Unavailable("no values to build historical context from")

    return {
        "mean": np.mean(all_values),
        "std": np.std(all_values),
        "min": np.min(all_values),
        "max": np.max(all_values),
        "percentile": calculate_percentile(current, all_values),
    }


def calculate_inflation_trend(cpi_values: Sequence[float]) -> dict[str, Any]:
    if len(cpi_values) < 6:
        raise Unavailable("fewer than 6 CPI observations; cannot derive inflation trend")

    mom_changes = []
    for i in range(1, min(7, len(cpi_values))):
        change = (
            (cpi_values[-i] - cpi_values[-i - 1]) / cpi_values[-i - 1]
        ) * 100
        mom_changes.append(change)

    recent_annualized = np.mean(mom_changes[:3]) * 12

    return {
        "recent_mom_changes": mom_changes[:3],
        "3m_annualized": recent_annualized,
        "momentum": (
            "accelerating" if mom_changes[0] > mom_changes[2] else "decelerating"
        ),
        "volatility": np.std(mom_changes),
    }


def assess_recession_risk(probability: float) -> str:
    if probability >= 0.7:
        return "high"
    elif probability >= 0.4:
        return "elevated"
    elif probability >= 0.2:
        return "moderate"
    else:
        return "low"


def categorize_series(title: str) -> str:
    title_lower = title.lower()

    if any(term in title_lower for term in ["federal funds", "fomc", "balance sheet"]):
        return "monetary_policy"
    elif any(
        term in title_lower
        for term in ["unemployment", "employment", "payroll", "claims"]
    ):
        return "employment"
    elif any(term in title_lower for term in ["cpi", "pce", "inflation", "price"]):
        return "inflation"
    elif any(term in title_lower for term in ["gdp", "gross domestic"]):
        return "gdp"
    elif any(
        term in title_lower for term in ["treasury", "yield", "interest rate"]
    ):
        return "interest_rates"
    elif any(term in title_lower for term in ["bank", "credit", "lending"]):
        return "banking"
    elif any(term in title_lower for term in ["trade", "exchange", "international"]):
        return "international"
    elif any(term in title_lower for term in ["housing", "home", "mortgage"]):
        return "housing"
    elif any(term in title_lower for term in ["consumer", "retail", "spending"]):
        return "consumer"
    elif any(
        term in title_lower for term in ["business", "corporate", "industrial"]
    ):
        return "business"
    else:
        return "monetary_policy"


def taylor_rule(
    inflation: float,
    output_gap: float,
    *,
    inflation_target: float = 2.0,
    neutral_real_rate: float = 0.5,
    inflation_weight: float = 1.5,
    output_gap_weight: float = 0.5,
) -> float:
    return (
        neutral_real_rate
        + inflation
        + inflation_weight * (inflation - inflation_target)
        + output_gap_weight * output_gap
    )


def taylor_rule_variant(
    inflation: float,
    output_gap: float,
    target: float,
    r_star: float,
    alpha: float,
    beta: float,
) -> float:
    return r_star + inflation + alpha * (inflation - target) + beta * output_gap


async def output_gap(
    gdp_values: Sequence[float], potential_values: Sequence[float]
) -> dict[str, Any]:
    """The CBO output gap: percent deviation of real GDP from potential.

    ``(gdp - potential) / potential * 100`` on the LEVEL series -- the textbook
    definition (and what ``taylor_rule``'s percent ``output_gap`` term expects),
    not v1's growth-rate-diff approximation
    (``economic_modeling.py:360`` ``output_gap = gdp_growth - potential_growth``,
    which it itself flagged "simplified"). GDPC1 and GDPPOT are both real, chained
    dollars, so the level ratio is directly meaningful and authoritative.

    Reads the latest of each series (the gap is a current STATE, one value per
    quarter). Raises on empty input or non-positive potential (a negative
    denominator would flip the gap's sign silently). Does not fabricate a gap
    when either series is absent.
    """
    if not gdp_values or not potential_values:
        raise Unavailable("missing GDP / potential GDP observations")
    gdp = float(gdp_values[-1])
    potential = float(potential_values[-1])
    if potential <= 0:
        raise Unavailable(
            f"non-positive potential GDP ({potential}); cannot compute output gap"
        )
    gap = (gdp - potential) / potential * 100.0
    return {"output_gap": gap, "gdp": gdp, "potential": potential}


def policy_stance(deviation: float) -> str:
    if deviation > 1.0:
        return "restrictive"
    elif deviation < -1.0:
        return "accommodative"
    else:
        return "neutral"


def policy_recommendation(taylor_rate: float, current_rate: float) -> str:
    diff = taylor_rate - current_rate

    if abs(diff) < 0.25:
        return "Maintain current policy stance"
    elif diff > 1.0:
        return "Consider accelerated tightening"
    elif diff > 0:
        return "Gradual tightening warranted"
    elif diff < -1.0:
        return "Consider accelerated easing"
    else:
        return "Gradual easing warranted"


def assess_policy_implications(
    taylor_rate: float, current_rate: float, stance: str
) -> dict[str, Any]:
    implications: dict[str, Any] = {
        "rate_adjustment_needed": taylor_rate - current_rate,
        "stance": stance,
        "risks": [],
    }

    if stance == "accommodative" and taylor_rate > current_rate + 1:
        implications["risks"].append("Inflation risk from overly loose policy")
    elif stance == "restrictive" and taylor_rate < current_rate - 1:
        implications["risks"].append("Growth risk from overly tight policy")

    implications["recommendation"] = policy_recommendation(taylor_rate, current_rate)
    return implications


async def sahm_rule(unemployment_values: Sequence[float]) -> dict[str, Any]:
    if len(unemployment_values) < 12:
        raise Unavailable(
            f"need >=12 unemployment observations for the Sahm rule, got {len(unemployment_values)}"
        )

    current_avg = np.mean(list(unemployment_values)[-3:])
    min_12m = np.min(list(unemployment_values)[-12:])
    sahm_indicator = current_avg - min_12m

    return {
        "value": sahm_indicator,
        "triggered": sahm_indicator >= 0.5,
        "current_unemployment": unemployment_values[-1],
        "12m_low": min_12m,
    }


async def yield_curve_inversion(
    series_2y: dict[Any, float],
    series_10y: dict[Any, float],
) -> dict[str, Any]:
    common_dates = sorted(set(series_2y.keys()) & set(series_10y.keys()))
    if not common_dates:
        raise Unavailable("no overlapping dates between 2Y and 10Y series")

    spreads = []
    for dt in common_dates[-252:]:
        spread = series_10y[dt] - series_2y[dt]
        spreads.append({"date": dt, "spread": spread, "inverted": spread < 0})

    current_spread = spreads[-1]["spread"]
    days_inverted = sum(1 for s in spreads[-90:] if s["inverted"])

    return {
        "current_spread": current_spread,
        "is_inverted": current_spread < 0,
        "days_inverted_90d": days_inverted,
        "historical_spreads": spreads[-30:],
    }


async def recession_probability(
    *,
    yield_curve_inverted: bool,
    sahm_triggered: bool,
    lei_signals: Sequence[str],
) -> dict[str, Any]:
    probability = 0.0

    if yield_curve_inverted:
        probability += 0.3

    if sahm_triggered:
        probability += 0.4

    if lei_signals:
        negative_lei = sum(1 for s in lei_signals if s == "negative")
        probability += (negative_lei / len(lei_signals)) * 0.3

    probability = min(probability, 1.0)

    return {
        "probability": probability,
        "assessment": assess_recession_risk(probability),
    }


async def inflation_measures(cpi_values: Sequence[float]) -> dict[str, Any]:
    if len(cpi_values) < 13:
        raise Unavailable(
            f"need >=13 CPI observations for YoY inflation, got {len(cpi_values)}"
        )

    current = cpi_values[-1]
    year_ago = cpi_values[-13]
    yoy_inflation = ((current - year_ago) / year_ago) * 100

    previous = cpi_values[-2]
    mom_inflation = ((current - previous) / previous) * 100 * 12

    three_month_ago = cpi_values[-4]
    three_month_annualized = ((current - three_month_ago) / three_month_ago) * 100 * 4

    return {
        "yoy": yoy_inflation,
        "mom_annualized": mom_inflation,
        "3m_annualized": three_month_annualized,
        "current_index": current,
        "trend": calculate_inflation_trend(cpi_values),
    }


async def pce_inflation(pce_values: Sequence[float], target: float = 2.0) -> dict[str, Any]:
    if len(pce_values) < 13:
        raise Unavailable(
            f"need >=13 PCE observations for YoY, got {len(pce_values)}"
        )

    current = pce_values[-1]
    year_ago = pce_values[-13]
    pce_yoy = ((current - year_ago) / year_ago) * 100

    return {
        "yoy": pce_yoy,
        "vs_target": pce_yoy - target,
        "distance_from_target": abs(pce_yoy - target),
    }


async def inflation_expectations(
    exp_5y: Sequence[float], exp_10y: Sequence[float]
) -> dict[str, Any]:
    if not exp_5y or not exp_10y:
        raise Unavailable("missing inflation expectations observations")

    latest_5y = exp_5y[-1]
    latest_10y = exp_10y[-1]

    return {
        "5y": latest_5y,
        "10y": latest_10y,
        "5y5y_forward": 2 * latest_10y - latest_5y,
        "anchored": abs(latest_10y - 2.0) < 0.5,
    }


async def labor_market_tightness(
    unemployment_rate: float, job_growth_3m_avg: float
) -> dict[str, Any]:
    if unemployment_rate <= 0:
        raise Unavailable("non-positive unemployment rate; cannot compute tightness")

    tightness_score = (1 / unemployment_rate) * (job_growth_3m_avg / 100000)

    if tightness_score > 0.5:
        assessment = "tight"
        wage_pressure = "high"
    elif tightness_score > 0.2:
        assessment = "balanced"
        wage_pressure = "moderate"
    else:
        assessment = "loose"
        wage_pressure = "low"

    return {
        "score": tightness_score,
        "assessment": assessment,
        "wage_pressure": wage_pressure,
    }


def assess_scenario_impact(
    baseline: dict[str, dict[str, float]],
    scenario: dict[str, dict[str, float]],
    variables: Sequence[str],
) -> dict[str, dict[str, float]]:
    impacts: dict[str, dict[str, float]] = {}

    for var in variables:
        if var not in baseline or var not in scenario:
            continue
        base_series = list(baseline[var].values())
        scen_series = list(scenario[var].values())
        if not base_series or not scen_series:
            raise Unavailable(f"empty forecast series for {var}")

        baseline_avg = np.mean(base_series)
        scenario_avg = np.mean(scen_series)
        paired = list(zip(scen_series, base_series))
        peak = max(s - b for s, b in paired)

        impacts[var] = {
            "average_impact": scenario_avg - baseline_avg,
            "peak_impact": peak,
        }

    return impacts
