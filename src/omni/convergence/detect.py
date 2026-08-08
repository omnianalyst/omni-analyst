"""Independent claim families agreeing about one entity inside a window.

`coverage/gaps.py::_find_contradictions` detects independent sources
*disagreeing* about the same key and weights it 1000. Nothing detected them
*agreeing*. This module is that missing symmetric counterpart.

The threshold is N distinct claim FAMILIES co-occurring in the window, never N
events. A volume threshold fires whenever one noisy source floods -- ten
`trade_tape` claims from a single venue is one venue's opinion sampled ten
times, not corroboration -- so the only quantity counted here is the number of
distinct families. There is deliberately no score, no multiplier and no count
boost: the confidence in a convergence is the family count itself, and a blend
on top of it would be exactly the kind of uncalibrated constant `gate.py`
exists to refuse.

Claims are read through `coverage.visibility.visible_claims_cte` scoped to the
requesting audience. A convergence computed over claims an audience cannot see
would fold another user's byo_only coverage into that audience's answer, so the
`claim` table is never queried directly here.

Fewer than `min_families` present is an abstention, not an error: `detect`
returns None and the caller has learned that the network does not corroborate
anything about that entity in that window.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from uuid import UUID

from omni.coverage.visibility import visible_claims_cte

# claim_type -> family. Claim types that are NOT independent of each other share
# a family, because the whole point of the count is to measure independent
# corroboration:
#
#   flow            an address's balance change and the supply figure derived
#                   from it are the same chain read
#   derivatives     funding, open interest, liquidations and basis all describe
#                   one perpetual market and move together mechanically; basis
#                   is computed from a funding/mark pair, so counting both would
#                   count one observation twice
#   microstructure  the tape and the book are two views of one venue's matching
#                   engine sampled at the same instant
#   narrative       perception_news is derived from news_event, so they are one
#                   voice
#   fundamentals    protocol fees and protocol revenue come from a single
#                   DefiLlama publication, fundamental_metric is the equity
#                   counterpart of the same kind of statement, and filing_event
#                   is the same filer's own disclosure cadence
#   macro           FRED series are the one observation stream in this map with
#                   no upstream in common with any other: an unemployment print
#                   is not a function of a price, a book, or a chain read
#
# A claim type absent from this map contributes nothing to a convergence. That
# is deliberate and fail-closed: every derived claim type in the enum
# (perception_divergence, the *_signal types, regime_assessment, sector_score,
# manipulation_signal) is a function of claims already counted here, so
# admitting one would let a single underlying observation vote twice.
#
# The membership rule is INDEPENDENCE OF UPSTREAM, not subject matter. That is
# why the on-chain aggregates sit with the flows rather than forming a family of
# their own -- TVL, chain TVL and stablecoin supply are different summaries of
# the same chain state an onchain_flow claim reads, and a family per summary
# would let one node's view of one block corroborate itself three times.
CLAIM_FAMILIES: dict[str, str] = {
    "price_snapshot": "price",
    "onchain_flow": "flow",
    "onchain_supply": "flow",
    "funding_rate": "derivatives",
    "open_interest": "derivatives",
    "liquidation_event": "derivatives",
    "basis": "derivatives",
    "orderbook_snapshot": "microstructure",
    "trade_tape": "microstructure",
    "news_event": "narrative",
    "perception_news": "narrative",
    "protocol_revenue": "fundamentals",
    "protocol_fees": "fundamentals",
    "fundamental_metric": "fundamentals",
    "filing_event": "fundamentals",
    "onchain_tvl": "flow",
    "chain_tvl": "flow",
    "stablecoin_supply": "flow",
    "macro_series_point": "macro",
}


@dataclass(frozen=True)
class Convergence:
    """Which families agreed about `entity_id`, and over what window."""

    entity_id: UUID
    families: tuple[str, ...]
    claim_ids: tuple[UUID, ...]
    window_start: datetime
    window_end: datetime

    @property
    def family_count(self) -> int:
        return len(self.families)


async def detect(
    pool,
    *,
    entity_id: UUID,
    audience_user_id: UUID | None,
    window: timedelta,
    min_families: int,
    as_of: datetime,
) -> Convergence | None:
    """Convergence for one entity over `[as_of - window, as_of]`, or None.

    The window is closed at both ends and cut on `knowledge_date`, so the
    answer is point-in-time: a claim that only became knowable after `as_of`
    cannot corroborate anything at `as_of`.
    """
    _require_diversity_threshold(min_families)
    window_start = as_of - window
    rows = await _claims_in_window(
        pool,
        audience_user_id=audience_user_id,
        window_start=window_start,
        as_of=as_of,
        entity_id=entity_id,
    )
    return _build(
        entity_id,
        rows,
        min_families=min_families,
        window_start=window_start,
        window_end=as_of,
    )


async def detect_all(
    pool,
    *,
    audience_user_id: UUID | None,
    window: timedelta,
    min_families: int,
    as_of: datetime,
) -> list[Convergence]:
    """One Convergence per entity that qualifies; non-qualifying entities are
    absent from the result rather than present with an empty family set."""
    _require_diversity_threshold(min_families)
    window_start = as_of - window
    rows = await _claims_in_window(
        pool,
        audience_user_id=audience_user_id,
        window_start=window_start,
        as_of=as_of,
        entity_id=None,
    )

    by_entity: dict[UUID, list] = {}
    for row in rows:
        by_entity.setdefault(row["entity_id"], []).append(row)

    found: list[Convergence] = []
    for candidate_id, entity_rows in by_entity.items():
        converged = _build(
            candidate_id,
            entity_rows,
            min_families=min_families,
            window_start=window_start,
            window_end=as_of,
        )
        if converged is not None:
            found.append(converged)
    return found


def _require_diversity_threshold(min_families: int) -> None:
    if min_families < 2:
        raise ValueError(
            f"min_families must be at least 2, got {min_families}: one family "
            "agreeing with itself is volume, not independent corroboration"
        )


async def _claims_in_window(
    pool,
    *,
    audience_user_id: UUID | None,
    window_start: datetime,
    as_of: datetime,
    entity_id: UUID | None,
) -> list:
    conditions = [
        "c.knowledge_date >= $2",
        "c.knowledge_date <= $3",
        "c.claim_type::text = ANY($4)",
    ]
    params: list = [
        audience_user_id,
        window_start,
        as_of,
        sorted(CLAIM_FAMILIES),
    ]
    if entity_id is not None:
        params.append(entity_id)
        conditions.append(f"c.entity_id = ${len(params)}")

    sql = f"""
        WITH visible AS (
        {visible_claims_cte("$1")}
        )
        SELECT c.id, c.entity_id, c.claim_type::text AS claim_type
        FROM visible c
        WHERE {" AND ".join(conditions)}
        ORDER BY c.knowledge_date, c.id
    """
    return await pool.fetch(sql, *params)


def _build(
    entity_id: UUID,
    rows: list,
    *,
    min_families: int,
    window_start: datetime,
    window_end: datetime,
) -> Convergence | None:
    families = {CLAIM_FAMILIES[row["claim_type"]] for row in rows}
    if len(families) < min_families:
        return None
    return Convergence(
        entity_id=entity_id,
        families=tuple(sorted(families)),
        claim_ids=tuple(row["id"] for row in rows),
        window_start=window_start,
        window_end=window_end,
    )
