"""YouTuber prediction probe v2: targeted channels + caching + event study.

Tests whether specific crypto YouTubers' calls have predictive value, both
aggregately and per-channel. Designed for Benjamin Cowen and VirtualBacon
specifically, with a general sample for comparison.

DESIGN:
    1. Discover videos from targeted channel searches + general crypto search
    2. Fetch transcripts with caching (never re-fetch the same video)
    3. Tag each video by uploader
    4. Extract per-asset directional sentiment
    5. Aggregate cross-sectional test through evaluate()
    6. Per-channel event study: each call -> forward return -> win rate + APR
    7. Report both aggregate and per-channel results

Run locally (YouTube blocks server IPs):
    OMNI_REGISTRY_PATH=_orchestrator/hypothesis_registry.jsonl \\
    uv run python ops/youtuber_probe.py

Re-run (uses cached transcripts, only fetches new videos):
    uv run python ops/youtuber_probe.py
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import time
from collections import defaultdict
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

HORIZONS = (1, 3, 7, 14, 30, 63)
COST_BPS = 23.0
CACHE_DIR = Path(os.environ.get("YT_CACHE_DIR", "/tmp/yt_transcripts"))
FETCH_DELAY = 2.0

TARGET_SEARCHES = [
    ("benjamin_cowen", "Benjamin Cowen bitcoin analysis"),
    ("benjamin_cowen", "Benjamin Cowen ethereum"),
    ("benjamin_cowen", "Benjamin Cowen crypto market"),
    ("virtual_bacon", "VirtualBacon crypto analysis"),
    ("virtual_bacon", "VirtualBacon bitcoin"),
    ("virtual_bacon", "VirtualBacon altcoin"),
    ("general", "crypto price prediction"),
    ("general", "bitcoin analysis"),
    ("general", "ethereum price prediction"),
    ("general", "crypto market analysis"),
    ("general", "best crypto to buy now"),
]
VIDEOS_PER_QUERY = 10

ASSET_ALIASES: dict[str, list[str]] = {
    "BTC": ["bitcoin", "btc"],
    "ETH": ["ethereum", "eth", "ether"],
    "SOL": ["solana", "sol"],
    "ADA": ["cardano", "ada"],
    "DOT": ["polkadot", "dot"],
    "AVAX": ["avalanche", "avax"],
    "LINK": ["chainlink", "link"],
    "XRP": ["ripple", "xrp"],
    "DOGE": ["dogecoin", "doge"],
    "BNB": ["bnb", "binance coin"],
    "MATIC": ["polygon", "matic"],
    "ARB": ["arbitrum", "arb"],
    "OP": ["optimism"],
    "LTC": ["litecoin", "ltc"],
    "ATOM": ["cosmos", "atom"],
    "NEAR": ["near protocol", "near "],
    "APT": ["aptos", "apt"],
    "FIL": ["filecoin", "fil"],
    "INJ": ["injective", "inj"],
    "SUI": ["sui"],
}

BULLISH_WORDS = [
    "bullish", "buy", "accumulate", "undervalued", "breakout",
    "pump", "moon", "rally", "surge", "soar", "bull market",
    "bottom is in", "reversal", "uptrend", "going up", "buy the dip",
    "support held", "opportunity", "long term hold",
]
BEARISH_WORDS = [
    "bearish", "sell", "overvalued", "breakdown", "dump",
    "crash", "correction", "bubble", "collapse", "bear market",
    "top is in", "downtrend", "going down", "distribution",
    "dangerous", "stay away", "be careful", "warning", "risk",
]

YFINANCE_TICKERS = {a: f"{a}-USD" for a in ASSET_ALIASES}


def _registry():
    from omni.research.registry import Registry
    path = os.environ.get("OMNI_REGISTRY_PATH")
    return Registry(path=path) if path else Registry()


def discover_videos() -> list[dict[str, Any]]:
    """Two-pass: flat search for IDs, then full extraction for dates + uploader."""
    from yt_dlp import YoutubeDL

    raw: list[tuple[str, str]] = []
    seen: set[str] = set()

    flat_opts = {
        "quiet": True, "no_warnings": True,
        "extract_flat": True, "skip_download": True,
    }
    for target, query in TARGET_SEARCHES:
        search = f"ytsearch{VIDEOS_PER_QUERY}:{query}"
        try:
            with YoutubeDL(flat_opts) as ydl:
                result = ydl.extract_info(search, download=False)
            for e in (result.get("entries", []) if result else []):
                if e and e.get("id") and e["id"] not in seen:
                    seen.add(e["id"])
                    raw.append((e["id"], target))
        except Exception:  # noqa: BLE001, S110
            pass

    print(f"  flat search: {len(raw)} IDs, fetching metadata...")
    detail_opts = {
        "quiet": True, "no_warnings": True,
        "skip_download": True, "noplaylist": True,
    }
    videos: list[dict[str, Any]] = []
    for i, (vid, target) in enumerate(raw):
        if i % 20 == 0 and i > 0:
            print(f"    {i}/{len(raw)}...")
        try:
            with YoutubeDL(detail_opts) as ydl:
                info = ydl.extract_info(
                    f"https://www.youtube.com/watch?v={vid}", download=False
                )
            upload_date = info.get("upload_date", "")
            if not upload_date:
                continue
            dt = datetime.strptime(upload_date, "%Y%m%d").replace(tzinfo=UTC)
            if dt < datetime(2024, 1, 1, tzinfo=UTC):
                continue
            uploader = info.get("uploader") or info.get("channel") or target
            videos.append({
                "id": vid,
                "title": info.get("title", ""),
                "date": dt,
                "uploader": uploader,
                "target": target,
            })
        except Exception:  # noqa: BLE001, S112
            continue

    videos.sort(key=lambda v: v["date"], reverse=True)
    return videos


def _cache_path(video_id: str) -> Path:
    return CACHE_DIR / f"{video_id}.json"


def fetch_transcript_cached(video: dict[str, Any]) -> str | None:
    """Fetch transcript with disk cache. Never re-downloads a cached video."""
    vid = video["id"]
    cached = _cache_path(vid)
    if cached.exists():
        try:
            data = json.loads(cached.read_text())
            return data.get("text")
        except Exception:  # noqa: BLE001, S110
            pass

    try:
        from youtube_transcript_api import YouTubeTranscriptApi

        yta = YouTubeTranscriptApi()
        transcript = yta.fetch(video_id=vid)
        text = " ".join(snippet.text for snippet in transcript)
    except Exception:  # noqa: BLE001
        return None

    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    cached.write_text(json.dumps({
        "text": text,
        "title": video.get("title", ""),
        "date": video["date"].isoformat(),
        "uploader": video.get("uploader", ""),
    }))
    time.sleep(FETCH_DELAY)
    return text


def extract_sentiment(text: str, *, min_signals: int = 2) -> dict[str, float]:
    """Per-asset net sentiment in [-1, 1] using windowed keyword counting.

    ``min_signals`` defaults to 2 (for transcripts). Pass 1 for title-only
    analysis where the text is much shorter.
    """
    text_lower = text.lower()
    results: dict[str, float] = {}

    for asset, aliases in ASSET_ALIASES.items():
        bull_count = 0
        bear_count = 0

        for alias in aliases:
            pattern = re.escape(alias)
            for match in re.finditer(pattern, text_lower):
                start = max(0, match.start() - 200)
                end = min(len(text_lower), match.end() + 200)
                context = text_lower[start:end]
                bull_count += sum(context.count(w) for w in BULLISH_WORDS)
                bear_count += sum(context.count(w) for w in BEARISH_WORDS)

        total = bull_count + bear_count
        if total >= min_signals:
            results[asset] = (bull_count - bear_count) / total

    return results


def build_calls(
    videos: list[dict], transcripts: dict[str, str]
) -> list[dict[str, Any]]:
    """Extract individual (date, asset, direction, uploader) calls.

    When transcripts are available, uses full-text sentiment. When blocked
    (rate-limited), falls back to title-only sentiment — a weaker signal
    but still directional ('Major Rejection', 'Buy Now', 'Warning').
    """
    calls: list[dict[str, Any]] = []
    for video in videos:
        text = transcripts.get(video["id"])
        source = "transcript"
        if not text:
            text = video.get("title", "")
            source = "title"
        if not text or len(text) < 5:
            continue
        scores = extract_sentiment(text, min_signals=1 if source == "title" else 2)
        date = pd.Timestamp(video["date"]).tz_localize(None).normalize()
        for asset, score in scores.items():
            calls.append({
                "date": date,
                "asset": asset,
                "direction": 1 if score > 0 else -1,
                "sentiment": score,
                "uploader": video.get("uploader", "unknown"),
                "target": video.get("target", "general"),
                "title": video.get("title", ""),
                "source": source,
            })
    return calls


def load_prices(assets: list[str]) -> pd.DataFrame:
    import yfinance as yf

    tickers = {a: YFINANCE_TICKERS.get(a, f"{a}-USD") for a in assets}
    ticker_str = " ".join(tickers.values())
    print(f"  fetching yfinance for {len(tickers)} assets...")
    df = yf.download(ticker_str, period="2y", interval="1d",
                     auto_adjust=True, progress=False, group_by="ticker")
    if df.empty:
        return pd.DataFrame()

    frames = {}
    if len(tickers) == 1:
        asset = next(iter(tickers.keys()))
        try:
            frames[asset] = df[("Close", tickers[asset])].dropna()
        except (KeyError, TypeError):
            if "Close" in df.columns:
                frames[asset] = df["Close"].dropna()
    else:
        for asset, ticker in tickers.items():
            try:
                col = df[ticker]["Close"]
                s = col.dropna() if hasattr(col, "dropna") else None
                if s is not None and not s.empty:
                    frames[asset] = s
            except (KeyError, TypeError):
                continue

    if not frames:
        return pd.DataFrame()

    panel = pd.DataFrame(frames).sort_index()
    panel.index = pd.to_datetime(panel.index).tz_localize(None).normalize()
    panel = panel.groupby(level=0).last()
    return panel


def event_study(
    calls: list[dict], prices: pd.DataFrame, horizons: tuple[int, ...]
) -> dict[int, dict[str, float]]:
    """For each call, compute forward return × direction. Aggregate per horizon."""
    results: dict[int, dict[str, float]] = {}
    for h in horizons:
        outcomes: list[float] = []
        for call in calls:
            asset = call["asset"]
            direction = call["direction"]
            if asset not in prices.columns:
                continue
            px = prices[asset].dropna()
            call_date = call["date"]
            future_dates = px.index[px.index > call_date]
            if len(future_dates) < h:
                continue
            entry_date = px.index[px.index <= call_date]
            if len(entry_date) == 0:
                continue
            entry_idx = px.index.get_loc(entry_date[-1])
            if entry_idx + h >= len(px):
                continue
            entry_px = px.iloc[entry_idx]
            exit_px = px.iloc[entry_idx + h]
            raw_ret = (exit_px / entry_px) - 1
            signed_ret = raw_ret * direction
            outcomes.append(signed_ret)

        if len(outcomes) >= 5:
            arr = np.array(outcomes)
            win_rate = float((arr > 0).mean())
            mean_annual = float(arr.mean() * 365 / h * 100)
            se = float(arr.std(ddof=1) / np.sqrt(len(arr)) * 365 / h * 100)
            t = mean_annual / se if se > 0 else 0.0
            results[h] = {
                "n": len(outcomes),
                "win_rate": win_rate,
                "mean_pct_yr": mean_annual,
                "t_stat": t,
            }
        else:
            results[h] = {"n": len(outcomes), "win_rate": 0, "mean_pct_yr": 0, "t_stat": 0}

    return results


def print_event_study(label: str, results: dict) -> None:
    print(f"\n  {label}")
    for h in sorted(results.keys()):
        r = results[h]
        if r["n"] < 5:
            print(f"    h={h:>2d}d  n={r['n']:>4d}  (insufficient)")
        else:
            print(
                f"    h={h:>2d}d  n={r['n']:>4d}  "
                f"win={r['win_rate']:.1%}  "
                f"APR={r['mean_pct_yr']:>+7.1f}%/yr  "
                f"t={r['t_stat']:>+5.1f}"
            )


async def main() -> int:
    print("=" * 72)
    print("YOUTUBER PREDICTION PROBE v2")
    print("=" * 72)

    # Step 1: discover
    print("\n1. Discovering videos...")
    videos = discover_videos()
    if len(videos) < 10:
        print("too few videos; aborting")
        return 1
    by_target: dict[str, int] = defaultdict(int)
    for v in videos:
        by_target[v.get("target", "general")] += 1
    print(f"  {len(videos)} videos ({videos[0]['date'].date()} -> {videos[-1]['date'].date()})")
    for t, c in sorted(by_target.items()):
        print(f"    {t}: {c}")

    # Step 2: fetch transcripts (cached)
    cached = sum(1 for v in videos if _cache_path(v["id"]).exists())
    print(f"\n2. Fetching transcripts ({cached} cached, {len(videos) - cached} new)...")
    transcripts: dict[str, str] = {}
    failed = 0
    consecutive_failures = 0
    for i, video in enumerate(videos):
        if _cache_path(video["id"]).exists():
            text = fetch_transcript_cached(video)
            if text:
                transcripts[video["id"]] = text
            continue
        if consecutive_failures >= 5:
            print(f"  {failed} consecutive failures — skipping remaining, using titles")
            break
        if i % 10 == 0:
            print(f"  {i+1}/{len(videos)}...")
        text = fetch_transcript_cached(video)
        if text:
            transcripts[video["id"]] = text
            consecutive_failures = 0
        else:
            failed += 1
            consecutive_failures += 1
    print(f"  {len(transcripts)} transcripts, {failed} failed")

    # Step 3: extract calls (falls back to title-only when transcripts blocked)
    print("\n3. Extracting predictions...")
    calls = build_calls(videos, transcripts)
    title_calls = sum(1 for c in calls if c.get("source") == "title")
    transcript_calls = len(calls) - title_calls
    print(f"  {len(calls)} calls ({transcript_calls} from transcripts, {title_calls} from titles)")
    if len(calls) < 5:
        print("too few calls extracted; aborting")
        return 1
    by_uploader: dict[str, int] = defaultdict(int)
    for c in calls:
        by_uploader[c["uploader"]] += 1
    for u, count in sorted(by_uploader.items(), key=lambda kv: kv[1], reverse=True)[:10]:
        print(f"    {u}: {count} calls")

    # Step 4: load prices
    print("\n4. Loading prices...")
    assets = sorted({c["asset"] for c in calls})
    prices = load_prices(assets)
    if prices.empty:
        print("no price data")
        return 1
    print(f"  {prices.shape[0]} days x {prices.shape[1]} assets ({prices.index.min().date()} -> {prices.index.max().date()})")

    # Step 5: event study — aggregate
    print("\n5. EVENT STUDY RESULTS")
    print("   (positive APR = calls are right, negative = calls are wrong)")
    print("   carry book baseline: ~11%/yr on notional, t=36.0")

    all_results = event_study(calls, prices, HORIZONS)
    print_event_study("ALL CHANNELS (follow direction)", all_results)

    inverse_calls = [{**c, "direction": -c["direction"]} for c in calls]
    inverse_results = event_study(inverse_calls, prices, HORIZONS)
    print_event_study("ALL CHANNELS (fade / inverse)", inverse_results)

    # Step 6: per-target event study
    for target in ["benjamin_cowen", "virtual_bacon"]:
        target_calls = [c for c in calls if c.get("target") == target]
        if len(target_calls) < 5:
            print(f"\n  {target}: {len(target_calls)} calls (insufficient)")
            continue
        target_results = event_study(target_calls, prices, HORIZONS)
        print_event_study(f"{target.upper()} (follow)", target_results)
        inv_results = event_study(
            [{**c, "direction": -c["direction"]} for c in target_calls], prices, HORIZONS
        )
        print_event_study(f"{target.upper()} (fade)", inv_results)

    # Step 7: aggregate cross-sectional evaluate()
    print("\n6. CROSS-SECTIONAL EVALUATE()")
    sentiment_records: dict[pd.Timestamp, dict[str, float]] = defaultdict(dict)
    for call in calls:
        sentiment_records[call["date"]][call["asset"]] = call["sentiment"]

    if len(sentiment_records) > 10:
        sentiment = pd.DataFrame(sentiment_records).T.sort_index().groupby(level=0).last()
        common_assets = sorted(set(prices.columns) & set(sentiment.columns))
        if len(common_assets) >= 3:
            prices_sub = prices[common_assets]
            sentiment_sub = sentiment[common_assets]

            from omni.research.harness import evaluate

            def follow_signal(p: pd.DataFrame) -> pd.DataFrame:
                return sentiment_sub.reindex(index=p.index, columns=p.columns).ffill()

            print(f"\n  ALL CHANNELS — FOLLOW (cross-sectional, {len(common_assets)} assets)")
            for v in evaluate(
                name="youtube.sentiment.follow", source="youtube_transcripts",
                signal=follow_signal, prices=prices_sub,
                horizons=(7, 14, 30), cost_bps=COST_BPS, quantile=3,
                registry=_registry(), record=False,
            ):
                print(f"    {v.summary()}")
    else:
        print("  insufficient data for cross-sectional test")

    print("\n" + "=" * 72)
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
