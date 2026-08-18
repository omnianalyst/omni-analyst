"""The ratio-room cell: does distance from the prior ratio peak predict room?

Tyler's refined claim: an alt can 3x while its ALT/BTC ratio sits far below
its old cycle peak -- that distance IS the remaining upside, so hold until
the ratio approaches the old peak. The mechanism working against it: ratio
peaks decay roughly by half each cycle (ETH/BTC ~0.15 in 2017, ~0.088 in
2021, lower since), so "below the old peak" is the permanent condition of
every finished alt, not a measure of remaining room.

Three cells, structured so the claim can fail informatively. If distance
from peak carries information, far-below-peak windows should show positive
excess vs BTC and near-peak windows negative. If both are flat, the peak
distance is noise -- and the declining-peak mechanism stands unanswered.

  ratio_room.below50.peak252   ratio < 50% of trailing 252d peak -> hold ALT
  ratio_room.below50.peak756   ratio < 50% of trailing 756d peak (the
                               prior-cycle operationalisation, the literal
                               version of the claim)
  ratio_room.near25.peak252    ratio > 75% of trailing 252d peak -> hold ALT
                               (the claimed exit zone; the claim needs this
                               one NEGATIVE)

Same panel, alignment, costs and verdict rule as the rotation batch: signal
at close t, entry t+1, 14d window, t on per-window excess vs BTC, verdict on
the most recent third against bar_for(pending), 40 bps per flip.

Run: uv run python ops/ratio_room_batch.py
"""

from __future__ import annotations

import asyncio
import sys

import numpy as np

sys.path.insert(0, "ops")
from rotation_batch import (
    SOURCE,
    TIERS,
    H,
    Registry,
    align_on_btc,
    evaluate_gate,
    load_panel,
    pd_peak,
    tier_index,
)


def trailing_peak(x: np.ndarray, w: int) -> np.ndarray:
    return pd_peak(x, w)


async def main() -> int:
    raw = await load_panel()
    closes = align_on_btc(raw)
    btc = closes["BTC"]
    alts = [s for members in TIERS.values() for s in members]
    alt = tier_index(closes, alts)
    ratio = alt / btc

    peak252 = trailing_peak(ratio, 252)
    peak756 = trailing_peak(ratio, 756)
    below50_252 = ratio < 0.50 * peak252
    below50_756 = ratio < 0.50 * peak756
    near25_252 = ratio > 0.75 * peak252
    n = len(ratio)
    for label, gate in (
        ("below 50% of 252d peak", below50_252),
        ("below 50% of 756d peak", below50_756),
        ("above 75% of 252d peak", near25_252),
    ):
        usable = np.sum(gate[756:n])  # gate comparable only where both peaks exist
        denom = max(n - 756, 1)
        print(f"gate {label:28} on {usable}/{denom} sessions "
              f"({100.0 * usable / denom:.0f}%)")

    eth = closes.get("ETH")
    if eth is not None:
        print("\nratio-peak decay, ETH/BTC max per rolling year (context, not a cell):")
        ethbtc = eth / btc
        for start in range(0, n - 365, 365):
            window = ethbtc[start : start + 365]
            frac = start / max(n - 365, 1)
            print(f"  year {frac:4.0%} into the aligned panel: max {window.max():.4f}")
        print()

    cells = [
        ("rotation.ratio_room.below50.peak252",
         evaluate_gate(btc, alt, below50_252, step=7, horizon=H)),
        ("rotation.ratio_room.below50.peak756",
         evaluate_gate(btc, alt, below50_756, step=7, horizon=H)),
        ("rotation.ratio_room.near25.peak252",
         evaluate_gate(btc, alt, near25_252, step=7, horizon=H)),
    ]

    reg = Registry()
    print(f"\n{'cell':40} {'calls':>6} {'wins':>5} {'t_rec':>7} {'t_full':>7} "
          f"{'net bps':>8} verdict")
    for name, r in cells:
        bar = reg.bar(pending_cells=max(r["calls"], 1))
        same_sign = r["t_full"] != 0 and (r["t_full"] > 0) == (r["t_recent_third"] > 0)
        verdict = (
            "pass"
            if abs(r["t_recent_third"]) >= bar
            and r["net_bps_per_period"] > 0
            and same_sign
            else "fail"
        )
        detail = dict(r)
        detail["bar"] = round(bar, 3)
        detail["best_recent_third_t"] = abs(r["t_recent_third"])
        print(
            f"{name:40} {r['calls']:>6} {r['wins']:>5} "
            f"{r['t_recent_third']:>+7.2f} {r['t_full']:>+7.2f} "
            f"{r['net_bps_per_period']:>8.1f} {verdict}"
        )
        reg.record(
            name=name, source=SOURCE, cells=max(r["calls"], 1),
            verdict=verdict, detail=detail,
        )

    print(f"\nregistry now {len(reg.entries())} entries, "
          f"bar for the next test {reg.bar(pending_cells=1):.3f}")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
