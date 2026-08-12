"""Count active Yes/No markets right now + sample resolution-rate estimate.

The paper trader's annual trade flow is:

    annual_trades ~= active_yesno_markets
                     × estimated_resolution_rate_per_month
                     × 12
                     × threshold_conversion_rate

The first two terms are properties of Polymarket; the third is what the
threshold sweep in run_stage_a.py measures. This script measures the first
two by paging through Gamma's open markets and counting Yes/No ones.

Usage:

    uv run python ops/count_universe.py
    uv run python ops/count_universe.py --max-pages 10
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from datetime import UTC, datetime
from pathlib import Path

import httpx


def _load_env(path: Path) -> None:
    if not path.is_file():
        return
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        import os
        os.environ.setdefault(k.strip(), v.strip())


_load_env(Path(__file__).resolve().parent.parent / ".env")

from omni.ingest.protocol import Unavailable
from omni.polymarket.active import list_active_markets


def _stderr(msg: str) -> None:
    print(msg, file=sys.stderr, flush=True)


async def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--max-pages", type=int, default=10)
    p.add_argument("--min-volume", type=float, default=0.0)
    args = p.parse_args()

    total = 0
    by_category: dict[str, int] = {}
    by_volume_bucket: dict[str, int] = {"< $1k": 0, "$1k-$10k": 0, "$10k-$100k": 0, "$100k+": 0}
    end_dates: list[datetime] = []

    async with httpx.AsyncClient(timeout=30.0) as client:
        for page in range(args.max_pages):
            try:
                batch = await list_active_markets(
                    client,
                    limit=100,
                    offset=page * 100,
                    min_volume=args.min_volume,
                    strict=False,
                )
            except (Unavailable, httpx.HTTPError) as exc:
                _stderr(f"page {page + 1} failed: {exc}")
                break
            if not batch:
                _stderr(f"page {page + 1}: empty — exhausted.")
                break
            _stderr(f"page {page + 1}: +{len(batch)} Yes/No (cumulative {total + len(batch)})")
            for m in batch:
                total += 1
                cat = m.category or "Unknown"
                by_category[cat] = by_category.get(cat, 0) + 1
                if m.volume < 1000:
                    by_volume_bucket["< $1k"] += 1
                elif m.volume < 10000:
                    by_volume_bucket["$1k-$10k"] += 1
                elif m.volume < 100000:
                    by_volume_bucket["$10k-$100k"] += 1
                else:
                    by_volume_bucket["$100k+"] += 1
                if m.end_date is not None:
                    end_dates.append(m.end_date)

    print(f"\nUniverse snapshot — {datetime.now(UTC).isoformat()}")
    print(f"  Total active Yes/No markets seen: {total}")
    print("\n  By category (top 10):")
    for cat, n in sorted(by_category.items(), key=lambda x: -x[1])[:10]:
        print(f"    {n:>5}  {cat}")

    print("\n  By volume bucket:")
    for bucket, n in by_volume_bucket.items():
        pct = (n / total * 100) if total else 0
        print(f"    {n:>5}  {bucket:<12} ({pct:.1f}%)")

    if end_dates:
        now = datetime.now(UTC)
        from collections import Counter
        months_ahead = Counter()
        for d in end_dates:
            delta_days = (d - now).total_seconds() / 86400
            if delta_days < 0:
                months_ahead["past due"] += 1
            elif delta_days < 30:
                months_ahead["< 1 month"] += 1
            elif delta_days < 90:
                months_ahead["1-3 months"] += 1
            elif delta_days < 180:
                months_ahead["3-6 months"] += 1
            else:
                months_ahead["6+ months"] += 1
        print("\n  Time-to-resolution distribution (by end_date):")
        for bucket in ["past due", "< 1 month", "1-3 months", "3-6 months", "6+ months"]:
            n = months_ahead.get(bucket, 0)
            print(f"    {n:>5}  {bucket}")

    print(f"\n  Estimated resolution within 30 days: ~{months_ahead.get('< 1 month', 0) + months_ahead.get('past due', 0)}")
    print(f"  Annualized (×12): ~{(months_ahead.get('< 1 month', 0) + months_ahead.get('past due', 0)) * 12}")
    print("\n  Multiply by your threshold conversion rate (from --threshold-list)")
    print("  to estimate annual trade count.")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(asyncio.run(main()))
    except KeyboardInterrupt:
        sys.exit(130)
