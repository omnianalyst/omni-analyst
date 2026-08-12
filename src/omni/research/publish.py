"""Publish the research registry so the running system can show its own search.

The JSONL registry is the writer; this is the mirror. The direction is one-way
and deliberately so -- see `migrations/055_hypothesis_test.sql` for why two
writers would corrupt the significance bar rather than merely duplicate it.

What this makes possible is the thing the product was missing: a system that has
tested 49 hypotheses and rejected all 49 can *say so*. A search whose failures
are invisible looks either like no search at all or like an unbroken run of
success, and both readings are wrong in a way that flatters.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from omni.research.registry import Registry, bar_for, fdr_bar_for

# Verdicts the harness writes. `evaluate()` records "PASS" when any horizon
# clears the bar and "fail" otherwise; compare case-insensitively so a mirror
# never silently reclassifies a pass as a failure.
PASS_VERDICT = "pass"


@dataclass(frozen=True)
class MirrorReport:
    """What a sync actually changed. `read` is entries seen in the registry."""

    read: int
    inserted: int
    already_present: int

    @property
    def changed(self) -> bool:
        return self.inserted > 0


def _parse_recorded_at(value: str) -> datetime:
    """Registry timestamps are ISO-8601 with an explicit UTC offset.

    A naive datetime here would be written into a TIMESTAMPTZ column using the
    server's timezone, which would silently shift the recorded instant. Refusing
    is correct: the recorded_at is half the row's identity, so a shifted one
    duplicates the entry rather than matching it.
    """
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        raise ValueError(
            f"registry entry has a naive recorded_at ({value!r}); refusing to "
            f"mirror it, because writing it would shift the instant and break "
            f"the (name, recorded_at) identity the mirror is idempotent on"
        )
    return parsed


async def mirror_registry(pool, registry: Registry | None = None) -> MirrorReport:
    """Copy every registry entry into `hypothesis_test`, idempotently.

    Safe to run repeatedly and safe to run concurrently: the conflict target is
    the table's primary key, so a duplicate is a no-op rather than an error.
    """
    reg = registry if registry is not None else Registry()
    entries = reg.entries()
    inserted = 0

    for entry in entries:
        recorded_at = _parse_recorded_at(entry.recorded_at)
        row = await pool.fetchrow(
            """
            INSERT INTO hypothesis_test (name, source, cells, verdict, recorded_at, detail)
            VALUES ($1, $2, $3, $4, $5, $6::jsonb)
            ON CONFLICT (name, recorded_at) DO NOTHING
            RETURNING 1 AS wrote
            """,
            entry.name,
            entry.source,
            int(entry.cells),
            entry.verdict,
            recorded_at,
            json.dumps(entry.detail or {}),
        )
        if row is not None:
            inserted += 1

    return MirrorReport(
        read=len(entries),
        inserted=inserted,
        already_present=len(entries) - inserted,
    )


async def read_history(pool) -> list[dict[str, Any]]:
    """Every mirrored test, newest first."""
    rows = await pool.fetch(
        """
        SELECT name, source, cells, verdict, recorded_at, detail, mirrored_at
        FROM hypothesis_test
        ORDER BY recorded_at DESC
        """
    )
    out: list[dict[str, Any]] = []
    for row in rows:
        detail = row["detail"]
        if isinstance(detail, str):
            detail = json.loads(detail)
        out.append(
            {
                "name": row["name"],
                "source": row["source"],
                "cells": int(row["cells"]),
                "verdict": row["verdict"],
                "recorded_at": row["recorded_at"].isoformat(),
                "detail": detail or {},
                "mirrored_at": row["mirrored_at"].isoformat(),
            }
        )
    return out


def summarise(history: list[dict[str, Any]]) -> dict[str, Any]:
    """Totals and the bar implied by them.

    `pending_cells` is zero here on purpose. During a research run the bar
    includes the test about to happen, because that test is part of the search;
    reported after the fact, the honest number is the bar the recorded history
    itself implies. Passing a non-zero pending count would show a stricter bar
    than any recorded result was actually judged against.
    """
    total_cells = sum(int(e["cells"]) for e in history)
    stats = [
        abs(float(t))
        for e in history
        if (t := (e.get("detail") or {}).get("best_recent_third_t")) is not None
    ]
    passed = [e for e in history if str(e["verdict"]).lower() == PASS_VERDICT]

    return {
        "tests": len(history),
        "cells": total_cells,
        "passed": len(passed),
        "failed": len(history) - len(passed),
        "bar": bar_for(total_cells=total_cells, pending_cells=0),
        "fdr_bar": fdr_bar_for(stats=stats, pending_cells=0),
        "best_t": max(stats, default=None),
        "sources": sorted({str(e["source"]) for e in history}),
        "last_recorded_at": history[0]["recorded_at"] if history else None,
        "last_mirrored_at": (
            max(e["mirrored_at"] for e in history) if history else None
        ),
    }
