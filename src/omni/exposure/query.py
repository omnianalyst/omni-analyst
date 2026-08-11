"""Load holding claims from the store into the structures overlap.analyze takes.

The computation in ``overlap.py`` is a pure function over ``ETFPosition`` records;
this module is the I/O half that resolves those records from the claim table.
It reads the latest non-superseded ``holding`` claim per constituent per ETF,
visible to the given audience, and folds them with the caller-supplied
allocations into the ``ETFPosition`` list the analyser expects.

Allocations are **not** read from the store. They are a property of the
portfolio the operator is asking about -- a target allocation, a live book, or a
what-if -- and none of those is a fact the coverage layer owns. The caller
states them; this module populates the holdings.
"""

from __future__ import annotations

import json
from datetime import datetime
from decimal import Decimal, InvalidOperation
from uuid import UUID

from omni.coverage.visibility import visible_claims_cte
from omni.exposure.overlap import ETFPosition, Holding

__all__ = ["CompositionEntry", "CompositionError", "load_positions"]


class CompositionError(Exception):
    """A symbol in the composition has no ETF entity in the store."""


def _D(value) -> Decimal:
    return value if isinstance(value, Decimal) else Decimal(str(value))


def _jsonb(value):
    if isinstance(value, str):
        return json.loads(value)
    return value


async def _resolve_entities(pool, symbols: list[str]) -> dict[str, tuple[UUID, str]]:
    """symbol -> (entity_id, bucket) for every ETF the composition names."""
    rows = await pool.fetch(
        "SELECT id, symbol, identifiers FROM entity "
        "WHERE kind = 'etf' AND symbol = ANY($1::text[])",
        symbols,
    )
    out: dict[str, tuple[UUID, str]] = {}
    for r in rows:
        identifiers = _jsonb(r["identifiers"]) or {}
        bucket = identifiers.get("bucket", "uncategorised")
        out[r["symbol"]] = (r["id"], bucket)
    return out


async def _latest_holdings(
    pool,
    *,
    entity_ids: list[UUID],
    audience: UUID | None,
    as_of: datetime | None,
) -> dict[UUID, list[Holding]]:
    """The most recently knowable holding claim per constituent per ETF.

    A fund rebalances quarterly, so the same ticker appears across multiple
    filings with different weights. The freshest one as of ``as_of`` wins;
    ``DISTINCT ON`` picks the newest by knowledge_date regardless of whether a
    later ingest set ``superseded_by``.
    """
    if not entity_ids:
        return {}

    # $1 = audience (for the visibility CTE), $2 = entity_ids (uuid[]).
    # as_of is $3 only when present, so the clause and the param move together.
    params: list = [audience, entity_ids]
    as_of_clause = ""
    if as_of is not None:
        params.append(as_of)
        as_of_clause = "AND c.knowledge_date <= $3"

    sql = f"""
        SELECT DISTINCT ON (c.entity_id, c.key)
               c.entity_id,
               c.key       AS ticker,
               c.value
        FROM ({visible_claims_cte("$1")}) c
        WHERE c.claim_type = 'holding'
          AND c.entity_id = ANY($2::uuid[])
          {as_of_clause}
        ORDER BY c.entity_id, c.key, c.knowledge_date DESC, c.event_date DESC
    """
    rows = await pool.fetch(sql, *params)

    by_etf: dict[UUID, list[Holding]] = {}
    for row in rows:
        value = _jsonb(row["value"])
        weight = _extract_weight(value)
        if weight is None:
            continue
        by_etf.setdefault(row["entity_id"], []).append(
            Holding(ticker=row["ticker"], weight=weight)
        )
    return by_etf


def _extract_weight(value) -> Decimal | None:
    """Pull the portfolio weight from a holding claim's JSON value.

    Issuer disclosures name it differently -- ``weight``, ``pct``,
    ``percentage_of_net_assets`` -- so this reads whichever key is present. A
    value that is absent, null, or non-finite produces ``None`` rather than a
    fabricated zero; a holding with no stated weight is not a holding the
    exposure tool can cost.
    """
    if not isinstance(value, dict):
        return None
    for field in (
        "weight",
        "pct",
        "weighting",
        "percentage_of_net_assets",
        "pct_of_nav",
        "market_value_percentage",
    ):
        raw = value.get(field)
        if raw is None:
            continue
        try:
            weight = _D(raw)
        except (InvalidOperation, ValueError, TypeError):
            continue
        if not weight.is_finite():
            return None
        return weight
    return None


async def load_positions(
    pool,
    *,
    composition: list[CompositionEntry],
    audience: UUID | None = None,
    as_of: datetime | None = None,
) -> list[ETFPosition]:
    """Resolve ETF positions from the store, ready for ``overlap.analyze``.

    ``composition`` carries the operator's stated allocations -- what fraction
    of the portfolio each ETF occupies. The holdings are read from the claim
    table for each resolved ETF entity.

    Raises ``CompositionError`` if a symbol in the composition has no matching
    ``etf`` entity. An exposure analysis on an entity that does not exist would
    silently drop a position from the overlap matrix, which is the same shape as
    the fabricated-default failure this codebase refuses everywhere else.
    """
    if not composition:
        return []

    symbols = [entry.symbol for entry in composition]
    entity_map = await _resolve_entities(pool, symbols)
    missing = [s for s in symbols if s not in entity_map]
    if missing:
        raise CompositionError(
            f"no etf entity for symbol(s): {', '.join(missing)}. "
            f"The exposure tool cannot analyse a fund the store does not know about"
        )

    entity_ids = [entity_map[e.symbol][0] for e in composition]
    holdings = await _latest_holdings(
        pool, entity_ids=entity_ids, audience=audience, as_of=as_of
    )

    positions: list[ETFPosition] = []
    for entry in composition:
        eid, stored_bucket = entity_map[entry.symbol]
        bucket = entry.bucket if entry.bucket is not None else stored_bucket
        positions.append(
            ETFPosition(
                symbol=entry.symbol,
                bucket=bucket,
                allocation=entry.allocation,
                holdings=tuple(holdings.get(eid, ())),
            )
        )
    return positions


class CompositionEntry:
    """One ETF in the portfolio, at its allocation.

    ``bucket`` overrides the entity's stored bucket when the caller wants a
    what-if that reclassifies a fund. When omitted, the bucket is read from the
    entity's identifiers (migration 051 stores ``bucket`` in the JSON).
    """

    __slots__ = ("allocation", "bucket", "symbol")

    def __init__(
        self,
        symbol: str,
        allocation: Decimal,
        bucket: str | None = None,
    ) -> None:
        self.symbol = symbol
        self.allocation = allocation
        self.bucket = bucket
