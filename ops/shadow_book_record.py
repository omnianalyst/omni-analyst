"""Record tomorrow's allocation decisions into the forward shadow book.

Production invocation:

    docker compose -f docker-compose.prod.yml exec -T scheduler \
      python /app/ops/shadow_book_record.py --dry-run
    docker compose -f docker-compose.prod.yml exec -T scheduler \
      python /app/ops/shadow_book_record.py

Run it daily. Every run that produces nothing is a day the record does not
have, and the record cannot be backfilled -- that is the entire reason the book
exists rather than another backtest.

The decision is stamped for the **next** session and the writer refuses any
other, so this must run before the session it applies to. It reads a panel that
ends at the last close, scores from it, and never looks at the session it is
deciding for.

A rule that refuses is reported and skipped. It is not replaced with a fallback
allocation: a book whose gaps are filled with whatever was available is no
longer a record of that rule.
"""

from __future__ import annotations

import argparse
import asyncio
import json
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

import pandas as pd

from omni.config import settings
from omni.db import connect
from omni.research.allocation import AllocationRefused, equal_weight, risk_balanced, top_measured
from omni.research.shadow_book import ShadowBookRefused, record_decision

# The sector ETFs the store actually covers, plus the benchmark every book is
# measured against. GLD, TLT and BND are deliberately absent: the store holds no
# prices for them, so the two multi-asset rules the brief names cannot be run at
# all, and running them over the subset that exists would be a different rule
# under the same name.
SECTORS = ["XLB", "XLC", "XLE", "XLF", "XLI", "XLK", "XLP", "XLRE", "XLU", "XLV", "XLY"]
BENCHMARK = "SPY"

# Charged against turnover into the recorded weights. 2 bps is the ETF spread
# assumption `etf_replication` already uses; stating it on the row means a later
# reader cannot discover a kinder number after seeing the outcome.
COST_BPS = Decimal(2)

RULES = (equal_weight, top_measured, risk_balanced)

_PANEL = """
SELECT e.symbol, c.event_date, (c.value->>'close')::float8 AS close
FROM claim c
JOIN entity e ON e.id = c.entity_id
WHERE c.claim_type = 'price_snapshot'
  AND e.symbol = ANY($1::text[])
  AND c.value->>'close' IS NOT NULL
  AND c.audience_user_id IS NOT DISTINCT FROM $2
ORDER BY e.symbol, c.event_date, c.knowledge_date
"""

_AUDIENCE = """
SELECT c.audience_user_id
FROM claim c
JOIN entity e ON e.id = c.entity_id
WHERE c.claim_type = 'price_snapshot' AND e.symbol = ANY($1::text[])
GROUP BY c.audience_user_id
ORDER BY count(*) DESC
LIMIT 1
"""


async def load_panel(pool, symbols: list[str]) -> tuple[pd.DataFrame, object]:
    """The adjusted-close panel for these symbols, from one audience.

    Scoped to a single `audience_user_id` rather than pooled across all of them:
    these are `byo_only` Polygon claims, and a panel assembled from two
    audiences would be this deployment redistributing one user's licensed data
    into another's decision record.
    """
    audience = await pool.fetchval(_AUDIENCE, symbols)
    rows = await pool.fetch(_PANEL, symbols, audience)
    frame = pd.DataFrame(rows, columns=["symbol", "date", "close"])
    if frame.empty:
        raise RuntimeError(
            f"no price claims for {', '.join(symbols)} under audience {audience}"
        )
    frame["date"] = (
        pd.to_datetime(frame["date"], utc=True).dt.tz_convert(None).dt.normalize()
    )
    frame = frame.drop_duplicates(["symbol", "date"], keep="last")
    panel = frame.pivot(index="date", columns="symbol", values="close").sort_index()
    return panel, audience


def next_session(last_close: date, *, today: date) -> date:
    """The first session a decision made now can apply to.

    Two bounds, and the later one wins. The next business day after the last
    close is the market's answer; strictly after today is the point-in-time
    rule's answer, and it is what stops a run on a stale panel from stamping a
    decision onto a session that has already happened. Holidays are not modelled
    -- if the chosen day is a market holiday the scorer opens the window at the
    first session on or after it, which is the same book either way.
    """
    candidate = last_close + timedelta(days=1)
    while candidate.weekday() >= 5:
        candidate += timedelta(days=1)
    if candidate <= today:
        candidate = today + timedelta(days=1)
        while candidate.weekday() >= 5:
            candidate += timedelta(days=1)
    return candidate


async def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="shadow_book_record",
        description="Record tomorrow's allocation decisions into the shadow book.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="compute and print the decisions without writing them",
    )
    args = parser.parse_args(argv)

    now = datetime.now(UTC)
    client = await connect(settings.database_url)
    try:
        panel, audience = await load_panel(client.pool, [*SECTORS, BENCHMARK])
        last_close = panel.index[-1].date()
        effective = next_session(last_close, today=now.date())

        print(f"audience       {audience}")
        print(f"panel          {len(panel)} sessions, {panel.index[0].date()} -> {last_close}")
        print(f"effective from {effective}")
        print(f"mode           {'DRY RUN' if args.dry_run else 'RECORDING'}")
        print()

        recorded = 0
        refused = 0
        for rule in RULES:
            try:
                allocation = rule(panel, SECTORS, benchmark=BENCHMARK)
            except AllocationRefused as exc:
                refused += 1
                print(f"{rule.__name__:<16} REFUSED  {exc}")
                continue

            held = {k: round(v, 4) for k, v in sorted(allocation.weights.items())}
            print(f"{rule.__name__:<16} {allocation.book}")
            print(f"                 {json.dumps(held)}")

            if args.dry_run:
                continue
            try:
                decision = await record_decision(
                    client.pool,
                    book=allocation.book,
                    rule_version=allocation.rule_version,
                    effective_from=effective,
                    universe=allocation.universe,
                    inputs=allocation.inputs,
                    weights=allocation.weights,
                    cost_bps=COST_BPS,
                    benchmark=allocation.benchmark,
                    note=f"panel ends {last_close}",
                    now=now,
                )
            except ShadowBookRefused as exc:
                refused += 1
                print(f"                 REFUSED  {exc}")
                continue
            recorded += 1
            print(f"                 recorded {decision.id}")

        print()
        print(f"recorded {recorded}, refused {refused}, of {len(RULES)} rules")
        return 0 if refused == 0 else 1
    finally:
        await client.close()


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
