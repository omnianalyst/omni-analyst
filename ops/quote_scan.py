"""Read-only measurement of the carry book's real cost surface.

Answers two questions the carry loop assumes rather than measures: what spread
and depth do the eight carry legs actually face, and what is the taker-vs-maker
cost delta at the size the book trades? The maker delta is the upper bound on
what resting entry could capture -- before adverse selection, which this cannot
see (only real resting orders experience the queue).

Reads public order books. No orders, no credentials, no money. Constructs a
CCXTVenue in READ_ONLY and walks each leg's book at a stated notional.

Per-market fees are read from the venue's own market metadata, not from
`Capabilities`: Hyperliquid files spot at 4/7 bps and perpetuals at 1.5/4.5, but
`Capabilities` carries the max (7) and charges it to every leg. The per-market
number is the real one; the max is the cost model's pessimism.

Run:
  uv run python ops/quote_scan.py sample --samples 12 --interval 300
  uv run python ops/quote_scan.py summarize
"""

from __future__ import annotations

import argparse
import asyncio
import json
import statistics
import sys
from collections import defaultdict
from collections.abc import Sequence
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

from omni.venue.ccxt_venue import BOOK_DEPTH, CCXTVenue, TradingMode, _walk_book
from omni.venue.protocol import MarketType, Side, TradeIntent

CARRY_ASSETS = ("BTC", "ETH", "SOL", "HYPE")
DEFAULT_NOTIONAL = Decimal(50)
DEFAULT_PATH = Path(__file__).resolve().parent / "quote_scan.jsonl"
BPS = Decimal(10_000)

_LEGS = (
    (MarketType.SPOT, Side.BUY),
    (MarketType.PERPETUAL, Side.SELL),
)


def _frac_to_bps(value: object) -> Decimal | None:
    """ccxt publishes fees as fractions (0.0001 = 1 bp); return bps, or None."""
    if value is None:
        return None
    return Decimal(str(value)) * BPS


def measure_leg(
    book: dict | None,
    *,
    asset: str,
    market_type: MarketType,
    side: Side,
    notional: Decimal,
    maker_fee_bps: Decimal,
    taker_fee_bps: Decimal,
    symbol: str,
    venue_name: str,
    as_of: datetime,
) -> dict:
    """One leg's cost surface, computed from a fetched book. Pure -- no network.

    `maker_fee_bps` / `taker_fee_bps` are the resolved per-market values in bps;
    the caller reads them from market metadata so a 4.5-bp perpetual is not
    charged the 7-bp spot taker the venue-wide `Capabilities` would apply.
    """
    rec: dict = {
        "ts": as_of.isoformat(),
        "venue": venue_name,
        "asset": asset,
        "market_type": market_type.value,
        "side": side.value,
        "symbol": symbol,
        "notional": notional,
        "maker_fee_bps": maker_fee_bps,
        "taker_fee_bps": taker_fee_bps,
        "best_bid": None,
        "best_ask": None,
        "mid": None,
        "spread_bps": None,
        "bid_depth_notional": None,
        "ask_depth_notional": None,
        "quantity": None,
        "taker_vwap": None,
        "slippage_bps": None,
        "taker_cost_bps": None,
        "maker_cost_bps": None,
        "gross_saving_bps": None,
    }

    bids = (book or {}).get("bids") or []
    asks = (book or {}).get("asks") or []
    if not bids or not asks:
        rec["error"] = "no_two_sided_market"
        return rec

    best_bid = Decimal(str(bids[0][0]))
    best_ask = Decimal(str(asks[0][0]))
    if best_bid <= 0 or best_ask <= 0 or best_ask < best_bid:
        rec["error"] = "no_two_sided_market"
        return rec

    mid = (best_bid + best_ask) / 2
    spread_bps = (best_ask - best_bid) / mid * BPS
    rec["best_bid"] = best_bid
    rec["best_ask"] = best_ask
    rec["mid"] = mid
    rec["spread_bps"] = spread_bps
    rec["bid_depth_notional"] = Decimal(str(bids[0][1])) * mid
    rec["ask_depth_notional"] = Decimal(str(asks[0][1])) * mid

    quantity = notional / mid
    intent = TradeIntent(
        venue=venue_name,
        symbol=symbol,
        side=side,
        market_type=market_type,
        quantity=quantity,
        reference_price=mid,
    )
    vwap = _walk_book(book, intent)
    if vwap is None:
        rec["error"] = "unfillable"
        return rec

    if side is Side.BUY:
        slippage_bps = (vwap - mid) / mid * BPS
    else:
        slippage_bps = (mid - vwap) / mid * BPS
    taker_cost_bps = slippage_bps + taker_fee_bps
    gross_saving_bps = taker_cost_bps - maker_fee_bps

    rec["quantity"] = quantity
    rec["taker_vwap"] = vwap
    rec["slippage_bps"] = slippage_bps
    rec["taker_cost_bps"] = taker_cost_bps
    rec["maker_cost_bps"] = maker_fee_bps
    rec["gross_saving_bps"] = gross_saving_bps
    return rec


async def sample_once(
    venue: CCXTVenue, assets: Sequence[str], notional: Decimal
) -> list[dict]:
    as_of = datetime.now(UTC)
    records: list[dict] = []
    for asset in assets:
        for market_type, side in _LEGS:
            symbol = venue.symbol_for(asset, market_type)
            if symbol is None:
                records.append(
                    {
                        "ts": as_of.isoformat(),
                        "venue": venue.name,
                        "asset": asset,
                        "market_type": market_type.value,
                        "side": side.value,
                        "error": "no_symbol",
                    }
                )
                continue

            market = venue._market(symbol)
            maker_fee_bps = _frac_to_bps(market.get("maker"))
            if maker_fee_bps is None:
                maker_fee_bps = venue.capabilities.maker_fee_bps
            taker_fee_bps = _frac_to_bps(market.get("taker"))
            if taker_fee_bps is None:
                taker_fee_bps = venue.capabilities.taker_fee_bps

            book = await venue._exchange.fetch_order_book(symbol, limit=BOOK_DEPTH)
            records.append(
                measure_leg(
                    book,
                    asset=asset,
                    market_type=market_type,
                    side=side,
                    notional=notional,
                    maker_fee_bps=maker_fee_bps,
                    taker_fee_bps=taker_fee_bps,
                    symbol=symbol,
                    venue_name=venue.name,
                    as_of=as_of,
                )
            )
    return records


async def run_samples(
    venue: CCXTVenue,
    assets: Sequence[str],
    notional: Decimal,
    samples: int,
    interval: int,
) -> list[dict]:
    out: list[dict] = []
    for i in range(samples):
        out.extend(await sample_once(venue, assets, notional))
        if i + 1 < samples and interval > 0:
            await asyncio.sleep(interval)
    return out


def _stats(values: list[Decimal]) -> dict:
    return {
        "n": len(values),
        "mean": statistics.mean(values),
        "median": statistics.median(values),
        "min": min(values),
        "max": max(values),
    }


def summarize(records: list[dict]) -> dict:
    groups: dict[tuple[str, str], list[dict]] = defaultdict(list)
    for r in records:
        if "error" in r:
            continue
        groups[(r["asset"], r["market_type"])].append(r)

    out: dict[str, dict] = {}
    for (asset, mt), rows in sorted(groups.items()):
        out[f"{asset} {mt}"] = {
            "spread_bps": _stats([r["spread_bps"] for r in rows]),
            "slippage_bps": _stats([r["slippage_bps"] for r in rows]),
            "taker_cost_bps": _stats([r["taker_cost_bps"] for r in rows]),
            "maker_cost_bps": _stats([r["maker_cost_bps"] for r in rows]),
            "gross_saving_bps": _stats([r["gross_saving_bps"] for r in rows]),
            "bid_depth_notional": _stats([r["bid_depth_notional"] for r in rows]),
            "ask_depth_notional": _stats([r["ask_depth_notional"] for r in rows]),
        }
    errors = sum(1 for r in records if "error" in r)
    out["_errors"] = {"count": errors, "total": len(records)}
    return out


def _jsonable(d: dict) -> dict:
    out = {}
    for k, v in d.items():
        if isinstance(v, Decimal):
            out[k] = str(v)
        else:
            out[k] = v
    return out


def _append_jsonl(path: Path, records: list[dict]) -> None:
    with path.open("a") as fh:
        for r in records:
            fh.write(json.dumps(_jsonable(r)) + "\n")


def _print_summary(summary: dict) -> None:
    for key, stats in summary.items():
        if key == "_errors":
            continue
        print(f"\n{key}")
        for metric, s in stats.items():
            print(
                f"  {metric:20s}  n={s['n']:>3}  "
                f"mean {s['mean']:#.4f}  median {s['median']:#.4f}  "
                f"[{s['min']:#.4f}, {s['max']:#.4f}]"
            )
    err = summary.get("_errors", {})
    print(f"\nerrors: {err.get('count', 0)} of {err.get('total', 0)} records")


async def _run_sample(args: argparse.Namespace) -> int:
    venue = await CCXTVenue.connect(
        venue="hyperliquid",
        quote_asset="USDC",
        mode=TradingMode.READ_ONLY,
    )
    try:
        records = await run_samples(
            venue,
            args.assets,
            Decimal(args.notional),
            args.samples,
            args.interval,
        )
    finally:
        await venue.aclose()
    _append_jsonl(args.path, records)
    print(f"wrote {len(records)} records to {args.path}")
    _print_summary(summarize(records))
    return 0


def _run_summarize(args: argparse.Namespace) -> int:
    if not args.path.exists():
        print(f"no data at {args.path}", file=sys.stderr)
        return 1
    records = [json.loads(line) for line in args.path.read_text().splitlines() if line.strip()]
    _print_summary(summarize(records))
    return 0


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("sample", help="take read-only book samples now")
    s.add_argument("--samples", type=int, default=1, help="number of sampling rounds")
    s.add_argument("--interval", type=int, default=300, help="seconds between samples")
    s.add_argument("--notional", type=str, default=str(DEFAULT_NOTIONAL), help="per-leg notional in USD")
    s.add_argument("--assets", nargs="+", default=list(CARRY_ASSETS))
    s.add_argument("--path", type=Path, default=DEFAULT_PATH)

    sm = sub.add_parser("summarize", help="report distributions from accumulated data")
    sm.add_argument("--path", type=Path, default=DEFAULT_PATH)

    args = p.parse_args()
    if args.cmd == "sample":
        return asyncio.run(_run_sample(args))
    return _run_summarize(args)


if __name__ == "__main__":
    raise SystemExit(main())
