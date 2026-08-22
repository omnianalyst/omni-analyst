"""Track a public caller's crypto calls, honestly, from call time forward.

WHY THIS SHAPE
The channel (t.me/official_d3f4ult) mixes third-party calls with promotion
of tokens the caller himself launches (MezzanineDAO launchpad, the HoodBot
funnel). Following-or-fading either half without separating them measures
nothing, so every call is classified before it enters the ledger:

  third_party  the token is not the caller's own product -- the auditable set
  ecosystem    the token or post ties to the caller's own launches -- excluded
               from any verdict, kept in the ledger so the exclusion itself is
               on record

WHAT IS RECORDED (append-only, never rewritten)
  the call      posted_at, text excerpt, symbol, chain, contract, class
  observations  {ts, price_usd, liquidity_usd, volume_h24_usd} snapshots at
                every run -- the forward record that cannot be backfilled
  entry         attempted ONCE per call from GeckoTerminal pool OHLC (the
                bar containing posted_at). Many microcaps are not indexed;
                entry_recoverable=false is the honest state and the call is
                still measured forward from first observation. No guessed
                entries, ever -- a fabricated entry price is a fabricated
                verdict.

SOURCES (free, keyless)
  t.me/s/<channel>      public preview HTML; <time datetime> per message
  DexScreener tokens    current price/liquidity/volume per contract
  GeckoTerminal OHLC    pool candles where indexed, for entry recovery

VERDICTS
This tracker records; it does not judge. When the forward record is long
enough, a registry battery on follower returns (entry at first observation,
costs, hold windows) is the honest test -- predeclared, run once.

Run:  uv run python ops/call_tracker.py          (laptop; prints summary)
      ops/call_tracker.sh                        (host cron wrapper)
Ledger: docs/research/calls_ledger.jsonl (committed)
"""

from __future__ import annotations

import asyncio
import hashlib
import html as _html
import json
import re
import sys
from datetime import UTC, datetime
from pathlib import Path

import httpx

CHANNEL = "official_d3f4ult"
CHANNEL_URL = f"https://t.me/s/{CHANNEL}"
def _arg(name: str, default: str) -> str:
    return sys.argv[sys.argv.index(name) + 1] if name in sys.argv else default


_LEDGER_PATH = str(
    Path(__file__).resolve().parents[1] / "docs" / "research" / "calls_ledger.jsonl"
)
# Read path and write path are separable because the host cron copies the
# ledger INTO the container (arriving root-owned; readable, not writable by
# the app user) and copies the result back OUT from a fresh path.
LEDGER = Path(_arg("--ledger", _LEDGER_PATH))
LEDGER_OUT = Path(_arg("--out", _arg("--ledger", _LEDGER_PATH)))

# The caller's own products. A call is ecosystem when its post promotes these
# or the token is one of them; those calls cannot be audited for information
# because the caller controls the supply he is calling.
ECOSYSTEM_TOKENS = {
    # launched on launchpad.mezzanine.fund by the channel itself
    "0x46544163f545a8e306e2671528ffc1b0ef8fe8cf": "flork (own launchpad)",
    # the HoodBot funnel token
    "0x8e62f281f282686fca6dcb39288069a93fc23f1c": "hoodrat (own bot funnel)",
}
ECOSYSTEM_MARKERS = (
    "mezzanine",
    "hoodbot",
    "launchpad.mezzanine",
    "referral",
)

_ETH_RE = re.compile(r"0x[a-fA-F0-9]{40}")
_SOL_RE = re.compile(r"\b[1-9A-HJ-NP-Za-km-z]{32,44}\b")
_TICKER_RE = re.compile(r"\$([a-zA-Z0-9]{2,10})")

_POST_SPLIT = "tgme_widget_message "
_TIME_RE = re.compile(r'datetime="([^"]+)"')
_TEXT_RE = re.compile(
    r'class="tgme_widget_message_text[^"]*"[^>]*>(.*?)</div>', re.S
)
_TAG_RE = re.compile(r"<[^>]+>")


def _strip(html: str) -> str:
    return _html.unescape(_TAG_RE.sub(" ", html))


async def _posts(client: httpx.AsyncClient) -> list[tuple[datetime, str]]:
    response = await client.get(CHANNEL_URL)
    response.raise_for_status()
    posts: list[tuple[datetime, str]] = []
    for block in response.text.split(_POST_SPLIT)[1:]:
        t = _TIME_RE.search(block)
        m = _TEXT_RE.search(block)
        if not t or not m:
            continue
        try:
            when = datetime.fromisoformat(t.group(1))
        except ValueError:
            continue
        posts.append((when, _strip(m.group(1))))
    return posts


def _extract_calls(text: str) -> list[dict]:
    """Third-party-shaped calls in one post: contract + ticker, unclassified."""
    out = []
    eth = _ETH_RE.findall(text)
    sol = [
        a
        for a in _SOL_RE.findall(text)
        if not a.lower().startswith(("http", "www"))
    ]
    tickers = _TICKER_RE.findall(text)
    for address in eth:
        out.append({
            "address": address,
            "chain": "ethereum-l2",
            "symbol": (tickers[0].lower() if tickers else "?"),
        })
    for address in sol:
        # base58 solana mints; reject things that are clearly urls
        out.append({
            "address": address,
            "chain": "solana",
            "symbol": (tickers[0].lower() if tickers else "?"),
        })
    return out


def _classify(text_lower: str, address: str) -> tuple[str, str | None]:
    if address.lower() in ECOSYSTEM_TOKENS:
        return "ecosystem", ECOSYSTEM_TOKENS[address.lower()]
    if any(marker in text_lower for marker in ECOSYSTEM_MARKERS):
        return "ecosystem", "post promotes the caller's own funnel"
    return "third_party", None


def _call_id(posted_at: datetime, address: str) -> str:
    seed = f"{posted_at.isoformat()}|{address.lower()}"
    return hashlib.sha1(seed.encode()).hexdigest()[:12]


async def _dex_snapshot(client: httpx.AsyncClient, address: str) -> dict | None:
    try:
        r = await client.get(
            f"https://api.dexscreener.com/latest/dex/tokens/{address}"
        )
        r.raise_for_status()
        pairs = r.json().get("pairs") or []
        if not pairs:
            return None
        best = max(
            pairs, key=lambda p: float(p.get("liquidity", {}).get("usd") or 0)
        )
        return {
            "price_usd": float(best["priceUsd"]),
            "liquidity_usd": float(best["liquidity"]["usd"]),
            "volume_h24_usd": float(best["volume"].get("h24") or 0),
            "dex": best["dexId"],
        }
    except Exception:  # noqa: BLE001 - a snapshot may fail; the run continues
        return None


async def _recover_entry(
    client: httpx.AsyncClient, chain: str, address: str, posted_at: datetime
) -> float | None:
    """The bar containing posted_at, from GeckoTerminal, or None.

    Politeness: one search + one OHLC per call, spaced; most microcaps on
    week-old L2s are simply not indexed and None is the honest answer.
    """
    # gecko's network ids; the ethereum-l2 bucket is a fallback guess -- a
    # week-old L2 is usually absent and None (unrecoverable) is the outcome.
    network = "solana" if chain == "solana" else "eth"
    try:
        await asyncio.sleep(1.2)
        s = await client.get(
            f"https://api.geckoterminal.com/api/v2/networks/{network}"
            f"/tokens/{address}/pools"
        )
        s.raise_for_status()
        pools = s.json().get("data") or []
        if not pools:
            return None
        pool_id = pools[0]["id"]
        network = pool_id.rsplit("_", 1)[0] if "_" in pool_id else network
        await asyncio.sleep(1.2)
        o = await client.get(
            f"https://api.geckoterminal.com/api/v2/networks/{network}"
            f"/pools/{pool_id}/ohlcs/hour",
            params={"aggregate": 4, "limit": 1000},
        )
        o.raise_for_status()
        bars = o.json().get("data", {}).get("ohlcs") or []
        target = posted_at.timestamp()
        for bar in bars:
            if bar["ts"] <= target <= bar["ts"] + 4 * 3600:
                return float(bar["c"])
    except Exception:  # noqa: BLE001
        return None
    return None


async def run() -> int:
    now = datetime.now(UTC)
    ledger: dict[str, dict] = {}
    if LEDGER.exists():
        for line in LEDGER.read_text().splitlines():
            if line.strip():
                rec = json.loads(line)
                ledger[rec["id"]] = rec

    async with httpx.AsyncClient(timeout=30.0) as client:
        posts = await _posts(client)
        print(f"posts scraped: {len(posts)}")

        new_calls = 0
        for posted_at, text in posts:
            lowered = text.lower()
            for call in _extract_calls(text):
                cid = _call_id(posted_at, call["address"])
                if cid in ledger:
                    continue
                classification, reason = _classify(lowered, call["address"])
                rec = {
                    "id": cid,
                    "source": f"t.me/{CHANNEL}",
                    "posted_at": posted_at.isoformat(),
                    "symbol": call["symbol"],
                    "chain": call["chain"],
                    "address": call["address"],
                    "classification": classification,
                    **({"exclusion_reason": reason} if reason else {}),
                    "text_excerpt": text.strip()[:160],
                    "first_seen": now.isoformat(),
                    "entry_recoverable": False,
                    "entry_price_usd": None,
                    "observations": [],
                }
                ledger[cid] = rec
                new_calls += 1

        for rec in ledger.values():
            if rec["classification"] != "third_party":
                continue
            # Entry recovery: attempted once, only while still unknown.
            if rec["entry_price_usd"] is None:
                entry = await _recover_entry(
                    client, rec["chain"], rec["address"],
                    datetime.fromisoformat(rec["posted_at"]),
                )
                if entry is not None:
                    rec["entry_price_usd"] = entry
                    rec["entry_recoverable"] = True
            snap = await _dex_snapshot(client, rec["address"])
            if snap is not None and rec.get("symbol") in (None, "?"):
                try:
                    detail = await client.get(
                        f"https://api.dexscreener.com/latest/dex/tokens/{rec['address']}"
                    )
                    pairs = detail.json().get("pairs") or []
                    if pairs:
                        rec["symbol"] = (
                            pairs[0]["baseToken"]["symbol"] or "?"
                        ).lower()
                except Exception:  # noqa: BLE001 - cosmetic enrichment
                    pass
            if snap is not None:
                last = rec["observations"][-1]["ts"] if rec["observations"] else None
                if last is None or snap["price_usd"] != rec["observations"][-1].get("price_usd"):
                    rec["observations"].append({"ts": now.isoformat(), **snap})
            await asyncio.sleep(1.2)

    LEDGER.parent.mkdir(parents=True, exist_ok=True)
    LEDGER_OUT.parent.mkdir(parents=True, exist_ok=True)
    with LEDGER_OUT.open("w") as fh:
        for rec in ledger.values():
            fh.write(json.dumps(rec) + "\n")

    print(f"ledger: {len(ledger)} calls ({new_calls} new) -> {LEDGER_OUT}")
    third = [r for r in ledger.values() if r["classification"] == "third_party"]
    eco = len(ledger) - len(third)
    print(f"  third_party: {len(third)}  ecosystem-excluded: {eco}\n")
    for rec in sorted(third, key=lambda r: r["posted_at"]):
        obs = rec["observations"]
        entry = rec.get("entry_price_usd")
        latest = obs[-1] if obs else None
        if entry and latest:
            move = (latest["price_usd"] / entry - 1) * 100
            entry_line = f"entry ${entry:.6g} -> now ${latest['price_usd']:.6g} ({move:+.1f}%)"
        elif latest:
            entry_line = (
                f"entry unrecoverable; first obs ${latest['price_usd']:.6g}"
            )
        else:
            entry_line = "no price source"
        print(
            f"  {rec['posted_at'][:16]}  ${rec['symbol']:<10} "
            f"{obs and len(obs) or 0} obs  {entry_line}"
        )
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(run()))
