"""Verify the carry ledger against the venue. Never rewrite it.

This script was once a hard-coded historical repair (duplicate-row deletion,
wrapped/unified merges, average_entry=1 inserts, hard-coded dust fills,
cash overwritten to venue truth) -- executed as separate autocommitted
writes, so a failure midway left the book half-repaired, and every correction
was inferred from CURRENT balances rather than execution evidence. That
writer is retired: the dump did not supply sufficient authoritative execution
history to manufacture a correct replacement ledger, and a rerun after state
changed would silently rewrite history again.

Any future historical correction must be separately reviewed, execution-
backed, and committed atomically against the order ledger.

Run inside the scheduler container:

    python ops/ledger_to_venue_truth.py
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from datetime import UTC, datetime
from decimal import Decimal

from omni.config import settings
from omni.db import connect
from omni.portfolio.reconcile import reconcile as reconcile_books
from omni.portfolio.state import load
from omni.trading.ops_scope import configured_book
from omni.venue.ccxt_venue import CCXTVenue, TradingMode
from omni.venue.credentials import wallet_credentials


async def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        description="Verify, never rewrite, the carry ledger"
    )
    parser.add_argument("--live", action="store_true", help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    if args.live:
        parser.error("historical balance-derived rewrites are disabled")
    client = await connect(settings.database_url)
    venue = None
    try:
        pid, _, _ = await configured_book(client.pool)
        venue = await CCXTVenue.connect(
            venue="hyperliquid",
            quote_asset="USDC",
            credentials=wallet_credentials(settings, "hyperliquid"),
            mode=TradingMode.READ_ONLY,
        )
        book = await load(client.pool, pid)
        result = await reconcile_books(
            book.positions,
            book.cash_positions,
            venue,
            tolerance=Decimal(0),
            now=datetime.now(UTC),
        )
        for item in result.discrepancies:
            print(item.detail)
        print(f"reconciled={result.reconciled}; no rows written")
        return 0 if result.reconciled else 1
    finally:
        if venue is not None:
            await venue.aclose()
        await client.close()


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
