"""One entity, measured — the page you read before deciding anything about it.

The coverage view answers "what do we hold about this name?". That is the right
question for an operator auditing the store and the wrong one for someone
looking at TSLA, who wants price, risk, fundamentals and where it sits against
its peers. This endpoint computes those from claims the caller may actually see,
and says plainly which of them it could not compute.

Everything here is a measurement over the claim store. Nothing is fetched live,
nothing is estimated to fill a hole, and every fundamental carries the filing it
came from. A figure this endpoint cannot support is `null` with a named reason
in `limits` -- never a zero, and never a stale number presented as current.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

import numpy as np
import pandas as pd
from neutron import App, Router
from neutron.error import not_found, unauthorized
from starlette.requests import Request

from omni.api.scanner import (
    MIN_SHARPE_VOLATILITY,
    _market_behavior,
    _risk_tier,
)
from omni.auth import resolve_audience_from_request
from omni.coverage.visibility import visible_claims_cte

# The market proxy every correlation is measured against. SPY rather than an
# index level because a tradeable series is what a reader can actually hold.
MARKET_SYMBOL = "SPY"

# Minimum overlapping sessions before a correlation is reported. Below this the
# coefficient is dominated by whichever few days happen to align.
MIN_CORRELATION_SESSIONS = 30

# Minimum price observations before any risk statistic is reported at all.
MIN_RISK_SESSIONS = 30

# The fundamentals worth surfacing, in reading order, with the plain-language
# label and whether a larger value is straightforwardly better. `None` means
# the direction is genuinely ambiguous (debt is not simply bad, cash is not
# simply good) and the UI should not colour it.
HEADLINE_FUNDAMENTALS: list[tuple[str, str, bool | None]] = [
    ("Revenues", "Revenue", True),
    ("GrossProfit", "Gross profit", True),
    ("NetIncomeLoss", "Net income", True),
    ("NetCashProvidedByUsedInOperatingActivities", "Operating cash flow", True),
    ("Assets", "Total assets", None),
    ("Liabilities", "Total liabilities", None),
    ("StockholdersEquity", "Shareholder equity", True),
    ("CashAndCashEquivalentsAtCarryingValue", "Cash and equivalents", None),
    ("LongTermDebt", "Long-term debt", None),
    ("PaymentsToAcquirePropertyPlantAndEquipment", "Capital expenditure", None),
    ("CommonStockSharesOutstanding", "Shares outstanding", None),
]

_FUNDAMENTAL_KEYS = [key for key, _, _ in HEADLINE_FUNDAMENTALS]


def _number(value: Any) -> float | None:
    """Unwrap a claim value, refusing anything that is not a finite number.

    asyncpg hands back `jsonb` as a **string** unless a codec is registered, so
    the dict case alone is not enough -- without the decode step every price
    silently became `None` and the page rendered as "no data stored" for an
    entity holding hundreds of observations. Honest-looking, and wrong.
    """
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


def _series(rows: list) -> pd.Series:
    """A price series indexed by event date, newest last, duplicates collapsed."""
    points: dict[pd.Timestamp, float] = {}
    for row in rows:
        price = _number(row["value"])
        if price is None or price <= 0:
            continue
        points[pd.Timestamp(row["event_date"]).tz_convert(UTC).normalize()] = price
    if not points:
        return pd.Series(dtype=float)
    return pd.Series(points).sort_index()


def _risk(prices: pd.Series, market: pd.Series) -> tuple[dict[str, Any], list[str]]:
    """Risk statistics, and the reasons any of them are missing."""
    limits: list[str] = []
    blank = {
        "volatility": None,
        "risk_tier": "unrated",
        "max_drawdown": None,
        "sharpe": None,
        "correlation_to_market": None,
        "market_behavior": "unrated",
        "sessions": len(prices),
        "history_days": None,
    }

    if len(prices) < MIN_RISK_SESSIONS:
        limits.append(
            f"Risk statistics need {MIN_RISK_SESSIONS} price observations; "
            f"{len(prices)} are stored."
        )
        return blank, limits

    daily = prices.pct_change().dropna()
    ann_vol = float(daily.std() * np.sqrt(252) * 100)
    ann_ret = float(daily.mean() * 252 * 100)

    cumulative = (1 + daily).cumprod()
    drawdown = (cumulative - cumulative.expanding().max()) / cumulative.expanding().max()

    # Same floor as the scanner, for the same reason: below it the ratio
    # measures the denominator's noise rather than risk-adjusted return.
    sharpe = (
        round(ann_ret / ann_vol, 2)
        if np.isfinite(ann_vol) and ann_vol >= MIN_SHARPE_VOLATILITY
        else None
    )
    if sharpe is None:
        limits.append(
            f"Sharpe is withheld below {MIN_SHARPE_VOLATILITY:.0f}% annualised "
            f"volatility, where the ratio reflects noise rather than skill."
        )

    correlation = None
    if not market.empty:
        joined = pd.concat([prices, market], axis=1, join="inner").pct_change().dropna()
        if len(joined) >= MIN_CORRELATION_SESSIONS:
            value = float(joined.iloc[:, 0].corr(joined.iloc[:, 1]))
            correlation = round(value, 2) if np.isfinite(value) else None
    if correlation is None:
        limits.append(
            f"Correlation to {MARKET_SYMBOL} needs {MIN_CORRELATION_SESSIONS} "
            f"overlapping sessions in the store."
        )

    span = (prices.index[-1] - prices.index[0]).days
    return {
        "volatility": round(ann_vol, 1) if np.isfinite(ann_vol) else None,
        "risk_tier": _risk_tier(ann_vol),
        "max_drawdown": round(float(drawdown.min()) * 100, 2),
        "sharpe": sharpe,
        "correlation_to_market": correlation,
        "market_behavior": _market_behavior(correlation),
        "sessions": len(prices),
        "history_days": int(span),
    }, limits


def _returns(prices: pd.Series) -> dict[str, float | None]:
    """Trailing returns, null where the window exceeds the stored history."""
    if prices.empty:
        return {"30d": None, "90d": None, "365d": None}
    current = float(prices.iloc[-1])

    def trailing(days: int) -> float | None:
        if len(prices) <= days:
            return None
        start = float(prices.iloc[-days - 1])
        if start <= 0:
            return None
        return round(((current / start) - 1) * 100, 2)

    return {"30d": trailing(30), "90d": trailing(90), "365d": trailing(365)}


def _fundamentals(rows: list) -> list[dict[str, Any]]:
    """The newest observation of each headline metric, with its filing.

    Newest by `knowledge_date`, not `event_date`: a restatement filed later for
    an earlier period supersedes the original, and ordering on the fiscal date
    would keep showing the number that was corrected.
    """
    labels = {key: label for key, label, _ in HEADLINE_FUNDAMENTALS}
    better = {key: direction for key, _, direction in HEADLINE_FUNDAMENTALS}
    newest: dict[str, Any] = {}

    for row in rows:
        key = row["key"]
        if key in newest:
            continue
        value = _number(row["value"])
        if value is None:
            continue
        evidence = row["evidence"] or {}
        if isinstance(evidence, str):
            evidence = json.loads(evidence)
        newest[key] = {
            "key": key,
            "label": labels.get(key, key),
            "value": value,
            "unit": row["unit"],
            "higher_is_better": better.get(key),
            "period_end": row["event_date"].date().isoformat(),
            "knowable_from": row["knowledge_date"].date().isoformat(),
            "fiscal_period": evidence.get("fp"),
            "fiscal_year": evidence.get("fy"),
            "form": evidence.get("form"),
            "source": row["source"],
        }

    return [newest[key] for key in _FUNDAMENTAL_KEYS if key in newest]


def _derived(
    fundamentals: list[dict[str, Any]], price: float | None
) -> tuple[dict[str, Any], list[str]]:
    """Ratios the reader would otherwise compute by hand.

    Each is either computable from stored values or omitted. A margin needs
    both its numerator and denominator from the same filing, so a mismatch is a
    refusal rather than a ratio across two periods.
    """
    limits: list[str] = []
    by_key = {item["key"]: item for item in fundamentals}

    def paired(numerator: str, denominator: str) -> float | None:
        top, bottom = by_key.get(numerator), by_key.get(denominator)
        if top is None or bottom is None:
            return None
        if top["period_end"] != bottom["period_end"]:
            limits.append(
                f"{top['label']} and {bottom['label']} come from different "
                f"periods ({top['period_end']} and {bottom['period_end']}), so "
                f"the ratio was not computed."
            )
            return None
        if abs(bottom["value"]) < 1.0:
            return None
        return round(top["value"] / bottom["value"] * 100, 1)

    shares = by_key.get("CommonStockSharesOutstanding")
    market_cap = None
    if shares is not None and price is not None:
        market_cap = shares["value"] * price
    elif price is None:
        limits.append("Market capitalisation needs a stored price.")
    else:
        limits.append("Market capitalisation needs a shares-outstanding filing.")

    return {
        "market_cap": market_cap,
        "market_cap_as_of": shares["period_end"] if shares and market_cap else None,
        "gross_margin": paired("GrossProfit", "Revenues"),
        "net_margin": paired("NetIncomeLoss", "Revenues"),
    }, limits


def build_router(app: App) -> Router:
    router = Router()

    @router.get("/entities/{entity_id}/profile")
    async def profile(request: Request, entity_id: str) -> dict:
        audience = resolve_audience_from_request(request)
        if audience is None:
            raise unauthorized("Authentication required")

        try:
            eid = UUID(entity_id)
        except ValueError:
            raise not_found("entity not found") from None

        pool = app.db.pool
        entity = await pool.fetchrow(
            "SELECT id, symbol, name, kind, identifiers FROM entity WHERE id = $1", eid
        )
        if entity is None:
            raise not_found("entity not found")

        visible = visible_claims_cte("$1")

        price_rows = await pool.fetch(
            f"""
            WITH visible AS ({visible})
            SELECT value, event_date, source
            FROM visible
            WHERE entity_id = $2 AND claim_type = 'price_snapshot'
            ORDER BY event_date
            """,
            audience,
            eid,
        )

        market_rows = await pool.fetch(
            f"""
            WITH visible AS ({visible})
            SELECT v.value, v.event_date
            FROM visible v
            JOIN entity e ON e.id = v.entity_id
            WHERE e.symbol = $2 AND v.claim_type = 'price_snapshot'
            ORDER BY v.event_date
            """,
            audience,
            MARKET_SYMBOL,
        )

        fundamental_rows = await pool.fetch(
            f"""
            WITH visible AS ({visible})
            SELECT key, value, unit, evidence, source, event_date, knowledge_date
            FROM visible
            WHERE entity_id = $2
              AND claim_type = 'fundamental_metric'
              AND key = ANY($3::text[])
            ORDER BY knowledge_date DESC, event_date DESC
            """,
            audience,
            eid,
            _FUNDAMENTAL_KEYS,
        )

        coverage_rows = await pool.fetch(
            f"""
            WITH visible AS ({visible})
            SELECT claim_type::text AS claim_type,
                   count(*) AS claims,
                   max(knowledge_date) AS newest,
                   min(event_date) AS oldest_event
            FROM visible
            WHERE entity_id = $2
            GROUP BY claim_type
            ORDER BY claims DESC
            """,
            audience,
            eid,
        )

        prices = _series(price_rows)
        market = _series(market_rows)
        risk, risk_limits = _risk(prices, market)
        fundamentals = _fundamentals(fundamental_rows)
        latest_price = float(prices.iloc[-1]) if not prices.empty else None
        derived, derived_limits = _derived(fundamentals, latest_price)

        limits = risk_limits + derived_limits
        if prices.empty:
            limits.append("No price observations are stored for this entity.")
        if not fundamentals:
            limits.append("No headline fundamentals are stored for this entity.")

        return {
            "entity": {
                "id": str(entity["id"]),
                "symbol": entity["symbol"],
                "name": entity["name"],
                "kind": entity["kind"],
                "identifiers": entity["identifiers"] or {},
            },
            "price": {
                "latest": latest_price,
                "as_of": prices.index[-1].date().isoformat() if not prices.empty else None,
                "source": price_rows[-1]["source"] if price_rows else None,
                "returns": _returns(prices),
                "series": [
                    {"date": stamp.date().isoformat(), "close": round(float(value), 4)}
                    for stamp, value in prices.items()
                ],
            },
            "risk": risk,
            "fundamentals": fundamentals,
            "derived": derived,
            "coverage": [
                {
                    "claim_type": row["claim_type"],
                    "claims": int(row["claims"]),
                    "newest": row["newest"].isoformat() if row["newest"] else None,
                    "oldest_event": (
                        row["oldest_event"].isoformat() if row["oldest_event"] else None
                    ),
                }
                for row in coverage_rows
            ],
            "limits": limits,
            "as_of": datetime.now(UTC).isoformat(),
        }

    return router
