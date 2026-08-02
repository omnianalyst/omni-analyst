"""Assemble the ``fundamentals`` dict ``dcf_valuation`` consumes, from EDGAR claims.

``dcf_valuation`` takes a nested dict (``cash_flow`` / ``balance_sheet`` /
``income_statement``) that no ``ArgumentSpec`` can assemble -- the structure is
nested, and EDGAR stores each us-gaap concept as a separate scalar
``fundamental_metric`` claim (``key`` = the concept, ``value = {"value": n}``).
This module is the purpose-built assembler: it reads the concept claims visible
to an audience, picks each value **point-in-time** (the latest knowable as of a
knowledge date, so a backtest cannot see a filing that did not yet exist),
derives the composite and rate fields the raw concepts do not directly carry
(``total_debt``, ``revenue_growth_rate``, ``market_cap``), and refuses with
``Unavailable`` when an essential input is missing rather than padding a zero --
the failure mode this project exists not to repeat.

Honest gaps, by design (each is a known incompleteness, not a silent default):
- ``revenue_growth_rate`` is derived from two consecutive annual Revenues
  filings (fp='FY'). Under a year of EDGAR history leaves it unset; the caller
  then passes ``growth_rate`` explicitly or the DCF refuses. Never a 5% default.
- ``total_debt`` is LongTermDebt plus its current portion. A company reporting
  debt under other concepts (ShortTermBorrowings, CommercialPaper, ...) or a
  genuinely debt-free firm (no LongTermDebt fact at all) is refused rather than
  read as zero-debt, because zero-debt and missing-debt-data are indistinguishable
  from the facts alone and reading either as the other biases the equity bridge.
- ``beta`` is not a us-gaap concept; it is omitted and ``dcf_valuation`` defaults
  it to 1.0 (market-average risk). Sourcing real beta is a follow-up.
- ``market_cap`` is ``shares_outstanding * current_price`` when a price is
  supplied; absent a price it is omitted and ``dcf_valuation`` falls back to
  cost-of-equity for the discount rate.
"""

from __future__ import annotations

import json
from datetime import datetime
from uuid import UUID

from omni.coverage.visibility import visible_claims_cte
from omni.ingest.protocol import Unavailable

CLAIM_TYPE = "fundamental_metric"

# (section, key) -> candidate us-gaap concepts; the first concept with a
# knowable-as-of value wins. Candidate names cross-checked against v1
# sec_edgar_service gaap_mappings (Revenues, OperatingCashFlow, Cash,
# StockholdersEquity, NetIncomeLoss) and the us-gaap taxonomy for capex/shares.
_CONCEPT_MAP: dict[tuple[str, str], tuple[str, ...]] = {
    ("cash_flow", "operating_cash_flow"):
        ("NetCashProvidedByUsedInOperatingActivities",),
    ("cash_flow", "capital_expenditures"):
        ("PaymentsToAcquirePropertyPlantAndEquipment",
         "PaymentsToAcquireProductiveAssets"),
    ("balance_sheet", "shares_outstanding"):
        ("CommonStockSharesOutstanding",
         "EntityCommonStockOutstandingShare"),
    ("balance_sheet", "cash_and_equivalents"):
        ("CashAndCashEquivalentsAtCarryingValue",),
    ("balance_sheet", "total_equity"):
        ("StockholdersEquity",),
}

_DEBT_CONCEPTS = ("LongTermDebt", "LongTermDebtCurrent")
_REVENUE_CONCEPT = "Revenues"

# Every concept the assembler reads, in one set so a single query fetches them.
_ALL_CONCEPTS: tuple[str, ...] = tuple(
    dict.fromkeys(
        [c for concepts in _CONCEPT_MAP.values() for c in concepts]
        + list(_DEBT_CONCEPTS)
        + [_REVENUE_CONCEPT]
    )
)

# The inputs without which the DCF cannot produce a defensible fair value. Each
# is refused (Unavailable) if missing rather than defaulted -- a fair value built
# on a zero padded for a missing line item is the fabrication vector v1 ran on.
_ESSENTIALS = (
    ("cash_flow", "operating_cash_flow"),
    ("cash_flow", "capital_expenditures"),
    ("balance_sheet", "shares_outstanding"),
    ("balance_sheet", "cash_and_equivalents"),
    ("balance_sheet", "total_equity"),
)


def _scalar(row) -> float | None:
    raw = row["value"]
    if isinstance(raw, (str, bytes)):
        raw = json.loads(raw)
    if not isinstance(raw, dict):
        return None
    val = raw.get("value")
    if val is None:
        return None
    try:
        return float(val)
    except (TypeError, ValueError):
        return None


def _evidence(row) -> dict:
    ev = row["evidence"]
    if isinstance(ev, (str, bytes)):
        try:
            ev = json.loads(ev)
        except (TypeError, ValueError, json.JSONDecodeError):
            return {}
    return ev if isinstance(ev, dict) else {}


async def _latest_per_concept(
    pool, *, entity_id: UUID, audience: UUID | None, as_of: datetime
) -> dict[str, dict]:
    """The latest-knowable claim per concept as of ``as_of`` (no lookahead).

    Collapses multiple facts for one concept (a 10-K/A restatement files at a
    later ``knowledge_date`` for the same period) to the newest knowable, so a
    point-in-time read sees the filing that actually existed then.
    """
    rows = await pool.fetch(
        f"""
        WITH visible AS (
        {visible_claims_cte("$3")}
        )
        SELECT c.key, c.value, c.event_date, c.knowledge_date, c.evidence
        FROM visible c
        WHERE c.entity_id = $1
          AND c.claim_type = '{CLAIM_TYPE}'::claim_type
          AND c.key = ANY($2)
          AND c.knowledge_date <= $4
        """,
        entity_id,
        list(_ALL_CONCEPTS),
        audience,
        as_of,
    )
    latest: dict[str, dict] = {}
    for r in rows:
        key = r["key"]
        seen = latest.get(key)
        if seen is None or r["knowledge_date"] > seen["knowledge_date"]:
            latest[key] = dict(r)
    return latest


async def _latest_annual_revenues(
    pool, *, entity_id: UUID, audience: UUID | None, as_of: datetime, limit: int = 2
) -> list[tuple[datetime, float]]:
    """The most recent annual (``fp='FY'``) Revenues values, oldest-first.

    Two consecutive annual filings yield a year-over-year growth rate. Fewer
    than two means the growth rate is genuinely unknown from EDGAR history; the
    caller passes ``growth_rate`` explicitly or the DCF abstains.
    """
    rows = await pool.fetch(
        f"""
        WITH visible AS (
        {visible_claims_cte("$3")}
        )
        SELECT c.value, c.event_date, c.knowledge_date, c.evidence
        FROM visible c
        WHERE c.entity_id = $1
          AND c.claim_type = '{CLAIM_TYPE}'::claim_type
          AND c.key = $2
          AND c.knowledge_date <= $4
        ORDER BY c.event_date DESC, c.knowledge_date DESC
        """,
        entity_id,
        _REVENUE_CONCEPT,
        audience,
        as_of,
    )
    annual: list[tuple[datetime, float]] = []
    seen_periods: set[str] = set()
    for r in rows:
        if _evidence(r).get("fp") != "FY":
            continue
        val = _scalar(r)
        if val is None or val <= 0:
            continue
        period = r["event_date"].isoformat()[:10]
        if period in seen_periods:
            continue
        seen_periods.add(period)
        annual.append((r["event_date"], val))
        if len(annual) >= limit:
            break
    annual.reverse()  # oldest-first so [-1] is the most recent
    return annual


async def assemble_fundamentals(
    pool,
    *,
    entity_id: UUID,
    as_of: datetime,
    current_price: float | None = None,
    audience: UUID | None = None,
) -> dict:
    """Build the fundamentals dict for ``dcf_valuation`` from EDGAR coverage.

    Raises ``Unavailable`` naming the missing essential when the DCF cannot run
    honestly on what is knowable as of ``as_of``. ``current_price`` (from a
    per-audience ``price_snapshot``) optionally populates ``market_cap`` = shares
    * price for the WACC weight; without it the discount rate falls back to
    cost-of-equity.
    """
    latest = await _latest_per_concept(
        pool, entity_id=entity_id, audience=audience, as_of=as_of
    )

    def value_for(candidates: tuple[str, ...]) -> float | None:
        for concept in candidates:
            row = latest.get(concept)
            if row is not None:
                v = _scalar(row)
                if v is not None:
                    return v
        return None

    fundamentals: dict = {"cash_flow": {}, "balance_sheet": {}, "income_statement": {}}
    for (section, key), candidates in _CONCEPT_MAP.items():
        v = value_for(candidates)
        if v is not None:
            fundamentals[section][key] = v

    # total_debt = long-term + current portion (both when present). Refused below
    # if no debt concept at all is knowable: zero-debt and missing-debt-data are
    # indistinguishable from the facts, and reading either as the other biases
    # the equity bridge.
    debt_total = 0.0
    debt_seen = False
    for concept in _DEBT_CONCEPTS:
        row = latest.get(concept)
        if row is not None:
            v = _scalar(row)
            if v is not None:
                debt_total += v
                debt_seen = True
    if debt_seen:
        fundamentals["balance_sheet"]["total_debt"] = debt_total

    missing = [
        f"{section}.{key}"
        for section, key in _ESSENTIALS
        if fundamentals[section].get(key) is None
    ]
    # total_debt is essential too, stored separately above.
    if not debt_seen:
        missing.append("balance_sheet.total_debt")
    if missing:
        raise Unavailable(
            f"fundamentals incomplete as of {as_of.date()}: missing "
            f"{', '.join(missing)}"
        )

    # revenue_growth_rate from two consecutive annual Revenues filings. Left
    # unset when fewer than two annual periods are knowable; the caller then
    # passes growth_rate or dcf_valuation refuses (no default).
    revenues = await _latest_annual_revenues(
        pool, entity_id=entity_id, audience=audience, as_of=as_of
    )
    if len(revenues) >= 2:
        prior, curr = revenues[-2][1], revenues[-1][1]
        if prior > 0:
            fundamentals["income_statement"]["revenue_growth_rate"] = (
                curr / prior
            ) - 1.0

    shares = fundamentals["balance_sheet"]["shares_outstanding"]
    if current_price and current_price > 0 and shares and shares > 0:
        fundamentals["balance_sheet"]["market_cap"] = shares * current_price

    return fundamentals
