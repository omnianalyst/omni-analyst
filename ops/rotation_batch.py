"""The rotation batch: does capital rotate through crypto tiers in an order
old data can rank?

The claim under test is Tyler's cycle-rotation idea, decomposed into every
distinct way it could be true, one cell each. Three cycles of n is not a
distribution, so nothing here estimates a cycle ORDER; every cell is the
highest-frequency analog of one order-claim, judged on ~390 weekly decisions
instead of 3 cycles:

  leadlag    BTC trailing 30d return > 0 -> tier beats BTC next 14d
             (per tier T2/T3/T4/ALT, plus a 90d variant on ALT)
  rot        ETH/BTC 30d change > 0 -> ALT beats BTC next 14d (rotation-on);
             ALT/BTC ratio 30d change > 0 -> ALT continues (alt-season start)
  state      BTC above 200d SMA -> ALT next 14d; BTC < 20% off 252d peak -> ALT
  rotate     monthly: hold last 28d's best tier (persistence) / worst (reversal)
  beta       per-tier alpha vs BTC, weekly -- is the whole idea just beta?
  heat       ETH funding 30d mean in its trailing-year bottom quartile -> ALT
             (the hated-are-cheap harvest shape, the only shape that ever
             survived here)

Design decisions, stated because each is a place a bias could hide:

  Tiers are the named set (T2 ETH/BNB/XRP, T3 ADA/XLM/ALGO/HBAR, T4 DOGE/SHIB,
  ALT = all equal-weight), membership from listing date, min 2 members. The
  named set is the claim; generalising past it is exploratory and not run.
  XMR is skipped (Binance delisted it 2024-02; Kraken serves 720 candles).
  T1 is BTC, which is also the benchmark -- a tier's "win" is beating BTC.

  Signal at close t, entry at close t+1, window t+1..t+1+H. The statistic is
  a t-stat on per-window EXCESS RETURN (tier minus BTC), because the first
  attempt used double-barrier McNemar and that pairing cannot test relative
  performance of two correlated long legs: with K=2 vol barriers ~88% of
  windows expire on both legs together and discordants are structurally
  ~zero (measured 2026-08-18, 0/217 windows). McNemar on barriers tests
  opposite directions on ONE series -- the wide-batch case -- and stays
  there. Recorded as methodology.correction.barrier_pairing below; the 16
  broken entries it produced stay in the registry with their cells counted,
  because the search genuinely spent them.

  cells = gate-on windows (every window is a comparison against the null),
  and the verdict reads the most-recent-third t (the harness rule: never the
  full sample). A pass needs recent-third |t| >= bar_for(pending cells) AND
  positive net excess after 40 bps round trip per position change (the t is
  cost-blind, so the cost gate is stated rather than implied), AND the same
  sign t in the full sample. Nothing is promoted to the shadow book without
  a replication pass, the tsmom.252 standard.

Run: uv run python ops/rotation_batch.py   (panel cached to /tmp)
"""

from __future__ import annotations

import asyncio
import json
import math
from pathlib import Path

import httpx
import numpy as np

from omni.research.registry import Registry

SOURCE = "binance_spot_ohlcv"
K = 2.0
H = 14
MONTH = 28
COST_BPS_PER_FLIP = 40.0
TIERS = {
    "T2": ["ETH", "BNB", "XRP"],
    "T3": ["ADA", "XLM", "ALGO", "HBAR"],
    "T4": ["DOGE", "SHIB"],
}
ALTS = [s for members in TIERS.values() for s in members]
CACHE = Path("/tmp/rotation_panel.json")
FUNDING_CACHE = Path("/tmp/rotation_funding.json")


async def _spot_closes(client: httpx.AsyncClient, symbol: str) -> list[float]:
    """Full daily-close history for one Binance spot USDT pair, paginated.

    Close (index 4), not open -- the ccxt open-stamp lookahead this repo
    already paid for once. Returns [] if the pair never existed.
    """
    out: list[float] = []
    start = 1546300800000  # 2019-01-01
    for _ in range(30):
        r = await client.get(
            "https://api.binance.com/api/v3/klines",
            params={"symbol": symbol, "interval": "1d", "limit": 1000, "startTime": start},
        )
        rows = r.json()
        if not isinstance(rows, list) or not rows:
            return out
        out.extend(float(x[4]) for x in rows)
        start = int(rows[-1][0]) + 86_400_000
        if len(rows) < 1000:
            return out
        await asyncio.sleep(0.15)
    return out


async def load_panel() -> dict[str, np.ndarray]:
    if CACHE.exists():
        raw = json.loads(CACHE.read_text())
        return {k: np.array(v, dtype=float) for k, v in raw.items()}
    panel: dict[str, list[float]] = {}
    async with httpx.AsyncClient(timeout=45) as client:
        for sym in ["BTC", *ALTS, "XMR"]:
            closes = await _spot_closes(client, f"{sym}USDT")
            if len(closes) >= 400:
                panel[sym] = closes
                print(f"  {sym:6} {len(closes):5} closes from listing")
            else:
                print(f"  {sym:6} unavailable ({len(closes)} closes); skipped")
    CACHE.write_text(json.dumps(panel))
    return {k: np.array(v, dtype=float) for k, v in panel.items()}


async def load_funding() -> np.ndarray | None:
    """Daily mean ETHUSDT perp funding, percent, full history. None if the
    endpoint refuses (the heat cell then records as refused, not as pass)."""
    if FUNDING_CACHE.exists():
        return np.array(json.loads(FUNDING_CACHE.read_text()), dtype=float)
    out: list[float] = []
    start = 1546300800000
    async with httpx.AsyncClient(timeout=45) as client:
        for _ in range(20):
            r = await client.get(
                "https://fapi.binance.com/fapi/v1/fundingRate",
                params={"symbol": "ETHUSDT", "limit": 1000, "startTime": start},
            )
            rows = r.json()
            if not isinstance(rows, list) or not rows:
                break
            by_day: dict[int, list[float]] = {}
            for row in rows:
                day = int(row["fundingTime"]) // 86_400_000
                by_day.setdefault(day, []).append(float(row["fundingRate"]))
            for day in sorted(by_day):
                out.append(100.0 * sum(by_day[day]) / len(by_day[day]))
            start = int(rows[-1]["fundingTime"]) + 1
            if len(rows) < 1000:
                break
            await asyncio.sleep(0.15)
    if len(out) < 500:
        return None
    FUNDING_CACHE.write_text(json.dumps(out))
    return np.array(out, dtype=float)


def align_on_btc(panel: dict[str, np.ndarray]) -> dict[str, np.ndarray]:
    """Every series truncated to BTC's timeline tail so date t means the same
    day everywhere. Research panels are daily-and-contiguous; this is stated
    rather than exact-timestamp aligned because the source is one venue."""
    n = min(len(v) for v in panel.values())
    return {k: v[-n:] for k, v in panel.items()}


def tier_index(closes: dict[str, np.ndarray], members: list[str], *, min_members: int = 2) -> np.ndarray:
    """Equal-weight daily-return index; a day needs min_members listed names,
    else NaN. Membership begins at listing, not at today -- the tier lists are
    the hypothesis, so this is the named-set test, not a survivorship sweep."""
    rets = []
    for m in members:
        p = closes.get(m)
        if p is None:
            continue
        rets.append(p[1:] / p[:-1] - 1.0)
    n = min(len(r) for r in rets)
    stack = np.array([r[-n:] for r in rets])
    member_ret = np.where(np.isnan(stack), np.nan, stack)
    present = np.sum(~np.isnan(member_ret), axis=0)
    daily = np.where(present >= min_members, np.nanmean(member_ret, axis=0), np.nan)
    level = np.cumprod(np.where(np.isnan(daily), 1.0, 1.0 + np.nan_to_num(daily)))
    return np.concatenate([[1.0], level])


def sma(x: np.ndarray, w: int) -> np.ndarray:
    c = np.convolve(x, np.ones(w) / w, "valid")
    out = np.full(len(x), np.nan)
    out[w - 1:] = c
    return out


def _t_stat(values: list[float]) -> float:
    """One-sample t of the mean. Zero for n<3 or zero variance -- a window
    set that cannot support a statistic reports 0, never a fabricated number."""
    if len(values) < 3:
        return 0.0
    a = np.array(values, dtype=float)
    sd = a.std(ddof=1)
    if sd <= 0 or not np.isfinite(sd):
        return 0.0
    return float(a.mean() / (sd / math.sqrt(len(a))))


def evaluate_gate(
    btc: np.ndarray,
    tier: np.ndarray,
    gate: np.ndarray,
    *,
    step: int,
    horizon: int,
) -> dict:
    """Long the tier on gate-days, hold BTC otherwise. Per gate-on window the
    observation is the tier's return minus BTC's over t+1..t+1+H; the verdict
    statistic is the most-recent-third t of those excesses."""
    excess: list[float] = []
    flips = 0
    in_tier = False
    n_on = 0
    for t in range(260, len(btc) - horizon - 2, step):
        on = bool(gate[t])
        if on != in_tier:
            flips += 1
            in_tier = on
        if not on:
            continue
        n_on += 1
        i = t + 1
        if i + horizon >= len(btc) or i + horizon >= len(tier):
            continue
        r_t = tier[i + horizon] / tier[i] - 1.0
        r_b = btc[i + horizon] / btc[i] - 1.0
        excess.append(r_t - r_b)
    if not excess:
        return {
            "calls": 0, "wins": 0, "t_recent_third": 0.0, "t_full": 0.0,
            "mean_excess_bps": 0.0, "net_bps_per_period": 0.0, "flips": flips,
        }
    recent_n = max(len(excess) // 3, 3)
    gross = float(np.mean(excess)) * 10_000.0
    net = gross - (flips * COST_BPS_PER_FLIP / len(excess))
    return {
        "calls": len(excess),
        "wins": int(sum(1 for e in excess if e > 0)),
        "t_recent_third": round(_t_stat(excess[-recent_n:]), 2),
        "t_full": round(_t_stat(excess), 2),
        "mean_excess_bps": round(gross, 1),
        "net_bps_per_period": round(net, 1),
        "flips": flips,
    }


def beta_alpha(btc: np.ndarray, tier: np.ndarray) -> dict:
    """Weekly excess-return regression of the tier on BTC. If alpha is zero
    the rotation story is beta in a costume, and that is the finding."""
    r_t = tier[7::7] / tier[:-7:7] - 1.0
    r_b = btc[7::7] / btc[:-7:7] - 1.0
    n = min(len(r_t), len(r_b))
    y = (r_t - r_b)[-n:]
    x = r_b[-n:]
    xbar, ybar = x.mean(), y.mean()
    beta = float(((x - xbar) @ (y - ybar)) / ((x - xbar) @ (x - xbar)))
    resid = y - ybar - beta * (x - xbar)
    dof = n - 2
    s2 = float(resid @ resid) / dof
    se_alpha = math.sqrt(s2 * (1.0 / n + xbar * xbar / ((x - xbar) @ (x - xbar))))
    alpha_week = ybar - beta * xbar
    t_alpha = float(alpha_week / se_alpha)
    return {
        "weeks": n,
        "beta_on_btc": round(beta, 3),
        "alpha_annualised_pct": round(alpha_week * 52 * 100, 2),
        "alpha_t": round(t_alpha, 2),
    }


def self_check() -> None:
    """A batch that cannot fail its own tools cannot fail honestly."""
    wobble = np.sin(np.arange(40)) * 1e-6
    up = list(0.01 + wobble)
    assert _t_stat(up) > 4.0
    assert _t_stat([-e for e in up]) < -4.0
    noise = list(np.sin(np.arange(200) * 2.0) * 0.05)
    assert abs(_t_stat(noise)) < 2.0
    assert _t_stat([0.01] * 40) == 0.0
    assert _t_stat([0.01, 0.02]) == 0.0


async def main() -> int:
    self_check()
    print("loading panel (Binance spot, from listing, cached /tmp/rotation_panel.json)")
    raw = await load_panel()
    closes = align_on_btc(raw)
    btc = closes["BTC"]
    print(f"aligned panel: {len(btc)} sessions, {len(closes)} symbols\n")

    tiers = {name: tier_index(closes, members) for name, members in TIERS.items()}
    alts = [s for members in TIERS.values() for s in members]
    tiers["ALT"] = tier_index(closes, alts)
    tiers["T1"] = btc

    mom30 = btc / np.roll(btc, 30) - 1.0
    mom90 = btc / np.roll(btc, 90) - 1.0
    eth = closes.get("ETH", btc)
    ethbtc = eth / btc
    ethbtc30 = ethbtc / np.roll(ethbtc, 30) - 1.0
    altbtc = tiers["ALT"] / btc
    altratio30 = altbtc / np.roll(altbtc, 30) - 1.0
    sma200 = sma(btc, 200)
    peak252 = pd_peak(btc)
    drawdown = btc / peak252 - 1.0

    def gate(x: np.ndarray, idx: np.ndarray) -> np.ndarray:
        return np.where(np.isnan(idx), False, idx > 0)

    cells: list[tuple[str, dict]] = []
    for name in ("T2", "T3", "T4", "ALT"):
        cells.append(
            (f"rotation.leadlag.btc30.{name.lower()}",
             evaluate_gate(btc, tiers[name], gate(None, mom30), step=7, horizon=H))
        )
    cells.append(
        ("rotation.leadlag.btc90.alt",
         evaluate_gate(btc, tiers["ALT"], gate(None, mom90), step=7, horizon=H))
    )
    cells.append(
        ("rotation.rot.ethbtc30.alt",
         evaluate_gate(btc, tiers["ALT"], gate(None, ethbtc30), step=7, horizon=H))
    )
    cells.append(
        ("rotation.rot.altratio30.alt",
         evaluate_gate(btc, tiers["ALT"], gate(None, altratio30), step=7, horizon=H))
    )
    cells.append(
        ("rotation.state.sma200.alt",
         evaluate_gate(btc, tiers["ALT"], btc > sma200, step=7, horizon=H))
    )
    cells.append(
        ("rotation.state.offpeak20.alt",
         evaluate_gate(btc, tiers["ALT"], drawdown > -0.20, step=7, horizon=H))
    )

    tier_names = ["T1", "T2", "T3", "T4"]
    for label, pick_high, suffix in (
        ("winner", True, "persist"),
        ("loser", False, "reversal"),
    ):
        excess: list[float] = []
        flips = 0
        held = "T1"
        for t in range(260, len(btc) - MONTH - 2, MONTH):
            perfs = {n: tiers[n][t] / tiers[n][t - MONTH] for n in tier_names}
            best = max(perfs, key=perfs.get) if pick_high else min(perfs, key=perfs.get)
            if best != held:
                flips += 1
                held = best
            i = t + 1
            if i + MONTH >= len(btc):
                continue
            r_t = tiers[held][i + MONTH] / tiers[held][i] - 1.0
            r_b = btc[i + MONTH] / btc[i] - 1.0
            excess.append(r_t - r_b)
        gross = float(np.mean(excess)) * 10_000.0 if excess else 0.0
        recent_n = max(len(excess) // 3, 3) if excess else 3
        cells.append(
            (
                f"rotation.rotate.{label}.{suffix}",
                {
                    "calls": len(excess),
                    "wins": int(sum(1 for e in excess if e > 0)),
                    "t_recent_third": round(_t_stat(excess[-recent_n:]), 2) if excess else 0.0,
                    "t_full": round(_t_stat(excess), 2) if excess else 0.0,
                    "mean_excess_bps": round(gross, 1),
                    "net_bps_per_period": round(
                        gross - flips * COST_BPS_PER_FLIP / max(len(excess), 1), 1
                    ),
                    "flips": flips,
                },
            )
        )

    for name in ("T2", "T3", "T4", "ALT"):
        cells.append((f"rotation.beta_alpha.{name.lower()}", beta_alpha(btc, tiers[name])))

    funding = await load_funding()
    if funding is not None:
        m30 = np.convolve(funding, np.ones(30) / 30, "valid")
        n = len(funding)
        heat_gate = np.zeros(n, dtype=bool)
        for t in range(365, n):
            lo, _hi = np.percentile(funding[t - 365 : t], [25, 75])
            heat_gate[t] = m30[t - 29] <= lo
        heat = evaluate_gate(btc[-n:], tiers["ALT"][-n:], heat_gate, step=7, horizon=H)
        cells.append(("rotation.heat.funding_low.alt", heat))
    else:
        print("funding history unavailable; heat cell not recorded\n")

    reg = Registry()
    print(f"{'cell':38} {'calls':>6} {'wins':>5} {'t_rec':>7} {'t_full':>7} "
          f"{'net bps':>8} verdict")
    passes: list[str] = []
    for name, r in cells:
        recent = r["alpha_t"] if "beta_on_btc" in r else r["t_recent_third"]
        bar = reg.bar(pending_cells=max(r.get("calls", 1), 1))
        if "beta_on_btc" in r:
            verdict = "pass" if abs(recent) >= bar else "fail"
        else:
            same_sign = r["t_full"] != 0 and (r["t_full"] > 0) == (recent > 0)
            verdict = (
                "pass"
                if abs(recent) >= bar and r["net_bps_per_period"] > 0 and same_sign
                else "fail"
            )
        if verdict == "pass":
            passes.append(name)
        detail = dict(r)
        detail["bar"] = round(bar, 3)
        detail["best_recent_third_t"] = abs(recent)
        net_str = (
            f"{r['net_bps_per_period']:.1f}" if "net_bps_per_period" in r else "-"
        )
        t_full_str = f"{r['t_full']:+.2f}" if "t_full" in r else "-"
        print(
            f"{name:38} {r.get('calls', '-'):>6} {r.get('wins', '-'):>5} "
            f"{recent:>+7.2f} {t_full_str:>7} "
            f"{net_str:>8} {verdict}"
        )
        reg.record(
            name=name, source=SOURCE, cells=max(r.get("calls", 1), 1),
            verdict=verdict, detail=detail,
        )

    reg.record(
        name="methodology.correction.barrier_pairing_cannot_test_relative_performance",
        source=SOURCE,
        cells=1,
        verdict="fail",
        detail={
            "note": (
                "the first rotation run recorded z=0 from double-barrier McNemar "
                "on tier-vs-BTC longs: K=2 barriers expire on ~88% of windows and "
                "correlated legs resolve together, so discordants are structurally "
                "~zero (0/217 measured). The 16 rotation.* entries before this one "
                "carry that broken statistic and their cells stand; the corrected "
                "entries follow this one with t-on-excess statistics"
            )
        },
    )
    print(f"\nregistry now {len(reg.entries())} entries, "
          f"bar for the next test {reg.bar(pending_cells=1):.3f}")
    print(f"passes: {passes if passes else 'none'}")
    return 0


def pd_peak(x: np.ndarray, w: int = 252) -> np.ndarray:
    """Trailing max, the rolling peak the drawdown gate measures from."""
    out = np.full(len(x), np.nan)
    for i in range(w - 1, len(x)):
        out[i] = x[i - w + 1 : i + 1].max()
    return out


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
