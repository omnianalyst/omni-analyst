"""Wind the carry book down: close every held pair, open nothing. Terminal.

Run inside the scheduler container:

    python - --live < ops/close_book.py

Steps, in order and for the reasons the cycle taught tonight:

1. Correct the two wrapped-spot rows to venue-measured truth. The Aug-18
   repair replayed positions from venue fills but rounded to LOT size
   (UETH 0.0366 vs venue 0.036482273; USOL 0.912 vs 0.91016009), and the
   07:41 partial close sold 0.91 against the 0.912 row. Setting the opening
   fills, order rows and position rows to what the venue's balance sheet
   implies makes the ledger replay land exactly on the venue's numbers --
   the same provenance-documented correction the cash row got.

2. Reconcile against the venue (tolerance 1.00, the measured value). A
   diverged book is not traded, including on the way out.

3. Take the cycle-ownership lease and call wind_down_book: settle funding
   since the boundary, close each pair at its own leg sizes, name any
   sub-minimum dust the venue will not trade.
"""

from __future__ import annotations

import asyncio
import sys
from datetime import UTC, datetime
from decimal import Decimal
from uuid import UUID

from omni.config import settings
from omni.db import connect
from omni.portfolio.reconcile import reconcile as reconcile_books
from omni.portfolio.state import load
from omni.scheduler.health import EXPECTED_OPERATION_INTERVALS, record_loop_health
from omni.trading.carry_loop import CarryConfig, CarryRiskPolicy, wind_down_book
from omni.trading.carry_runner import boundary, carry_cycle_ownership
from omni.venue.ccxt_venue import CCXTVenue, TradingMode
from omni.venue.credentials import wallet_credentials

OWNER = UUID("97e7737f-cad3-439a-b8b3-3ae4536a7eac")
UNIVERSE = ["BTC", "ETH", "SOL", "HYPE", "PENGU", "PURR"]
VENUE = "hyperliquid"
LIVE = "--live" in sys.argv
PORTFOLIO = UUID("2b485740-2be4-46bf-ac49-e571ca355e30")
SOLD_SOL = Decimal("0.91")  # the 07:41 partial close, from the order row


async def _correct_spot_rows(pool, venue) -> None:
    """Set the wrapped spot rows to what the venue's balance sheet implies.

    For SOL, the venue's current balance plus the already-sold quantity is
    the opening the replay must carry for the row to land on venue truth.
    """
    balances = {b.asset: b.free for b in await venue.balances()}
    eth_now = balances.get("ETH", Decimal(0))
    sol_now = balances.get("SOL", Decimal(0))
    implied = {
        "UETH/USDC": (eth_now, eth_now),
        "USOL/USDC": (sol_now + SOLD_SOL, sol_now),
    }
    for symbol, (opening, row) in implied.items():
        await pool.execute(
            """
            UPDATE order_event e
            SET payload = jsonb_set(
                payload, '{fill,filled_quantity}', to_jsonb($2::numeric))
            FROM trade_order t
            WHERE t.id = e.order_id
              AND t.portfolio_id = $1
              AND t.symbol = $3
              AND t.market_type = 'spot'
              AND t.side = 'buy'
              AND e.payload ? 'fill'
            """,
            PORTFOLIO, opening, symbol,
        )
        await pool.execute(
            "UPDATE trade_order SET quantity = $2, filled_quantity = $2 "
            "WHERE portfolio_id = $1 AND symbol = $3 AND market_type = 'spot' "
            "AND side = 'buy'",
            PORTFOLIO, opening, symbol,
        )
        await pool.execute(
            "UPDATE position SET quantity = $2 WHERE portfolio_id = $1 "
            "AND symbol = $3 AND market_type = 'spot'",
            PORTFOLIO, row, symbol,
        )
        print(f"corrected {symbol}: opening fill -> {opening}, row -> {row}")


async def main() -> int:
    client = await connect(settings.database_url)
    try:
        v = await CCXTVenue.connect(
            venue=VENUE,
            quote_asset="USDC",
            credentials=wallet_credentials(settings, VENUE),
            mode=TradingMode.LIVE if LIVE else TradingMode.READ_ONLY,
        )
        try:
            print(f"mode           {'LIVE - WILL PLACE ORDERS' if LIVE else 'READ_ONLY'}")
            await _correct_spot_rows(client.pool, v)

            book = await load(client.pool, PORTFOLIO)
            verified = await reconcile_books(
                book.positions,
                book.cash_positions,
                v,
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
                "SELECT id, symbol FROM entity WHERE symbol = ANY($1::text[])",
                UNIVERSE,
            )
            assets = {r["id"]: r["symbol"] for r in rows}
            known = await boundary(client.pool, PORTFOLIO, VENUE)
            since = known.opens_at
            if since is None:
                since = await client.pool.fetchval(
                    "SELECT created_at FROM portfolio WHERE id = $1", PORTFOLIO
                )
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

            now = datetime.now(UTC)
            async with carry_cycle_ownership(
                client.pool, portfolio_id=PORTFOLIO, venue=VENUE, now=now
            ) as ownership:
                result = await wind_down_book(
                    client.pool,
                    venue=v,
                    portfolio_id=PORTFOLIO,
                    config=config,
                    entity_ids=list(assets),
                    audience_user_id=OWNER,
                    as_of=now,
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
            await v.aclose()
    finally:
        await client.close()


raise SystemExit(asyncio.run(main()))
