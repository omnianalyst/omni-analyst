"""One-shot: backfill Hyperliquid funding since the store's last settlement.

The funding_rate claims the carry book reads were a one-time pre-launch
calibration fill that ends 2026-08-10 04:00 UTC -- the hour the book opened.
No recurring producer exists for them (the registered derivatives capability
speaks Binance; Hyperliquid claims come from the any-venue funding adapter,
which is tested but was never wired to a schedule). The wind-down cycle
abstained honestly on 2026-08-19: `no_funding_coverage_visible`, because the
selector ranks names on trailing funding and there was none.

This walks Hyperliquid's own funding history for the six governed carry
names from the last stored settlement to now and writes the drafts through
the standard writer with the operator as credential owner -- the same
audience every prior funding claim carries and the same one the cycle reads
under. Idempotent by the claim-store's unique index: a re-run inserts
nothing. Not a recurring fix: standing Hyperliquid funding ingestion is a
separate, proper piece of work for the next session.

Run inside the scheduler container:

    python - < ops/funding_backfill_hl.py
"""

from __future__ import annotations

import asyncio
from uuid import UUID

from omni.config import settings
from omni.coverage.writer import write_claims
from omni.db import connect
from omni.ingest.funding import CCXTFundingAdapter

OWNER = UUID("97e7737f-cad3-439a-b8b3-3ae4536a7eac")
VENUE = "hyperliquid"
SYMBOLS = ["BTC/USDC:USDC", "ETH/USDC:USDC", "SOL/USDC:USDC",
           "HYPE/USDC:USDC", "PENGU/USDC:USDC", "PURR/USDC:USDC"]

_LAST_STORED = """
SELECT max(c.event_date)
FROM claim c
JOIN entity e ON e.id = c.entity_id
WHERE c.claim_type::text = 'funding_rate'
  AND c.key LIKE $1 || '%'
"""

_ENTITY_BY_SYMBOL = """
SELECT e.id
FROM entity e
WHERE e.symbol = $1
"""


async def main() -> int:
    client = await connect(settings.database_url)
    try:
        since = await client.pool.fetchval(_LAST_STORED, f"{VENUE}:")
        if since is None:
            print("no stored Hyperliquid funding at all; refusing rather "
                  "than guessing a start")
            return 1
        print(f"last stored settlement {since}; backfilling from there")

        adapter = CCXTFundingAdapter(venue=VENUE, since=since)
        total = 0
        for symbol in SYMBOLS:
            base = symbol.split("/")[0]
            entity_id = await client.pool.fetchval(_ENTITY_BY_SYMBOL, base)
            if entity_id is None:
                print(f"{symbol:20} no entity row; skipped")
                continue
            drafts = await adapter.fetch(symbol)
            written = await write_claims(
                client.pool,
                drafts,
                entity_id=entity_id,
                source=adapter.source,
                provider_key=adapter.provider_key,
                credential_owner=OWNER,
            )
            total += len(written)
            print(f"{symbol:20} {len(drafts):4} settlements walked, "
                  f"{len(written)} inserted")

        newest = await client.pool.fetchval(_LAST_STORED, f"{VENUE}:")
        print(f"store now current through {newest} ({total} inserted)")
        return 0
    finally:
        await client.close()


raise SystemExit(asyncio.run(main()))
