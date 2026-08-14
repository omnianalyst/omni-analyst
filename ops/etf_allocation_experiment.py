"""Replay the ETF allocation rules on stored history.

Production invocation:

    docker compose -f docker-compose.prod.yml exec -T scheduler \
      python /app/ops/etf_allocation_experiment.py

This is the backward half. `ops/shadow_book_record.py` is the forward half, and
it is the one whose output will eventually be evidence. Run this to decide
whether a rule is worth the shadow book's time; do not run it to decide where
capital goes. Two years of daily history covers one regime and holds nothing
back, which is not a gate however the numbers land.
"""

from __future__ import annotations

import argparse
import asyncio
import json

from omni.config import settings
from omni.db import connect
from omni.research.allocation import equal_weight, risk_balanced, top_measured
from omni.research.etf_allocation import run_allocation_experiment, summary
from ops.shadow_book_record import BENCHMARK, SECTORS, load_panel

RULES = {
    "equal_weight": equal_weight,
    "top_measured": top_measured,
    "risk_balanced": risk_balanced,
}


def _table(experiment) -> str:
    header = (
        f"{'book':<16}{'cadence':<11}{'CAGR':>9}{'vol':>8}{'Sharpe':>8}"
        f"{'maxDD':>9}{'turn':>7}{'cost':>7}{'rebal':>7}{'exCAGR':>9}{'exSharpe':>10}"
    )
    lines = [header, "-" * len(header)]
    base = experiment.baseline
    lines.append(
        f"{base.book:<16}{base.cadence:<11}{base.cagr_pct:>8.2f}%"
        f"{base.volatility_pct:>7.2f}%{base.sharpe:>8.2f}{base.max_drawdown_pct:>8.2f}%"
        f"{base.turnover:>7.2f}{base.modelled_cost_pct:>6.2f}%{base.rebalances:>7}"
        f"{'—':>9}{'—':>10}"
    )
    for r in experiment.results:
        lines.append(
            f"{r.book:<16}{r.cadence:<11}{r.cagr_pct:>8.2f}%"
            f"{r.volatility_pct:>7.2f}%{r.sharpe:>8.2f}{r.max_drawdown_pct:>8.2f}%"
            f"{r.turnover:>7.2f}{r.modelled_cost_pct:>6.2f}%{r.rebalances:>7}"
            f"{r.excess_cagr_pct:>+8.2f}%{r.excess_sharpe:>+10.2f}"
        )
    return "\n".join(lines)


async def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="etf_allocation_experiment")
    parser.add_argument("--cost-bps", type=float, default=2.0)
    parser.add_argument("--json", action="store_true", help="emit the full summary")
    args = parser.parse_args(argv)

    client = await connect(settings.database_url)
    try:
        panel, audience = await load_panel(client.pool, [*SECTORS, BENCHMARK])
        experiment = run_allocation_experiment(
            panel, RULES,
            universe=SECTORS, benchmark=BENCHMARK, cost_bps=args.cost_bps,
        )
        if args.json:
            print(json.dumps(summary(experiment), indent=2, default=str))
            return 0

        print(f"audience   {audience}")
        print(f"window     {experiment.first_session} -> {experiment.last_session}")
        print(f"universe   {len(experiment.universe)} sector ETFs vs {experiment.benchmark}")
        print(f"cost       {experiment.cost_bps} bps per unit of turnover")
        print()
        print(_table(experiment))
        print()
        for warning in experiment.warnings:
            print(f"  - {warning}")

        beat = [r for r in experiment.results if r.excess_cagr_pct > 0]
        print()
        print(
            f"{len(beat)} of {len(experiment.results)} rule/cadence combinations "
            f"beat {experiment.benchmark} buy-and-hold on CAGR after costs"
        )
        return 0
    finally:
        await client.close()


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
