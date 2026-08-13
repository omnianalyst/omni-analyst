"""How much notional the delta-neutral carry book can take before execution
cost eats the funding edge.

WHAT IT MEASURES

For each name in the carry universe it fetches the live spot and perpetual order
books from Hyperliquid and walks them to price the four legs of one pair's round
trip -- buy spot and sell perp to open, sell spot and buy perp to close. Each leg
is charged its own realised slippage against the mid at the instant of the
snapshot, plus the venue's published taker fee for that market. The four leg
costs sum to a round-trip cost in bps of one leg's notional, which is the same
denominator the +780 bps/yr carry edge is quoted against.

The book rebalances every six weeks (`REBALANCE_PERIOD` in
`omni.trading.carry_runner`), so 365.25/42 = 8.696 round trips a year. The edge
survives while

    round_trip_bps * 8.696 <= 780 bps

which puts the entire annual execution budget at 89.70 bps per round trip. The
probe walks the ladder $500 / $1k / $5k / $10k / $50k / $100k, then bisects the
same snapshot for the exact notional at which the round trip reaches 89.70 bps.
That crossing is the capacity number.

Fees are read from the venue's own per-market metadata, not from `Capabilities`.
Hyperliquid files spot at 4.0/7.0 bps and perpetuals at 1.5/4.5, while
`Capabilities` carries the max across markets and would charge 7.0 bps to a perp
leg that costs 4.5. Both models are reported: `published` uses the per-market
fee (2x spot taker + 2x perp taker = 23.0 bps), `flat` charges the 7.0 bps spot
taker to all four legs (28.0 bps) as the carry documentation assumes. Slippage
is identical under both -- only the constant differs.

Read-only. Fetches public books, places no orders, needs no credentials.

WHAT IT CANNOT MEASURE

- **It is a snapshot.** Depth on this venue varies with the hour and with
  volatility. One run is one instant. Take several with `--samples` and read the
  min/max spread, not the mean.
- **It sees 20 price levels a side.** That is what the Hyperliquid l2Book
  endpoint serves at full precision. A size the visible levels cannot fill is
  recorded `unfillable_at_visible_depth` with the wall it hit; it is never
  extrapolated into the invisible remainder. Resting liquidity deeper than level
  20 exists and this cannot see it, so every capacity number reported as
  `beyond_visible_depth` is a lower bound.
- **It prices a taker.** The carry loop trades taker. Resting the entry would
  cost the maker fee instead, but a resting order that fills has been selected
  against, and only a real order in a real queue measures that.
- **It ignores market impact beyond the visible book.** Walking a book assumes
  the resting orders stay put while you cross them. At the larger sizes here
  they will not.
- **It prices one pair in isolation.** It does not model correlated depletion of
  several names at once, nor the funding rate's own response to a larger short.
  A short big enough to move funding shrinks the edge it is harvesting, and that
  feedback is outside this measurement entirely.
- **It says nothing about whether a size is safe.** Liquidation risk from perp
  margin, venue concentration and the six-week gap between rebalances are
  separate questions.

Run:
  uv run python ops/carry_capacity.py measure --samples 3 --interval 300
  uv run python ops/carry_capacity.py summarize
"""

from __future__ import annotations

import argparse
import asyncio
import json
import statistics
import sys
from collections import defaultdict
from collections.abc import Callable, Sequence
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

from ccxt.base.errors import NetworkError

from omni.venue.ccxt_venue import CCXTVenue, TradingMode, _decimal
from omni.venue.protocol import MarketType, Side

BPS = Decimal(10_000)
CARRY_UNIVERSE = ("BTC", "ETH", "SOL", "HYPE", "PENGU", "PURR")
LADDER = (
    Decimal(500),
    Decimal(1_000),
    Decimal(5_000),
    Decimal(10_000),
    Decimal(50_000),
    Decimal(100_000),
)

EDGE_BPS_PER_YEAR = Decimal(780)
REBALANCE_DAYS = Decimal(42)
DAYS_PER_YEAR = Decimal("365.25")
REBALANCES_PER_YEAR = DAYS_PER_YEAR / REBALANCE_DAYS
BUDGET_BPS_PER_ROUND_TRIP = EDGE_BPS_PER_YEAR / REBALANCES_PER_YEAR

FLAT_TAKER_BPS = Decimal(7)
MIN_NOTIONAL = Decimal(10)

DEFAULT_PATH = Path(__file__).resolve().parent / "carry_capacity.jsonl"

ROUND_TRIP_LEGS = (
    ("open_spot_buy", MarketType.SPOT, Side.BUY),
    ("open_perp_sell", MarketType.PERPETUAL, Side.SELL),
    ("close_spot_sell", MarketType.SPOT, Side.SELL),
    ("close_perp_buy", MarketType.PERPETUAL, Side.BUY),
)


def _levels(book: object, side: Side) -> list[tuple[Decimal, Decimal]]:
    """The side of `book` a taker on `side` consumes, cleaned of junk levels.

    A BUY lifts asks, a SELL hits bids. Levels that do not parse to a finite
    positive price and size are dropped rather than defaulted, so an exchange
    sending a null size shrinks the measured depth instead of inventing it.
    """
    if not isinstance(book, dict):
        return []
    raw = book.get("asks") if side is Side.BUY else book.get("bids")
    out: list[tuple[Decimal, Decimal]] = []
    for level in raw or []:
        if not isinstance(level, Sequence) or len(level) < 2:
            continue
        price = _decimal(level[0])
        size = _decimal(level[1])
        if price is None or size is None or price <= 0 or size <= 0:
            continue
        out.append((price, size))
    return out


def touch(book: object) -> tuple[Decimal, Decimal, Decimal] | None:
    bids = _levels(book, Side.SELL)
    asks = _levels(book, Side.BUY)
    if not bids or not asks:
        return None
    best_bid = bids[0][0]
    best_ask = asks[0][0]
    if best_ask < best_bid:
        return None
    mid = (best_bid + best_ask) / 2
    if mid <= 0:
        return None
    return best_bid, best_ask, mid


def walk(
    levels: Sequence[tuple[Decimal, Decimal]], quantity: Decimal
) -> tuple[Decimal, Decimal] | None:
    """Size-weighted fill price for `quantity`, or None if the levels run out.

    Returns None rather than the price of a partial fill: a VWAP for a quantity
    the book could not supply is a cost for a trade that would not happen, and
    the caller needs to hear the refusal, not a number.
    """
    if quantity <= 0:
        return None
    remaining = quantity
    spent = Decimal(0)
    for price, size in levels:
        take = min(remaining, size)
        spent += take * price
        remaining -= take
        if remaining <= 0:
            return spent / quantity, quantity
    return None


def leg_cost(
    book: object,
    *,
    side: Side,
    notional: Decimal,
    taker_fee_bps: Decimal,
) -> dict:
    t = touch(book)
    if t is None:
        return {"error": "no_two_sided_market"}
    best_bid, best_ask, mid = t

    levels = _levels(book, side)
    visible_quantity = sum((size for _, size in levels), Decimal(0))
    wall_notional = visible_quantity * mid

    quantity = notional / mid
    walked = walk(levels, quantity)
    if walked is None:
        return {
            "error": "unfillable_at_visible_depth",
            "mid": mid,
            "wall_notional": wall_notional,
            "visible_levels": len(levels),
        }

    vwap, _ = walked
    slippage_bps = (
        (vwap - mid) / mid * BPS if side is Side.BUY else (mid - vwap) / mid * BPS
    )
    return {
        "mid": mid,
        "best_bid": best_bid,
        "best_ask": best_ask,
        "spread_bps": (best_ask - best_bid) / mid * BPS,
        "vwap": vwap,
        "slippage_bps": slippage_bps,
        "taker_fee_bps": taker_fee_bps,
        "cost_bps": slippage_bps + taker_fee_bps,
        "wall_notional": wall_notional,
        "visible_levels": len(levels),
    }


def pair_round_trip(
    spot_book: object,
    perp_book: object,
    *,
    notional: Decimal,
    spot_taker_bps: Decimal,
    perp_taker_bps: Decimal,
) -> dict:
    """Cost of opening and closing one delta-neutral pair at `notional` a leg.

    Four legs against two books: the open lifts spot asks and hits perp bids,
    the close hits spot bids and lifts perp asks. All four are charged, because
    a carry pair that is never closed has not been measured.
    """
    books = {MarketType.SPOT: spot_book, MarketType.PERPETUAL: perp_book}
    fees = {MarketType.SPOT: spot_taker_bps, MarketType.PERPETUAL: perp_taker_bps}

    legs: dict[str, dict] = {}
    for label, market_type, side in ROUND_TRIP_LEGS:
        legs[label] = leg_cost(
            books[market_type],
            side=side,
            notional=notional,
            taker_fee_bps=fees[market_type],
        )

    walls = [
        leg["wall_notional"] for leg in legs.values() if leg.get("wall_notional") is not None
    ]
    wall = min(walls) if walls else None

    unfillable = {label: leg["error"] for label, leg in legs.items() if "error" in leg}
    if unfillable:
        return {
            "notional": notional,
            "legs": legs,
            "wall_notional": wall,
            "error": "unfillable",
            "unfillable_legs": unfillable,
        }

    slippage_bps = sum((leg["slippage_bps"] for leg in legs.values()), Decimal(0))
    fee_published_bps = 2 * spot_taker_bps + 2 * perp_taker_bps
    fee_flat_bps = 4 * FLAT_TAKER_BPS

    out = {"notional": notional, "legs": legs, "wall_notional": wall}
    out["slippage_bps"] = slippage_bps
    for model, fee in (("published", fee_published_bps), ("flat", fee_flat_bps)):
        round_trip = slippage_bps + fee
        out[f"fee_bps_{model}"] = fee
        out[f"round_trip_bps_{model}"] = round_trip
        out[f"annual_cost_bps_{model}"] = round_trip * REBALANCES_PER_YEAR
        out[f"net_edge_bps_{model}"] = (
            EDGE_BPS_PER_YEAR - round_trip * REBALANCES_PER_YEAR
        )
    return out


def capacity(
    spot_book: object,
    perp_book: object,
    *,
    spot_taker_bps: Decimal,
    perp_taker_bps: Decimal,
    model: str = "published",
    budget_bps: Decimal = BUDGET_BPS_PER_ROUND_TRIP,
    floor: Decimal = MIN_NOTIONAL,
) -> dict:
    """Notional at which the round trip reaches the annual execution budget.

    Round-trip cost rises monotonically with size -- a larger order walks
    strictly further into the same levels -- so the crossing is found by
    bisection on the snapshot rather than by interpolating the ladder.

    Three outcomes, and only one of them is a number:
      `measured`              the crossing lies inside the visible book
      `beyond_visible_depth`  the book runs out before the cost does; the
                              reported notional is the wall, a LOWER BOUND on
                              capacity, not capacity
      `below_floor`           the venue minimum already costs more than the
                              edge can pay, so no size works
    """

    def cost(notional: Decimal) -> Decimal | None:
        rt = pair_round_trip(
            spot_book,
            perp_book,
            notional=notional,
            spot_taker_bps=spot_taker_bps,
            perp_taker_bps=perp_taker_bps,
        )
        if "error" in rt:
            return None
        return rt[f"round_trip_bps_{model}"]

    floor_cost = cost(floor)
    if floor_cost is None:
        return {
            "status": "below_floor",
            "reason": f"the venue minimum of ${floor} does not fill in the visible book",
            "notional": None,
            "budget_bps": budget_bps,
        }
    if floor_cost >= budget_bps:
        return {
            "status": "below_floor",
            "reason": (
                f"round trip at the ${floor} venue minimum is {floor_cost:.2f} bps, "
                f"already past the {budget_bps:.2f} bps budget"
            ),
            "notional": None,
            "cost_at_floor_bps": floor_cost,
            "budget_bps": budget_bps,
        }

    probe = pair_round_trip(
        spot_book,
        perp_book,
        notional=floor,
        spot_taker_bps=spot_taker_bps,
        perp_taker_bps=perp_taker_bps,
    )
    wall = probe["wall_notional"]
    if wall is None or wall <= floor:
        return {
            "status": "below_floor",
            "reason": "no visible depth to walk",
            "notional": None,
            "budget_bps": budget_bps,
        }

    ceiling = wall * Decimal("0.999")
    ceiling_cost = cost(ceiling)
    if ceiling_cost is not None and ceiling_cost < budget_bps:
        return {
            "status": "beyond_visible_depth",
            "reason": (
                f"the visible book is exhausted at ${ceiling:,.0f} a leg where the "
                f"round trip is still only {ceiling_cost:.2f} bps of the "
                f"{budget_bps:.2f} bps budget"
            ),
            "notional": ceiling,
            "cost_at_wall_bps": ceiling_cost,
            "budget_bps": budget_bps,
        }

    lo, hi = floor, ceiling
    for _ in range(60):
        midpoint = (lo + hi) / 2
        c = cost(midpoint)
        if c is None or c >= budget_bps:
            hi = midpoint
        else:
            lo = midpoint
    return {
        "status": "measured",
        "notional": lo,
        "cost_bps": cost(lo),
        "wall_notional": wall,
        "budget_bps": budget_bps,
    }


def _frac_to_bps(value: object) -> Decimal | None:
    parsed = _decimal(value)
    if parsed is None:
        return None
    return parsed * BPS


async def sample_once(venue: CCXTVenue, assets: Sequence[str]) -> list[dict]:
    as_of = datetime.now(UTC)
    records: list[dict] = []
    for asset in assets:
        spot_symbol = venue.symbol_for(asset, MarketType.SPOT)
        perp_symbol = venue.symbol_for(asset, MarketType.PERPETUAL)
        if spot_symbol is None or perp_symbol is None:
            records.append(
                {
                    "ts": as_of.isoformat(),
                    "venue": venue.name,
                    "asset": asset,
                    "error": "no_pair",
                    "reason": (
                        f"spot={spot_symbol!r} perp={perp_symbol!r}; a carry pair "
                        f"needs both legs listed"
                    ),
                }
            )
            continue

        spot_market = venue._market(spot_symbol)
        perp_market = venue._market(perp_symbol)
        spot_taker = _frac_to_bps(spot_market.get("taker"))
        perp_taker = _frac_to_bps(perp_market.get("taker"))
        if spot_taker is None or perp_taker is None:
            records.append(
                {
                    "ts": as_of.isoformat(),
                    "venue": venue.name,
                    "asset": asset,
                    "error": "no_published_fee",
                    "reason": (
                        f"spot taker={spot_market.get('taker')!r} perp "
                        f"taker={perp_market.get('taker')!r}; refusing to assume one"
                    ),
                }
            )
            continue

        try:
            spot_book = await venue._exchange.fetch_order_book(spot_symbol)
            perp_book = await venue._exchange.fetch_order_book(perp_symbol)
        except NetworkError as exc:
            records.append(
                {
                    "ts": as_of.isoformat(),
                    "venue": venue.name,
                    "asset": asset,
                    "error": "venue_unavailable",
                    "reason": f"{type(exc).__name__}: {exc}",
                }
            )
            continue

        ladder = [
            pair_round_trip(
                spot_book,
                perp_book,
                notional=size,
                spot_taker_bps=spot_taker,
                perp_taker_bps=perp_taker,
            )
            for size in LADDER
        ]
        record = {
            "ts": as_of.isoformat(),
            "venue": venue.name,
            "asset": asset,
            "spot_symbol": spot_symbol,
            "perp_symbol": perp_symbol,
            "spot_taker_bps": spot_taker,
            "perp_taker_bps": perp_taker,
            "spot_spread_bps": None,
            "perp_spread_bps": None,
            "ladder": ladder,
        }
        spot_touch = touch(spot_book)
        perp_touch = touch(perp_book)
        if spot_touch is not None:
            record["spot_spread_bps"] = (spot_touch[1] - spot_touch[0]) / spot_touch[2] * BPS
            record["spot_mid"] = spot_touch[2]
        if perp_touch is not None:
            record["perp_spread_bps"] = (perp_touch[1] - perp_touch[0]) / perp_touch[2] * BPS
            record["perp_mid"] = perp_touch[2]
        for model in ("published", "flat"):
            record[f"capacity_{model}"] = capacity(
                spot_book,
                perp_book,
                spot_taker_bps=spot_taker,
                perp_taker_bps=perp_taker,
                model=model,
            )
        records.append(record)
    return records


async def run_samples(
    venue: CCXTVenue,
    assets: Sequence[str],
    samples: int,
    interval: int,
    on_round: Callable[[list[dict]], None] | None = None,
) -> list[dict]:
    """Sample `samples` times, handing each round to `on_round` as it completes.

    Persisting per round rather than at the end is not tidiness. A multi-hour
    sampling run that writes once at the end loses every snapshot it took when
    the venue drops one request twenty minutes in, which is how the first run of
    this probe lost 24 minutes of book history to a single `No route to host`.
    """
    out: list[dict] = []
    for i in range(samples):
        records = await sample_once(venue, assets)
        if on_round is not None:
            on_round(records)
        out.extend(records)
        if i + 1 < samples and interval > 0:
            await asyncio.sleep(interval)
    return out


def _stats(values: list[Decimal]) -> dict:
    return {
        "n": len(values),
        "median": statistics.median(values),
        "min": min(values),
        "max": max(values),
    }


def summarize(records: list[dict], model: str = "published") -> dict:
    by_asset: dict[str, list[dict]] = defaultdict(list)
    skipped: list[dict] = []
    for r in records:
        if "error" in r:
            skipped.append(r)
            continue
        by_asset[r["asset"]].append(r)

    out: dict[str, dict] = {}
    for asset, rows in by_asset.items():
        ladder: dict[str, dict] = {}
        for index, size in enumerate(LADDER):
            costs = [
                Decimal(str(row["ladder"][index][f"round_trip_bps_{model}"]))
                for row in rows
                if f"round_trip_bps_{model}" in row["ladder"][index]
            ]
            unfillable = sum(1 for row in rows if "error" in row["ladder"][index])
            entry: dict = {"snapshots": len(rows), "unfillable": unfillable}
            if costs:
                entry["round_trip_bps"] = _stats(costs)
                entry["annual_cost_bps"] = _stats(
                    [c * REBALANCES_PER_YEAR for c in costs]
                )
                entry["net_edge_bps"] = _stats(
                    [EDGE_BPS_PER_YEAR - c * REBALANCES_PER_YEAR for c in costs]
                )
            ladder[f"${int(size):,}"] = entry

        caps = [row[f"capacity_{model}"] for row in rows]
        measured = [Decimal(str(c["notional"])) for c in caps if c["notional"] is not None]
        out[asset] = {
            "snapshots": len(rows),
            "spot_spread_bps": _stats(
                [Decimal(str(r["spot_spread_bps"])) for r in rows if r.get("spot_spread_bps") is not None]
            )
            if any(r.get("spot_spread_bps") is not None for r in rows)
            else None,
            "perp_spread_bps": _stats(
                [Decimal(str(r["perp_spread_bps"])) for r in rows if r.get("perp_spread_bps") is not None]
            )
            if any(r.get("perp_spread_bps") is not None for r in rows)
            else None,
            "ladder": ladder,
            "capacity_status": sorted({c["status"] for c in caps}),
            "capacity_notional": _stats(measured) if measured else None,
        }
    out["_model"] = {
        "fee_model": model,
        "budget_bps_per_round_trip": BUDGET_BPS_PER_ROUND_TRIP,
        "rebalances_per_year": REBALANCES_PER_YEAR,
        "edge_bps_per_year": EDGE_BPS_PER_YEAR,
    }
    out["_skipped"] = {"count": len(skipped), "assets": [r["asset"] for r in skipped]}
    return out


def _jsonable(value: object) -> object:
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, dict):
        return {k: _jsonable(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_jsonable(v) for v in value]
    return value


def _append_jsonl(path: Path, records: list[dict]) -> None:
    with path.open("a") as fh:
        for r in records:
            fh.write(json.dumps(_jsonable(r)) + "\n")


def _print_summary(summary: dict) -> None:
    model = summary["_model"]
    print(
        f"\nedge {model['edge_bps_per_year']} bps/yr, "
        f"{model['rebalances_per_year']:.3f} rebalances/yr "
        f"-> budget {model['budget_bps_per_round_trip']:.2f} bps per round trip "
        f"({model['fee_model']} fees)"
    )
    for asset, block in summary.items():
        if asset.startswith("_"):
            continue
        spot = block["spot_spread_bps"]
        perp = block["perp_spread_bps"]
        print(f"\n{asset}  ({block['snapshots']} snapshots)")
        if spot and perp:
            print(
                f"  spread    spot {spot['median']:.2f} bps "
                f"[{spot['min']:.2f}, {spot['max']:.2f}]   "
                f"perp {perp['median']:.2f} bps "
                f"[{perp['min']:.2f}, {perp['max']:.2f}]"
            )
        print(f"  {'size':>10}  {'round trip':>22}  {'annual cost':>14}  {'net edge':>14}")
        for label, entry in block["ladder"].items():
            if "round_trip_bps" not in entry:
                print(f"  {label:>10}  {'unfillable at visible depth':>22}")
                continue
            rt = entry["round_trip_bps"]
            ann = entry["annual_cost_bps"]
            net = entry["net_edge_bps"]
            flag = "" if net["median"] > 0 else "   NEGATIVE"
            partial = f"  ({entry['unfillable']} unfillable)" if entry["unfillable"] else ""
            print(
                f"  {label:>10}  {rt['median']:>8.2f} bps "
                f"[{rt['min']:.2f}, {rt['max']:.2f}]  "
                f"{ann['median']:>9.0f} bps  {net['median']:>9.0f} bps{flag}{partial}"
            )
        cap = block["capacity_notional"]
        status = ", ".join(block["capacity_status"])
        if cap is None:
            print(f"  capacity  none ({status})")
        else:
            print(
                f"  capacity  ${cap['median']:,.0f} a leg "
                f"[${cap['min']:,.0f}, ${cap['max']:,.0f}]   ({status})"
            )
    skipped = summary["_skipped"]
    if skipped["count"]:
        print(f"\nskipped: {skipped['count']} {skipped['assets']}")


async def _run_measure(args: argparse.Namespace) -> int:
    venue = await CCXTVenue.connect(
        venue="hyperliquid",
        quote_asset="USDC",
        mode=TradingMode.READ_ONLY,
    )
    written = 0

    def flush(records: list[dict]) -> None:
        nonlocal written
        _append_jsonl(args.path, records)
        written += len(records)
        print(f"wrote {len(records)} records to {args.path} ({written} total)")

    try:
        records = await run_samples(
            venue, args.assets, args.samples, args.interval, on_round=flush
        )
    finally:
        await venue.aclose()
    _print_summary(summarize(records, args.model))
    return 0


def _run_summarize(args: argparse.Namespace) -> int:
    if not args.path.exists():
        print(f"no data at {args.path}", file=sys.stderr)
        return 1
    records = [
        json.loads(line) for line in args.path.read_text().splitlines() if line.strip()
    ]
    _print_summary(summarize(records, args.model))
    return 0


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = p.add_subparsers(dest="cmd", required=True)

    m = sub.add_parser("measure", help="fetch live books and price the round trip")
    m.add_argument("--samples", type=int, default=1)
    m.add_argument("--interval", type=int, default=300, help="seconds between snapshots")
    m.add_argument("--assets", nargs="+", default=list(CARRY_UNIVERSE))
    m.add_argument("--model", choices=("published", "flat"), default="published")
    m.add_argument("--path", type=Path, default=DEFAULT_PATH)

    s = sub.add_parser("summarize", help="report from accumulated snapshots")
    s.add_argument("--model", choices=("published", "flat"), default="published")
    s.add_argument("--path", type=Path, default=DEFAULT_PATH)

    args = p.parse_args()
    if args.cmd == "measure":
        return asyncio.run(_run_measure(args))
    return _run_summarize(args)


if __name__ == "__main__":
    raise SystemExit(main())
