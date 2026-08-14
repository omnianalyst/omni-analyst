"""Record one NAV point for the carry book. Runs daily, unattended.

A NAV series cannot be backfilled: it is a mark taken at an instant against a
book as it stood, and neither the composition nor the visible price survives the
positions moving. A day missed is a point lost, which is why this runs on a
schedule rather than when someone remembers.

READ_ONLY throughout. Marking a book reads the venue and writes one row; it
never places an order, and the venue is constructed so that it could not.
"""

import asyncio
from datetime import UTC, datetime
from decimal import Decimal
from uuid import UUID

from omni.config import settings
from omni.db import connect
from omni.scheduler.health import EXPECTED_OPERATION_INTERVALS, record_loop_health
from omni.trading.carry_health import Verdict, assess
from omni.trading.nav_job import Unmarkable, snapshot
from omni.trading.tradeable import affordability, affordable_ids
from omni.venue.ccxt_venue import CCXTVenue, TradingMode
from omni.venue.credentials import wallet_credentials

OWNER = UUID("97e7737f-cad3-439a-b8b3-3ae4536a7eac")
UNIVERSE = ["BTC", "ETH", "SOL", "HYPE", "PENGU", "PURR"]


async def main() -> int:
    c = await connect(settings.database_url)
    v = None
    try:
        v = await CCXTVenue.connect(
            venue="hyperliquid", quote_asset="USDC",
            credentials=wallet_credentials(settings, "hyperliquid"),
            mode=TradingMode.READ_ONLY,
        )
        pid = await c.pool.fetchval("SELECT id FROM portfolio LIMIT 1")
        if pid is None:
            print("no portfolio; nothing to mark")
            await record_loop_health(
                c.pool,
                loop_name="nav",
                ok=True,
                result="no portfolio; nothing to mark",
                expected_interval_seconds=EXPECTED_OPERATION_INTERVALS["nav"],
            )
            return 0
        rows = await c.pool.fetch(
            "SELECT id, symbol FROM entity WHERE symbol = ANY($1::text[])", UNIVERSE
        )
        try:
            nav = await snapshot(
                c.pool, venue=v, portfolio_id=pid,
                entity_ids=[r["id"] for r in rows],
                audience_user_id=OWNER, at=datetime.now(UTC),
            )
        except Unmarkable as exc:
            # A partial NAV reads as authoritative while understating exactly
            # the exposure nobody could price. A gap in the series is the
            # honest outcome and is visible; a wrong point is neither.
            print(f"refused, nothing written: {exc}")
            await record_loop_health(
                c.pool,
                loop_name="nav",
                ok=False,
                error=f"Unmarkable: {exc}",
                result="no NAV snapshot written",
                expected_interval_seconds=EXPECTED_OPERATION_INTERVALS["nav"],
            )
            return 1
        print(f"nav {nav}")

        # The decay check. The book runs itself and nothing in it notices when
        # the premium it harvests stops paying; Finding 32 measured that decay
        # as elapsed time, which does not reverse. Logged every day so the trend
        # is visible before it is a question of whether to stop.
        # Health must describe the book that would ACTUALLY trade. Assessing
        # the full six ranks PURR first at 12.3%/yr and PURR cannot be executed
        # at this size -- which is Finding 44's error committed inside the
        # monitor built to catch it. So the execution filter runs first and the
        # reading is taken on what survives it.
        assets = {r["id"]: r["symbol"] for r in rows}
        measured = await affordability(
            v, assets=assets, notional_per_pair=Decimal(70), as_of=datetime.now(UTC)
        )
        tradeable = affordable_ids(measured, max_execution_bps=Decimal(40))
        print(f"executable universe: {sorted(assets[t] for t in tradeable)}")

        health = await assess(
            c.pool,
            assets={t: assets[t] for t in tradeable},
            audience_user_id=OWNER,
            funding_venue="hyperliquid",
            as_of=datetime.now(UTC),
            enter_rank=2,
            execution_cost_bps=Decimal(30),
        )
        print(health.summary())
        for asset, pct in sorted(health.per_asset_pct.items(), key=lambda kv: -kv[1]):
            print(f"    {asset:<7} {pct:>7.2f}%/yr")
        if health.verdict in (Verdict.DEGRADED, Verdict.BELOW_FLOOR):
            print(f"WARNING [{health.verdict.value}] {health.verdict.detail}")
        await record_loop_health(
            c.pool,
            loop_name="nav",
            ok=True,
            result=f"NAV snapshot {nav}; carry health {health.verdict.value}",
            expected_interval_seconds=EXPECTED_OPERATION_INTERVALS["nav"],
        )
        return 0
    except BaseException as exc:
        try:
            await record_loop_health(
                c.pool,
                loop_name="nav",
                ok=False,
                error=f"{type(exc).__name__}: {exc}",
                expected_interval_seconds=EXPECTED_OPERATION_INTERVALS["nav"],
            )
        except Exception:  # noqa: BLE001,S110
            pass
        raise
    finally:
        if v is not None:
            await v.aclose()
        await c.close()


raise SystemExit(asyncio.run(main()))
