"""Shared read helpers for the autonomous loops.

The autonomous layer reads claims directly from the coverage store rather than
going through the gap engine. The gap engine is demand-driven (a user asks, a
gap opens, a filler is dispatched); the autonomous loops are the opposite --
they scan proactively, so they read what is there and compose new claims from
it. These helpers centralize the read patterns every loop shares, so the
audience-scoping rule lives once rather than at each call site.

All reads are point-in-time: a claim filed after the loop's ``as_of`` is
invisible, so a backtest replaying a historical scan cannot peek at data that
had not been published yet.
"""

from __future__ import annotations

import json
from datetime import datetime
from typing import Any
from uuid import UUID


async def latest_shared_claim(
    pool,
    *,
    claim_type: str,
    key: str | None = None,
    as_of: datetime | None = None,
) -> Any | None:
    """The newest shared (allowed, audience-NULL) claim of a type, or None.

    Shared coverage is what every audience sees: ``redistributable = 'allowed'``
    and ``audience_user_id IS NULL``. The autonomous loops compose from shared
    coverage because their output (regime assessment, sector scores) is itself
    shared -- a byo_only input would propagate its restriction through the
    derivation licence trigger (migration 002), and the autonomous layer's
    value is in its public, auditable chain.

    ``as_of`` makes the read point-in-time: a claim whose ``knowledge_date`` is
    after ``as_of`` is invisible. None means "now" (the live loop).

    Returns the full row (id, entity_id, value, event_date, knowledge_date,
    evidence) or None. ``value`` and ``evidence`` are decoded from JSON if the
    pool returns them as strings.
    """
    clauses = [
        "claim_type = $1::claim_type",
        "audience_user_id IS NULL",
        "redistributable = 'allowed'",
        "superseded_by IS NULL",
    ]
    params: list[Any] = [claim_type]
    if key is not None:
        params.append(key)
        clauses.append(f"key = ${len(params)}")
    if as_of is not None:
        params.append(as_of)
        clauses.append(f"knowledge_date <= ${len(params)}")
    sql = (
        "SELECT id, entity_id, value, event_date, knowledge_date, evidence "
        "FROM claim WHERE "
        + " AND ".join(clauses)
        + " ORDER BY knowledge_date DESC, event_date DESC LIMIT 1"
    )
    row = await pool.fetchrow(sql, *params)
    return _decode_row(row) if row else None


async def macro_series_values(
    pool,
    *,
    key: str,
    limit: int = 25,
    as_of: datetime | None = None,
) -> list[tuple[datetime, float]]:
    """Trailing macro_series_point values for a FRED series key, oldest-first.

    Returns ``(event_date, value)`` tuples. Null-valued observations (FRED's
    "." placeholder for "not published yet") are included -- a null in the
    window is a fact about the data, not a gap to fill.
    """
    clauses = [
        "claim_type = 'macro_series_point'",
        "key = $1",
        "audience_user_id IS NULL",
        "redistributable = 'allowed'",
        "superseded_by IS NULL",
    ]
    params: list[Any] = [key]
    if as_of is not None:
        params.append(as_of)
        clauses.append(f"knowledge_date <= ${len(params)}")
    sql = (
        "SELECT event_date, value FROM claim WHERE "
        + " AND ".join(clauses)
        + " ORDER BY event_date DESC LIMIT $" + str(len(params) + 1)
    )
    params.append(limit)
    rows = await pool.fetch(sql, *params)
    vals: list[tuple[datetime, float]] = []
    for r in rows:
        raw = r["value"]
        if isinstance(raw, (str, bytes)):
            raw = json.loads(raw)
        v = raw.get("value") if isinstance(raw, dict) else None
        vals.append((r["event_date"], v))
    vals.reverse()
    return vals


async def latest_claim_for_entity(
    pool,
    *,
    entity_id: UUID,
    claim_type: str,
    key: str | None = None,
) -> Any | None:
    """The newest claim of a type on a specific entity, across all audiences.

    Used by the sector scanner to read an ETF's prices. Price claims are
    ``byo_only`` (Polygon's terms forbid redistribution), so they are never in
    shared coverage. On a single-operator deployment -- which is what the
    autonomous layer targets first -- the operator's byo_only prices ARE the
    system's prices, and the scanner reads them without an audience filter. A
    multi-tenant deployment would need to scope this to the operator's audience;
    that is a documented follow-up, not a silent assumption.
    """
    clauses = [
        "entity_id = $1",
        "claim_type = $2::claim_type",
        "superseded_by IS NULL",
    ]
    params: list[Any] = [entity_id, claim_type]
    if key is not None:
        params.append(key)
        clauses.append(f"key = ${len(params)}")
    sql = (
        "SELECT id, entity_id, value, event_date, knowledge_date, evidence "
        "FROM claim WHERE "
        + " AND ".join(clauses)
        + " ORDER BY event_date DESC LIMIT 1"
    )
    row = await pool.fetchrow(sql, *params)
    return _decode_row(row) if row else None


async def price_closes(
    pool,
    *,
    entity_id: UUID,
    limit: int = 60,
) -> list[float]:
    """Trailing daily closes for an entity, oldest-first.

    Reads ``price_snapshot`` claims (Polygon bars carry ``close``). Up to
    ``limit`` observations, ordered oldest-first so a rolling-mean or
    return computation can index sequentially. A bar without a readable close
    is skipped -- never substituted.
    """
    rows = await pool.fetch(
        "SELECT value FROM claim "
        "WHERE entity_id = $1 AND claim_type = 'price_snapshot' "
        "AND superseded_by IS NULL "
        "ORDER BY event_date DESC LIMIT $2",
        entity_id,
        limit,
    )
    closes: list[float] = []
    for r in rows:
        raw = r["value"]
        if isinstance(raw, (str, bytes)):
            raw = json.loads(raw)
        if not isinstance(raw, dict):
            continue
        scalar = raw.get("close")
        if scalar is None:
            continue
        try:
            closes.append(float(scalar))
        except (TypeError, ValueError):
            continue
    closes.reverse()
    return closes


def to_returns(closes: list[float]) -> list[float]:
    """Simple returns from a close series, one shorter than the input."""
    if len(closes) < 2:
        return []
    return [
        (closes[i] / closes[i - 1] - 1.0)
        for i in range(1, len(closes))
        if closes[i - 1] != 0.0
    ]


def _decode_row(row) -> dict:
    """Decode JSONB columns the pool may return as strings."""
    out = dict(row)
    for col in ("value", "evidence"):
        raw = out.get(col)
        if isinstance(raw, (str, bytes)):
            try:
                out[col] = json.loads(raw)
            except (json.JSONDecodeError, TypeError):
                pass
    return out
