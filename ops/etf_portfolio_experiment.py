"""Run the exploratory ETF-versus-constituents portfolio comparison.

Production invocation:

    docker compose -f docker-compose.prod.yml exec -T scheduler \
      python /app/ops/etf_portfolio_experiment.py

The deployment currently stores today's S&P 500 membership and sector links,
not historical membership snapshots.  Results are therefore labelled
``current_membership_preview`` and are not a capital-allocation gate.
"""

from __future__ import annotations

import argparse
import asyncio
import json
from dataclasses import asdict

import pandas as pd

from omni.config import settings
from omni.research.etf_replication import run_experiment


async def _load_panel(conn) -> tuple[pd.DataFrame, dict[str, list[str]]]:
    memberships: dict[str, list[str]] = {"SPY": []}
    rows = await conn.fetch(
        """
        SELECT company.symbol AS company, sector.symbol AS etf
        FROM entity_edge edge
        JOIN entity company ON company.id = edge.from_entity AND company.kind = 'company'
        JOIN entity sector ON sector.id = edge.to_entity AND sector.kind = 'sector_etf'
        WHERE edge.relation = 'member_of_sector'
        ORDER BY sector.symbol, company.symbol
        """
    )
    for row in rows:
        memberships.setdefault(row["etf"], []).append(row["company"])
        memberships["SPY"].append(row["company"])

    symbols = sorted({symbol for members in memberships.values() for symbol in members} | set(memberships))
    price_rows = await conn.fetch(
        """
        SELECT e.symbol, c.event_date, (c.value->>'close')::float8 AS close
        FROM claim c
        JOIN entity e ON e.id = c.entity_id
        WHERE c.claim_type = 'price_snapshot'
          AND e.symbol = ANY($1::text[])
          AND c.value->>'close' IS NOT NULL
          AND c.audience_user_id = (
              SELECT audience_user_id FROM claim p
              JOIN entity pe ON pe.id = p.entity_id
              WHERE p.claim_type = 'price_snapshot' AND pe.kind = 'company'
              GROUP BY audience_user_id ORDER BY count(*) DESC LIMIT 1
          )
        ORDER BY e.symbol, c.event_date, c.knowledge_date
        """,
        symbols,
    )
    frame = pd.DataFrame(price_rows, columns=["symbol", "date", "close"])
    if frame.empty:
        raise RuntimeError("no audience-owned equity prices are available")
    frame["date"] = pd.to_datetime(frame["date"], utc=True).dt.tz_convert(None).dt.normalize()
    frame = frame.drop_duplicates(["symbol", "date"], keep="last")
    panel = frame.pivot(index="date", columns="symbol", values="close").sort_index()
    return panel, memberships


def _summary(experiment) -> dict:
    return {
        "etf": experiment.etf_symbol,
        "membership_mode": experiment.membership_mode,
        "decision_rule": experiment.decision_rule,
        "warnings": list(experiment.warnings),
        "strategies": {
            name: asdict(path.metrics)
            for name, path in experiment.strategies.items()
        },
    }


async def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--top-n", type=int, default=10)
    parser.add_argument("--cost-bps", type=float, default=20.0)
    parser.add_argument("--hybrid-weight", type=float, default=0.20)
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()

    import asyncpg

    conn = await asyncpg.connect(settings.database_url)
    try:
        panel, memberships = await _load_panel(conn)
    finally:
        await conn.close()

    reports = []
    failures = []
    for etf, members in sorted(memberships.items()):
        try:
            experiment = run_experiment(
                panel,
                etf_symbol=etf,
                constituents=members,
                membership_mode="current_membership_preview",
                top_n=args.top_n,
                hybrid_active_weight=args.hybrid_weight,
                constituent_cost_bps=args.cost_bps,
            )
        except ValueError as exc:
            failures.append({"etf": etf, "error": str(exc)})
            continue
        reports.append(_summary(experiment))

    payload = {
        "panel": {
            "first_date": panel.index.min().date().isoformat(),
            "last_date": panel.index.max().date().isoformat(),
            "sessions": len(panel),
            "symbols": len(panel.columns),
        },
        "assumptions": {
            "membership": "current S&P 500 and current GICS sector links",
            "top_n": args.top_n,
            "constituent_cost_bps_per_turnover": args.cost_bps,
            "etf_spread_bps": 2.0,
            "etf_expense_bps_annual": 10.0,
            "hybrid_active_weight": args.hybrid_weight,
            "rebalance_sessions": 21,
            "warmup_sessions": 126,
        },
        "reports": reports,
        "failures": failures,
    }
    if args.json:
        print(json.dumps(payload, indent=2, allow_nan=False))
        return 0 if reports else 1

    print(json.dumps(payload["panel"], indent=2))
    print(json.dumps(payload["assumptions"], indent=2))
    print()
    print("ETF   strategy         CAGR     vol   max DD  Sharpe  turnover  net vs ETF")
    for report in reports:
        benchmark = report["strategies"]["etf"]["cagr_pct"]
        for strategy, metrics in report["strategies"].items():
            print(
                f"{report['etf']:<5} {strategy:<15} "
                f"{metrics['cagr_pct']:>7.2f}% {metrics['volatility_pct']:>7.2f}% "
                f"{metrics['max_drawdown_pct']:>7.2f}% {metrics['sharpe']:>7.2f} "
                f"{metrics['turnover']:>9.2f} {metrics['cagr_pct'] - benchmark:>+9.2f}%"
            )
    if failures:
        print("failures:", json.dumps(failures, indent=2))
    print("\nEXPLORATORY ONLY: current membership is applied backward in time.")
    return 0 if reports else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
