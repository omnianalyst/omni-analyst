"""Address-label store: attributed on-chain identity, sourced not guessed.

The load-bearing properties, each asserted on its own:

- Case-insensitive lookup. EVM identity is case-insensitive, so a label stored
  in one case must be found when queried in another. This is the assertion that
  catches the whole class of bug where labels silently never match.
- An unlabelled address returns None, not a default. Returning a fabricated
  label is the exact failure this module exists not to have.
- Two sources labelling one address both persist, and lookup returns the
  higher-confidence one (tie-break documented in labels.py).
- lookup_many returns only the addresses that are labelled -- never a dict
  padded with None for the rest.
"""

import asyncpg
import pytest

from omni.ingest.labels import (
    AddressLabel,
    is_valid_address,
    lookup,
    lookup_many,
    seed_labels,
    upsert_labels,
)
from omni.ingest.onchain import KNOWN_EXCHANGES

# 40-hex addresses used as syntactically-valid but non-seed inputs, so the
# write/read tests never collide with the carryover set. Mixed-case forms
# exercise the case-insensitive path.
MIXED = "0x" + "Ab" * 20
LOWER = MIXED.lower()
MIXED_B = "0x" + "Cd" * 20
LOWER_B = MIXED_B.lower()
UNLABELLED = "0x0000000000000000000000000000000000000001"


@pytest.fixture(autouse=True)
async def _clean(db):
    await db.pool.execute("TRUNCATE address_label")
    yield


class TestWriteRead:
    async def test_write_and_read_back(self, db):
        n = await upsert_labels(
            db.pool,
            [AddressLabel("eth", MIXED, "Test Exchange", "exchange", "test", 1.0, "Test")],
        )
        assert n == 1
        got = await lookup(db.pool, "eth", MIXED)
        assert got is not None
        assert got.label == "Test Exchange"
        assert got.category == "exchange"
        assert got.source == "test"
        assert got.confidence == 1.0
        assert got.entity_name == "Test"
        assert got.chain == "eth"
        assert got.address == LOWER


class TestCaseInsensitive:
    async def test_lookup_finds_address_stored_in_a_different_case(self, db):
        await upsert_labels(
            db.pool,
            [AddressLabel("eth", MIXED, "Mixed-Case Exchange", "exchange", "test", 1.0)],
        )
        got = await lookup(db.pool, "eth", LOWER)
        assert got is not None, "address stored mixed-case was not found querying lowercase"
        assert got.label == "Mixed-Case Exchange"
        assert got.address == LOWER

    async def test_lookup_many_is_case_insensitive(self, db):
        await upsert_labels(
            db.pool,
            [AddressLabel("eth", MIXED, "Mixed", "exchange", "test", 1.0)],
        )
        out = await lookup_many(db.pool, "eth", [LOWER])
        assert set(out) == {LOWER}
        assert out[LOWER].label == "Mixed"


class TestIdempotence:
    async def test_upsert_same_key_twice_leaves_one_row(self, db):
        lbl = AddressLabel("eth", LOWER, "Exchange", "exchange", "test", 1.0)
        await upsert_labels(db.pool, [lbl])
        await upsert_labels(db.pool, [lbl])
        count = await db.pool.fetchval("SELECT count(*)::int FROM address_label")
        assert count == 1

    async def test_re_upsert_with_new_confidence_updates_in_place(self, db):
        # Same (chain, address, source) updating confidence must not add a row.
        await upsert_labels(
            db.pool, [AddressLabel("eth", LOWER, "Exchange", "exchange", "test", 0.5)]
        )
        await upsert_labels(
            db.pool, [AddressLabel("eth", LOWER, "Exchange", "exchange", "test", 0.9)]
        )
        count = await db.pool.fetchval("SELECT count(*)::int FROM address_label")
        assert count == 1
        got = await lookup(db.pool, "eth", LOWER)
        assert got is not None and got.confidence == 0.9


class TestMultipleSources:
    async def test_two_sources_persist_and_lookup_returns_higher_confidence(self, db):
        await upsert_labels(
            db.pool,
            [
                AddressLabel("eth", LOWER, "Exchange A", "exchange", "source_a", 0.6),
                AddressLabel("eth", LOWER, "Exchange B", "exchange", "source_b", 0.9),
            ],
        )
        count = await db.pool.fetchval(
            "SELECT count(*)::int FROM address_label WHERE address = $1", LOWER
        )
        assert count == 2
        got = await lookup(db.pool, "eth", LOWER)
        assert got is not None
        assert got.source == "source_b"
        assert got.confidence == 0.9

    async def test_tie_break_is_deterministic_when_confidence_equal(self, db):
        # Equal confidence -> source ascending wins. Deterministic because the
        # unique constraint guarantees the two sources differ.
        await upsert_labels(
            db.pool,
            [
                AddressLabel("eth", LOWER, "Zeta", "exchange", "zzz_source", 0.8),
                AddressLabel("eth", LOWER, "Alpha", "exchange", "aaa_source", 0.8),
            ],
        )
        got = await lookup(db.pool, "eth", LOWER)
        assert got is not None and got.source == "aaa_source"

    async def test_lookup_many_picks_higher_confidence_per_address(self, db):
        await upsert_labels(
            db.pool,
            [
                AddressLabel("eth", LOWER, "Weak", "exchange", "weak", 0.4),
                AddressLabel("eth", LOWER, "Strong", "exchange", "strong", 0.95),
            ],
        )
        out = await lookup_many(db.pool, "eth", [LOWER])
        assert len(out) == 1
        assert out[LOWER].source == "strong"


class TestUnlabelled:
    async def test_unlabelled_address_returns_none(self, db):
        got = await lookup(db.pool, "eth", UNLABELLED)
        assert got is None


class TestLookupMany:
    async def test_returns_only_labelled_addresses_no_none_values(self, db):
        await upsert_labels(
            db.pool,
            [
                AddressLabel("eth", LOWER, "Exchange", "exchange", "test", 1.0),
                AddressLabel("eth", LOWER_B, "Fund", "fund", "test", 1.0),
            ],
        )
        out = await lookup_many(db.pool, "eth", [LOWER, LOWER_B, UNLABELLED])
        assert set(out) == {LOWER, LOWER_B}
        assert None not in out.values()
        assert UNLABELLED not in out
        assert out[LOWER].label == "Exchange"
        assert out[LOWER_B].label == "Fund"

    async def test_empty_input_returns_empty_dict(self, db):
        assert await lookup_many(db.pool, "eth", []) == {}

    async def test_input_duplicates_collapse_to_one_result(self, db):
        await upsert_labels(
            db.pool, [AddressLabel("eth", LOWER, "Exchange", "exchange", "test", 1.0)]
        )
        out = await lookup_many(db.pool, "eth", [LOWER, LOWER, LOWER])
        assert list(out) == [LOWER]


class TestSeed:
    def test_every_seeded_address_is_valid_for_its_chain(self):
        labels = seed_labels()
        assert labels, "seed is empty"
        for lbl in labels:
            assert is_valid_address(lbl.chain, lbl.address), (
                f"invalid {lbl.chain} address: {lbl.address} ({lbl.label})"
            )

    def test_seed_includes_all_seven_carryover_addresses_verbatim(self):
        seeded = {(l.chain, l.address) for l in seed_labels()}
        for addr in KNOWN_EXCHANGES:
            assert ("eth", addr) in seeded, f"missing carryover exchange {addr}"

        by_key = {(l.chain, l.address): l for l in seed_labels()}
        for addr, name in KNOWN_EXCHANGES.items():
            lbl = by_key[("eth", addr)]
            assert lbl.label == name
            assert lbl.source == "v1_known_exchanges"
            assert lbl.category == "exchange"
            assert lbl.confidence == 1.0

    async def test_seed_upserts_and_reads_back(self, db):
        labels = seed_labels()
        n = await upsert_labels(db.pool, labels)
        assert n == len(labels)
        binance14 = "0x28c6c06298d514db089934071355e5743bf21d60"
        got = await lookup(db.pool, "eth", binance14)
        assert got is not None
        assert got.label == "Binance 14"
        assert got.entity_name == "Binance"
        assert got.source == "v1_known_exchanges"

    async def test_seed_is_idempotent(self, db):
        await upsert_labels(db.pool, seed_labels())
        first = await db.pool.fetchval("SELECT count(*)::int FROM address_label")
        await upsert_labels(db.pool, seed_labels())
        second = await db.pool.fetchval("SELECT count(*)::int FROM address_label")
        assert second == first


class TestConfidenceCheck:
    async def test_confidence_zero_rejected(self, db):
        with pytest.raises(asyncpg.exceptions.CheckViolationError):
            await upsert_labels(db.pool, [AddressLabel("eth", LOWER, "X", "exchange", "test", 0.0)])

    async def test_negative_confidence_rejected(self, db):
        with pytest.raises(asyncpg.exceptions.CheckViolationError):
            await upsert_labels(
                db.pool, [AddressLabel("eth", LOWER, "X", "exchange", "test", -0.2)]
            )

    async def test_confidence_above_one_rejected(self, db):
        with pytest.raises(asyncpg.exceptions.CheckViolationError):
            await upsert_labels(db.pool, [AddressLabel("eth", LOWER, "X", "exchange", "test", 1.5)])

    async def test_confidence_one_is_accepted(self, db):
        await upsert_labels(db.pool, [AddressLabel("eth", LOWER, "X", "exchange", "test", 1.0)])
        assert await lookup(db.pool, "eth", LOWER) is not None
