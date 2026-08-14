"""One-off retry of cycle one to validate the spot slippage fix.

Identical to cycle_one.py except it passes ignore_cadence=True, because the
04:00 UTC cycle already recorded as completed and the six-week hold would
otherwise block this re-run. The override exists for exactly this: an
operational re-run to test a fix. Not for the cron path.
"""

import asyncio
import sys
from datetime import UTC, datetime
from decimal import Decimal
from uuid import UUID

from omni.config import settings
from omni.db import connect
from omni.scheduler.health import EXPECTED_OPERATION_INTERVALS, record_loop_health
from omni.trading.carry_loop import CarryConfig, CarryRiskPolicy
from omni.trading.carry_runner import (
    CarryRunRefused,
    carry_cycle_ownership,
    run_due_cycle,
)
from omni.venue.ccxt_venue import CCXTVenue, TradingMode
from omni.venue.credentials import wallet_credentials

OWNER = UUID("97e7737f-cad3-439a-b8b3-3ae4536a7eac")
UNIVERSE = ["BTC", "ETH", "SOL", "HYPE", "PENGU", "PURR"]
VENUE = "hyperliquid"
LIVE = "--live" in sys.argv


async def main() -> int:
    c = await connect(settings.database_url)
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
            risk_policy=CarryRiskPolicy(
                max_gross_notional=Decimal(280),
                daily_loss_limit_pct_nav=Decimal("0.02"),
                max_drawdown_pct=Decimal("0.10"),
            ),
            lookback_days=7,
            min_settlements=2,
        )

        print(f"mode           {'LIVE - WILL PLACE ORDERS' if LIVE else 'READ_ONLY'}")
        print(f"portfolio      {pid}")
        print(f"inception      {inception}")
        print()

        now = datetime.now(UTC)
        async with carry_cycle_ownership(
            c.pool, portfolio_id=pid, venue=VENUE, now=now
        ) as ownership:
            v = await CCXTVenue.connect(
                venue=VENUE,
                quote_asset="USDC",
                credentials=wallet_credentials(settings, VENUE),
                mode=TradingMode.LIVE if LIVE else TradingMode.READ_ONLY,
            )
            try:
                result = await run_due_cycle(
                    c.pool,
                    venue=v,
                    portfolio_id=pid,
                    config=config,
                    entity_ids=list(assets),
                    audience_user_id=OWNER,
                    now=now,
                    inception=inception,
                    ignore_cadence=True,
                    max_execution_bps=Decimal(40),
                    assets=assets,
                    ownership=ownership,
                )
            finally:
                await v.aclose()

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
        await record_loop_health(
            c.pool,
            loop_name="carry",
            ok=not result.halted,
            error=result.halt_reason if result.halted else None,
            result=(
                f"manual retry held={len(result.held)} opened={len(result.opened)} "
                f"closed={len(result.closed)} halted={result.halted}"
            ),
            expected_interval_seconds=EXPECTED_OPERATION_INTERVALS["carry"],
        )
        return 1 if result.halted else 0
    except CarryRunRefused as exc:
        print(f"REFUSED [{exc.guard}], book untouched:\n  {exc}")
        await record_loop_health(
            c.pool,
            loop_name="carry",
            ok=True,
            result=f"manual retry refused [{exc.guard}]: {exc}",
            expected_interval_seconds=EXPECTED_OPERATION_INTERVALS["carry"],
        )
        return 2
    except BaseException as exc:
        try:
            await record_loop_health(
                c.pool,
                loop_name="carry",
                ok=False,
                error=f"{type(exc).__name__}: {exc}",
                expected_interval_seconds=EXPECTED_OPERATION_INTERVALS["carry"],
            )
        except Exception:  # noqa: BLE001,S110
            pass
        raise
    finally:
        await c.close()


raise SystemExit(asyncio.run(main()))
