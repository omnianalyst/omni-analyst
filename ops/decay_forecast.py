"""Forecast whether/when the carry premium crosses below the idle-stablecoin floor.

Finding 32 measured the top-quintile funding premium decaying linearly with
elapsed time (slope -0.80 pp/month, t -4.24, volatility's coefficient -> 0) on
78 monthly observations. This asks whether THAT trend is stable enough on THIS
book's actual basket -- top-2 of the executable four on Hyperliquid -- to
forecast the floor crossover, with the project's hard discipline: fit on the
early two-thirds, validate on the recent third. Every strategy retired here was
significant full-sample; only the recent third settles it.

Not a registry cell. This is robustness/monitoring of the one thing that works,
not a directional hypothesis. `Registry.record` is never called, so it costs no
multiplicity and raises the bar for nothing.

The gross premium is forecast; the floor is stated in net terms (4.5% for idle
stablecoin). At a ~23 bps per-pair round trip amortised over the 42-day hold,
the cost drag is ~2.0%/yr, so the 4.5% net floor corresponds to ~6.5% gross.

Run: uv run python ops/decay_forecast.py
"""

from __future__ import annotations

import asyncio
import sys
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import UUID

import asyncpg
import numpy as np
from scipy import stats

from omni.trading.carry_health import assess

ASSETS = {
    UUID("3ebe40a1-c43b-4095-8ca2-998537f8cee0"): "BTC",
    UUID("259d7ee3-c806-4a8e-8c6f-81e01052795e"): "ETH",
    UUID("97c32f03-5f65-4871-9724-fbfd6b9ed8d8"): "SOL",
    UUID("fc0c8885-59cc-4108-af79-273dff6e62b4"): "HYPE",
}
AUDIENCE = UUID("75b9a817-ab7b-4896-8c13-b29f27f2eff3")
FUNDING_VENUE = "hyperliquid"

# The cost drag the carry book actually pays, in bps per pair round trip.
# Perp taker 4.5 + spot taker 7.0 per leg, two legs each way = 23 bps. Sourced
# from the live quote scan (ops/quote_scan.py), not the ccxt default tier.
ROUND_TRIP_BPS = Decimal(23)
HOLD_DAYS = 42
GROSS_FLOOR = Decimal("4.5") + ROUND_TRIP_BPS / Decimal(100) * (Decimal(365) / Decimal(HOLD_DAYS))

DSN = "postgresql://postgres:postgres@localhost:5434/omni_v2"


@dataclass(frozen=True)
class Fit:
    slope_pp_per_month: float
    intercept_pct: float
    r_squared: float
    slope_se: float
    n: int


@dataclass(frozen=True)
class Validation:
    train: Fit
    holdout_n: int
    holdout_mean_actual: float
    holdout_mean_predicted: float
    holdout_rmse_pp: float
    holdout_bias_pp: float
    recent_third_within_band: bool


def fit_linear(months: np.ndarray, values: np.ndarray) -> Fit:
    """OLS of values on months. months is float months since the series start."""
    result = stats.linregress(months, values)
    return Fit(
        slope_pp_per_month=float(result.slope),
        intercept_pct=float(result.intercept),
        r_squared=float(result.rvalue ** 2),
        slope_se=float(result.stderr),
        n=len(values),
    )


def fit_and_validate(months: np.ndarray, values: np.ndarray, holdout_frac: float = 1 / 3) -> Validation:
    """Fit on the earliest (1 - holdout_frac), predict the recent holdout_frac.

    The split is by TIME, not by row -- the recent third is the most recent
    `holdout_frac` of the date range, which is the only definition consistent
    with "the recent third settles it."
    """
    split_idx = int(len(months) * (1 - holdout_frac))
    train_m, train_v = months[:split_idx], values[:split_idx]
    hold_m, hold_v = months[split_idx:], values[split_idx:]

    train = fit_linear(train_m, train_v)
    predicted = train.intercept_pct + train.slope_pp_per_month * hold_m
    residuals = hold_v - predicted
    rmse = float(np.sqrt(np.mean(residuals ** 2)))
    bias = float(np.mean(residuals))

    # A prediction band from the training fit's residual std. The recent third
    # "validates" if its mean lands inside it; a mean outside is a regime break.
    train_pred = train.intercept_pct + train.slope_pp_per_month * train_m
    train_resid_std = float(np.std(train_v - train_pred, ddof=2))
    band = 1.96 * train_resid_std / np.sqrt(len(hold_v))
    within = bool(abs(bias) <= band)

    return Validation(
        train=train,
        holdout_n=len(hold_v),
        holdout_mean_actual=float(np.mean(hold_v)),
        holdout_mean_predicted=float(np.mean(predicted)),
        holdout_rmse_pp=rmse,
        holdout_bias_pp=bias,
        recent_third_within_band=within,
    )


def crossover_month(fit: Fit, floor_pct: float) -> float | None:
    """The month index at which the fitted line crosses `floor_pct`, or None."""
    if fit.slope_pp_per_month >= 0:
        return None
    return (floor_pct - fit.intercept_pct) / fit.slope_pp_per_month


async def reconstruct_series(
    pool: asyncpg.Pool | asyncpg.Connection,
    *,
    start: datetime,
    end: datetime,
    step_days: int = 7,
    lookback_days: int = 7,
) -> list[tuple[datetime, float]]:
    """Weekly gross basket premium (top-2, trailing lookback), PIT per snapshot."""
    out: list[tuple[datetime, float]] = []
    t = start
    while t <= end:
        h = await assess(
            pool,
            assets=ASSETS,
            audience_user_id=AUDIENCE,
            funding_venue=FUNDING_VENUE,
            as_of=t,
            enter_rank=2,
            lookback_days=lookback_days,
        )
        if h.basket_gross_pct is not None:
            out.append((t, float(h.basket_gross_pct)))
        t = t + timedelta(days=step_days)
    return out


def _print_series(series: list[tuple[datetime, float]]) -> None:
    print("series (weekly, gross %/yr):")
    for t, v in series:
        print(f"  {t.date().isoformat()}  {v:7.2f}")


def _print_validation(v: Validation, floor_gross: float) -> None:
    t = v.train
    print(f"\ntrain fit (oldest {1 - 1/3:.0%}):")
    print(f"  premium = {t.intercept_pct:.2f} + ({t.slope_pp_per_month:.3f}) * months")
    print(f"  r^2 = {t.r_squared:.3f}   slope se = {t.slope_se:.3f}   n = {t.n}")
    print(f"\nholdout (recent third, n = {v.holdout_n}):")
    print(f"  mean actual    {v.holdout_mean_actual:7.2f}%")
    print(f"  mean predicted {v.holdout_mean_predicted:7.2f}%")
    print(f"  bias           {v.holdout_bias_pp:+7.2f} pp   (positive = premium beat the trend)")
    print(f"  rmse           {v.holdout_rmse_pp:7.2f} pp")
    verdict = "STABLE" if v.recent_third_within_band else "REGIME BREAK"
    print(f"  verdict:       {verdict}")


async def main() -> int:
    pool = await asyncpg.connect(DSN)
    try:
        start = datetime(2024, 12, 15, tzinfo=UTC)
        end = datetime(2026, 8, 9, tzinfo=UTC)
        series = await reconstruct_series(pool, start=start, end=end)
    finally:
        await pool.close()

    if len(series) < 9:
        print(f"only {len(series)} weekly points; need at least 9 for a 2/3-1/3 split", file=sys.stderr)
        return 1

    _print_series(series)
    print(f"\nfloor: {float(GROSS_FLOOR):.2f}% gross (= 4.5% net + cost drag)")

    t0 = series[0][0]
    months = np.array([(t - t0).days / 30.4375 for t, _ in series])
    values = np.array([v for _, v in series])

    v = fit_and_validate(months, values)
    _print_validation(v, float(GROSS_FLOOR))

    full = fit_linear(months, values)
    print("\nfull-series fit (reference, not the basis for the verdict):")
    print(f"  premium = {full.intercept_pct:.2f} + ({full.slope_pp_per_month:.3f}) * months   r^2 = {full.r_squared:.3f}")

    cross = crossover_month(full, float(GROSS_FLOOR))
    if cross is not None and cross >= 0:
        cross_date = t0 + timedelta(days=cross * 30.4375)
        print(f"  full-series crossover at {cross_date.date().isoformat()} (gross {float(GROSS_FLOOR):.2f}%)")
    else:
        print("  no downward crossover in-sample (slope not negative, or floor already below)")

    print("\nverdict rests on the holdout, not the full-series fit.")
    if not v.recent_third_within_band:
        print(
            "the recent third broke the trend fitted on the early window. A linear "
            "decay forecast does NOT generalise -- an automated exit built on it "
            "would have acted on the trend right before the premium moved against it."
        )
    else:
        print("the recent third is consistent with the early-window trend. The forecast generalises.")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
