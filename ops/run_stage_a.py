"""Stage A calibration runner: resolved Polymarket markets -> GLM 5.2 -> Brier.

Usage:

    uv pip install openai              # one-time
    export GLM_API_KEY=...             # or add to .env
    uv run python ops/run_stage_a.py

    # smaller / cheaper first run:
    uv run python ops/run_stage_a.py --limit 25 --thinking auto
    # full run with max thinking:
    uv run python ops/run_stage_a.py --limit 200 --thinking max

Every parameter below is stated rather than defaulted, and each is here
because a default would have been wrong:

  --limit         50   first run is plumbing validation, not a paper. 50
                       markets x ~3s/call at thinking=auto is ~3 minutes
                       and ~$0 of GLM cost. Scale after the plumbing works.
  --min-volume  1000   filters markets too thin to carry an honest price
                       history. A market that resolved on $200 of volume can
                       have a benchmark, but not one that generalises.
  --thinking   auto    the default for the FIRST run. max is the requested
                       production setting, but max on a broken plumbing run
                       burns tokens for nothing. Switch to max once the
                       first 50 come back clean.
  --horizon-days 7     cutoff = resolution_date - 7d. Long enough that
                       pre-cutoff evidence exists for most markets, short
                       enough that the LLM has to actually forecast.
  --max-concurrent 1   sequential. Concurrency cuts wall time but the GLM
                       gateway rate-limits aggressively and a 429 mid-run
                       aborts the whole batch. Concurrency is a follow-up
                       once the rate budget is measured.

The runner prints progress to stderr so a tee'd stdout file holds only the
report. Ctrl-C aborts cleanly between markets (the LLM call in flight is
lost; the markets already estimated are not, but they are also not
persisted — Stage A v1 is in-memory only).

What this DOES NOT do, deliberately:
- No disk cache of LLM responses. Re-runs repay the LLM cost. The prompt
  fingerprint exists (see `llm/protocol.py`) and POLY3 can hang a cache off
  it; POLY2 does not.
- No write to the prediction table. Calibration is pure analysis; the
  prediction ledger remains the crypto system's territory.
- No Stage B. No fills, no fees, no P&L beyond Brier.

Honest expectation: the first run will likely produce exclusions for 30-50%
of markets. The CLOB price-history endpoint is thin on older or low-volume
markets. The report's `exclusions` section breaks this down by reason; if
"no price sample within tolerance" dominates, widen `--tolerance-hours` or
narrow `--min-volume`.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from collections import Counter
from datetime import timedelta
from pathlib import Path

import httpx


# Load .env without a python-dotenv dependency. The file is two lines of
# KEY=VALUE; a real parser handles quoting/comments but the project's .env
# uses neither, and pulling in dotenv for this is over-engineering.
def _load_env(path: Path) -> None:
    if not path.is_file():
        return
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip())


_load_env(Path(__file__).resolve().parent.parent / ".env")

from omni.ingest.protocol import Unavailable
from omni.polymarket.calibrate import (
    DEFAULT_TOLERANCE,
    Exclusion,
    MarketAtCutoff,
    prepare_snapshot,
    run_stage_a,
)
from omni.polymarket.gamma import list_resolved_markets_until
from omni.polymarket.glm_adapter import OpenAICompatibleLanguageModel
from omni.polymarket.news import (
    compose_providers,
    derive_category,
    gamma_description_provider,
    gdelt_news_provider,
)


def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Polymarket Stage A calibration runner")
    p.add_argument("--limit", type=int, default=100,
                   help="per-page fetch size (Gamma caps at 100)")
    p.add_argument("--target-markets", type=int, default=100,
                   help="stop paging once this many Yes/No markets are parsed")
    p.add_argument("--max-pages", type=int, default=10,
                   help="safety cap on pages to fetch (default 10 = up to 1000 raw markets)")
    p.add_argument("--min-volume", type=float, default=0.0)
    p.add_argument("--category", action="append", default=None,
                   help="restrict to category (repeatable); default all")
    p.add_argument("--model", default="glm-5.2")
    p.add_argument("--thinking", choices=("max", "auto", "none"), default="auto")
    p.add_argument("--no-news", action="store_true",
                   help="disable Gamma description + GDELT news; use market question only")
    p.add_argument("--article-body", action="store_true",
                   help="also fetch full article body via Jina Reader (slower, richer context)")
    p.add_argument("--max-articles", type=int, default=5,
                   help="max GDELT articles per market (default 5)")
    p.add_argument("--by-category", action="store_true",
                   help="run the harness once per derived category and print a per-category summary")
    p.add_argument("--min-per-category", type=int, default=5,
                   help="skip categories with fewer than this many snapshots (default 5)")
    p.add_argument("--pnl-threshold", type=float, default=0.05,
                   help="min |llm_prob - mkt_price| to count a trade in the P&L backtest (default 0.05 = 5%%)")
    p.add_argument("--size-usd", type=float, default=5.0,
                   help="per-trade stake for the P&L backtest (default $5)")
    p.add_argument("--taker", action="store_true",
                   help="use taker fee curve for the P&L backtest (default maker=0)")
    p.add_argument("--horizon-days", type=int, default=7)
    p.add_argument("--tolerance-hours", type=int, default=6)
    p.add_argument("--max-concurrent", type=int, default=1)
    p.add_argument("--method", default="polymarket_glm_5_2")
    p.add_argument("--json", action="store_true",
                   help="emit JSON to stdout instead of a human report")
    return p.parse_args()


def _stderr(msg: str) -> None:
    print(msg, file=sys.stderr, flush=True)


async def _gather_snapshots(
    client: httpx.AsyncClient,
    markets: list,
    *,
    horizon: timedelta,
    tolerance: timedelta,
    document_provider=None,
) -> tuple[list[MarketAtCutoff], list[Exclusion]]:
    snapshots: list[MarketAtCutoff] = []
    exclusions: list[Exclusion] = []
    for i, market in enumerate(markets):
        try:
            result = await prepare_snapshot(
                client, market,
                horizon=horizon, tolerance=tolerance,
                document_provider=document_provider,
            )
        except (Unavailable, ValueError) as exc:
            result = Exclusion(market=market, reason=f"prepare failed: {exc}")
        if isinstance(result, Exclusion):
            exclusions.append(result)
        else:
            snapshots.append(result)
        if (i + 1) % 10 == 0 or i + 1 == len(markets):
            _stderr(f"  prepared {i + 1}/{len(markets)} ({len(snapshots)} ok, {len(exclusions)} excluded)")
    return snapshots, exclusions


def _print_human_report(report, prep_exclusions: list[Exclusion], args: argparse.Namespace) -> None:
    print(f"\nStage A calibration — method={report.method}")
    print(f"  estimated:  {report.n_estimated}")
    print(f"  excluded:   {report.n_excluded} (LLM) + {len(prep_exclusions)} (prep)")

    if report.brier_score is not None:
        print(f"\n  brier (LLM):     {report.brier_score:.4f}")
    else:
        print("\n  brier (LLM):     n/a (no estimates)")
    if report.market_brier_score is not None:
        print(f"  brier (market):  {report.market_brier_score:.4f}")
    if report.brier_edge is not None:
        sign = "+" if report.brier_edge >= 0 else ""
        print(f"  edge (LLM-mkt):  {sign}{report.brier_edge:.4f}   ({'LLM WORSE' if report.brier_edge > 0 else 'LLM BETTER' if report.brier_edge < 0 else 'tie'})")
    if report.log_loss is not None:
        print(f"  log loss (LLM):  {report.log_loss:.4f}")

    if report.pnl_summary is not None and report.pnl_summary.n_closed > 0:
        ps = report.pnl_summary
        wr = ps.win_rate
        wr_str = f"{wr * 100:.1f}%" if wr is not None else "-"
        roi = ps.avg_roi_pct if ps.avg_roi_pct is not None else 0.0
        print(f"\n  Backtest P&L (threshold={args.pnl_threshold}, size=${args.size_usd}, "
              f"{'taker' if args.taker else 'maker'}):")
        print(f"    trades closed: {ps.n_closed} ({wr_str} win rate)")
        print(f"    gross P&L:     ${ps.gross_pnl:+.2f}")
        print(f"    fees:          ${ps.fee_pnl:+.2f}")
        print(f"    net P&L:       ${ps.net_pnl:+.2f}")
        print(f"    avg ROI/trade: {roi:+.2f}%")
        print(f"    worst DD:      ${ps.worst_drawdown_usd:.2f}")
    elif report.pnl_summary is not None:
        print(f"\n  Backtest P&L: 0 trades crossed threshold={args.pnl_threshold}")

    print(f"\nCalibration buckets ({report.method}):")
    print(f"  {'bucket':<12} {'n':>5} {'hit':>6} {'mean_conf':>10} {'mkt_mean':>10} {'n_bm':>5}")
    for buckets in report.method_buckets.values():
        for b in buckets:
            hit = f"{b.hit_rate:.3f}" if b.hit_rate is not None else "  -  "
            conf = f"{b.mean_confidence:.3f}" if b.mean_confidence is not None else "  -  "
            mkt = f"{b.market_mean_probability:.3f}" if b.market_mean_probability is not None else "  -  "
            print(f"  {b.bucket_low:.1f}-{b.bucket_high:.1f}     {b.n:>5} {hit:>6} {conf:>10} {mkt:>10} {b.benchmarked_n:>5}")

    all_exclusions = list(report.exclusions) + prep_exclusions
    if all_exclusions:
        reason_counts = Counter(e.reason.split(":", 1)[0] for e in all_exclusions)
        print("\nExclusion reasons (top 5):")
        for reason, n in reason_counts.most_common(5):
            print(f"  {n:>4}x  {reason}")


def _print_json_report(report, prep_exclusions: list[Exclusion]) -> None:
    payload = {
        "method": report.method,
        "n_estimated": report.n_estimated,
        "n_excluded_llm": report.n_excluded,
        "n_excluded_prep": len(prep_exclusions),
        "brier_score": report.brier_score,
        "market_brier_score": report.market_brier_score,
        "brier_edge": report.brier_edge,
        "log_loss": report.log_loss,
        "method_buckets": {
            method: [
                {
                    "bucket_low": b.bucket_low,
                    "bucket_high": b.bucket_high,
                    "n": b.n,
                    "hit_rate": b.hit_rate,
                    "mean_confidence": b.mean_confidence,
                    "market_mean_probability": b.market_mean_probability,
                    "benchmarked_n": b.benchmarked_n,
                }
                for b in buckets
            ]
            for method, buckets in report.method_buckets.items()
        },
        "exclusions": [
            {"market_id": e.market.condition_id, "reason": e.reason}
            for e in list(report.exclusions) + prep_exclusions
        ],
    }
    print(json.dumps(payload, indent=2))


def _build_document_provider(args: argparse.Namespace):
    """Construct the document provider chain from flags. Returns None for the
    bare-question baseline (`--no-news`)."""
    if args.no_news:
        _stderr("document provider: question only (--no-news)")
        return None

    from omni.polymarket.news import article_body_provider

    def _gdelt_with_max(client, market, cutoff):
        return gdelt_news_provider(
            client, market, cutoff, max_articles=args.max_articles,
        )

    def _body_with_max(client, market, cutoff):
        return article_body_provider(
            client, market, cutoff, max_articles=max(1, args.max_articles // 2),
        )

    providers = [gamma_description_provider, _gdelt_with_max]
    if args.article_body:
        providers.append(_body_with_max)
        _stderr(
            f"document provider: gamma description + GDELT titles "
            f"+ {max(1, args.max_articles // 2)} article bodies via Jina Reader"
        )
    else:
        _stderr(f"document provider: gamma description + GDELT (max {args.max_articles} titles)")
    return compose_providers(*providers)


async def _run_single(model, snapshots, args) -> tuple:
    """One harness run over all snapshots. Returns (report,)."""
    from omni.polymarket.calibrate import run_stage_a
    report = await run_stage_a(
        model, snapshots, method=args.method,
        pnl_threshold=args.pnl_threshold,
        pnl_size_usd=args.size_usd,
        pnl_taker=args.taker,
    )
    return (report,)


async def _run_per_category(model, snapshots, args) -> dict[str, object]:
    """Run the harness once per derived category. Categories with fewer than
    `--min-per-category` (default 5) snapshots are skipped — a per-category
    Brier on n=2 describes nothing and would dilute the summary table."""
    from collections import defaultdict

    from omni.polymarket.calibrate import run_stage_a

    groups: dict[str, list] = defaultdict(list)
    for snap in snapshots:
        groups[derive_category(snap.market)].append(snap)

    min_per = max(2, getattr(args, "min_per_category", 5))
    reports: dict[str, object] = {}
    for cat, group_snaps in sorted(groups.items()):
        if len(group_snaps) < min_per:
            _stderr(f"  skipping {cat}: only {len(group_snaps)} snapshots (min {min_per})")
            continue
        _stderr(f"  running {cat}: {len(group_snaps)} snapshots...")
        report = await run_stage_a(
            model, group_snaps, method=f"{args.method}:{cat}",
            pnl_threshold=args.pnl_threshold,
            pnl_size_usd=args.size_usd,
            pnl_taker=args.taker,
        )
        reports[cat] = report
    return reports


def _print_per_category_summary(reports: dict[str, object], prep_exclusions, args) -> None:
    print(f"\nPer-category Stage A summary — base method={args.method}")
    print(
        f"  {'category':<14} {'n':>5} {'brier_llm':>10} {'brier_mkt':>10} "
        f"{'edge':>9} {'log_loss':>9}   P&L (n_trades, net, roi)"
    )
    for cat, report in reports.items():
        bl = f"{report.brier_score:.4f}" if report.brier_score is not None else "-"
        bm = f"{report.market_brier_score:.4f}" if report.market_brier_score is not None else "-"
        ll = f"{report.log_loss:.4f}" if report.log_loss is not None else "-"
        if report.brier_edge is not None:
            edge = f"{report.brier_edge:+.4f}"
        else:
            edge = "-"
        pnl_str = "-"
        if report.pnl_summary is not None and report.pnl_summary.n_closed > 0:
            ps = report.pnl_summary
            roi = ps.avg_roi_pct if ps.avg_roi_pct is not None else 0.0
            pnl_str = f"({ps.n_closed}, ${ps.net_pnl:+.2f}, {roi:+.1f}%)"
        elif report.pnl_summary is not None and report.pnl_summary.n_closed == 0:
            pnl_str = "(0 trades below threshold)"
        print(
            f"  {cat:<14} {report.n_estimated:>5} {bl:>10} {bm:>10} {edge:>9} {ll:>9}   {pnl_str}"
        )
    print(f"\n  prep exclusions: {len(prep_exclusions)}")
    for cat, report in reports.items():
        if report.exclusions:
            print(f"    {cat}: {len(report.exclusions)} LLM exclusions")


async def main() -> int:
    args = _parse_args()

    thinking_type = None if args.thinking == "none" else args.thinking
    try:
        model = OpenAICompatibleLanguageModel(
            model=args.model,
            thinking_type=thinking_type,
        )
    except (ImportError, ValueError) as exc:
        _stderr(f"cannot initialise model: {exc}")
        return 2

    timeout = httpx.Timeout(30.0, connect=10.0)
    async with httpx.AsyncClient(timeout=timeout) as client:
        _stderr(
            f"fetching up to {args.target_markets} Yes/No markets from Gamma "
            f"(page size {args.limit}, max {args.max_pages} pages)..."
        )
        skipped_during_fetch: list[str] = []

        def _on_skip(raw, exc):
            market_id = str(raw.get("id") or raw.get("slug") or "?")
            skipped_during_fetch.append(f"{market_id}: {exc}")

        def _on_page(idx, n):
            _stderr(f"  page {idx + 1}: +{n} parsed (running total to filter)")

        try:
            markets = await list_resolved_markets_until(
                client,
                target_count=args.target_markets,
                page_size=args.limit,
                max_pages=args.max_pages,
                categories=args.category,
                min_volume=args.min_volume,
                on_skip=_on_skip,
                on_page=_on_page,
            )
        except (httpx.HTTPError, Unavailable) as exc:
            _stderr(f"gamma fetch failed: {exc}")
            return 3
        _stderr(
            f"got {len(markets)} Yes/No markets "
            f"({len(skipped_during_fetch)} skipped at parse); building snapshots..."
        )

        horizon = timedelta(days=args.horizon_days)
        tolerance = timedelta(hours=args.tolerance_hours) if args.tolerance_hours > 0 else DEFAULT_TOLERANCE
        document_provider = _build_document_provider(args)

        snapshots, prep_exclusions = await _gather_snapshots(
            client, markets, horizon=horizon, tolerance=tolerance,
            document_provider=document_provider,
        )
        _stderr(f"prepared {len(snapshots)} snapshots ({len(prep_exclusions)} excluded at prep)")
        _stderr(f"running GLM estimation (model={args.model}, thinking={args.thinking})...")

        try:
            if args.by_category:
                reports = await _run_per_category(model, snapshots, args)
                if not reports:
                    _stderr("no category had enough snapshots to run; aborting.")
                    return 4
                if args.json:
                    payload = {
                        cat: {
                            "method": r.method,
                            "n_estimated": r.n_estimated,
                            "brier_score": r.brier_score,
                            "market_brier_score": r.market_brier_score,
                            "brier_edge": r.brier_edge,
                            "log_loss": r.log_loss,
                            "n_excluded": r.n_excluded,
                        }
                        for cat, r in reports.items()
                    }
                    print(json.dumps(payload, indent=2))
                else:
                    _print_per_category_summary(reports, prep_exclusions, args)
            else:
                report = await run_stage_a(model, snapshots, method=args.method)
                if args.json:
                    _print_json_report(report, prep_exclusions)
                else:
                    _print_human_report(report, prep_exclusions, args)
        except KeyboardInterrupt:
            _stderr("interrupted; no report.")
            return 130

    return 0


if __name__ == "__main__":
    try:
        sys.exit(asyncio.run(main()))
    except KeyboardInterrupt:
        sys.exit(130)
