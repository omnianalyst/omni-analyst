"""Entity resolution: turn free text into a single entity, or nothing.

The orchestrator carries an objective's `target` as free text and has until now
handed it verbatim to every adapter. That cannot be right: EDGAR fetches by CIK,
Polygon by ticker, CoinGecko by coin id, and "Apple" / "AAPL" / CIK 320193 are
three different strings for one company. This module is the single place that
maps any of those strings onto an `entity` row, and maps an entity back onto the
key a given provider's adapter expects.

The rule that overrides every other consideration: **ambiguity resolves to None,
never a guess.** Picking the wrong company silently attributes claims to it, and
that silent misattribution is the failure this store exists to prevent. If
"AAPL" is the symbol of two entities (an equity and an ETF), or if "320193"
appears as an identifier value on two rows, `resolve` returns None and the
caller declines to fill rather than guessing.

`key_for` is identifier-driven and has no symbol fallback. For an equity the
Polygon key happens to equal the ticker, but for a crypto asset it does not, and
a blanket fallback would silently hand Polygon a symbol it does not index -- the
exact misattribution this module exists to prevent. Seeders populate
`identifiers` for every provider an entity is meant to be fillable from; a
missing key fails honestly via `Unavailable` rather than being substituted.
"""

from __future__ import annotations

import json
from typing import Any

from omni.ingest.protocol import Unavailable

# Which `identifiers` JSONB key holds the lookup key each provider's adapter
# fetches by. Only providers whose adapters actually key per-entity are listed;
# the work order names these three (EDGAR/CIK, Polygon/ticker, CoinGecko/slug).
# A provider absent here has no resolvable key, and asking for it fails honestly
# rather than being served a default.
_PROVIDER_IDENTIFIER: dict[str, str] = {
    "sec_edgar": "cik",
    "polygon": "polygon",
    "coingecko": "coingecko",
}

_MATCH_SQL = """
SELECT id, kind, symbol, name, identifiers
FROM entity
WHERE lower(symbol) = lower($1)
   OR lower(name) = lower($1)
   OR EXISTS (
        SELECT 1 FROM jsonb_each_text(identifiers)
        WHERE lower(value) = lower($1)
   )
"""


async def resolve(pool, text: str) -> Any | None:
    """Resolve free text to a single entity row, or None.

    Matches case-insensitively against the entity's symbol, its name, or any
    value in its `identifiers` JSONB. A non-empty input that matches exactly one
    entity returns that row; an input matching none, or more than one distinct
    entity, returns None. Blank input is missing input rather than a query, and
    raises `Unavailable` -- treating "" as a match against every entity whose
    symbol or name is empty would be a guess.

    The returned row is an asyncpg Record (or compatible mapping); callers read
    `id`, `kind`, `symbol`, `name` and `identifiers`.
    """
    if text is None or not str(text).strip():
        raise Unavailable("resolve() requires non-empty text")
    rows = await pool.fetch(_MATCH_SQL, text)
    if len(rows) != 1:
        return None
    return rows[0]


def key_for(entity, provider_key: str) -> str:
    """The key `provider_key`'s adapter fetches `entity` by.

    Read from the entity's `identifiers` JSONB under the key this provider uses
    (CIK for SEC EDGAR, the polygon ticker, the coingecko coin id). `identifiers`
    is decoded defensively because the pool sets no jsonb codec and asyncpg
    returns the column as a JSON string (see capability/derived.py, api/alerts).

    Raises `Unavailable` if the provider has no identifier mapping or the entity
    carries no value for it. Never returns a default.
    """
    ident_key = _PROVIDER_IDENTIFIER.get(provider_key)
    if ident_key is None:
        raise Unavailable(f"no identifier mapping for provider {provider_key!r}")

    identifiers = entity["identifiers"]
    if isinstance(identifiers, str):
        identifiers = json.loads(identifiers)
    if not identifiers:
        raise Unavailable(
            f"entity carries no {ident_key!r} identifier for {provider_key!r}"
        )
    value = identifiers.get(ident_key)
    if value is None or value == "":
        raise Unavailable(
            f"entity carries no {ident_key!r} identifier for {provider_key!r}"
        )
    return value
