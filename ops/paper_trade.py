"""Paper trader CLI. Three subcommands: scan, resolve, report.

Usage:

    uv pip install openai
    export GLM_API_KEY=...

    # open paper positions on markets the LLM disagrees with
    uv run python ops/paper_trade.py scan --target-markets 50

    # close out positions whose markets have resolved
    uv run python ops/paper_trade.py resolve

    # rolling P&L summary
    uv run python ops/paper_trade.py report

The JSONL log lives at `ops/.paper_trades.jsonl` by default and is
gitignored — it accumulates state across runs. Override with `--log`.

Every parameter below has a stated default rather than an implicit one:

  --target-markets 50     scans up to 50 active Yes/No markets per pass;
                          Gamma's open market universe is much smaller than
                          its resolved one, so 50 is the upper useful bound
  --threshold      0.05   minimum |llm_prob - market_price| to open a paper
                          position; below this the LLM has no disagreement
                          worth tracking
  --size-usd        5.0   per-trade stake in USD; equal-weight across signals
                          (no Kelly, no confidence weighting — a real trader
                          would size by edge, the paper trader does not)
  --min-volume   1000.0   filter out micro-volume markets whose mid price is
                          noise; the resolved Stage A runs used 0 because
                          those are historical, this is forward so liquidity
                          matters

The paper trader is **maker-only by default**: positions are recorded at the
market's current mid, with fees set to zero. The `--taker` switch records
positions with the V2 taker fee curve applied (`C × feeRate × p × (1-p)`)
for the worst-case P&L. Both run off the same JSONL log; compare them via
two scans with different `--method` tags.

NO LIVE ORDERS. NO REAL MONEY. This is a forward backtest.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
from pathlib import Path

import httpx

_HERE = Path(__file__).resolve().parent
_PROJECT_ROOT = _HERE.parent


def _load_env(path: Path) -> None:
    if not path.is_file():
        return
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip())


_load_env(_PROJECT_ROOT / ".env")

from omni.ingest.protocol import Unavailable
from omni.polymarket.glm_adapter import OpenAICompatibleLanguageModel
from omni.polymarket.news import (
    compose_providers,
    gamma_description_provider,
    gdelt_news_provider,
)
from omni.polymarket.paper import (
    DEFAULT_SIZE_USD,
    DEFAULT_THRESHOLD,
    report,
    resolve,
    scan,
)


def _stderr(msg: str) -> None:
    print(msg, file=sys.stderr, flush=True)


def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Polymarket paper trader")
    sub = p.add_subparsers(dest="mode", required=True)

    common_log = lambda sp: sp.add_argument("--log", type=Path, default=_PROJECT_ROOT / "ops" / ".paper_trades.jsonl")

    sp_scan = sub.add_parser("scan", help="open paper positions on disagreeing markets")
    sp_scan.add_argument("--target-markets", type=int, default=50)
    sp_scan.add_argument("--threshold", type=float, default=DEFAULT_THRESHOLD)
    sp_scan.add_argument("--size-usd", type=float, default=DEFAULT_SIZE_USD)
    sp_scan.add_argument("--min-volume", type=float, default=1000.0)
    sp_scan.add_argument("--max-pages", type=int, default=5)
    sp_scan.add_argument("--model", default="glm-5.2")
    sp_scan.add_argument("--thinking", choices=("max", "auto", "none"), default="auto")
    sp_scan.add_argument("--method", default="polymarket_paper_glm_5_2")
    sp_scan.add_argument("--max-articles", type=int, default=5)
    sp_scan.add_argument("--taker", action="store_true",
                         help="record with taker fee curve instead of maker-only")
    sp_scan.add_argument("--no-news", action="store_true")
    common_log(sp_scan)

    sp_resolve = sub.add_parser("resolve", help="close out positions whose markets resolved")
    common_log(sp_resolve)

    sp_report = sub.add_parser("report", help="print rolling P&L summary")
    common_log(sp_report)

    return p.parse_args()


def _build_model(args):
    thinking_type = None if getattr(args, "thinking", "auto") == "none" else getattr(args, "thinking", "auto")
    return OpenAICompatibleLanguageModel(model=args.model, thinking_type=thinking_type)


def _build_document_provider(args):
    if args.no_news:
        return None
    def _gdelt(client, market, cutoff):
        return gdelt_news_provider(client, market, cutoff, max_articles=args.max_articles)
    return compose_providers(gamma_description_provider, _gdelt)


async def _do_scan(args) -> int:
    model = _build_model(args)
    document_provider = _build_document_provider(args)
    timeout = httpx.Timeout(30.0, connect=10.0)
    n_seen = n_opened = 0
    async with httpx.AsyncClient(timeout=timeout) as client:
        _stderr(f"scanning up to {args.target_markets} active Yes/No markets (threshold={args.threshold})...")
        try:
            n_seen, n_opened = await scan(
                client, model, args.log,
                target_markets=args.target_markets,
                threshold=args.threshold,
                size_usd=args.size_usd,
                method=args.method,
                min_volume=args.min_volume,
                document_provider=document_provider,
            )
        except (httpx.HTTPError, Unavailable) as exc:
            _stderr(f"scan failed: {exc}")
            return 3
    _stderr(f"scanned {n_seen} markets; opened {n_opened} paper positions.")
    return 0


async def _do_resolve(args) -> int:
    timeout = httpx.Timeout(30.0, connect=10.0)
    async with httpx.AsyncClient(timeout=timeout) as client:
        _stderr("resolving open positions...")
        try:
            n_resolved, n_open = await resolve(client, args.log)
        except (httpx.HTTPError, Unavailable) as exc:
            _stderr(f"resolve failed: {exc}")
            return 3
    _stderr(f"resolved {n_resolved}; {n_open} still open.")
    return 0


def _do_report(args) -> int:
    summary = report(args.log)
    print(f"\nPaper trader report — log: {summary['log_path']}")
    print(f"  open positions:   {summary['n_open']}")
    print(f"  closed positions: {summary['n_closed']}")
    if summary["n_closed"] > 0:
        wr = summary["win_rate"]
        wr_str = f"{wr * 100:.1f}%" if wr is not None else "-"
        print(f"  win rate:         {wr_str}")
        print(f"  gross P&L:        ${summary['gross_pnl']:+.2f}")
        print(f"  fees:             ${summary['fee_pnl']:+.2f}")
        print(f"  net P&L:          ${summary['net_pnl']:+.2f}")
        if summary["avg_roi_pct"] is not None:
            print(f"  avg ROI/trade:    {summary['avg_roi_pct']:+.2f}%")
        print(f"  worst drawdown:   ${summary['worst_drawdown_usd']:.2f}")

    if summary["open_positions"]:
        print("\n  Open positions (showing first 10):")
        for op in summary["open_positions"][-10:]:
            edge = op["llm_prob"] - op["market_prob"]
            print(
                f"    [{op['ts'][:10]}] {op['direction']} @ {op['entry_price']:.3f} "
                f"(edge {edge:+.3f}, ${op['size_usd']:.2f}) "
                f"{op['question'][:60]}"
            )
    return 0


async def main() -> int:
    args = _parse_args()
    if args.mode == "scan":
        return await _do_scan(args)
    if args.mode == "resolve":
        return await _do_resolve(args)
    if args.mode == "report":
        return _do_report(args)
    _stderr(f"unknown mode: {args.mode}")
    return 2


if __name__ == "__main__":
    try:
        sys.exit(asyncio.run(main()))
    except KeyboardInterrupt:
        sys.exit(130)
