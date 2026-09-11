"""Pre-registered measurement: 6-week hold vs 12-week hold for the carry book.

HYPOTHESIS (stated before running):
    A 12-week hold amortises the 28 bps round trip over more carry, reducing
    the annualised cost drag from ~2.4%/yr to ~1.2%/yr. The counter-hypothesis
    (Finding 9): turnover is what destroys this strategy, but re-ranking
    frequency is also what captures funding shifts -- so a longer hold may save
    cost while giving up timing. The test settles which effect dominates.

WHAT IT MEASURES:
    For each hold length, at every rebalance point the script:
      1. Ranks the universe by trailing 7-day mean funding rate
      2. Selects the top enter_rank (default 2)
      3. Computes the ACTUAL funding collected over the hold (not the trailing
         rate projected forward -- the realised settlements in the window)
      4. Charges the measured 28 bps round-trip cost, amortised over the hold
      5. Records the period return

    Reports mean %/yr, std, t-stat, and n_periods for each. The recent third is
    reported alongside the full sample, because every strategy retired in this
    project was significant full-sample.

WHAT IT DOES NOT DO:
    It does not decide. A 12-week hold that scores higher might be picking up a
    regime that does not persist; the operator reads the numbers and decides.

DATA REQUIREMENT:
    Needs at least 2x the hold period of funding history (168 days for the
    12-week hold). If the store does not have enough, the script says so and
    exits without producing a number -- a short sample would produce a
    confident t-stat from two overlapping holds, which is noise.

Run:
    python ops/hold_length_probe.py --settlement-hours 1
    python ops/hold_length_probe.py --settlement-hours 1 --enter-rank 2 --cost-bps 28
    python ops/hold_length_probe.py --settlement-hours 8 --venue other-venue
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import UUID

import numpy as np

logger = logging.getLogger("omni.ops.hold_length_probe")

CARRY_UNIVERSE = ["BTC", "ETH", "SOL", "HYPE", "PENGU", "PURR"]

_QUERY = """
SELECT
    c.event_date,
    split_part(split_part(c.key, ':', 2), '/', 1)  AS asset,
    (c.value ->> 'rate')::numeric AS rate
FROM ({visible}) c
WHERE c.claim_type = 'funding_rate'
  AND split_part(c.key, ':', 1) = $2
  AND c.event_date > $3
  AND c.event_date <= $4
ORDER BY c.event_date
"""


async def load_funding_panel(pool, *, venue: str, assets: list[str], start: datetime, end: datetime, audience=None):
    """Read funding history into a pandas DataFrame indexed by timestamp.

    Returns a wide frame: rows = settlement timestamps, columns = asset
    symbols, values = per-settlement funding rate (positive = longs pay
    shorts, so a short perp collects).
    """
    import pandas as pd

    from omni.coverage.visibility import visible_claims_cte

    rows = await pool.fetch(
        _QUERY.format(visible=visible_claims_cte("$1")),
        audience,
        venue,
        start,
        end,
    )

    if not rows:
        return pd.DataFrame(), set()

    records = [
        {"ts": r["event_date"], "asset": r["asset"], "rate": float(r["rate"])}
        for r in rows
        if r["asset"] in assets and r["rate"] is not None
    ]
    if not records:
        return pd.DataFrame(), set()

    df = pd.DataFrame(records)
    df["ts"] = pd.to_datetime(df["ts"], utc=True)
    panel = df.pivot_table(index="ts", columns="asset", values="rate", aggfunc="last")
    panel = panel.sort_index()
    found = set(panel.columns)
    return panel, found


def _sample_statistics(values: np.ndarray) -> tuple[float, float | None, float | None]:
    """Mean, std, t-stat; the t-stat is None when the sample cannot define one.

    A constant or singleton sample has no t-statistic -- reporting a made-up
    zero would present noise as a measurement, the exact failure this probe
    exists to avoid.
    """
    mean = float(values.mean())
    if values.size < 2:
        return mean, None, None
    std = float(values.std(ddof=1))
    scale = float(np.max(np.abs(values)))
    if np.ptp(values) == 0 or std <= 1e-12 * scale:
        return mean, std, None
    return mean, std, mean / (std / np.sqrt(values.size))


def simulate(
    panel,
    *,
    hold_days: int,
    settlement_hours: int,
    lookback_days: int = 7,
    enter_rank: int = 2,
    cost_bps: Decimal = Decimal(28),
) -> dict:
    """Simulate the carry basket at one hold length.

    The settlement cadence is an explicit input, not an assumed 24/day: the
    panel's timestamp grid is validated against it, so a gapped or irregular
    history is refused rather than silently read as row slots. An all-missing
    held window yields no funding (min_count), never a fabricated zero, and
    the final exactly-complete holding window is included.

    Returns a dict with mean_pct_yr, std, t_stat, n_periods, and the
    per-period returns; t-stat keys are None when undefined.
    """
    import pandas as pd

    empty = {"n_periods": 0, "mean_pct_yr": None, "t_stat": None}
    if settlement_hours not in (1, 2, 3, 4, 6, 8, 12, 24):
        raise ValueError("settlement_hours must divide a 24-hour day")
    if hold_days <= 0 or lookback_days <= 0 or enter_rank <= 0:
        raise ValueError("holding, lookback and selection sizes must be positive")
    if not cost_bps.is_finite() or cost_bps < 0:
        raise ValueError("cost must be finite and non-negative")
    if panel.empty:
        return empty
    if not isinstance(panel.index, pd.DatetimeIndex) or panel.index.tz is None:
        raise ValueError("funding timestamps must be timezone-aware")
    if not panel.index.is_unique or not panel.index.is_monotonic_increasing:
        raise ValueError("funding timestamps must be unique and ordered")
    if not panel.columns.is_unique or len(panel.columns) < enter_rank:
        raise ValueError("not enough uniquely identified assets")
    expected = pd.date_range(
        panel.index[0], panel.index[-1],
        freq=pd.Timedelta(hours=settlement_hours),
    )
    if not panel.index.equals(expected):
        raise ValueError("funding grid has missing or irregular settlements")
    if not np.isfinite(panel.to_numpy(dtype=float)).all():
        raise ValueError("funding panel has missing or non-finite measurements")

    per_day = 24 // settlement_hours
    lookback = lookback_days * per_day
    holding = hold_days * per_day
    period_returns = []
    for t in range(lookback, len(panel) - holding + 1, holding):
        trailing = panel.iloc[t - lookback:t].mean()
        selected = trailing.nlargest(enter_rank).index
        measured = panel.iloc[t:t + holding][selected]
        funding = float(measured.sum(min_count=holding).mean())
        annual_return = (
            funding - float(cost_bps) / 10000.0
        ) * (365.0 / hold_days) * 100.0
        period_returns.append(annual_return)
    if not period_returns:
        return empty

    values = np.asarray(period_returns, dtype=float)
    mean, std, t_stat = _sample_statistics(values)
    recent = values[-max(1, len(values) // 3):]
    recent_mean, _, recent_t = _sample_statistics(recent)
    return {
        "n_periods": len(values), "mean_pct_yr": mean,
        "std_pct_yr": std, "t_stat": t_stat,
        "recent_third_mean": recent_mean, "recent_third_t": recent_t,
        "returns": period_returns,
    }


def _format_result(label: str, result: dict) -> str:
    if result["n_periods"] == 0:
        return f"  {label:12s}  n=0 (insufficient data)"

    def fmt(value):
        return "n/a" if value is None else f"{value:+.2f}"

    return (
        f"  {label:12s} n={result['n_periods']} "
        f"mean {fmt(result['mean_pct_yr'])}%/yr t {fmt(result['t_stat'])} | "
        f"recent 1/3 {fmt(result['recent_third_mean'])}%/yr "
        f"t {fmt(result['recent_third_t'])}"
    )


async def main(argv: Sequence[str] | None = None) -> int:
    from omni.config import settings
    from omni.db import connect

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )

    parser = argparse.ArgumentParser(
        description="Pre-registered: 6wk vs 12wk hold for the carry book.",
    )
    parser.add_argument("--venue", default="hyperliquid")
    parser.add_argument("--enter-rank", type=int, default=2)
    parser.add_argument("--cost-bps", type=str, default="28")
    parser.add_argument("--lookback-days", type=int, default=7)
    parser.add_argument(
        "--settlement-hours", type=int, required=True,
        choices=(1, 2, 3, 4, 6, 8, 12, 24),
        help="Measured settlement cadence for every asset in the panel",
    )
    parser.add_argument("--universe", nargs="*", default=CARRY_UNIVERSE)
    parser.add_argument("--audience", default=None, help="UUID of the BYO credential owner for private funding claims")
    args = parser.parse_args(argv)

    cost_bps = Decimal(args.cost_bps)
    end = datetime.now(UTC)
    start = end - timedelta(days=365 * 3)

    logger.info("connecting to the configured database")
    client = await connect(settings.database_url)
    try:
        logger.info("loading funding history for %s on %s", args.universe, args.venue)
        panel, found = await load_funding_panel(
            client.pool,
            venue=args.venue,
            assets=args.universe,
            start=start,
            end=end,
            audience=UUID(args.audience) if args.audience else None,
        )

        missing = set(args.universe) - found
        if missing:
            logger.info("universe assets not in funding data: %s", sorted(missing))
        if panel.empty:
            print("No funding history found. The store has no funding_rate claims")
            print(f"for venue={args.venue} in the requested universe.")
            return 1

        logger.info(
            "panel: %d settlements, %d assets, %s to %s",
            len(panel),
            panel.shape[1],
            panel.index[0],
            panel.index[-1],
        )

        available_days = (panel.index[-1] - panel.index[0]).days
        if available_days < 168:
            print(
                f"Insufficient history: {available_days} days available, "
                f"need >= 168 (24 weeks) for a 12-week hold with a trailing "
                f"lookback. Re-run when more data accumulates."
            )
            return 1

        print("=" * 72)
        print("HOLD-LENGTH COMPARISON (pre-registered)")
        print(f"  universe: {sorted(found)}")
        print(f"  venue: {args.venue}  enter_rank: {args.enter_rank}  cost: {cost_bps} bps/pair")
        print(f"  panel: {len(panel)} settlements  ({available_days} days)")
        print(f"  window: {panel.index[0].date()} to {panel.index[-1].date()}")
        print()

        r6 = simulate(
            panel,
            hold_days=42,
            settlement_hours=args.settlement_hours,
            lookback_days=args.lookback_days,
            enter_rank=args.enter_rank,
            cost_bps=cost_bps,
        )
        r12 = simulate(
            panel,
            hold_days=84,
            settlement_hours=args.settlement_hours,
            lookback_days=args.lookback_days,
            enter_rank=args.enter_rank,
            cost_bps=cost_bps,
        )

        print(_format_result("6-week", r6))
        print(_format_result("12-week", r12))
        print()

        if r6["n_periods"] > 0 and r12["n_periods"] > 0:
            diff = r6["mean_pct_yr"] - r12["mean_pct_yr"]
            winner = "6-week" if diff > 0 else "12-week"
            print(f"  difference: {abs(diff):+.2f}%/yr in favour of {winner}")
            cost6 = float(cost_bps) / 100 * (365.0 / 42)
            cost12 = float(cost_bps) / 100 * (365.0 / 84)
            print(f"  cost drag:  6wk {cost6:.2f}%/yr  vs  12wk {cost12:.2f}%/yr")
        print("=" * 72)
        return 0
    finally:
        await client.close()


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main(sys.argv[1:])))
