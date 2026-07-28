"""Watchlists as the implicit demand channel.

A watchlist is a user-facing list, and adding to it raises demand rows which
the gap engine already consumes. This is not a second mechanism next to the
demand ledger; it is a second inlet. A user watching AAPL is asking the system
to keep AAPL covered whether or not they ever type a query about it, and forty
users holding an uncovered name *is* the signal that lets coverage accumulate
for reasons other than an explicit question.

Two behaviours are load-bearing here, and both come from the demand ledger's
own invariants rather than being reinvented:

  * Demand is not deduplicated on write. Two users watching the same entity
    produce two demand rows; ``rank`` sums weight across users and reports
    ``requester_count``. Collapsing them at the watchlist layer would erase
    exactly the signal that makes shared coverage worth having, so add_entity
    never does.
  * Demand is deactivated, never deleted. remove_entity withdraws via
    ``ledger.withdraw`` (which sets ``active = false``), because the ledger is a
    record of what was asked for and that history is itself the signal.

Default claim types vary by entity kind: a company gets fundamentals and price;
a crypto asset gets price and on-chain. The map is explicit per kind rather
than a single hardcoded list, because demanding on-chain flows for a stock or a
10-K filing for a token would be noise dressed as coverage.

The one place the demand ledger fought this design: ``direct_attention``
hardcodes ``channel = 'direct'``, so watchlist-raised demand is indistinguishable
from question-raised demand. ``watchlist_entry.demand_id`` is a single column
but add_entity raises one row per default claim type, so it cannot name the
whole set. Removal therefore withdraws the rows add_entity wrote by their shape
(entity, owner, keyless, channel 'direct', claim types in the kind's defaults)
rather than by id alone. A precise origin marker on the ledger would remove the
one residual ambiguity noted in the report.
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

from omni.demand.ledger import direct_attention, withdraw

# The default claim types an entity of a given kind should stay covered for when
# watched. Each entry is the smallest honest set: enough to mean "kept current",
# not so much that demand becomes noise. A single list for every kind would
# demand on-chain flows for stocks and 10-K filings for tokens, which is exactly
# the fabricated-coverage shape the store exists to avoid.
#
# company: price + fundamentals. A quarterly filing (filing_event) is left out
#   because absence of a filing is informative on its own cadence, not a gap to
#   chase every cycle.
# crypto_asset: price + the on-chain types that apply to a token generically.
#   onchain_tvl is protocol-level (a DeFi protocol's locked value), not a
#   property of a raw token, so it is not a default for the asset class.
# macro: the series itself; a macro entity is its series.
#
# An unrecognised kind falls back to price alone -- the one claim type universal
# to anything a user would watch. A silent no-op for an unknown kind would be a
# watchlist entry that raises no demand at all, which is a feature that does not
# work and does not say so.
DEFAULT_CLAIM_TYPES: dict[str, tuple[str, ...]] = {
    "company": ("price_snapshot", "fundamental_metric"),
    "crypto_asset": ("price_snapshot", "onchain_supply", "onchain_flow"),
    "macro": ("macro_series_point",),
}

_FALLBACK_CLAIM_TYPES: tuple[str, ...] = ("price_snapshot",)


def claim_types_for_kind(kind: str | None) -> tuple[str, ...]:
    """The default claim types a watched entity of this kind demands coverage for.

    An unknown kind yields the price-only floor rather than nothing: watching
    should raise demand, and price is the one fact common to anything watched.
    """
    if kind is None:
        return _FALLBACK_CLAIM_TYPES
    return DEFAULT_CLAIM_TYPES.get(kind, _FALLBACK_CLAIM_TYPES)


async def create(pool, *, user_id: UUID, name: str) -> Any:
    """Create a watchlist owned by ``user_id``."""
    return await pool.fetchrow(
        "INSERT INTO watchlist (user_id, name) VALUES ($1, $2) "
        "RETURNING id, user_id, name, created_at",
        user_id,
        name,
    )


async def lists_for_user(pool, *, user_id: UUID) -> list:
    """A user's watchlists. Another user's lists never appear here."""
    return await pool.fetch(
        "SELECT id, name, created_at FROM watchlist WHERE user_id = $1 "
        "ORDER BY created_at",
        user_id,
    )


async def add_entity(
    pool,
    *,
    watchlist_id: UUID,
    entity_id: UUID,
    user_id: UUID,
) -> Any | None:
    """Add an entity to a watchlist, raising demand for its kind's defaults.

    Returns the entry row, or ``None`` if the watchlist is not owned by
    ``user_id`` or the entity does not exist -- the caller turns that into a 404
    rather than this layer guessing. Ownership is checked here so a watchlist is
    nobody's shared asset: another user cannot add to a list they do not own.

    Adding the same entity twice is idempotent: it does not raise demand a
    second time, because two demand rows for the same (entity, type, owner, key)
    would inflate weight without adding signal -- exactly the deduplication the
    ledger forbids on the write path.
    """
    owned = await pool.fetchval(
        "SELECT 1 FROM watchlist WHERE id = $1 AND user_id = $2",
        watchlist_id,
        user_id,
    )
    if owned is None:
        return None

    kind = await pool.fetchval("SELECT kind FROM entity WHERE id = $1", entity_id)
    if kind is None:
        return None

    existing = await pool.fetchrow(
        "SELECT watchlist_id, entity_id, added_at, demand_id "
        "FROM watchlist_entry WHERE watchlist_id = $1 AND entity_id = $2",
        watchlist_id,
        entity_id,
    )
    if existing is not None:
        return existing

    claim_types = claim_types_for_kind(kind)
    first_demand_id = None
    for claim_type in claim_types:
        demand_id = await direct_attention(
            pool,
            entity_id=entity_id,
            claim_type=claim_type,
            key=None,
            requested_by=user_id,
        )
        if first_demand_id is None:
            first_demand_id = demand_id

    # demand_id names one of the rows raised (the representative link). A single
    # column cannot name the per-claim-type set; removal matches the rows by
    # shape. See the module docstring.
    return await pool.fetchrow(
        "INSERT INTO watchlist_entry (watchlist_id, entity_id, demand_id) "
        "VALUES ($1, $2, $3) "
        "RETURNING watchlist_id, entity_id, added_at, demand_id",
        watchlist_id,
        entity_id,
        first_demand_id,
    )


async def remove_entity(
    pool,
    *,
    watchlist_id: UUID,
    entity_id: UUID,
    user_id: UUID,
) -> bool:
    """Remove an entity from a watchlist, withdrawing the demand it raised.

    Returns False if the watchlist is not owned by ``user_id`` or the entry does
    not exist. The demand rows are deactivated (the ledger is a record), not
    deleted.
    """
    owned = await pool.fetchval(
        "SELECT 1 FROM watchlist WHERE id = $1 AND user_id = $2",
        watchlist_id,
        user_id,
    )
    if owned is None:
        return False

    entry = await pool.fetchrow(
        "SELECT 1 FROM watchlist_entry "
        "WHERE watchlist_id = $1 AND entity_id = $2",
        watchlist_id,
        entity_id,
    )
    if entry is None:
        return False

    kind = await pool.fetchval("SELECT kind FROM entity WHERE id = $1", entity_id)
    claim_types = list(claim_types_for_kind(kind))

    # Withdraw the rows add_entity wrote. They are keyless ('direct' channel, no
    # specific series) and target the kind's default claim types; matching that
    # shape deactivates exactly the set raised. withdraw itself only flips
    # active, so history is preserved.
    rows = await pool.fetch(
        "SELECT id FROM demand "
        "WHERE entity_id = $1 AND requested_by = $2 AND key IS NULL "
        "AND channel = 'direct' AND claim_type = ANY($3::claim_type[]) "
        "AND active",
        entity_id,
        user_id,
        claim_types,
    )
    for row in rows:
        await withdraw(pool, row["id"])

    await pool.execute(
        "DELETE FROM watchlist_entry "
        "WHERE watchlist_id = $1 AND entity_id = $2",
        watchlist_id,
        entity_id,
    )
    return True


async def entries(pool, *, watchlist_id: UUID, user_id: UUID) -> list | None:
    """The entities on a watchlist, or ``None`` if not owned by ``user_id``."""
    owned = await pool.fetchval(
        "SELECT 1 FROM watchlist WHERE id = $1 AND user_id = $2",
        watchlist_id,
        user_id,
    )
    if owned is None:
        return None
    return await pool.fetch(
        "SELECT e.entity_id, en.kind, en.symbol, en.name, "
        "e.added_at, e.demand_id "
        "FROM watchlist_entry e "
        "JOIN entity en ON en.id = e.entity_id "
        "WHERE e.watchlist_id = $1 "
        "ORDER BY e.added_at",
        watchlist_id,
    )
