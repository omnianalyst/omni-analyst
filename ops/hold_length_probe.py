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
    python ops/hold_length_probe.py
    python ops/hold_length_probe.py --enter-rank 2 --cost-bps 28
    python ops/hold_length_probe.py --venue hyperliquid
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import numpy as np

logger = logging.getLogger("omni.ops.hold_length_probe")

CARRY_UNIVERSE = ["BTC", "ETH", "SOL", "HYPE", "PENGU", "PURR"]
SETTLEMENTS_PER_YEAR = Decimal(24 * 365)

_QUERY = """
SELECT
    c.event_date,
    split_part(c.key, ':', 2)  AS asset,
    (c.value ->> 'rate')::numeric AS rate
FROM ({visible}) c
WHERE c.claim_type = 'funding_rate'
  AND split_part(c.key, ':', 1) = $2
  AND c.event_date > $3
  AND c.event_date <= $4
ORDER BY c.event_date
"""


async def load_funding_panel(pool, *, venue: str, assets: list[str], start: datetime, end: datetime):
    """Read funding history into a pandas DataFrame indexed by timestamp.

    Returns a wide frame: rows = settlement timestamps, columns = asset
    symbols, values = per-settlement funding rate (positive = longs pay
    shorts, so a short perp collects).
    """
    import pandas as pd

    from omni.coverage.visibility import visible_claims_cte

    rows = await pool.fetch(
        _QUERY.format(visible=visible_claims_cte("$1")),
        None,
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


def simulate(
    panel,
    *,
    hold_days: int,
    lookback_days: int = 7,
    enter_rank: int = 2,
    cost_bps: Decimal = Decimal(28),
) -> dict:
    """Simulate the carry basket at one hold length.

    Returns a dict with mean_pct_yr, std, t_stat, n_periods, and the per-period
    returns. The simulation is a vectorised walk over the panel: at each
    rebalance point (spaced hold_days apart), it ranks by trailing mean funding,
    selects the top names, and sums the realised funding over the hold window.
    """
    if panel.empty or len(panel) < 2:
        return {"n_periods": 0, "mean_pct_yr": None, "t_stat": None}

    lookback_settlements = int(lookback_days * 24)
    hold_settlements = int(hold_days * 24)

    if len(panel) < lookback_settlements + hold_settlements:
        return {"n_periods": 0, "mean_pct_yr": None, "t_stat": None}

    rebalance_points = range(lookback_settlements, len(panel) - hold_settlements, hold_settlements)
    rebalance_points = list(rebalance_points)

    if not rebalance_points:
        return {"n_periods": 0, "mean_pct_yr": None, "t_stat": None}

    period_returns: list[float] = []
    for t in rebalance_points:
        trailing = panel.iloc[t - lookback_settlements : t]
        mean_trailing = trailing.mean()
        eligible = mean_trailing.dropna()
        if len(eligible) < enter_rank:
            continue

        top = eligible.nlargest(enter_rank).index

        hold_window = panel.iloc[t : t + hold_settlements]
        held = hold_window[top].sum()
        per_asset_total = held.mean()

        # per_asset_total is the SUM of per-settlement rates over the hold.
        # Annualising: mean_rate_per_settlement * settlements_per_year.
        # mean_rate = sum / hold_settlements, so the annualisation factor is
        # settlements_per_year / hold_settlements.
        annualisation = SETTLEMENTS_PER_YEAR / Decimal(hold_settlements)
        funding_annualised = float(
            Decimal(str(per_asset_total)) * annualisation * Decimal(100)
        )
        cost_annualised = float(cost_bps) / 100 * (365.0 / hold_days)

        period_returns.append(funding_annualised - cost_annualised)

    if not period_returns:
        return {"n_periods": 0, "mean_pct_yr": None, "t_stat": None}

    arr = np.array(period_returns, dtype=float)
    mean = float(arr.mean())
    std = float(arr.std(ddof=1)) if len(arr) > 1 else 0.0
    se = std / np.sqrt(len(arr)) if len(arr) > 0 else 0.0
    t_stat = mean / se if se > 0 else 0.0

    third = max(1, len(arr) // 3)
    recent = arr[-third:]
    recent_mean = float(recent.mean())
    recent_std = float(recent.std(ddof=1)) if len(recent) > 1 else 0.0
    recent_se = recent_std / np.sqrt(len(recent)) if len(recent) > 0 else 0.0
    recent_t = recent_mean / recent_se if recent_se > 0 else 0.0

    return {
        "n_periods": len(arr),
        "mean_pct_yr": mean,
        "std_pct_yr": std,
        "t_stat": t_stat,
        "recent_third_mean": recent_mean,
        "recent_third_t": recent_t,
        "returns": period_returns,
    }


def _format_result(label: str, r: dict) -> str:
    if r["n_periods"] == 0:
        return f"  {label:12s}  n=0 (insufficient data)"

    mean = r["mean_pct_yr"]
    t = r["t_stat"]
    n = r["n_periods"]
    recent_mean = r.get("recent_third_mean", 0.0)
    recent_t = r.get("recent_third_t", 0.0)

    return (
        f"  {label:12s}  n={n:>4d}  "
        f"mean {mean:>+7.2f}%/yr  t {t:>+6.2f}  | "
        f"recent 1/3 {recent_mean:>+7.2f}%/yr  t {recent_t:>+6.2f}"
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
    parser.add_argument("--universe", nargs="*", default=CARRY_UNIVERSE)
    args = parser.parse_args(argv)

    cost_bps = Decimal(args.cost_bps)
    end = datetime.now(UTC)
    start = end - timedelta(days=365 * 3)

    logger.info("connecting to %s", settings.database_url[:50] + "...")
    client = await connect(settings.database_url)
    try:
        logger.info("loading funding history for %s on %s", args.universe, args.venue)
        panel, found = await load_funding_panel(
            client.pool,
            venue=args.venue,
            assets=args.universe,
            start=start,
            end=end,
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
            lookback_days=args.lookback_days,
            enter_rank=args.enter_rank,
            cost_bps=cost_bps,
        )
        r12 = simulate(
            panel,
            hold_days=84,
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
