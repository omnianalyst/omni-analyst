"""Ingest ETF holdings from issuer websites and write as holding claims.

Sources:
- SPDR (SPY): XLSX from ssga.com (confirmed working)
- iShares (TLT, SLV, SHV): CSV from ishares.com (requires correct product URL)
- Vanguard (VTI, VXUS): CSV from advisors.vanguard.com
- Invesco (QQQ): HTML from invesco.com

Run on the deployment host:
    docker compose -f docker-compose.prod.yml exec -T scheduler python - < ops/ingest_etf_holdings.py
"""

import asyncio
import io
import json
import logging
from datetime import UTC, datetime
from decimal import Decimal

import httpx
import openpyxl

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
logger = logging.getLogger(__name__)

SPDR_XLSX = "https://www.ssga.com/us/en/intermediary/library-content/products/fund-data/etfs/us/holdings-daily-us-en-{slug}.xlsx"

SPDR_FUNDS = {
    "SPY": "spy",
    "GLD": "gld",
    "SLV": "slv",
}


async def fetch_spdr_holdings(client, symbol: str, slug: str):
    """Fetch and parse SPDR XLSX holdings."""
    url = SPDR_XLSX.format(slug=slug)
    r = await client.get(url, headers={"User-Agent": "Mozilla/5.0"})
    if r.status_code != 200:
        logger.warning("%s SPDR fetch returned %d", symbol, r.status_code)
        return []
    wb = openpyxl.load_workbook(io.BytesIO(r.content), read_only=True)
    ws = wb.active
    rows = list(ws.iter_rows(values_only=True))
    holdings = []
    for row in rows[5:]:
        if not row or len(row) < 5:
            continue
        ticker = row[1]
        weight_raw = row[4]
        if not ticker or not weight_raw:
            continue
        if not isinstance(weight_raw, (int, float)):
            continue
        weight = Decimal(str(weight_raw)) / Decimal(100)
        if weight <= 0 or weight > 1:
            continue
        holdings.append((str(ticker).strip().upper(), weight))
    logger.info("%s: %d holdings from SPDR", symbol, len(holdings))
    return holdings


async def main():
    import asyncpg

    from omni.config import settings

    logger.info("connecting to DB...")
    conn = await asyncpg.connect(settings.database_url)

    # Resolve ETF entity IDs
    etfs = await conn.fetch(
        "SELECT id, symbol FROM entity WHERE kind = 'etf' AND symbol = ANY($1::text[])",
        list(SPDR_FUNDS.keys()),
    )
    entity_map = {r["symbol"]: r["id"] for r in etfs}
    logger.info("found ETFs: %s", list(entity_map.keys()))

    async with httpx.AsyncClient(timeout=30, follow_redirects=True) as client:
        for symbol, slug in SPDR_FUNDS.items():
            if symbol not in entity_map:
                logger.warning("%s not in entity store, skipping", symbol)
                continue
            entity_id = entity_map[symbol]
            holdings = await fetch_spdr_holdings(client, symbol, slug)
            if not holdings:
                continue

            now = datetime.now(UTC)
            inserted = 0
            for ticker, weight in holdings:
                try:
                    await conn.execute(
                        """
                        INSERT INTO claim (entity_id, claim_type, key, value, source,
                                           event_date, knowledge_date, confidence,
                                           redistributable)
                        VALUES ($1, 'holding', $2, $3::jsonb, 'etf_holdings_spdr',
                                $4, $4, 1.0, 'allowed')
                        ON CONFLICT DO NOTHING
                        """,
                        entity_id,
                        ticker,
                        json.dumps({"weight": str(weight), "fund": symbol}),
                        now,
                    )
                    inserted += 1
                except Exception as e:  # noqa: BLE001
                    logger.debug("skip %s %s: %s", symbol, ticker, e)

            logger.info("%s: wrote %d holding claims", symbol, inserted)

    await conn.close()
    logger.info("done")


raise SystemExit(asyncio.run(main()))
