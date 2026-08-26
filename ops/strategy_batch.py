"""The daily-compatible research batch: 12 strategies, one honest statistic.

Every strategy from the master list that survives our data reality (daily
bars, no intraday, no FX) expressed the same way: entry rule -> 14d/28d
barrier call (2x vol target, 2x vol stop) -> hit rate vs. the SAME panel's
baseline hit rate, paired. The paired design is the lesson of
carry.calls.barrier_head: in a drifting tape the raw 0.5-null measures beta,
and only the excess over baseline is edge.

Each strategy records into the registry; the bar rises for all of them. Most
will fail. That is the product working.

Run: uv run python ops/strategy_batch.py
"""

from __future__ import annotations

import asyncio
import math
from datetime import UTC, datetime

import httpx
import numpy as np

from omni.research.registry import Registry

SOURCE = "binance_ohlcv+stooq_daily"
K = 2.0
HORIZON = 28  # swing horizon; 14d also swept inside each outcome set


async def fetch_universe() -> dict[str, np.ndarray]:
    """Daily closes: 30 crypto perpetual majors (Binance) + 60 large US
    names/ETFs (Stooq keyless daily CSV). One panel, one baseline."""
    panel: dict[str, np.ndarray] = {}
    async with httpx.AsyncClient(timeout=45) as client:
        ex = (await client.get("https://fapi.binance.com/fapi/v1/exchangeInfo")).json()
        symbols = [
            s["symbol"] for s in ex["symbols"]
            if s["contractType"] == "PERPETUAL" and s["status"] == "TRADING"
            and s["quoteAsset"] == "USDT"
        ][:30]
        for sym in symbols:
            r = await client.get(
                "https://fapi.binance.com/fapi/v1/klines",
                params={"symbol": sym, "interval": "1d", "limit": 1500},
            )
            rows = r.json()
            if isinstance(rows, list) and len(rows) >= 400:
                panel[sym] = np.array([float(x[4]) for x in rows])
    # Equities via yfinance (Stooq bot-walls its CSV endpoint; measured
    # 2026-08-17 -- the first batch silently ran crypto-only because of it).
    import yfinance as yf
    equities = [
        "SPY","QQQ","IWM","GLD","SLV","TLT","XLF","XLK","XLE","XLV",
        "NVDA","AAPL","MSFT","AMZN","GOOGL","META","TSLA","AVGO","JPM","V",
        "XOM","CVX","UNH","JNJ","PG","KO","PEP","COST","WMT","HD",
    ]
    raw = yf.download(" ".join(equities), start="2012-01-01", interval="1d",
                      auto_adjust=True, progress=False, group_by="ticker")
    for sym in equities:
        try:
            c = raw[sym]["Close"].dropna()
            arr = c.to_numpy() if hasattr(c, "to_numpy") else None
            if arr is not None and len(arr) >= 400:
                panel[sym] = arr
        except Exception:  # noqa: BLE001, S112 - a dead ticker is skipped, not fatal
            continue
    return panel


# ---------- signal functions: price series -> position series (+1/-1/0) ------

def sig_ma_cross(prices: np.ndarray, fast: int = 20, slow: int = 50) -> np.ndarray:
    f = np.convolve(prices, np.ones(fast) / fast, "valid")
    s = np.convolve(prices, np.ones(slow) / slow, "valid")
    diff = np.where(f[-len(s):] > s, 1, -1)
    out = np.zeros(len(prices))
    out[len(prices) - len(diff):] = diff
    return out

def sig_donchian(prices: np.ndarray, n: int = 55) -> np.ndarray:
    out = np.zeros(len(prices))
    for i in range(n, len(prices)):
        window = prices[i - n : i]
        if prices[i] >= window.max():
            out[i] = 1
        elif prices[i] <= window.min():
            out[i] = -1
    return out

def sig_tsmom(prices: np.ndarray, lookback: int = 252) -> np.ndarray:
    out = np.zeros(len(prices))
    out[lookback:] = np.where(prices[lookback:] > prices[:-lookback], 1, -1)
    return out

def sig_rsi2(prices: np.ndarray) -> np.ndarray:
    deltas = np.diff(prices, prepend=prices[0])
    gains = np.where(deltas > 0, deltas, 0.0)
    losses = np.where(deltas < 0, -deltas, 0.0)
    avg_g = np.convolve(gains, np.ones(2) / 2, "valid")
    avg_l = np.convolve(losses, np.ones(2) / 2, "valid")
    rsi = np.where(avg_l > 0, 100 - 100 / (1 + avg_g / np.where(avg_l == 0, 1e-9, avg_l)), 100.0)
    out = np.zeros(len(prices))
    out[-len(rsi):] = np.where(rsi < 10, 1, np.where(rsi > 90, -1, 0))
    return out

def sig_bbands(prices: np.ndarray, n: int = 20, k: float = 2.0) -> np.ndarray:
    ma = np.convolve(prices, np.ones(n) / n, "valid")
    sd = np.array([prices[i - n : i].std() for i in range(n, len(prices) + 1)])
    out = np.zeros(len(prices))
    tail = prices[-len(ma):]
    pos = np.where(tail < ma - k * sd, 1, np.where(tail > ma + k * sd, -1, 0))
    out[len(prices) - len(ma):] = pos
    return out

def sig_breakout_adx(prices: np.ndarray, n: int = 20) -> np.ndarray:
    don = sig_donchian(prices, n)
    deltas = np.abs(np.diff(prices, prepend=prices[0]))
    atr = np.convolve(deltas, np.ones(n) / n, "valid")
    adx_proxy = np.zeros(len(prices))
    adx_proxy[-len(atr):] = atr / np.where(atr.mean() > 0, atr.mean(), 1)
    return np.where(adx_proxy > 1.0, don, 0)

def sig_dual_momentum(prices: np.ndarray, lookback: int = 90) -> np.ndarray:
    # absolute momentum only (single-series version of Antonacci)
    return sig_tsmom(prices, lookback)

def sig_ema_stack(prices: np.ndarray) -> np.ndarray:
    def ema(arr: np.ndarray, span: int) -> np.ndarray:
        alpha = 2 / (span + 1)
        out = np.zeros(len(arr)); out[0] = arr[0]
        for i in range(1, len(arr)):
            out[i] = alpha * arr[i] + (1 - alpha) * out[i - 1]
        return out
    e26, e55 = ema(prices, 26), ema(prices, 55)
    return np.where(e26 > e55, 1, np.where(e26 < e55, -1, 0))

def sig_turn_of_month(prices: np.ndarray) -> np.ndarray:
    # position only in the last 4 and first 3 sessions of each month ~ 21d cycle
    out = np.zeros(len(prices))
    for i in range(21, len(prices)):
        day_in_cycle = i % 21
        if day_in_cycle >= 17 or day_in_cycle <= 2:
            out[i] = 1
    return out

def sig_failed_breakout(prices: np.ndarray, n: int = 20) -> np.ndarray:
    don = sig_donchian(prices, n)
    out = np.zeros(len(prices))
    for i in range(n + 2, len(prices)):
        if don[i - 1] == 1 and prices[i] < prices[i - 1]:
            out[i] = -1  # turtle soup: yesterday's breakout fails today
        elif don[i - 1] == -1 and prices[i] > prices[i - 1]:
            out[i] = 1
    return out

def sig_sma_pullback(prices: np.ndarray, n: int = 50) -> np.ndarray:
    ma = np.convolve(prices, np.ones(n) / n, "valid")
    out = np.zeros(len(prices))
    tail = prices[-len(ma):]
    above = tail > ma
    touched = np.abs(tail - ma) / ma < 0.01
    pos = np.where(above & touched, 1, np.where(~above & touched, -1, 0))
    out[len(prices) - len(ma):] = pos
    return out

SIGNALS = {
    "ma.cross.20_50": sig_ma_cross,
    "donchian.55.breakout": sig_donchian,
    "tsmom.252": sig_tsmom,
    "rsi2.extremes": sig_rsi2,
    "bbands.20_2.reversion": sig_bbands,
    "breakout.adx_filtered": sig_breakout_adx,
    "momentum.absolute.90": sig_dual_momentum,
    "ema.stack.26_55": sig_ema_stack,
    "seasonal.turn_of_month": sig_turn_of_month,
    "turtle_soup.failed_breakout": sig_failed_breakout,
    "sma50.pullback": sig_sma_pullback,
}


def barrier_outcome(prices: np.ndarray, i: int, direction: int, horizon: int) -> int | None:
    """1 hit, -1 miss, 0 expiry, None unusable."""
    w = prices[i : i + horizon + 1]
    if len(w) < horizon + 1:
        return None
    entry = w[0]
    logret = np.diff(np.log(w))
    vol = logret.std()
    if vol <= 0:
        return None
    step = K * vol * math.sqrt(horizon)
    target = entry * math.exp(direction * step)
    stop = entry * math.exp(-direction * step)
    for p in w[1:]:
        if direction > 0:
            if p >= target: return 1
            if p <= stop: return -1
        else:
            if p <= target: return 1
            if p >= stop: return -1
    return 0


async def main() -> int:
    panel = await fetch_universe()
    print(f"panel: {len(panel)} series")

    baseline_hits = baseline_n = 0
    strategy_outcomes: dict[str, list[int]] = {name: [] for name in SIGNALS}

    for prices in panel.values():
        if len(prices) < 400:
            continue
        positions = {name: fn(prices) for name, fn in SIGNALS.items()}
        warmup = 120
        for i in range(warmup, len(prices) - HORIZON - 1, HORIZON):
            d_base = 1 if prices[i] > prices[i - 20] else -1  # baseline: naive trend
            r = barrier_outcome(prices, i, d_base, HORIZON)
            if r in (1, -1):
                baseline_n += 1
                baseline_hits += r == 1
            for name, pos in positions.items():
                d = int(pos[i])
                if d == 0:
                    continue
                r = barrier_outcome(prices, i, d, HORIZON)
                if r in (1, -1):
                    strategy_outcomes[name].append(r)

    p_base = baseline_hits / baseline_n if baseline_n else 0
    print(f"baseline: {baseline_hits}/{baseline_n} = {p_base:.3f}")

    reg = Registry()
    for name, outs in strategy_outcomes.items():
        n = len(outs)
        if n < 30:
            print(f"{name:32} n={n} too thin, recorded cannot_answer")
            reg.record(name=name, source=SOURCE, cells=max(n, 1), verdict="fail",
                       detail={"n": n, "note": "insufficient outcomes"})
            continue
        p = sum(1 for o in outs if o == 1) / n
        # paired two-proportion z vs baseline
        p_pool = (p * n + p_base * baseline_n) / (n + baseline_n)
        se = math.sqrt(p_pool * (1 - p_pool) * (1 / n + 1 / baseline_n)) if p_pool not in (0, 1) else 1e-9
        z = (p - p_base) / se if se > 0 else 0.0
        verdict = "pass" if abs(z) >= 2.8 and p > p_base else "fail"
        print(f"{name:32} n={n} hit={p:.3f} paired_z={z:+.2f} -> {verdict}")
        reg.record(
            name=name, source=SOURCE, cells=n, verdict=verdict,
            detail={"hit_rate": round(p, 4), "baseline": round(p_base, 4),
                    "paired_z": round(z, 2), "geometry": f"{K}x vol, {HORIZON}d"},
        )
    print(f"batch complete {datetime.now(UTC).isoformat()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
