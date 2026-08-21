"""Bulk EDGAR fundamentals ingest for the full company universe.

The demand-driven layer fetches fundamentals per-company only when demanded, so
only companies someone watched ever get them. A cross-sectional factor test
needs the universe. This loops every company with a resolved CIK and ingests
its full companyfacts history (one bulk JSON per company, all years), via the
same EdgarAdapter + write_claims path the scheduler uses.

Idempotent: write_claims is ON CONFLICT DO NOTHING on the unique
(entity, type, key, source, event_date, knowledge_date), so a re-run costs
nothing and duplicates nothing. Free: SEC EDGAR is a public API.

Run on the deployment host from /srv/omni:
  docker compose -f docker-compose.prod.yml exec -T scheduler \
    python - < ops/ingest_edgar_bulk.py
"""

from __future__ import annotations

import asyncio

from omni.config import settings
from omni.coverage.writer import write_claims
from omni.db import connect
from omni.ingest.edgar import EdgarAdapter
from omni.ingest.protocol import Unavailable

PROVIDER_KEY = "sec_edgar"
SOURCE = "sec_edgar"


async def main() -> int:
    ua = settings.sec_user_agent
    if not ua:
        print("sec_user_agent is not set; EDGAR requires a contact User-Agent", flush=True)
        return 1

    c = await connect(settings.database_url)
    adapter = EdgarAdapter(user_agent=ua)

    rows = await c.pool.fetch(
        "SELECT id, symbol, identifiers->>'cik' AS cik "
        "FROM entity WHERE kind='company' AND identifiers ? 'cik' ORDER BY symbol"
    )
    print(f"{len(rows)} companies with CIKs; ingesting companyfacts (idempotent)", flush=True)

    total_written = 0
    skipped: list[tuple[str, str]] = []
    for i, r in enumerate(rows, 1):
        try:
            drafts = await adapter.fetch(r["cik"])
        except Unavailable as exc:
            skipped.append((r["symbol"], str(exc)[:90]))
            continue
        except Exception as exc:  # noqa: BLE001 - one company must not abort the run
            skipped.append((r["symbol"], f"{type(exc).__name__}: {exc}"[:90]))
            continue

        if drafts:
            written = await write_claims(
                c.pool,
                drafts,
                entity_id=r["id"],
                source=SOURCE,
                provider_key=PROVIDER_KEY,
            )
            total_written += len(written)

        if i % 25 == 0 or i == len(rows):
            print(f"  [{i:>3}/{len(rows)}] last={r['symbol']:6s} written={total_written}", flush=True)

    print(f"\ndone. claims written this run: {total_written}", flush=True)
    if skipped:
        print(f"skipped {len(skipped)} companies:", flush=True)
        for sym, why in skipped[:15]:
            print(f"  {sym}: {why}", flush=True)

    await c.close()
    return 0


raise SystemExit(asyncio.run(main()))
