"""Correct the carry ledger to venue truth: the book closed on the venue on
2026-08-19 while the script believed its orders had failed.

WHAT ACTUALLY HAPPENED (venue fills, tid-provenanced)

    2026-08-19 07:41  SOL pair closed (perp buy 0.91 tid 391779605573356,
                      spot sell 0.91 tid 1057214491641915)
    2026-08-19 17:33  ETH pair closed (perp buy 0.0365 tid 1066142263935279,
                      spot sell 0.0364 tid 621668439663004)
    2026-08-20 00:00  venue dust sweep sold the residuals
                      (ETH 0.000082273 @ 2251.3, SOL 0.00016009 @ 85.33;
                      tid not reported)
    after             the remaining ~202 USDC left the venue -- a withdrawal
                      this deployment did not make; venue holds 0.19805617

The perp legs closed cleanly in the ledger. The spot legs are the dual-spelling
residue the wind-down handoff predicted ("close fills return unified symbols
against wrapped-name rows -- offsetting rows netting zero, delete/merge
documented"), plus one genuine defect the merge must account for:

    - SOL: the 07:41 sell is counted TWICE -- embedded in the corrected USOL
      row (0.91016009 opening -> 0.00016009 post-sale) AND as its own
      SOL/USDC -0.91 row. The duplicate row must go, not be merged.
    - ETH: UETH +0.036482273 (pre-close opening) and ETH -0.0364 (the close)
      net to exactly the swept dust +0.000082273. Merge, then apply the sweep.

WHAT THIS DOES (read-only unless --live)

    1. Verifies the venue is still flat and reads its USDC -- if the venue has
       moved again, refuse: this correction is for the state recorded above.
    2. Idempotent end-state guard: no position rows and cash at venue truth
       already -> exit 0.
    3. Deletes the duplicate SOL/USDC -0.91 row (provenance: this commit).
    4. Merges each wrapped/unified spot pair to one unified row at its net
       quantity (provenance: this commit).
    5. Applies the venue's dust-sweep fills through apply_fill -- real fills,
       real sizes and prices, moving real cash, so the flat end state carries
       fill provenance rather than a hand-written zero.
    6. Sets cash to the venue's USDC (the withdrawal is not reconstructable;
       venue truth is the arbiter, the rule the 2026-08-18 repair used).
    7. Reconciles against the venue -- must PASS.
    8. Records one NAV point, proving the nightly loop that refused since
       2026-08-19 goes green on the corrected book.

Run inside the scheduler container:

    python - < ops/ledger_to_venue_truth.py          # verify + plan
    python - --live < ops/ledger_to_venue_truth.py   # correct + reconcile + NAV
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
from omni.portfolio.state import apply_fill, load
from omni.trading.nav_job import snapshot
from omni.venue.ccxt_venue import CCXTVenue, TradingMode
from omni.venue.credentials import wallet_credentials
from omni.venue.protocol import Fill, MarketType, Side

LIVE = "--live" in sys.argv
PORTFOLIO = UUID("2b485740-2be4-46bf-ac49-e571ca355e30")
OWNER = UUID("97e7737f-cad3-439a-b8b3-3ae4536a7eac")
VENUE = "hyperliquid"
UNIVERSE = ["BTC", "ETH", "SOL", "HYPE", "PENGU", "PURR"]

# The venue's 2026-08-20 00:00 dust sweep, from fetch_my_trades.
DUST_FILLS = [
    ("ETH/USDC", Decimal("0.000082273"), Decimal("2251.3")),
    ("SOL/USDC", Decimal("0.00016009"), Decimal("85.33")),
]

_END_STATE_CORRECT = """
SELECT (SELECT count(*) FROM position WHERE portfolio_id = $1) = 0
   AND EXISTS (
     SELECT 1 FROM cash_balance
     WHERE portfolio_id = $1 AND venue = $2 AND asset = 'USDC'
       AND free = $3 AND locked = 0
   )
"""

# (wrapped, unified): the dual-spelling pairs. The merged row carries the
# pair's net quantity under the unified spelling the venue's fills address.
SPOT_PAIRS = [("UETH/USDC", "ETH/USDC"), ("USOL/USDC", "SOL/USDC")]


async def _fetch_qty(pool, symbol: str) -> Decimal | None:
    return await pool.fetchval(
        "SELECT quantity FROM position WHERE portfolio_id = $1 "
        "AND venue = $2 AND symbol = $3 AND market_type = 'spot'",
        PORTFOLIO, VENUE, symbol,
    )


async def main() -> int:
    client = await connect(settings.database_url)
    pool = client.pool
    try:
        v = await CCXTVenue.connect(
            venue=VENUE, quote_asset="USDC",
            credentials=wallet_credentials(settings, VENUE),
            mode=TradingMode.READ_ONLY,
        )
        print(f"mode     {'LIVE' if LIVE else 'READ-ONLY verify'}")

        if await v.positions():
            for p in await v.positions():
                print(f"REFUSED: venue holds {p.symbol} {p.quantity}")
            return 1
        usdc = {b.asset: b.free for b in await v.balances()}.get(
            "USDC", Decimal(0)
        )
        print(f"venue    flat; USDC {usdc}")

        if await pool.fetchval(_END_STATE_CORRECT, PORTFOLIO, VENUE, usdc):
            print("already correct: no positions, cash at venue truth")
            return 0

        # The duplicate: the 07:41 SOL sell already lives inside the corrected
        # USOL row; its unified -0.91 row counts it a second time.
        dup = await _fetch_qty(pool, "SOL/USDC")
        if dup is not None and dup == Decimal("-0.91"):
            print(f"{'deleting' if LIVE else 'would delete'} duplicate "
                  f"SOL/USDC spot {dup} (sell already in the USOL row)")
            if LIVE:
                await pool.execute(
                    "DELETE FROM position WHERE portfolio_id = $1 "
                    "AND venue = $2 AND symbol = 'SOL/USDC' "
                    "AND market_type = 'spot'",
                    PORTFOLIO, VENUE,
                )

        for wrapped, unified in SPOT_PAIRS:
            w = await _fetch_qty(pool, wrapped)
            u = await _fetch_qty(pool, unified)
            if w is None and u is None:
                print(f"pair     {wrapped}/{unified}: already gone")
                continue
            net = (w or Decimal(0)) + (u or Decimal(0))
            print(f"{'merging' if LIVE else 'would merge'} {wrapped} {w} + "
                  f"{unified} {u} -> {unified} {net}")
            if LIVE:
                if w is not None:
                    await pool.execute(
                        "DELETE FROM position WHERE portfolio_id = $1 "
                        "AND venue = $2 AND symbol = $3 "
                        "AND market_type = 'spot'",
                        PORTFOLIO, VENUE, wrapped,
                    )
                if u is None:
                    await pool.execute(
                        "INSERT INTO position (portfolio_id, venue, symbol, "
                        "market_type, quantity, average_entry) "
                        "VALUES ($1, $2, $3, 'spot', $4, 1)",
                        PORTFOLIO, VENUE, unified, net,
                    )
                else:
                    await pool.execute(
                        "UPDATE position SET quantity = $3 "
                        "WHERE portfolio_id = $1 AND venue = $2 "
                        "AND symbol = $4 AND market_type = 'spot'",
                        PORTFOLIO, VENUE, net, unified,
                    )

        for symbol, qty, px in DUST_FILLS:
            remaining = await _fetch_qty(pool, symbol)
            if remaining is None or remaining == 0:
                print(f"sweep    {symbol}: nothing to sweep")
                continue
            fill = Fill(
                intent_id=f"venue-dust-sweep-2026-08-20:{symbol}",
                venue=VENUE, symbol=symbol, side=Side.SELL,
                filled_quantity=qty, average_price=px,
                fee_paid=Decimal(0),
                filled_at=datetime(2026, 8, 20, tzinfo=UTC),
                external_id=None,
                raw={"note": "ledger_to_venue_truth 2026-08-20: the venue's "
                             "00:00 dust sweep, applied to the merged row"},
            )
            print(f"{'sweeping' if LIVE else 'would sweep'} {qty} {symbol} "
                  f"@ {px} (row holds {remaining})")
            if LIVE:
                await apply_fill(pool, PORTFOLIO, fill, MarketType.SPOT)

        if not LIVE:
            print("dry run: nothing written")
            return 0

        await pool.execute(
            """
            UPDATE cash_balance SET free = $3, locked = 0, updated_at = now()
            WHERE portfolio_id = $1 AND venue = $2 AND asset = 'USDC'
            """,
            PORTFOLIO, VENUE, usdc,
        )
        print(f"cash     USDC -> {usdc} (venue truth)")

        book = await load(pool, PORTFOLIO)
        verified = await reconcile_books(
            book.positions, book.cash_positions, v,
            tolerance=Decimal("1.00"), now=datetime.now(UTC),
        )
        if not verified.reconciled:
            for d in verified.discrepancies:
                print(f"DIVERGED {d.detail}")
            return 1
        print("reconciled against the venue")

        rows = await pool.fetch(
            "SELECT id, symbol FROM entity WHERE symbol = ANY($1::text[])",
            UNIVERSE,
        )
        nav = await snapshot(
            pool, venue=v, portfolio_id=PORTFOLIO,
            entity_ids=[r["id"] for r in rows],
            audience_user_id=OWNER,
            at=datetime.now(UTC),
        )
        print(f"NAV recorded {nav}")
        return 0
    finally:
        await client.close()


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
