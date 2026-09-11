"""Wind the carry book down: close every held pair, open nothing. Terminal.

Run inside the scheduler container:

    python ops/close_book.py            # READ_ONLY: print positions, write nothing
    python ops/close_book.py --live     # wind down under the ownership lease

The historical-balance correction step this script once ran first is gone:
it inferred opening fills from the venue's CURRENT balances plus a hard-coded
sold quantity, which is false after any later close, withdrawal or repeat
run, and it rewrote order_event/trade_order/position history outside both the
cycle-ownership lease and reconciliation. Correcting an already-damaged
ledger requires authoritative execution evidence, not current-balance
inference.

Steps, in order:

1. Resolve the configured book (OMNI_TRADING_PORTFOLIO_ID / _OWNER_ID) and,
   without --live, print the local positions and write nothing.
2. Take the cycle-ownership lease, then gate on unsettled orders: an
   unresolved live order is an operator decision, not a state to wind down
   around.
3. Reconcile against the venue (tolerance 1.00, the measured value). A
   diverged book is not traded, including on the way out.
4. Call wind_down_book: settle funding since the boundary, close each pair
   at its own leg sizes, name any sub-minimum dust the venue will not trade.
"""

from __future__ import annotations

import asyncio
import sys
from datetime import UTC, datetime
from decimal import Decimal

from omni.config import settings
from omni.db import connect
from omni.portfolio.reconcile import reconcile as reconcile_books
from omni.portfolio.state import load
from omni.scheduler.health import EXPECTED_OPERATION_INTERVALS, record_loop_health
from omni.trading.carry_loop import CarryConfig, CarryRiskPolicy, wind_down_book
from omni.trading.carry_runner import boundary, carry_cycle_ownership
from omni.trading.ops_scope import configured_book
from omni.trading.order_safety import require_settled_orders
from omni.venue.ccxt_venue import CCXTVenue, TradingMode
from omni.venue.credentials import wallet_credentials

UNIVERSE = ["BTC", "ETH", "SOL", "HYPE", "PENGU", "PURR"]
VENUE = "hyperliquid"
LIVE = "--live" in sys.argv


async def main() -> int:
    client = await connect(settings.database_url)
    venue = None
    try:
        pid, inception, owner = await configured_book(client.pool)
        if not LIVE:
            book = await load(client.pool, pid)
            print(
                f"READ_ONLY: {len(book.positions)} local positions; "
                f"nothing written or traded"
            )
            return 0
        now = datetime.now(UTC)
        async with carry_cycle_ownership(
            client.pool, portfolio_id=pid, venue=VENUE, now=now
        ) as ownership:
            await require_settled_orders(client.pool, pid)
            venue = await CCXTVenue.connect(
                venue=VENUE,
                quote_asset="USDC",
                credentials=wallet_credentials(settings, VENUE),
                mode=TradingMode.LIVE,
            )
            book = await load(client.pool, pid)
            verified = await reconcile_books(
                book.positions,
                book.cash_positions,
                venue,
                tolerance=Decimal("1.00"),
                now=datetime.now(UTC),
            )
            if not verified.reconciled:
                for d in verified.discrepancies:
                    print(f"DIVERGED  {d.detail}")
                print("book not reconciled; refusing to trade")
                return 1
            print("reconciled against the venue")

            rows = await client.pool.fetch(
                "SELECT id FROM entity WHERE symbol = ANY($1::text[])", UNIVERSE
            )
            known = await boundary(client.pool, pid, VENUE)
            since = known.opens_at or inception
            print(f"funding since  {since}")

            config = CarryConfig(
                enter_rank=2,
                exit_rank=3,
                notional_per_pair=Decimal(70),
                funding_venue=VENUE,
                spread_bps=Decimal(5),
                reconciliation_tolerance=Decimal("1.00"),
                risk_policy=CarryRiskPolicy(
                    max_gross_notional=Decimal(280),
                    daily_loss_limit_pct_nav=Decimal("0.02"),
                    max_drawdown_pct=Decimal("0.10"),
                ),
            )

            result = await wind_down_book(
                client.pool,
                venue=venue,
                portfolio_id=pid,
                config=config,
                entity_ids=[r["id"] for r in rows],
                audience_user_id=owner,
                as_of=datetime.now(UTC),
                funding_since=since,
                ownership=ownership,
            )

            print(f"halted         {result.halted}  {result.halt_reason or ''}")
            print(f"closed         {len(result.closed)} pairs")
            for p in result.closed:
                print(f"   {p.symbol}  spot {p.spot.filled_quantity} / "
                      f"perp {p.perp.filled_quantity}")
            print(f"funding        {result.funding_collected}")
            print(f"fees paid      {result.fees_paid}")
            print(f"settled thru   {result.funding_settled_through}")
            if result.refused:
                print(f"refused        {result.refused}")
            await record_loop_health(
                client.pool,
                loop_name="carry",
                ok=not result.halted,
                error=result.halt_reason if result.halted else None,
                result=(
                    f"wind-down: closed={len(result.closed)} "
                    f"halted={result.halted}"
                ),
                expected_interval_seconds=EXPECTED_OPERATION_INTERVALS["carry"],
            )
            return 1 if result.halted else 0
    finally:
        if venue is not None:
            await venue.aclose()
        await client.close()


raise SystemExit(asyncio.run(main()))
