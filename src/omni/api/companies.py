"""Individual companies, measured on the same terms as everything else.

DELIBERATELY A SEPARATE ENDPOINT, not a fourth category inside
`/scanner/market`. The ETF-versus-constituent experiment
(`docs/ETF_PORTFOLIO_EXPERIMENT.md`) measured a price-quality ranker over these
same names and it did not pass: it beat its sector ETF in 3 of 9 testable
sectors, median excess CAGR -2.70%, with the positive mean carried almost
entirely by one technology result. ETFs remain the default core.

So these rankings exist because hiding a measurement is its own dishonesty --
the scanner ranks 28 diversified funds and a reader is entitled to know how the
underlying companies score on the same axes -- and they sit apart because
folding them into the core would read as an endorsement the evidence does not
support.

Prices come from the audience-visible claim store (Polygon, `byo_only`), not
from the yfinance display feed the broad-asset scanner uses. That means an
anonymous caller sees nothing here, which is correct: the underlying claims are
licensed to their fetcher.
"""

from __future__ import annotations

import json
import time
from datetime import UTC, datetime
from typing import Any

import numpy as np
import pandas as pd
from neutron import App, Router
from neutron.error import unauthorized
from starlette.requests import Request

from omni.api.scanner import (
    MIN_SHARPE_VOLATILITY,
    _compute_metrics,
    _percentile_scores,
    _risk_tier,
    _tier_census,
)
from omni.auth import resolve_audience_from_request
from omni.coverage.visibility import visible_claims_cte

CACHE_TTL = 3600
MIN_SESSIONS = 60
DISPLAY_LIMIT = 15

# A rank within a sub-industry only says something if there is a field to be
# ranked within. At two names the percentile construction can only ever emit 50
# and 100, so "1st of 2" would read as a standing the comparison cannot support.
# Below this the industry rank is withheld with a stated reason -- never filled
# with the global rank, which would silently answer a different question.
MIN_INDUSTRY_PEERS = 3

_cache: dict[str, dict[str, Any]] = {}


def _identifiers(value: Any) -> dict[str, Any]:
    """asyncpg hands back jsonb as a string, not a dict. See `_number`."""
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except json.JSONDecodeError:
            return {}
    return value if isinstance(value, dict) else {}


def _text(identifiers: dict[str, Any], key: str) -> str | None:
    value = identifiers.get(key)
    if not isinstance(value, str):
        return None
    value = value.strip()
    return value or None


def _number(value: Any) -> float | None:
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except json.JSONDecodeError:
            return None
    if isinstance(value, dict):
        value = value.get("value")
    if value is None or isinstance(value, bool):
        return None
    try:
        out = float(value)
    except (TypeError, ValueError):
        return None
    return out if np.isfinite(out) else None


def _balanced(entries: list[dict], *, key: str = "scores") -> None:
    """Attach a population-relative balanced score, in place.

    Same construction as the broad-asset scanner: percentile ranks within this
    population, reweighted over whichever components a name actually has. It is
    a ranking against these peers, not a forecast and not a recommendation.

    `key` is what makes the industry ranking a genuinely different measurement
    rather than the global one re-sorted: called over a sub-industry's members
    the percentiles are computed against those members only, so a semiconductor
    company is scored against semiconductor companies.
    """
    if not entries:
        return
    components = {
        "cagr_5y": (_percentile_scores(entries, "cagr_5y"), 0.35),
        "positive_years": (_percentile_scores(entries, "positive_year_rate"), 0.25),
        "volatility": (_percentile_scores(entries, "volatility", inverse=True), 0.20),
        "return_365d": (_percentile_scores(entries, "return_365d"), 0.10),
        "drawdown": (_percentile_scores(entries, "max_drawdown"), 0.10),
    }
    for entry in entries:
        total = 0.0
        weight = 0.0
        for scores, w in components.values():
            value = scores.get(entry["symbol"])
            if value is None:
                continue
            total += value * w
            weight += w
        entry[key] = {
            "balanced": round(total / weight, 1) if weight > 0 else None,
            "components_available": round(weight / 1.0, 2),
        }


def _rank_within_industries(ranked: list[dict]) -> list[dict]:
    """Score and rank each company against its own GICS sub-industry.

    The global ranking stays exactly as it was; this is an additional
    measurement, not a replacement. A company with no stored sub-industry, or
    one in a group too small to rank in, gets a null rank and a named reason --
    never a fabricated position and never the global rank standing in.

    Returns the group summaries, largest first.
    """
    groups: dict[str, list[dict]] = {}
    for entry in ranked:
        entry["industry_rank"] = None
        entry["industry_peers"] = 0
        entry["industry_scores"] = None
        industry = entry.get("industry")
        if industry is None:
            entry["industry_rank_reason"] = "no verified GICS sub-industry stored"
            continue
        groups.setdefault(industry, []).append(entry)

    summaries: list[dict] = []
    for industry, members in groups.items():
        for entry in members:
            entry["industry_peers"] = len(members)
        if len(members) < MIN_INDUSTRY_PEERS:
            for entry in members:
                entry["industry_rank_reason"] = (
                    f"{len(members)} measured in this sub-industry, "
                    f"fewer than the {MIN_INDUSTRY_PEERS} a rank needs to mean anything"
                )
            summaries.append({
                "industry": industry,
                "sector": members[0].get("sector"),
                "measured": len(members),
                "ranked": False,
                "reason": (
                    f"fewer than {MIN_INDUSTRY_PEERS} measured companies in this "
                    "sub-industry"
                ),
                "companies": [],
            })
            continue

        _balanced(members, key="industry_scores")
        scored = [m for m in members if m["industry_scores"]["balanced"] is not None]
        scored.sort(key=lambda m: m["industry_scores"]["balanced"], reverse=True)
        for position, entry in enumerate(scored, start=1):
            entry["industry_rank"] = position
            entry["industry_rank_reason"] = None
        for entry in members:
            if entry["industry_rank"] is None:
                entry["industry_rank_reason"] = "no component available to score"
        summaries.append({
            "industry": industry,
            "sector": members[0].get("sector"),
            "measured": len(members),
            "ranked": True,
            "reason": None,
            "companies": [m["symbol"] for m in scored],
        })

    summaries.sort(key=lambda s: (-s["measured"], s["industry"]))
    return summaries


async def _load(pool, audience) -> dict[str, Any]:
    visible = visible_claims_cte("$1")
    rows = await pool.fetch(
        f"""
        WITH visible AS ({visible})
        SELECT e.symbol, e.name, e.identifiers, v.value, v.event_date
        FROM visible v
        JOIN entity e ON e.id = v.entity_id
        WHERE e.kind = 'company' AND v.claim_type = 'price_snapshot'
        ORDER BY e.symbol, v.event_date
        """,
        audience,
    )

    series: dict[str, dict[str, Any]] = {}
    for row in rows:
        price = _number(row["value"])
        if price is None or price <= 0:
            continue
        identifiers = _identifiers(row["identifiers"])
        bucket = series.setdefault(row["symbol"], {
            "name": row["name"],
            "sector": _text(identifiers, "gics_sector"),
            "industry": _text(identifiers, "gics_sub_industry"),
            "points": {},
        })
        bucket["points"][pd.Timestamp(row["event_date"]).tz_convert(UTC).normalize()] = price

    entries: list[dict[str, Any]] = []
    thin = 0
    for symbol, bucket in series.items():
        prices = pd.Series(bucket["points"]).sort_index()
        if len(prices) < MIN_SESSIONS:
            thin += 1
            continue
        metrics = _compute_metrics(prices, "stocks")
        entries.append({
            "symbol": symbol,
            "name": bucket["name"],
            "sector": bucket["sector"],
            "industry": bucket["industry"],
            "price": metrics["price"],
            "return_30d": metrics["returns"].get("30d"),
            "return_90d": metrics["returns"].get("90d"),
            "return_365d": metrics["returns"].get("365d"),
            "volatility": metrics["volatility"],
            "risk_tier": _risk_tier(metrics["volatility"]),
            "max_drawdown": metrics["max_drawdown"],
            "sharpe": metrics["sharpe"],
            "cagr_5y": metrics["cagr_5y"],
            "positive_year_rate": metrics["positive_year_rate"],
            "history_years": metrics["history_years"],
            "sessions": len(prices),
        })

    _balanced(entries)
    ranked = [e for e in entries if e.get("scores", {}).get("balanced") is not None]
    ranked.sort(key=lambda e: e["scores"]["balanced"], reverse=True)

    industries = _rank_within_industries(ranked)

    return {
        "companies": ranked,
        "leaders": ranked[:DISPLAY_LIMIT],
        "industries": industries,
        "risk_census": _tier_census(ranked),
        "coverage": {
            "measured": len(ranked),
            "with_prices": len(series),
            "too_thin": thin,
            "min_sessions": MIN_SESSIONS,
            "with_industry": sum(1 for e in ranked if e.get("industry") is not None),
            "without_industry": sum(1 for e in ranked if e.get("industry") is None),
            "industries_ranked": sum(1 for i in industries if i["ranked"]),
            "industries_below_min_peers": sum(1 for i in industries if not i["ranked"]),
            "min_industry_peers": MIN_INDUSTRY_PEERS,
        },
        "standing": {
            "verdict": (
                "ETFs remain the default core. A price-quality ranker over these "
                "same names beat its sector ETF in only 3 of 9 testable sectors, "
                "median excess CAGR -2.70%, with the mean carried by a single "
                "technology result."
            ),
            "report": "docs/ETF_PORTFOLIO_EXPERIMENT.md",
            "scope": (
                "Percentile ranks against the other companies measured here. A "
                "measurement, not a forecast and not a recommendation."
            ),
            "industry": (
                "`industry_rank` re-runs the same construction against the "
                "company's own GICS sub-industry, so a semiconductor company is "
                "scored against semiconductor companies rather than against every "
                "software house that shares its sector. The global rank is "
                f"unchanged and still published. Withheld below "
                f"{MIN_INDUSTRY_PEERS} measured peers, and for any company whose "
                "sub-industry the store does not hold -- with the reason on the "
                "company, never a filled-in position."
            ),
            "risk_tier": (
                "Annualised volatility under 10% is low, under 30% medium, at or "
                "above it high. Unlike the diversified-fund categories, individual "
                "companies do reach the high tier."
            ),
            "sharpe": (
                f"Withheld below {MIN_SHARPE_VOLATILITY:.0f}% annualised volatility, "
                f"where the ratio measures noise rather than risk-adjusted return."
            ),
        },
        "as_of": datetime.now(UTC).isoformat(),
    }


def build_router(app: App) -> Router:
    router = Router()

    @router.get("/scanner/companies")
    async def companies(request: Request) -> dict:
        audience = resolve_audience_from_request(request)
        if audience is None:
            # Company prices are byo_only Polygon claims. An anonymous caller is
            # not entitled to them, and returning an empty ranking would imply
            # the universe is empty rather than unlicensed to them.
            raise unauthorized("Authentication required")

        key = f"companies:{audience}"
        hit = _cache.get(key)
        now = time.time()
        if hit and now - hit["at"] < CACHE_TTL:
            return hit["payload"]

        payload = await _load(app.db.pool, audience)
        _cache[key] = {"at": now, "payload": payload}
        return payload

    return router
