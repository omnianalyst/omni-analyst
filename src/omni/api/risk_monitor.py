"""PCA-based risk monitor for the carry book.

Computes the carry book's actual factor exposures by eigendecomposing the
return covariance matrix of its held assets. The carry book is designed to be
delta-neutral (long spot + short perp on the same asset). PCA verifies this:
if the first principal component (the market factor) has significant exposure,
the book has drifted from neutrality and carries hidden directional risk.

Returns a simple verdict: delta-neutral (healthy) or factor-exposed (drift).
"""

from __future__ import annotations

from typing import Any

import numpy as np
import pandas as pd
from neutron import App, Router
from starlette.requests import Request

LOOKBACK_DAYS = 60


async def _positions(pool, portfolio_id) -> list[dict]:
    rows = await pool.fetch(
        """
        SELECT symbol, market_type, quantity::float8 as quantity
        FROM position
        WHERE portfolio_id = $1 AND quantity != 0
        """,
        portfolio_id,
    )
    return [dict(r) for r in rows]


async def _prices(pool, symbols: list[str]) -> pd.DataFrame:
    """Daily closes for the carry book's assets from the claim store."""
    if not symbols:
        return pd.DataFrame()

    rows = await pool.fetch(
        """
        SELECT c.event_date,
               split_part(split_part(c.key, '/', 1), ':', 1) AS asset,
               (c.value ->> 'close')::float8 AS close
        FROM claim c
        WHERE c.claim_type = 'price_snapshot'
          AND c.source = 'ccxt'
          AND c.superseded_by IS NULL
          AND c.event_date > now() - interval '90 days'
          AND split_part(split_part(c.key, '/', 1), ':', 1) = ANY($1::text[])
        ORDER BY c.event_date
        """,
        symbols,
    )
    if not rows:
        return pd.DataFrame()

    df = pd.DataFrame([
        {"date": pd.Timestamp(r["event_date"]).tz_convert(None).normalize(),
         "asset": r["asset"], "close": r["close"]}
        for r in rows if r["close"] is not None
    ])
    if df.empty:
        return pd.DataFrame()

    panel = df.pivot_table(index="date", columns="asset", values="close", aggfunc="last")
    return panel.sort_index().tail(LOOKBACK_DAYS)


def _extract_assets(positions: list[dict]) -> list[str]:
    """Extract base asset symbols from position symbols (BTC/USDC -> BTC)."""
    assets: set[str] = set()
    for pos in positions:
        sym = pos["symbol"].split("/")[0].split(":")[0]
        if sym:
            assets.add(sym)
    return sorted(assets)


def _pca_risk(returns: pd.DataFrame, positions: list[dict]) -> dict[str, Any]:
    """Eigendecompose the covariance matrix and check for market-factor exposure.

    For a delta-neutral book, the position-weighted exposure to the first
    principal component (market mode) should be near zero. If it's not, the
    book is carrying hidden directional risk.
    """
    assets = returns.columns.tolist()
    if len(assets) < 2 or len(returns) < 20:
        return {
            "verdict": "insufficient_data",
            "detail": f"Need 2+ assets and 20+ days; have {len(assets)} assets, {len(returns)} days",
        }

    cov = returns.cov()
    eigenvalues, eigenvectors = np.linalg.eigh(cov.values)

    # Sort descending (eigh returns ascending)
    order = np.argsort(eigenvalues)[::-1]
    eigenvalues = eigenvalues[order]
    eigenvectors = eigenvectors[:, order]

    # First PC = market factor. Its explained variance share:
    total_var = eigenvalues.sum()
    pc1_share = float(eigenvalues[0] / total_var) if total_var > 0 else 0

    # Net position per asset (spot + perp, signed)
    net: dict[str, float] = {a: 0.0 for a in assets}
    for pos in positions:
        sym = pos["symbol"].split("/")[0].split(":")[0]
        if sym in net:
            qty = float(pos["quantity"])
            mt = pos["market_type"]
            if mt == "perpetual":
                net[sym] += qty
            else:
                net[sym] += qty

    # Exposure to PC1: dot product of net positions with the first eigenvector
    net_vector = np.array([net.get(a, 0) for a in assets])
    pc1_vector = eigenvectors[:, 0]

    # Normalize: the magnitude of net vs the magnitude of gross
    gross = sum(abs(v) for v in net.values())
    abs(float(net_vector @ pc1_vector))

    # For a balanced pair (equal long/short), net_vector ≈ 0
    # The ratio of net to gross tells us how delta-exposed we are
    net_ratio = abs(sum(net.values())) / gross if gross > 0 else 0

    if net_ratio < 0.05:
        verdict = "delta_neutral"
    elif net_ratio < 0.15:
        verdict = "slight_drift"
    else:
        verdict = "factor_exposed"

    return {
        "verdict": verdict,
        "net_ratio": round(net_ratio, 4),
        "pc1_share": round(pc1_share, 3),
        "pc1_label": _pc1_label(pc1_share),
        "positions": {a: round(net[a], 6) for a in assets},
        "eigenvalues": [round(float(e), 8) for e in eigenvalues[:5]],
        "n_assets": len(assets),
        "n_days": len(returns),
    }


def _pc1_label(share: float) -> str:
    if share > 0.6:
        return "One factor dominates (typical: assets move together)"
    if share > 0.4:
        return "Moderate common factor"
    return "Diversified (no single dominant factor)"


async def _assess(pool) -> dict[str, Any]:
    """Assess the carry book's risk via PCA."""
    pid = await pool.fetchval("SELECT id FROM portfolio LIMIT 1")
    if pid is None:
        return {"verdict": "no_portfolio"}

    positions = await _positions(pool, pid)
    if not positions:
        return {"verdict": "flat", "detail": "No open positions"}

    assets = _extract_assets(positions)
    prices = await _prices(pool, assets)

    if prices.empty or len(prices) < 20:
        return {
            "verdict": "insufficient_data",
            "detail": f"Need 20+ days of price history; have {len(prices)}",
            "positions": len(positions),
        }

    returns = prices.pct_change().dropna()
    result = _pca_risk(returns, positions)
    result["portfolio_id"] = str(pid)
    return result


_cache: dict[str, Any] = {"data": None, "ts": 0.0}


def build_router(app: App) -> Router:
    router = Router()

    @router.get("/scanner/risk")
    async def scanner_risk(request: Request) -> dict:
        import time as _time
        now = _time.time()
        if _cache["data"] is not None and now - _cache["ts"] < 600:
            return _cache["data"]

        result = await _assess(app.db.pool)
        _cache["data"] = result
        _cache["ts"] = now
        return result

    return router


__all__ = ["build_router"]
