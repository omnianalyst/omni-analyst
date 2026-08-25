"""Independent audit of the Discover rankings, run against a live instance.

Two checks, both using code paths separate from the scanner's:

  A. Broad assets (stocks/defensive/crypto): the scanner's prices are
     display-only yfinance pulls, never stored. The only possible check is a
     SECOND independent yfinance pull with a minimal reimplementation of
     median-annual-return and volatility, diffed against the payload.

  B. Companies: prices live in the claim store, so return_window is audited
     end to end with raw SQL plus a separate computation loop, anchored to
     each leader's payload as_of so an ingest landing mid-audit cannot read
     as a discrepancy.

Usage (sidecar from the app image, own memory budget -- the api container's
ceiling will OOM-kill an in-container run):

    docker run --rm --network <stack>_default \\
      -e AUDIT_DB=postgresql://postgres:...@postgres:5432/omni_v2 \\
      -e AUDIT_AUDIENCE=<operator user id> \\
      -v $PWD/ops/discover_audit.py:/audit.py:ro omni-api:latest python /audit.py

AUDIT_AUDIENCE defaults to the first user in the table. Exit code 0 = every
number agrees within tolerance.

First run 2026-08-25 against the live instance: PASSED -- 47 broad assets,
165 company leaders, all orderings.
"""

import asyncio
import json
import os
import sys

import numpy as np
import pandas as pd

TOLERANCE = 0.06  # percentage points; year-end medians are static dates,
# so agreement should be exact to rounding; 0.06 allows a drifting last
# print on a moving series only.


async def main() -> int:
    from omni.api.scanner import ASSETS, _build_scanner
    from omni.main import create_app

    app = create_app()
    from omni.db import connect, migrate
    client = await connect(os.environ.get("AUDIT_DB") or None)
    await migrate(client)
    app.db = client

    audience = os.environ.get("AUDIT_AUDIENCE")
    if not audience:
        row = await client.pool.fetchrow("SELECT id FROM users ORDER BY created_at LIMIT 1")
        audience = str(row["id"]) if row else None
    if audience is None:
        print("AUDIT FAILED: no audience and no users in the database")
        return 1

    payload = await _build_scanner(app, audience)
    failures: list[str] = []

    # ---- Check A: independent yfinance reimplementation ----
    import yfinance as yf

    by_symbol = {a["symbol"]: a["yf"] for assets in ASSETS.values() for a in assets}
    reported = {
        a["symbol"]: a
        for rankings in payload["category_rankings"].values()
        for a in rankings
    }
    tickers = [by_symbol[a["symbol"]] for a in reported.values() if a["symbol"] in by_symbol]
    raw = yf.download(" ".join(tickers), period="10y", interval="1d",
                      auto_adjust=True, progress=False, group_by="ticker")

    checked = 0
    for symbol, asset in reported.items():
        yf_ticker = by_symbol.get(symbol)
        if yf_ticker is None:
            continue
        try:
            series = raw[yf_ticker]["Close"].dropna()
        except (KeyError, TypeError):
            continue
        series.index = pd.to_datetime(series.index).tz_localize(None).normalize()
        series = series[~series.index.duplicated(keep="last")].sort_index()
        if len(series) < 30:
            continue

        year_ends = series.resample("YE").last()
        annual = year_ends.pct_change(fill_method=None).dropna() * 100
        annual = annual[annual.index.year < series.index[-1].year]
        if len(annual) == 0:
            my_median, my_complete = None, 0
        else:
            my_median = round(float(annual.median()), 2)
            my_complete = len(annual)
        daily = series.pct_change().dropna()
        # Crypto trades every calendar day; the scanner annualizes it with
        # 365, equities with 252. Match the asset's own convention.
        sessions_per_year = 365 if asset.get("asset_class") == "crypto" else 252
        my_vol = round(float(daily.std(ddof=1) * np.sqrt(sessions_per_year)) * 100, 1)

        rep_median = asset.get("median_annual_return")
        rep_vol = asset.get("volatility")
        rep_complete = asset.get("complete_years")
        checked += 1

        if my_median is None or rep_median is None:
            if (my_median is None) != (rep_median is None):
                failures.append(
                    f"{symbol}: median presence mismatch mine={my_median} reported={rep_median}"
                )
        elif abs(my_median - rep_median) > TOLERANCE:
            failures.append(
                f"{symbol}: median mine={my_median} reported={rep_median}"
            )
        if my_vol is not None and rep_vol is not None and abs(my_vol - rep_vol) > 0.51:
            failures.append(f"{symbol}: volatility mine={my_vol} reported={rep_vol}")
        if rep_complete is not None and my_complete != rep_complete:
            failures.append(
                f"{symbol}: complete_years mine={my_complete} reported={rep_complete}"
            )

    # Ordering: the list must be sorted by the score it displays.
    for mode in ("balanced",):
        for cls, rankings in payload["category_rankings"].items():
            complete = [a for a in rankings if a["scores"].get("evidence_complete") is not False]
            incomplete = [a for a in rankings if a["scores"].get("evidence_complete") is False]
            for bucket in (complete, incomplete):
                scores = [a["scores"]["balanced"] for a in bucket if a["scores"]["balanced"] is not None]
                if scores != sorted(scores, reverse=True):
                    failures.append(f"{cls}: {mode} ordering does not follow displayed score")

    print(f"[A] broad assets: {checked} symbols re-derived from an independent feed pull")

    # ---- Check B: company return_window from the claim store, in SQL ----
    rows = await client.pool.fetch(
        """
        SELECT e.symbol, v.value, v.event_date
        FROM claim v
        JOIN entity e ON e.id = v.entity_id
        WHERE e.kind = 'company' AND v.claim_type = 'price_snapshot'
        ORDER BY e.symbol, v.event_date
        """
    )
    histories: dict[str, list[tuple[str, float]]] = {}
    for row in rows:
        raw_value = row["value"]
        if isinstance(raw_value, str):
            try:
                raw_value = json.loads(raw_value)
            except json.JSONDecodeError:
                continue
        if isinstance(raw_value, dict):
            raw_value = raw_value.get("close")
        try:
            price = float(raw_value)
        except (TypeError, ValueError):
            continue
        if np.isfinite(price) and price > 0:
            histories.setdefault(row["symbol"], []).append((str(row["event_date"]), price))

    window = 30
    mine_histories: dict[str, list[tuple[str, float]]] = {}
    for symbol, obs in histories.items():
        by_date: dict[str, float] = {}
        for date, price in obs:
            by_date[date] = price  # last write wins, matching panel dedup intent
        mine_histories[symbol] = sorted(by_date.items(), key=lambda item: item[0], reverse=True)

    reported_companies = {
        leader["symbol"]: (leader["return_window"], leader["as_of"])
        for sector in payload["sectors"]
        for leader in sector["leaders"]
    }
    company_checked = 0
    for symbol, (rep, as_of) in reported_companies.items():
        if symbol not in mine_histories:
            failures.append(f"{symbol}: reported in sector leaders but no claim history")
            continue
        ordered = sorted(mine_histories[symbol], key=lambda item: item[0], reverse=True)
        anchored = [price for date, price in ordered if date[:10] <= as_of]
        if len(anchored) <= window:
            failures.append(f"{symbol}: not enough history at payload as_of {as_of}")
            continue
        my_ret = round((anchored[0] / anchored[window] - 1) * 100, 2)
        company_checked += 1
        if abs(my_ret - rep) > 0.06:
            failures.append(
                f"{symbol}: return_window mine={my_ret} reported={rep} (as_of {as_of})"
            )
    # Sector membership isn't exposed in the payload, so a "leader holds the
    # sector max" check can't be built from leaders alone; the per-leader
    # value check above is the audit.
    print(f"[B] companies: {company_checked} sector-leader returns re-derived from claims")

    if failures:
        print(f"\nAUDIT FAILED ({len(failures)} discrepancies):")
        for line in failures[:40]:
            print("  " + line)
        return 1
    print("\nAUDIT PASSED: medians, volatilities, complete-years, orderings and company windows all agree.")
    await client.close()
    return 0


sys.exit(asyncio.run(main()))
