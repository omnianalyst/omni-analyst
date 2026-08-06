"""Seed the entity graph with relationships that are true by definition.

`entity_edge` and the traversal built on it do nothing until somebody writes
edges. This module is that somebody, for the small set of links that do not
need to be inferred from data because they are structural facts:

  - every operating company is exposed to the US macro environment
  - a handful of cross-domain links whose reason can be stated in a sentence

Each edge carries a `source` naming why it is asserted. An edge without a
reason is exactly the kind of unfounded claim this system exists not to make,
so nothing is added here that cannot be justified in prose. No correlation is
inferred and no price data is mined: inference belongs to the gap-fillers, and
a relationship guessed from co-movement would be a hallucinated edge.
"""

from __future__ import annotations

from uuid import UUID

from omni.coverage.graph import relate

MACRO_KIND = "macro"
MACRO_SYMBOL = "US"
MACRO_NAME = "US macro environment"

COMPANY_KIND = "company"

MACRO_RELATION = "influenced_by"
MACRO_SOURCE = (
    "Operating companies are exposed to the US macro environment "
    "(rates, growth, inflation)."
)

# Each entry is a directed edge (from_symbol, to_symbol, relation, source).
#
# These are written as a single directed edge whose direction matches the
# reason: graph traversal (related_coverage, find_path) walks an edge both
# ways, so one BTC -> COIN edge already makes COIN reachable from BTC and vice
# versa. Asserting the reverse as well would claim "BTC tracks COIN's revenue",
# which is the converse of the truth and has no independent justification.
#
# Endpoints are resolved by symbol; a pair whose endpoint is absent is skipped
# rather than conjured into existence.
_CROSS_DOMAIN: tuple[tuple[str, str, str, str], ...] = (
    (
        "BTC",
        "COIN",
        "influences",
        ("Coinbase trading revenue tracks crypto volume; BTC is the largest "
         "driver of that volume."),
    ),
    (
        "BTC",
        "MSTR",
        "influences",
        ("MicroStrategy holds BTC on its balance sheet, so its book value "
         "moves with the BTC price."),
    ),
    (
        "ETH",
        "COIN",
        "influences",
        ("ETH is a leading share of Coinbase's listed crypto volume and thus "
         "of its trading revenue."),
    ),
)


async def _find_or_create_macro(pool) -> UUID:
    existing = await pool.fetchval(
        "SELECT id FROM entity WHERE kind = $1 AND symbol = $2",
        MACRO_KIND,
        MACRO_SYMBOL,
    )
    if existing is not None:
        return existing
    # ON CONFLICT ... DO UPDATE (not NOTHING) so RETURNING yields the id even
    # if another runner created the macro entity between the SELECT and here.
    return await pool.fetchval(
        "INSERT INTO entity (kind, symbol, name) VALUES ($1, $2, $3) "
        "ON CONFLICT (kind, symbol) DO UPDATE SET kind = EXCLUDED.kind "
        "RETURNING id",
        MACRO_KIND,
        MACRO_SYMBOL,
        MACRO_NAME,
    )


async def _entity_id_by_symbol(pool, symbol: str) -> UUID | None:
    return await pool.fetchval(
        "SELECT id FROM entity WHERE symbol = $1", symbol
    )


async def seed_known_relationships(pool) -> int:
    """Write the structurally-true edges. Idempotent; returns edges written.

    Running this twice neither duplicates edges nor raises: `relate` is
    idempotent on (from_entity, to_entity, relation). A cross-domain pair whose
    endpoint does not exist is skipped (and not counted) rather than creating
    an entity out of nothing.
    """
    macro_id = await _find_or_create_macro(pool)

    written = 0

    company_ids = await pool.fetch(
        "SELECT id FROM entity WHERE kind = $1", COMPANY_KIND
    )
    for row in company_ids:
        await relate(
            pool,
            row["id"],
            macro_id,
            relation=MACRO_RELATION,
            source=MACRO_SOURCE,
        )
        written += 1

    for from_symbol, to_symbol, relation, source in _CROSS_DOMAIN:
        from_id = await _entity_id_by_symbol(pool, from_symbol)
        to_id = await _entity_id_by_symbol(pool, to_symbol)
        if from_id is None or to_id is None:
            continue
        await relate(pool, from_id, to_id, relation=relation, source=source)
        written += 1

    return written
