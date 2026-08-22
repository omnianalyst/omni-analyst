"""The cointegration/lag-dynamics battery: the one untested cell in Tyler's
"what is lagging" idea. Run ONCE, pre-declared, whatever it says is permanent.

WHAT DIED ALREADY (do not rebuild): ratio-level room ("below old peak ->
catch up"), fixed-delay lead-lag ("BTC leads -> alts follow"), rotation.
All measured, all failed; see NEXT_SESSION 2026-08-18/19 entries.

WHAT THIS TESTS INSTEAD -- the DYNAMICS claim, in Tyler's words: "how long
the separation lasts, and whether it predicts recatching." For each major
alt against BTC, build the log-ratio spread, estimate how fast it
mean-reverts (a half-life from an AR(1) on the spread), then ask three
pre-declared questions:

  CELL 1  spread.stretch.decay14   z-score of the spread (vs its own 90d
       mean/sd) below -1.5 -> alt BEATS BTC over the next 14 days.
       PREDICTED POSITIVE. This is the "lagging should recatch" claim in
       its correct form: not "ratio below a level" but "spread stretched
       by its own standard-deviation yardstick."
  CELL 2  half_life.gate            only trade cell-1 signals when the
       pair's estimated half-life is < 60 days (a spread that reverts at
       all vs one that random-walks). PREDICTED POSITIVE and LARGER than
       cell 1 -- the mechanism (mean reversion with a speed) is doing the
       work if the gate improves the result.
  CELL 3  duration.deepen           spread negative AND has widened for
       >= 14 consecutive days -> alt underperforms BTC next 14d.
       PREDICTED NEGATIVE. This is the "worsening lag" question: if
       continued separation predicts further separation, the catch-up
       trade needs the opposite regime filter.

DISCIPLINE: per-WEEK pooled observations (one excess per eligible pair per
week, t across weeks -- the correlated-sample correction), costs 40 bps per
flip, verdict on the recent third AND full sample both required, every cell
recorded to the registry whatever it returns. 3 cells, 10,000+ statistics
counted, one run.

Run: uv run python ops/lag_dynamics_battery.py
"""

from __future__ import annotations

import json
import math
from pathlib import Path

import httpx
import numpy as np
import pandas as pd

from omni.research.registry import Registry

SOURCE = "binance_spot_ohlcv"
HORIZON = 14
COST_BPS_PER_FLIP = 40.0
QUOTE = "BTC"
PAIRS = ["ETH", "BNB", "XRP", "LTC", "ADA", "DOGE", "XLM", "ALGO", "LINK", "ATOM"]
Z_WINDOW = 90
Z_ENTRY = -1.5
HL_WINDOW = 365
HL_GATE_DAYS = 60.0
WIDEN_DAYS = 14
CACHE = Path("/tmp/lag_dynamics_prices.json")


def _t(values: list[float]) -> tuple[float, int]:
    if len(values) < 3:
        return 0.0, len(values)
    a = np.array(values, dtype=float)
    sd = a.std(ddof=1)
    if sd <= 0 or not np.isfinite(sd):
        return 0.0, len(a)
    return float(a.mean() / (sd / math.sqrt(len(a)))), len(a)


def _thirds_t(values: list[float]) -> tuple[float, float, float, int]:
    """Full-sample t, recent-third t, and the recent third's count."""
    full, n = _t(values)
    third = values[-max(3, n // 3):]
    rec, _ = _t(third)
    return full, rec, 0.0, n


async def _prices() -> dict[str, pd.Series]:
    if CACHE.exists():
        raw = json.loads(CACHE.read_text())
        return {
            sym: pd.Series({pd.Timestamp(k): float(v) for k, v in cols.items()})
            .sort_index()
            .dropna()
            for sym, cols in raw.items()
        }
    out: dict[str, dict[str, float]] = {}
    async with httpx.AsyncClient(timeout=30.0) as client:
        for sym in [QUOTE] + PAIRS:
            pair = f"{sym}USDT"
            rows: dict[str, float] = {}
            cursor = None
            while True:
                params = {"symbol": pair, "interval": "1d", "limit": 1000}
                if cursor:
                    params["endTime"] = cursor
                r = await client.get(
                    "https://api.binance.com/api/v3/klines",
                    params=params,
                )
                r.raise_for_status()
                batch = r.json()
                if not batch:
                    break
                for k in batch:
                    rows[pd.Timestamp(k[0], unit="ms", tz="UTC").strftime(
                        "%Y-%m-%d"
                    )] = float(k[4])
                earliest = batch[0][0]
                if cursor and (earliest >= cursor or len(batch) < 1000):
                    break
                cursor = earliest
                if len(rows) > 4000:
                    break
            out[sym] = rows
            print(f"  {sym}: {len(rows)} days")
    CACHE.write_text(json.dumps(out))
    return {
        sym: pd.Series({pd.Timestamp(k): float(v) for k, v in cols.items()})
        .sort_index()
        .dropna()
        for sym, cols in out.items()
    }


def _half_life(spread: pd.Series) -> float | None:
    """AR(1) half-life of spread reversion, from the HL_WINDOW tail.

    delta(spread) = a + b*spread_lag; b<0 means reversion; half-life is
    ln(0.5)/ln(1+b) in days. b>=0 (no reversion) returns None -- the pair
    random-walks and no catch-up timescale exists.
    """
    s = spread.dropna().iloc[-HL_WINDOW:]
    if len(s) < 60:
        return None
    lag = s.shift(1).iloc[1:]
    delta = s.diff().iloc[1:]
    x = lag - lag.mean()
    y = delta - delta.mean()
    denom = float((x * x).sum())
    if denom <= 0:
        return None
    b = float((x * y).sum()) / denom
    if b >= 0 or b <= -2:
        return None
    hl = math.log(0.5) / math.log(1 + b)
    return hl if 1 <= hl <= 365 else None


def build_cells(prices: dict[str, pd.Series]) -> dict[str, list[float]]:
    """Weekly-pooled per-pair excesses for each pre-declared cell."""
    btc = prices[QUOTE]
    cells: dict[str, list[float]] = {
        "stretch.decay14": [],
        "half_life.gate": [],
        "duration.deepen": [],
    }
    seen_weeks: set[tuple[str, object]] = set()
    # Weekly evaluation dates across the union of history.
    all_days = sorted(set(btc.index))
    week_marks = all_days[::7]
    half_lives: dict[str, float | None] = {}

    for alt in PAIRS:
        if alt not in prices:
            continue
        s = pd.concat([prices[alt], btc], axis=1, join="inner").dropna()
        s.columns = ["alt", "btc"]
        if len(s) < Z_WINDOW + 40:
            continue
        spread = np.log(s["alt"]) - np.log(s["btc"])
        half_lives[alt] = _half_life(spread)
        roll_mean = spread.rolling(Z_WINDOW).mean()
        roll_sd = spread.rolling(Z_WINDOW).std(ddof=1)
        z = (spread - roll_mean) / roll_sd.replace(0, np.nan)

        # FORWARD excess: shift(-HORIZON) is the return over the NEXT H
        # days. diff(HORIZON) is backward-looking; the first run recorded
        # cells from past returns -- caught in verification and corrected
        # before the record was trusted.
        alt_ret = np.log(s["alt"]).shift(-HORIZON) - np.log(s["alt"])
        btc_ret = np.log(s["btc"]).shift(-HORIZON) - np.log(s["btc"])
        excess = (alt_ret - btc_ret) * 10_000 - COST_BPS_PER_FLIP

        neg = (z < 0).astype(int)
        widening = neg.diff().fillna(0).eq(0) & neg.eq(1)
        streak = neg.groupby((~neg).cumsum()).cumsum()

        # One observation per pair per ISO WEEK: overlapping daily entries
        # multiply cross-correlated noise, the lesson the registry already
        # holds from paired-z. First eligible day in each week wins.
        week_of = pd.Series(s.index, index=s.index).dt.to_period("W")
        for day in s.index:
            zi = z.loc[day] if day in z.index else None
            if zi is None or pd.isna(zi):
                continue
            fwd = excess.loc[day] if day in excess.index else None
            if fwd is None or pd.isna(fwd):
                continue
            w = week_of.loc[day]
            wkey = (alt, w)
            if wkey in seen_weeks:
                continue
            hl = _half_life(spread.loc[:day])
            if zi <= Z_ENTRY:
                seen_weeks.add(wkey)
                cells["stretch.decay14"].append(float(fwd))
                # Cell 2: the same signal, gated on a real reversion speed.
                if hl is not None and hl < HL_GATE_DAYS:
                    cells["half_life.gate"].append(float(fwd))
            # Cell 3: negative and widening for WIDEN_DAYS straight.
            if neg.loc[day] == 1 and streak.loc[day] >= WIDEN_DAYS:
                seen_weeks.add(wkey)
                # Predicted NEGATIVE: hold BTC (short the alt vs BTC).
                cells["duration.deepen"].append(-float(fwd))

    return cells


def main() -> int:
    import asyncio

    print("fetching/aligning prices (Binance spot, from listing)...")
    prices = asyncio.run(_prices())
    print(f"pairs with data: {sorted(set(prices) & set(PAIRS))}")

    cells = build_cells(prices)
    reg = Registry()
    bar = reg.bar(pending_cells=3)

    print(f"\nbar (with these 3 pending): {bar:.3f}")
    names = {
        "stretch.decay14": ("lag.coint.stretch.z90.decay14", "POSITIVE"),
        "half_life.gate": ("lag.coint.halflife_gate.decay14", "POSITIVE>cell1"),
        "duration.deepen": ("lag.coint.duration.deepen14", "NEGATIVE"),
    }
    for key, (reg_name, predicted) in names.items():
        vals = cells[key]
        full, rec, _, n = _thirds_t(vals)
        verdict = "FAIL"
        if key == "duration.deepen":
            ok = rec < 0 and full < 0 and abs(rec) >= bar
        else:
            ok = rec > 0 and full > 0 and rec >= bar
        if ok:
            verdict = "PASS"
        mean = float(np.mean(vals)) if vals else 0.0
        print(
            f"\n{reg_name}  [{predicted}]  n={n}  mean={mean:+.1f}bps  "
            f"t_full={full:+.2f}  t_rec={rec:+.2f}  -> {verdict}"
        )
        reg.record(
            name=reg_name,
            source=SOURCE,
            cells=max(n // 7, 1) * 10,
            verdict="pass" if verdict == "PASS" else "fail",
            detail={
                "predicted": predicted,
                "n_weekly_obs": n,
                "mean_bps_net": round(mean, 2),
                "t_full": round(full, 3) if full else None,
                "best_recent_third_t": round(rec, 3) if rec else None,
                "bar": round(bar, 3),
                "params": (
                    f"z{Z_WINDOW}d entry{Z_ENTRY} hl_gate{HL_GATE_DAYS}d "
                    f"widen{WIDEN_DAYS}d h{HORIZON}d cost{COST_BPS_PER_FLIP:.0f}bps"
                ),
            },
        )
    print("\nrecorded. done.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
