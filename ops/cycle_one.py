"""Cycle one: the first live rebalance of the carry book.

Every parameter below is stated rather than defaulted, and each one is here
because a default would have been wrong:

  notional_per_pair   70   two pairs at 3x needs ~$187 of the $211 available
  enter_rank           2   the measured strategy: top 2
  exit_rank            3   NOT the shipped 4. The selector's floor is
                           max(exit+1, enter*2); at exit=4 that is 5 names and
                           the executable universe is 4, so exit=4 abstains.
                           Finding 44 measured the four-name book at 14.58%/yr
                           net, t 5.93.
  max_execution_bps   40   PURR modelled at 212 bps and PENGU at 50; both are
                           excluded by this and both were net-negative or near
                           zero after cost.
  spread_bps           5   conservative for the liquid four, whose measured
                           round trip was almost entirely fee (4 legs x 7 bps).
                           Zero is the permissive value and hands the strategy
                           the spread for free.
  reconciliation_tol 0.01  tight. A tolerance wide enough to swallow a real
                           divergence is a check that always passes.

`inception` is the portfolio's own creation instant. The book has held nothing
since, so no settlement is skipped or re-walked by that choice, and it is a fact
on the row rather than a number someone picked.

TRADING IS OFF unless --live is passed. Credentials being present is necessary
and never sufficient; `CCXTVenue` defaults to READ_ONLY and refuses to place an
order before any network call.
"""

import asyncio
import sys
from datetime import UTC, datetime
from decimal import Decimal
from uuid import UUID

from omni.config import settings
from omni.db import connect
from omni.trading.carry_loop import CarryConfig
from omni.trading.carry_runner import CarryRunRefused, run_due_cycle
from omni.venue.ccxt_venue import CCXTVenue, TradingMode
from omni.venue.credentials import wallet_credentials

OWNER = UUID("97e7737f-cad3-439a-b8b3-3ae4536a7eac")
UNIVERSE = ["BTC", "ETH", "SOL", "HYPE", "PENGU", "PURR"]
LIVE = "--live" in sys.argv
IGNORE_WINDOW = "--ignore-window" in sys.argv
IGNORE_CADENCE = "--ignore-cadence" in sys.argv


async def main() -> int:
    c = await connect(settings.database_url)
    v = await CCXTVenue.connect(
        venue="hyperliquid",
        quote_asset="USDC",
        credentials=wallet_credentials(settings, "hyperliquid"),
        mode=TradingMode.LIVE if LIVE else TradingMode.READ_ONLY,
    )
    try:
        pid = await c.pool.fetchval("SELECT id FROM portfolio LIMIT 1")
        inception = await c.pool.fetchval(
            "SELECT created_at FROM portfolio WHERE id = $1", pid
        )
        rows = await c.pool.fetch(
            "SELECT id, symbol FROM entity WHERE symbol = ANY($1::text[])", UNIVERSE
        )
        assets = {r["id"]: r["symbol"] for r in rows}

        config = CarryConfig(
            enter_rank=2,
            exit_rank=3,
            notional_per_pair=Decimal(70),
            funding_venue="hyperliquid",
            spread_bps=Decimal(5),
            reconciliation_tolerance=Decimal("0.01"),
            lookback_days=7,
            min_settlements=2,
        )

        print(f"mode           {'LIVE - WILL PLACE ORDERS' if LIVE else 'READ_ONLY'}")
        print(f"portfolio      {pid}")
        print(f"inception      {inception}")
        print()

        result = await run_due_cycle(
            c.pool,
            venue=v,
            portfolio_id=pid,
            config=config,
            entity_ids=list(assets),
            audience_user_id=OWNER,
            now=datetime.now(UTC),
            inception=inception,
            max_execution_bps=Decimal(40),
            assets=assets,
            ignore_window=IGNORE_WINDOW,
            ignore_cadence=IGNORE_CADENCE,
        )

        print(f"as_of          {result.as_of}")
        print(f"halted         {result.halted}  {result.halt_reason or ''}")
        print(f"abstention     {result.abstention}")
        print(f"opened         {len(result.opened)} pairs")
        for p in result.opened:
            print(f"   {p.symbol}  qty {p.quantity}")
        print(f"closed         {len(result.closed)} pairs")
        print(f"held           {len(result.held)}")
        print(f"funding        {result.funding_collected}")
        print(f"fees paid      {result.fees_paid}")
        print(f"modelled cost  {result.modelled_turnover_cost}")
        print(f"settled thru   {result.funding_settled_through}")
        if result.refused:
            print(f"refused        {result.refused}")
        return 1 if result.halted else 0
    except CarryRunRefused as exc:
        print(f"REFUSED [{exc.guard}], book untouched:\n  {exc}")
        return 2
    finally:
        await v.aclose()
        await c.close()


raise SystemExit(asyncio.run(main()))
