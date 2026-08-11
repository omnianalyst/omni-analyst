"""Diagnostic: does a formatted cloid fix the Hyperliquid order path?

Three carry cycles failed. The MARKET path was fixed (market intents now route
as aggressive LIMIT). The remaining failure is NoneType.split inside ccxt's
signing path when clientOrderId is attached. Hypothesis: the carry loop's
descriptive idempotency key (colons, timestamps) is not valid Hyperliquid cloid
format (0x + 128-bit hex), and the signing pipeline chokes on it.

This tests three shapes on a ~$10 ETH spot probe (venue minimum). Any fill is
sold straight back. Run on deployment-host:

  docker compose -f docker-compose.prod.yml exec -T scheduler \
    python - < ops/diag_spot_order.py

  A: bare params (proven to work — the baseline)
  B: aggressive limit + formatted cloid (0x + 32 hex) — the fix
  C: aggressive limit + raw descriptive cloid — reproduces the bug
"""

import asyncio
import hashlib
from decimal import Decimal

from omni.config import settings
from omni.venue.ccxt_venue import CCXTVenue, TradingMode
from omni.venue.credentials import wallet_credentials


async def _probe(exchange, label: str, kind: str, symbol: str, amount, price, params) -> bool:
    print(f"\n--- {label} ---")
    print(f"    kind={kind} price={price} params={params}")
    try:
        order = await exchange.create_order(symbol, kind, "buy", amount, price=price, params=params)
        print(f"  OK: filled {order.get('filled')} @ {order.get('average')}")
        sold = await exchange.create_order(
            symbol, "market", "sell", order.get("filled") or amount, price=None, params={}
        )
        print(f"  sold back: {sold.get('filled')} @ {sold.get('average')}")
        return True
    except Exception as exc:  # noqa: BLE001 - diagnostic must report any failure shape
        print(f"  FAIL: {type(exc).__name__}: {str(exc)[:300]}")
        return False


async def main() -> int:
    v = await CCXTVenue.connect(
        venue="hyperliquid",
        quote_asset="USDC",
        credentials=wallet_credentials(settings, "hyperliquid"),
        mode=TradingMode.LIVE,
    )
    try:
        print("defaultSlippage in options:", v._exchange.options.get("defaultSlippage"))
        book = await v._exchange.fetch_order_book("ETH/USDC", limit=5)
        mid = (Decimal(str(book["bids"][0][0])) + Decimal(str(book["asks"][0][0]))) / 2
        amount = v._rounded_amount("ETH/USDC", Decimal(10) / mid)
        px = v._rounded_price("ETH/USDC", mid * Decimal("1.05"))
        print(f"ETH/USDC mid={mid} amount={amount} aggressive_px={px}")

        raw_key = "97e7737f:carry:2026-08-11T04:00:00:ETH/USDC:spot:long"
        formatted_cloid = "0x" + hashlib.sha256(raw_key.encode()).hexdigest()[:32]
        print(f"raw descriptive key: {raw_key}")
        print(f"formatted cloid:     {formatted_cloid}")

        # A: bare params (baseline — proven to work)
        if await _probe(v._exchange, "A bare params", "limit", "ETH/USDC", amount, px, {}):
            print("\n>>> A (bare params) works — baseline confirmed")

        # B: formatted cloid (the fix)
        if await _probe(
            v._exchange, "B formatted cloid", "limit", "ETH/USDC", amount, px,
            {"clientOrderId": formatted_cloid},
        ):
            print("\n>>> B (formatted cloid) works — FIX CONFIRMED, implement in ccxt_venue.py")

        # C: raw descriptive cloid (reproduce the bug)
        if await _probe(
            v._exchange, "C raw descriptive cloid", "limit", "ETH/USDC", amount, px,
            {"clientOrderId": raw_key},
        ):
            print("\n>>> C (raw cloid) works — hypothesis WRONG, bug is elsewhere")
        else:
            print("\n>>> C (raw cloid) failed as expected — cloid format IS the cause")

        return 0
    finally:
        await v.aclose()


raise SystemExit(asyncio.run(main()))
