"""YouTuber prediction probe: do crypto YouTube calls have any predictive value?

HYPOTHESIS (pre-registered):
    Crypto YouTube influencers make directional calls about assets. If those
    calls have ANY predictive power (positive or inverse), a crude keyword
    extraction from transcripts should produce a signal that clears the gate.

    Two directions are tested:
    - Follow: buy what they're bullish on, sell what they're bearish on
    - Fade (inverse): sell what they're bullish on, buy what they're bearish on

WHAT IT MEASURES:
    1. Searches YouTube for crypto prediction/analysis videos
    2. Fetches transcripts for each
    3. Extracts per-asset sentiment (bullish/bearish keyword counts)
    4. Builds a daily sentiment panel aligned with crypto prices
    5. Runs the research harness (evaluate()) at horizons 1/3/7/14/30 days
    6. Reports the verdict against the carry book's ~11%/yr

WHAT IT DOES NOT DO:
    Ingest transcripts into the claim store. This is a measurement-only probe.
    Prices come from the claim store (ccxt). Transcripts come from YouTube
    directly and are never stored as claims.

DATA NOTE:
    Transcript extraction is deliberately crude — keyword matching, not NLP.
    If crude extraction finds nothing, sophisticated extraction won't either
    (the bias runs the other way: crude extraction UNDERSTATES any real signal).
    If crude extraction DOES find something, it's worth building proper NLP.

Run on deployment-host:
    pip install --target=/tmp/yflib --no-deps youtube-transcript-api yt-dlp
    PYTHONPATH=/tmp/yflib python ops/youtuber_probe.py
"""

from __future__ import annotations

import asyncio
import re
from collections import defaultdict
from datetime import UTC, datetime
from typing import Any

import pandas as pd

HORIZONS = (1, 3, 7, 14, 30)
COST_BPS = 23.0
QUANTILE = 5

ASSET_ALIASES: dict[str, list[str]] = {
    "BTC": ["bitcoin", "btc"],
    "ETH": ["ethereum", "eth"],
    "SOL": ["solana", "sol"],
    "ADA": ["cardano", "ada"],
    "DOT": ["polkadot", "dot"],
    "AVAX": ["avalanche", "avax"],
    "LINK": ["chainlink", "link"],
    "XRP": ["ripple", "xrp"],
    "DOGE": ["dogecoin", "doge"],
    "BNB": ["bnb"],
    "MATIC": ["polygon", "matic"],
    "ARB": ["arbitrum", "arb"],
    "OP": ["optimism"],
    "LTC": ["litecoin", "ltc"],
    "ATOM": ["cosmos", "atom"],
    "NEAR": ["near protocol", "near"],
    "APT": ["aptos", "apt"],
    "FIL": ["filecoin", "fil"],
    "INJ": ["injective", "inj"],
    "SUI": ["sui"],
}

BULLISH_WORDS = [
    "bullish", "buy", "accumulate", "undervalued", "breakout", "pump",
    "moon", "rally", "surge", "soar", "bull market", "bottom is in",
    "reversal", "uptrend", "going up", "buy the dip", "support held",
    "super cycle", "this is huge", "massive opportunity",
]
BEARISH_WORDS = [
    "bearish", "sell", "overvalued", "breakdown", "dump",
    "crash", "correction", "bubble", "collapse", "bear market",
    "resistance", "top is in", "downtrend", "going down", "distribution",
    "dangerous", "stay away", "be careful", "warning",
]

SEARCH_QUERIES = [
    "bitcoin price prediction",
    "ethereum analysis",
    "crypto market prediction",
    "altcoin prediction",
    "crypto price target",
    "bitcoin crash or rally",
    "best crypto to buy",
    "crypto market analysis",
]
VIDEOS_PER_QUERY = 15


def _database_url() -> str:
    try:
        from omni.config import settings
        if settings.database_url:
            return settings.database_url
    except ImportError:
        pass
    return "postgresql://postgres:postgres@localhost:5434/omni_v2"


def _registry():
    import os

    from omni.research.registry import Registry
    path = os.environ.get("OMNI_REGISTRY_PATH")
    return Registry(path=path) if path else Registry()


def discover_videos() -> list[dict[str, Any]]:
    """Search YouTube for crypto prediction videos using yt-dlp.

    Two-pass: flat search to get IDs fast, then individual lookups for dates
    (flat search doesn't return upload_date).
    """
    from yt_dlp import YoutubeDL

    raw_ids: list[str] = []
    seen: set[str] = set()

    flat_opts = {
        "quiet": True, "no_warnings": True,
        "extract_flat": True, "skip_download": True,
    }
    for query in SEARCH_QUERIES:
        search = f"ytsearch{VIDEOS_PER_QUERY}:{query}"
        try:
            with YoutubeDL(flat_opts) as ydl:
                result = ydl.extract_info(search, download=False)
            for e in (result.get("entries", []) if result else []):
                if e and e.get("id") and e["id"] not in seen:
                    seen.add(e["id"])
                    raw_ids.append(e["id"])
        except Exception as exc:  # noqa: BLE001
            print(f"  search '{query}' failed: {type(exc).__name__}: {str(exc)[:80]}")

    print(f"  flat search found {len(raw_ids)} unique IDs, fetching dates...")

    detail_opts = {
        "quiet": True, "no_warnings": True,
        "skip_download": True,
        "noplaylist": True,
    }
    videos: list[dict[str, Any]] = []
    for i, vid in enumerate(raw_ids):
        if i % 20 == 0 and i > 0:
            print(f"    {i}/{len(raw_ids)}...")
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
            videos.append({
                "id": vid,
                "title": info.get("title", ""),
                "date": dt,
            })
        except Exception:  # noqa: BLE001, S112
            continue

    videos.sort(key=lambda v: v["date"], reverse=True)
    print(f"discovered {len(videos)} unique videos "
          f"({videos[0]['date'].date() if videos else 'n/a'} -> "
          f"{videos[-1]['date'].date() if videos else 'n/a'})")
    return videos


def fetch_transcript(video_id: str) -> str | None:
    """Fetch a video's transcript, returning the full text or None."""
    try:
        from youtube_transcript_api import YouTubeTranscriptApi

        yta = YouTubeTranscriptApi()
        transcript = yta.fetch(video_id=video_id)
        return " ".join(snippet.text for snippet in transcript)
    except Exception:  # noqa: BLE001
        return None


def extract_sentiment(text: str) -> dict[str, float]:
    """Extract per-asset net sentiment from transcript text.

    Returns {ASSET: net_score} where net_score is in [-1, 1].
    Uses a windowed approach: for each asset mention, counts directional
    keywords within a context window around the mention.
    """
    text_lower = text.lower()
    text_lower.split()

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
        if total >= 2:
            results[asset] = (bull_count - bear_count) / total

    return results


def build_sentiment_panel(videos: list[dict]) -> pd.DataFrame:
    """Build a date-indexed, asset-columned sentiment panel from videos."""
    daily_scores: dict[pd.Timestamp, dict[str, float]] = defaultdict(lambda: defaultdict(list))

    fetched = 0
    skipped = 0
    for i, video in enumerate(videos):
        if i % 10 == 0:
            print(f"  transcript {i+1}/{len(videos)}...")
        text = fetch_transcript(video["id"])
        if text is None or len(text) < 100:
            skipped += 1
            continue
        fetched += 1

        scores = extract_sentiment(text)
        date = pd.Timestamp(video["date"]).tz_localize(None).normalize()
        for asset, score in scores.items():
            daily_scores[date][asset].append(score)

    print(f"  fetched {fetched} transcripts, skipped {skipped}")

    if not daily_scores:
        return pd.DataFrame()

    rows = {}
    for date, asset_scores in sorted(daily_scores.items()):
        row = {}
        for asset, scores in asset_scores.items():
            row[asset] = sum(scores) / len(scores)
        rows[date] = row

    panel = pd.DataFrame(rows).T.sort_index()
    panel = panel.groupby(level=0).last()
    return panel


def load_price_panel(assets: list[str]) -> pd.DataFrame:
    """Load crypto daily prices from yfinance (measurement-only, not ingested).

    yfinance covers years of history for all major crypto assets, while the
    claim store's ccxt data spans only 45 days and 6 assets. This probe never
    ingests prices as claims -- same pattern as door_b_yfinance_probe.py.
    """
    import yfinance as yf

    ticker_map = {a: f"{a}-USD" for a in assets}
    tickers = list(ticker_map.values())
    ticker_str = " ".join(tickers)

    print(f"  fetching yfinance prices for {len(tickers)} assets...")
    df = yf.download(ticker_str, period="2y", interval="1d",
                     auto_adjust=True, progress=False, group_by="ticker")
    if df.empty:
        return pd.DataFrame()

    frames = {}
    if len(tickers) == 1:
        sym = assets[0]
        if "Close" in df.columns:
            frames[sym] = df["Close"].dropna()
    else:
        for asset, ticker in ticker_map.items():
            if ticker in df.columns.get_level_values(0):
                col = df[ticker]["Close"]
                s = col.dropna() if hasattr(col, "dropna") else None
                if s is not None and not s.empty:
                    frames[asset] = s

    if not frames:
        return pd.DataFrame()

    panel = pd.DataFrame(frames).sort_index()
    panel.index = pd.to_datetime(panel.index).tz_localize(None).normalize()
    panel = panel.groupby(level=0).last()
    return panel


async def main() -> int:

    print("=" * 72)
    print("YOUTUBER PREDICTION PROBE (pre-registered)")
    print("=" * 72)

    # Step 1: discover videos
    print("\n1. Discovering crypto YouTube videos...")
    videos = discover_videos()
    if len(videos) < 10:
        print("too few videos discovered; aborting")
        return 1

    # Step 2: fetch transcripts + extract sentiment
    print(f"\n2. Fetching transcripts for {len(videos)} videos...")
    sentiment = build_sentiment_panel(videos)
    if sentiment.empty:
        print("no sentiment extracted; transcripts may be unavailable or no assets mentioned")
        return 1

    print(f"   sentiment panel: {sentiment.shape[0]} dates x {sentiment.shape[1]} assets")
    print(f"   date range: {sentiment.index.min().date()} -> {sentiment.index.max().date()}")
    finite = sentiment.notna().sum().sum()
    print(f"   finite cells: {finite}")

    # Step 3: load price panel from yfinance
    print("\n3. Loading crypto prices from yfinance...")
    assets = list(sentiment.columns)
    prices = load_price_panel(assets)

    if prices.empty:
        print("no matching price data in claim store")
        return 1

    common_assets = sorted(set(prices.columns) & set(sentiment.columns))
    print(f"   price panel: {prices.shape[0]} dates x {prices.shape[1]} assets")
    print(f"   overlapping assets with sentiment: {len(common_assets)}")

    if len(common_assets) < 3:
        print("too few overlapping assets for a cross-sectional signal")
        return 1

    prices = prices[common_assets]
    sentiment = sentiment[common_assets]

    # Step 4: evaluate through the research harness
    print(f"\n4. Evaluating signal (horizons={HORIZONS}, cost={COST_BPS} bps)...")
    print("   testing BOTH follow and fade directions")

    from omni.research.harness import evaluate

    def follow_signal(p: pd.DataFrame) -> pd.DataFrame:
        return sentiment.reindex(index=p.index, columns=p.columns).ffill()

    def fade_signal(p: pd.DataFrame) -> pd.DataFrame:
        return -sentiment.reindex(index=p.index, columns=p.columns).ffill()

    print("\n   --- FOLLOW (buy what they're bullish on) ---")
    follow_verdicts = evaluate(
        name="youtube.sentiment.follow",
        source="youtube_transcripts",
        signal=follow_signal,
        prices=prices,
        horizons=HORIZONS,
        cost_bps=COST_BPS,
        quantile=QUANTILE,
        registry=_registry(),
        record=False,
    )
    for v in follow_verdicts:
        print(f"   {v.summary()}")
        for w in v.warnings:
            print(f"     warn: {w}")

    print("\n   --- FADE (inverse: sell what they're bullish on) ---")
    fade_verdicts = evaluate(
        name="youtube.sentiment.fade",
        source="youtube_transcripts",
        signal=fade_signal,
        prices=prices,
        horizons=HORIZONS,
        cost_bps=COST_BPS,
        quantile=QUANTILE,
        registry=_registry(),
        record=False,
    )
    for v in fade_verdicts:
        print(f"   {v.summary()}")
        for w in v.warnings:
            print(f"     warn: {w}")

    # Step 5: verdict
    print("\n" + "=" * 72)
    print("VERDICT")
    print("=" * 72)

    follow_passes = [v for v in follow_verdicts if v.passed]
    fade_passes = [v for v in fade_verdicts if v.passed]

    best_follow = max(follow_verdicts, key=lambda v: v.t_stat) if follow_verdicts else None
    best_fade = max(fade_verdicts, key=lambda v: v.t_stat) if fade_verdicts else None

    if follow_passes:
        print(f"FOLLOW PASSES at h={follow_passes[0].horizon}")
    elif fade_passes:
        print(f"FADE PASSES at h={fade_passes[0].horizon}")
    elif best_follow and best_fade:
        follow_t = best_follow.t_stat
        fade_t = best_fade.t_stat
        if abs(follow_t) > abs(fade_t):
            print(f"FAIL. Best follow |t|={abs(follow_t):.2f} (bar={best_follow.bar:.2f})")
        else:
            print(f"FAIL. Best fade |t|={abs(fade_t):.2f} (bar={best_fade.bar:.2f})")
        print("   carry book baseline: ~11%/yr on notional, t=36.0")
        print(f"   youtuber best: follow t={follow_t:.2f}, fade t={fade_t:.2f}")
    else:
        print("FAIL. Insufficient data to evaluate.")

    print("=" * 72)
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
