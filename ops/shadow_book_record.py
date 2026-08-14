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
from datetime import UTC, datetime

from asyncpg.exceptions import UniqueViolationError

from omni.config import settings
from omni.db import connect
from omni.research.allocation import AllocationRefused, equal_weight, risk_balanced, top_measured
from omni.research.shadow_book import ShadowBookRefused, record_decision
from omni.research.shadow_run import BENCHMARK, COST_BPS, SECTORS, load_panel, next_session

RULES = (equal_weight, top_measured, risk_balanced)


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
        already = 0
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
            except UniqueViolationError:
                # This session already has a decision for this book, and it
                # stands. On a daily cron it is the ordinary outcome of a run
                # whose panel has not advanced -- a weekend, a holiday, a second
                # run the same evening -- so it is reported rather than raised.
                #
                # It is emphatically not upgraded to an upsert. Overwriting the
                # earlier row with weights computed from a later panel is the
                # revision this whole table exists to make impossible, and it
                # would arrive looking like a bug fix.
                already += 1
                print(f"                 already recorded for {effective}; left as it was")
                continue
            recorded += 1
            print(f"                 recorded {decision.id}")

        print()
        print(
            f"recorded {recorded}, already present {already}, refused {refused}, "
            f"of {len(RULES)} rules"
        )
        return 0 if refused == 0 else 1
    finally:
        await client.close()


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
