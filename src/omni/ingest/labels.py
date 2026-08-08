"""Address labels: attributed on-chain identity, sourced not guessed.

Replaces the seven-address `KNOWN_EXCHANGES` literal in onchain.py with a
queryable store. The store is what Nansen sells and what
`flow.exchange_reserve` / `onchain.smart_money` are blocked on: Binance alone
runs hundreds of hot wallets, so any "exchange inflow" signal computed from
seven addresses is anecdote, not measurement.

Provenance is the whole point. `source` records where a label came from and
`confidence` is 1.0 only for an address sourced from the operator itself or a
published label set; anything inferred is below 1.0 and says so in `source`.
An unlabelled address stays unlabelled: `lookup` returns `None`, and nothing
here guesses a label from transaction shape, volume or clustering. A heuristic
label presented as a known one is fabricated provenance, and every flow signal
built on it would inherit the fabrication -- so a heuristic, if ever recorded,
belongs in its own category and its own source string, never as
`source="etherscan"`.

Addresses are stored lowercase because EVM identity is case-insensitive;
`normalise_address` is the one function both write and read call, so they
cannot disagree about casing. Two sources may label one address differently
and both rows persist (UNIQUE is chain+address+source, not chain+address);
`lookup` returns the highest-confidence one, ties broken by `source` ascending
-- deterministic because the unique constraint guarantees no two competing rows
share a source.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass

from omni.ingest.onchain import KNOWN_EXCHANGES

CATEGORY_BRIDGE = "bridge"
CATEGORY_EXCHANGE = "exchange"
CATEGORY_FUND = "fund"
CATEGORY_MINER = "miner"
CATEGORY_PROTOCOL = "protocol"
CATEGORY_TREASURY = "treasury"

_EVM_CHAINS = frozenset({"eth"})
_EVM_ADDRESS_RE = re.compile(r"^0x[a-f0-9]{40}\Z")


@dataclass(frozen=True)
class AddressLabel:
    chain: str
    address: str
    label: str
    category: str
    source: str
    confidence: float
    entity_name: str | None = None


def normalise_address(chain: str, address: str) -> str:
    # The single casing authority. Write and read both go through here, so a
    # label stored in one case is found when queried in another and the stored
    # value is always canonical.
    if chain in _EVM_CHAINS:
        return address.strip().lower()
    return address.strip()


def is_valid_address(chain: str, address: str) -> bool:
    if chain in _EVM_CHAINS:
        return _EVM_ADDRESS_RE.match(normalise_address(chain, address)) is not None
    return bool(address and address.strip())


def _entity_name(label: str) -> str:
    parts = label.rsplit(" ", 1)
    if len(parts) == 2 and parts[1].isdigit():
        return parts[0]
    return label


# Sourced beyond the v1 carryover, each verifiable at the cited URL.
#   WBTC -- CoinGecko published dataset: the `platforms.ethereum` value for
#   coin id "wrapped-bitcoin" (https://api.coingecko.com/api/v3/coins/
#   wrapped-bitcoin). A tokenized-BTC bridge contract; 1.0 because CoinGecko is
#   a published label set, not an inference.
_VERIFIED: tuple[AddressLabel, ...] = (
    AddressLabel(
        chain="eth",
        address="0x2260fac5e5542a773aa44fbcfedf7c193bc2c599",
        label="WBTC Token Contract",
        category=CATEGORY_BRIDGE,
        source="coingecko",
        confidence=1.0,
        entity_name="Wrapped Bitcoin",
    ),
)


def seed_labels() -> tuple[AddressLabel, ...]:
    carryover = tuple(
        AddressLabel(
            chain="eth",
            address=addr,
            label=name,
            category=CATEGORY_EXCHANGE,
            source="v1_known_exchanges",
            confidence=1.0,
            entity_name=_entity_name(name),
        )
        for addr, name in KNOWN_EXCHANGES.items()
    )
    return carryover + _VERIFIED


_UPSERT = """
INSERT INTO address_label (chain, address, label, category, entity_name, source, confidence)
VALUES ($1, $2, $3, $4, $5, $6, $7)
ON CONFLICT (chain, address, source) DO UPDATE SET
    label       = EXCLUDED.label,
    category    = EXCLUDED.category,
    entity_name = EXCLUDED.entity_name,
    confidence  = EXCLUDED.confidence
"""

_LOOKUP = """
SELECT chain, address, label, category, entity_name, source, confidence
FROM address_label
WHERE chain = $1 AND address = $2
ORDER BY confidence DESC, source ASC
LIMIT 1
"""

_LOOKUP_MANY = """
SELECT chain, address, label, category, entity_name, source, confidence
FROM address_label
WHERE chain = $1 AND address = ANY($2)
ORDER BY confidence DESC, source ASC
"""


def _row_to_label(rec) -> AddressLabel:
    return AddressLabel(
        chain=rec["chain"],
        address=rec["address"],
        label=rec["label"],
        category=rec["category"],
        source=rec["source"],
        confidence=float(rec["confidence"]),
        entity_name=rec["entity_name"],
    )


def _affected(status: str) -> int:
    # asyncpg command tag e.g. "INSERT 0 1" -> rows touched (insert or update).
    try:
        return int(status.rsplit(" ", 1)[-1])
    except (ValueError, AttributeError):
        return 0


async def upsert_labels(pool, labels: Iterable[AddressLabel]) -> int:
    rows = [
        (
            lbl.chain,
            normalise_address(lbl.chain, lbl.address),
            lbl.label,
            lbl.category,
            lbl.entity_name,
            lbl.source,
            float(lbl.confidence),
        )
        for lbl in labels
    ]
    if not rows:
        return 0
    written = 0
    async with pool.acquire() as conn, conn.transaction():
        for r in rows:
            written += _affected(await conn.execute(_UPSERT, *r))
    return written


async def lookup(pool, chain: str, address: str) -> AddressLabel | None:
    rec = await pool.fetchrow(_LOOKUP, chain, normalise_address(chain, address))
    return _row_to_label(rec) if rec is not None else None


async def lookup_many(pool, chain: str, addresses: Iterable[str]) -> dict[str, AddressLabel]:
    addrs = [normalise_address(chain, a) for a in addresses]
    if not addrs:
        return {}
    rows = await pool.fetch(_LOOKUP_MANY, chain, addrs)
    out: dict[str, AddressLabel] = {}
    for rec in rows:
        addr = rec["address"]
        if addr in out:
            # ORDER BY confidence DESC, source ASC: the first row per address is
            # the winner, so a later duplicate is a strictly worse label.
            continue
        out[addr] = _row_to_label(rec)
    return out
