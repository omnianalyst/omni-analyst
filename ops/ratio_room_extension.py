"""The ratio-room extension: replicate the near-peak pulse or kill it.

The first ratio-room run had one structural weakness: tail-alignment to the
shallowest symbol cut the panel to 2021-09 onward -- a single regime -- and
a t of -4.14 living only in the recent third of one regime is the exact
shape of every false positive this registry holds. This batch buys the
replication that decides it:

  per-pair long history  each alt aligned to BTC individually from its own
                         listing (ETH/BNB/XRP/LTC from 2017-18), so the
                         2017-21 ratio-peak cycles enter the sample instead
                         of being truncated away
  per-week pooling       one observation per TIME SLICE (mean excess across
                         eligible pairs that week), then t across weeks --
                         the correction the registry already recorded after
                         paired-z inflated 7/11 false passes; pooling per
                         pair multiplies cross-correlated noise

Five predeclared cells, predicted signs stated before running:

  near25.peak252.pairs_long   pair/BTC > 75% of its trailing 252d peak ->
                              pair underperforms BTC next 14d. PREDICTED
                              NEGATIVE. Pass = |t_rec| >= bar with the
                              predicted sign in t_rec AND t_full AND the
                              pre-2021-09 segment (both regimes agree).
  below50.peak756.pairs_long  Tyler's literal rule on long history: ratio <
                              50% of trailing 756d peak -> pair beats BTC.
                              PREDICTED POSITIVE, and net of 40 bps/flip.
  mechanism.reversion         near-peak -> 14d change in the ratio itself.
                              PREDICTED NEGATIVE (mean reversion is the
                              mechanism; without it the flag is fragile).
  near25.stability_surface    70/80/90% x 126/252/378d neighbourhood of the
                              headliner, reported as detail on ONE cell --
                              a real effect is stable across neighbours, an
                              artifact lives at one setting. Not a sweep.
  equities.sector_52w_stretch the same shape on a different market: sector
                              ETF/SPY ratio > 75% of its 252d peak -> sector
                              underperforms SPY next 10 sessions, 2012->now,
                              pooled per week. PREDICTED NEGATIVE if the
                              effect is behavioural structure rather than a
                              crypto artifact.

Run: uv run python ops/ratio_room_extension.py
"""

from __future__ import annotations

import asyncio
import json
import math
from datetime import UTC, datetime
from pathlib import Path

import httpx
import numpy as np
import pandas as pd

from omni.research.registry import Registry

SOURCE = "binance_spot+yfinance"
HORIZON = 14
EQ_HORIZON = 10
COST_BPS_PER_FLIP = 40.0
PAIRS = ["ETH", "BNB", "XRP", "LTC", "ADA", "DOGE", "XLM", "ALGO"]
SECTORS = ["XLB", "XLC", "XLE", "XLF", "XLI", "XLK", "XLP", "XLRE", "XLU", "XLV", "XLY"]
NEW_REGIME_START = pd.Timestamp("2021-09-01")
CACHE = Path("/tmp/ratio_pairs.json")
EQ_CACHE = Path("/tmp/ratio_sectors.csv")


def _t_stat(values: list[float]) -> tuple[float, int]:
    if len(values) < 3:
        return 0.0, len(values)
    a = np.array(values, dtype=float)
    sd = a.std(ddof=1)
    if sd <= 0 or not np.isfinite(sd):
        return 0.0, len(values)
    return float(a.mean() / (sd / math.sqrt(len(a)))), len(values)


def sign(x: float) -> int:
    return 0 if x == 0 else (1 if x > 0 else -1)


async def _spot_closes_dated(client: httpx.AsyncClient, symbol: str) -> list[list[float]]:
    out: list[list[float]] = []
    start = 1501113600000  # 2017-07-27, Binance's first bars
    for _ in range(40):
        r = await client.get(
            "https://api.binance.com/api/v3/klines",
            params={"symbol": symbol, "interval": "1d", "limit": 1000, "startTime": start},
        )
        rows = r.json()
        if not isinstance(rows, list) or not rows:
            return out
        out.extend([float(x[0]), float(x[4])] for x in rows)
        start = int(rows[-1][0]) + 86_400_000
        if len(rows) < 1000:
            return out
        await asyncio.sleep(0.15)
    return out


async def load_pairs() -> dict[str, pd.Series]:
    """Daily closes WITH dates, per symbol, from listing. Dates are kept
    because per-pair alignment is by date here, not by shared truncation."""
    if CACHE.exists():
        raw = json.loads(CACHE.read_text())
        return {
            k: pd.Series(
                [r[1] for r in v],
                index=pd.to_datetime([r[0] for r in v], unit="ms").normalize(),
                dtype=float,
            )
            for k, v in raw.items()
        }
    panel: dict[str, list[list[float]]] = {}
    async with httpx.AsyncClient(timeout=45) as client:
        for sym in ["BTC", *PAIRS]:
            rows = await _spot_closes_dated(client, f"{sym}USDT")
            if len(rows) >= 400:
                panel[sym] = rows
                print(f"  {sym:5} {len(rows):5} closes "
                      f"{datetime.fromtimestamp(rows[0][0] / 1000, UTC):%Y-%m-%d} ->")
            else:
                print(f"  {sym:5} unavailable ({len(rows)}); skipped")
    CACHE.write_text(json.dumps(panel))
    return {
        k: pd.Series(
            [r[1] for r in v],
            index=pd.to_datetime([r[0] for r in v], unit="ms").normalize(),
            dtype=float,
        )
        for k, v in panel.items()
    }


def load_sectors() -> dict[str, pd.Series] | None:
    if EQ_CACHE.exists():
        frame = pd.read_csv(EQ_CACHE, index_col=0, parse_dates=True)
        return {c: frame[c].dropna() for c in frame.columns}
    import yfinance as yf

    raw = yf.download(
        " ".join([*SECTORS, "SPY"]), start="2012-01-01", interval="1d",
        auto_adjust=True, progress=False, group_by="ticker",
    )
    out: dict[str, pd.Series] = {}
    for s in [*SECTORS, "SPY"]:
        try:
            closes = raw[s]["Close"].dropna()
            if len(closes) >= 500:
                out[s] = closes
        except Exception:  # noqa: BLE001,S112 - a dead ticker is skipped, not fatal
            continue
    if not out:
        return None
    frame = pd.DataFrame(out)
    frame.to_csv(EQ_CACHE)
    return out


def pooled_weekly(
    btc: pd.Series,
    pairs: dict[str, pd.Series],
    *,
    window: int,
    threshold: float,
    above: bool,
    horizon: int,
    step: int,
    ratio_mode: bool = False,
) -> dict:
    """Per-week pooled evaluation of a ratio-vs-trailing-peak gate.

    Each pair's gate and excess are computed on its own aligned pair/BTC
    ratio; the weekly observation is the mean across eligible pairs, so the
    t runs across time slices, not across correlated pairs. `ratio_mode`
    measures the ratio's own change (the mechanism cell) instead of excess.
    """
    grid = btc.index
    btc_grid = btc.to_numpy()
    per_pair: dict[str, dict[str, list]] = {}
    week_dates: list = []
    week_vals: list[list[float]] = []
    flips_total = 0
    for sym, series in pairs.items():
        aligned = series.reindex(grid)
        pair = aligned.to_numpy()
        ratio = pair / btc_grid
        peak = pd.Series(ratio).rolling(window, min_periods=window).max().to_numpy()
        eligible = (
            ~np.isnan(ratio) & ~np.isnan(peak) & ~np.isnan(pair)
        )
        gate = np.zeros(len(grid), dtype=bool)
        if above:
            gate[eligible] = ratio[eligible] > threshold * peak[eligible]
        else:
            gate[eligible] = ratio[eligible] < threshold * peak[eligible]
        last_on = False
        started = False
        per_pair[sym] = {"on": 0, "excess": [], "ratio_chg": []}
        for t in range(0, len(grid) - horizon - 2, step):
            if not eligible[t]:
                continue
            on = bool(gate[t])
            if started and on != last_on:
                flips_total += 1
            last_on = on
            started = True
            if not on:
                continue
            i = t + 1
            j = i + horizon
            if j >= len(grid):
                continue
            if np.isnan(pair[i]) or np.isnan(pair[j]) or np.isnan(btc_grid[j]):
                continue
            per_pair[sym]["on"] += 1
            if ratio_mode:
                if not np.isnan(ratio[j]) and not np.isnan(ratio[i]) and ratio[i] > 0:
                    per_pair[sym]["ratio_chg"].append(ratio[j] / ratio[i] - 1.0)
            else:
                r_p = pair[j] / pair[i] - 1.0
                r_b = btc_grid[j] / btc_grid[i] - 1.0
                per_pair[sym]["excess"].append(r_p - r_b)
        # fold this pair into the weekly grid by date
        for t in range(0, len(grid) - horizon - 2, step):
            if gate[t] and eligible[t]:
                i, j = t + 1, t + 1 + horizon
                if j >= len(grid):
                    continue
                if np.isnan(pair[i]) or np.isnan(pair[j]) or np.isnan(btc_grid[j]):
                    continue
                if ratio_mode:
                    if np.isnan(ratio[j]) or np.isnan(ratio[i]) or ratio[i] <= 0:
                        continue
                    val = ratio[j] / ratio[i] - 1.0
                else:
                    val = (pair[j] / pair[i] - 1.0) - (btc_grid[j] / btc_grid[i] - 1.0)
                date = grid[t]
                if date not in week_dates:
                    week_dates.append(date)
                    week_vals.append([val])
                else:
                    week_vals[week_dates.index(date)].append(val)

    if not week_dates:
        return {"weeks": 0, "t_recent_third": 0.0, "t_full": 0.0, "t_old": 0.0,
                "t_new": 0.0, "mean_bps": 0.0, "flips": flips_total,
                "per_pair": per_pair, "pairs_on": 0}

    order = np.argsort(week_dates)
    dates = [week_dates[k] for k in order]
    obs = [float(np.mean(week_vals[k])) for k in order]
    old = [o for o, d in zip(obs, dates) if d < NEW_REGIME_START]
    new = [o for o, d in zip(obs, dates) if d >= NEW_REGIME_START]
    recent_n = max(len(obs) // 3, 3)
    return {
        "weeks": len(obs),
        "t_recent_third": round(_t_stat(obs[-recent_n:])[0], 2),
        "t_full": round(_t_stat(obs)[0], 2),
        "t_old": round(_t_stat(old)[0], 2),
        "t_new": round(_t_stat(new)[0], 2),
        "n_old": len(old),
        "n_new": len(new),
        "mean_bps": round(float(np.mean(obs)) * 10_000.0, 1),
        "flips": flips_total,
        "per_pair": {
            s: {
                "on": v["on"],
                "t": round(_t_stat(v["excess"] or v["ratio_chg"])[0], 2),
            }
            for s, v in per_pair.items()
        },
    }


def self_check() -> None:
    wobble = np.sin(np.arange(40)) * 1e-6
    assert _t_stat(list(0.01 + wobble))[0] > 4.0
    assert _t_stat(list(-(0.01 + wobble)))[0] < -4.0
    noise = list(np.sin(np.arange(200) * 2.0) * 0.05)
    assert abs(_t_stat(noise)[0]) < 2.0
    x = pd.Series(np.linspace(1.0, 2.0, 300))
    peak = x.rolling(252, min_periods=252).max().to_numpy()
    assert np.isnan(peak[0]) and not np.isnan(peak[-1]) and peak[-1] >= x.iloc[-1]


def verdict_for(pred: int, r: dict, bar: float, *, net_bps: float | None = None) -> str:
    ok = (
        abs(r["t_recent_third"]) >= bar
        and sign(r["t_recent_third"]) == pred
        and sign(r["t_full"]) == pred
        and (r["t_old"] == 0.0 or sign(r["t_old"]) == pred)
    )
    if net_bps is not None and sign(net_bps) != pred:
        ok = False
    return "pass" if ok else "fail"


async def main() -> int:
    self_check()
    print("loading per-pair history from listing (cached /tmp/ratio_pairs.json)")
    series = await load_pairs()
    btc = series["BTC"]
    print(f"BTC grid: {len(btc)} sessions {btc.index[0].date()} -> {btc.index[-1].date()}")
    pairs = {k: v for k, v in series.items() if k != "BTC"}
    print()

    reg = Registry()

    near = pooled_weekly(btc, pairs, window=252, threshold=0.75, above=True,
                         horizon=HORIZON, step=7)
    bar = reg.bar(pending_cells=max(near["weeks"], 1))
    print("near-peak (>75% of 252d peak), per-week pooled, predicted NEGATIVE")
    print(f"  weeks={near['weeks']} (old {near['n_old']} / new {near['n_new']}) "
          f"mean={near['mean_bps']} bps  t_full={near['t_full']:+.2f} "
          f"t_old={near['t_old']:+.2f} t_new={near['t_new']:+.2f} "
          f"t_rec={near['t_recent_third']:+.2f} bar={bar:.3f}")
    print(f"  per-pair: {near['per_pair']}")
    v = verdict_for(-1, near, bar)
    print(f"  -> {v}\n")
    reg.record(
        name="rotation.ratio_room.near25.peak252.pairs_long", source=SOURCE,
        cells=max(near["weeks"], 1), verdict=v,
        detail={**{k: near[k] for k in
                   ("weeks", "n_old", "n_new", "mean_bps", "t_full", "t_old",
                    "t_new", "t_recent_third", "per_pair")},
                "predicted_sign": "negative", "bar": round(bar, 3),
                "best_recent_third_t": abs(near["t_recent_third"])},
    )

    below = pooled_weekly(btc, pairs, window=756, threshold=0.50, above=False,
                          horizon=HORIZON, step=7)
    bar = reg.bar(pending_cells=max(below["weeks"], 1))
    on_windows = sum(v["on"] for v in below["per_pair"].values())
    net = below["mean_bps"] - (
        below["flips"] * COST_BPS_PER_FLIP / max(on_windows, 1)
    )
    print("far-below (<50% of 756d peak), Tyler's literal rule, predicted POSITIVE")
    print(f"  weeks={below['weeks']} (old {below['n_old']} / new {below['n_new']}) "
          f"mean={below['mean_bps']} bps net~{net:.1f} bps  "
          f"t_full={below['t_full']:+.2f} t_old={below['t_old']:+.2f} "
          f"t_rec={below['t_recent_third']:+.2f} bar={bar:.3f}")
    print(f"  per-pair: {below['per_pair']}")
    v = verdict_for(1, below, bar, net_bps=net)
    print(f"  -> {v}\n")
    reg.record(
        name="rotation.ratio_room.below50.peak756.pairs_long", source=SOURCE,
        cells=max(below["weeks"], 1), verdict=v,
        detail={**{k: below[k] for k in
                   ("weeks", "n_old", "n_new", "mean_bps", "t_full", "t_old",
                    "t_new", "t_recent_third", "per_pair")},
                "net_bps_per_period": round(net, 1),
                "predicted_sign": "positive", "bar": round(bar, 3),
                "best_recent_third_t": abs(below["t_recent_third"])},
    )

    mech = pooled_weekly(btc, pairs, window=252, threshold=0.75, above=True,
                         horizon=HORIZON, step=7, ratio_mode=True)
    bar = reg.bar(pending_cells=max(mech["weeks"], 1))
    print("mechanism: near-peak -> 14d ratio change, predicted NEGATIVE")
    print(f"  weeks={mech['weeks']} mean ratio chg={mech['mean_bps'] / 100:.4%} "
          f"t_full={mech['t_full']:+.2f} t_old={mech['t_old']:+.2f} "
          f"t_rec={mech['t_recent_third']:+.2f} bar={bar:.3f}")
    v = verdict_for(-1, mech, bar)
    print(f"  -> {v}\n")
    reg.record(
        name="rotation.ratio_room.mechanism.reversion.pairs_long", source=SOURCE,
        cells=max(mech["weeks"], 1), verdict=v,
        detail={**{k: mech[k] for k in
                   ("weeks", "n_old", "n_new", "mean_bps", "t_full", "t_old",
                    "t_new", "t_recent_third")},
                "predicted_sign": "negative", "bar": round(bar, 3),
                "best_recent_third_t": abs(mech["t_recent_third"])},
    )

    surface: dict[str, float] = {}
    for w in (126, 252, 378):
        for thr in (0.70, 0.75, 0.80, 0.90):
            r = pooled_weekly(btc, pairs, window=w, threshold=thr, above=True,
                              horizon=HORIZON, step=7)
            surface[f"w{w}/t{thr:.2f}"] = r["t_recent_third"]
    bar = reg.bar(pending_cells=1)
    v = verdict_for(-1, near, bar)
    print("stability surface (t_recent_third per setting; detail on one cell):")
    for k, t in surface.items():
        print(f"  {k:14} {t:+.2f}")
    print(f"  -> {v} (from the w252/t0.75 headliner)\n")
    reg.record(
        name="rotation.ratio_room.near25.stability_surface", source=SOURCE,
        cells=1, verdict=v,
        detail={"surface_t_recent_third": surface, "predicted_sign": "negative",
                "bar": round(bar, 3),
                "best_recent_third_t": abs(near["t_recent_third"])},
    )

    eq = load_sectors()
    if eq is None:
        print("sector panel unavailable; equities cell not recorded")
    else:
        spy = eq["SPY"]
        eq_pairs = {k: v for k, v in eq.items() if k != "SPY"}
        eqr = pooled_weekly(spy, eq_pairs, window=252, threshold=0.75, above=True,
                            horizon=EQ_HORIZON, step=5)
        bar = reg.bar(pending_cells=max(eqr["weeks"], 1))
        print("equities: sector/SPY near 52w peak -> 10-session excess vs SPY, "
              "predicted NEGATIVE")
        print(f"  weeks={eqr['weeks']} mean={eqr['mean_bps']} bps "
              f"t_full={eqr['t_full']:+.2f} t_rec={eqr['t_recent_third']:+.2f} "
              f"bar={bar:.3f}")
        print(f"  per-sector: {eqr['per_pair']}")
        v = verdict_for(-1, eqr, bar)
        print(f"  -> {v}")
        reg.record(
            name="rotation.ratio_room.equities.sector_52w_stretch", source=SOURCE,
            cells=max(eqr["weeks"], 1), verdict=v,
            detail={**{k: eqr[k] for k in
                       ("weeks", "mean_bps", "t_full", "t_recent_third",
                        "per_pair")},
                    "predicted_sign": "negative", "bar": round(bar, 3),
                    "best_recent_third_t": abs(eqr["t_recent_third"])},
        )

    print(f"\nregistry now {len(reg.entries())} entries, "
          f"bar for the next test {reg.bar(pending_cells=1):.3f}")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
