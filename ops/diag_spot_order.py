"""Read-only order formatting diagnostic.

The earlier version of this script placed unledgered live orders and sold
back `order.get("filled") or amount` -- selling the full intended amount on a
zero fill, potentially dumping pre-existing holdings while the buy rested.
That capability is retired, not gated: real execution testing belongs behind
the same durable ledger and reconciliation controls as trading. What remains
checks precision and client-order-id construction without buying or selling
anything.

Run on the deployment host:

  docker compose -f docker-compose.prod.yml exec -T scheduler \
    python ops/diag_spot_order.py --symbol ETH/USDC
"""

import argparse
import asyncio
import hashlib
from decimal import Decimal

from omni.config import settings
from omni.venue.ccxt_venue import CCXTVenue, TradingMode
from omni.venue.credentials import wallet_credentials


async def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        description="Read-only order formatting diagnostic"
    )
    parser.add_argument("--symbol", default="ETH/USDC")
    parser.add_argument(
        "--live",
        action="store_true",
        help=argparse.SUPPRESS,
    )
    args = parser.parse_args(argv)
    if args.live:
        parser.error(
            "raw live probes are disabled; use the ledger-backed execution path"
        )
    venue = await CCXTVenue.connect(
        venue="hyperliquid",
        quote_asset="USDC",
        credentials=wallet_credentials(settings, "hyperliquid"),
        mode=TradingMode.READ_ONLY,
    )
    try:
        book = await venue._exchange.fetch_order_book(args.symbol, limit=5)
        if not book.get("bids") or not book.get("asks"):
            raise ValueError("book has no usable bid/ask")
        bid = Decimal(str(book["bids"][0][0]))
        ask = Decimal(str(book["asks"][0][0]))
        if not bid.is_finite() or not ask.is_finite() or not 0 < bid <= ask:
            raise ValueError("invalid or crossed book")
        mid = (bid + ask) / 2
        amount = venue._rounded_amount(args.symbol, Decimal(10) / mid)
        price = venue._rounded_price(args.symbol, mid * Decimal("1.05"))
        key = "read-only-format-diagnostic"
        cloid = "0x" + hashlib.sha256(key.encode()).hexdigest()[:32]
        print(f"READ_ONLY symbol={args.symbol} amount={amount} price={price} cloid={cloid}")
        print("No order was submitted; signing/execution success is not asserted.")
        return 0
    finally:
        await venue.aclose()


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
