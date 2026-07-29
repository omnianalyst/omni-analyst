"""Entity resolution -- asserting behaviour, not shape.

`resolve` must turn any of a symbol, a name or an identifier value into exactly
one entity, and must return None rather than guess whenever the same text could
mean two different entities. `key_for` must hand each provider the key its
adapter fetches by, and must fail loudly via `Unavailable` whenever it does not
have one -- because substituting a default here is how claims get attributed to
the wrong company.
"""

import json

import pytest

from omni.entities.resolve import key_for, resolve
from omni.ingest.protocol import Unavailable


@pytest.fixture(autouse=True)
async def _clean(db):
    await db.pool.execute("TRUNCATE entity CASCADE")
    yield


async def _entity(
    db,
    *,
    kind: str = "company",
    symbol: str | None = None,
    name: str | None = None,
    identifiers: dict | None = None,
):
    return await db.pool.fetchval(
        "INSERT INTO entity (kind, symbol, name, identifiers) "
        "VALUES ($1, $2, $3, $4::jsonb) RETURNING id",
        kind,
        symbol,
        name or symbol,
        json.dumps(identifiers or {}),
    )


# --- resolve: the happy paths -------------------------------------------------


async def test_resolves_by_symbol(db):
    eid = await _entity(db, symbol="AAPL", name="Apple Inc")
    row = await resolve(db.pool, "AAPL")
    assert row is not None
    assert row["id"] == eid
    assert row["symbol"] == "AAPL"


async def test_resolves_by_name(db):
    eid = await _entity(db, symbol="AAPL", name="Apple Inc")
    row = await resolve(db.pool, "Apple Inc")
    assert row is not None and row["id"] == eid


async def test_resolves_case_insensitively(db):
    eid = await _entity(db, symbol="AAPL", name="Apple Inc")
    for text in ("aapl", "aApL", "apple inc", "APPLE INC"):
        row = await resolve(db.pool, text)
        assert row is not None and row["id"] == eid, f"failed for {text!r}"


async def test_resolves_by_identifier_value(db):
    eid = await _entity(
        db, symbol="AAPL", name="Apple Inc",
        identifiers={"cik": "0000320193", "polygon": "AAPL"},
    )
    row = await resolve(db.pool, "0000320193")
    assert row is not None and row["id"] == eid


async def test_text_matching_the_same_entity_two_ways_returns_it(db):
    # "AAPL" hits both the symbol and the polygon identifier value, but it is
    # one entity, so the multi-criterion match must not read as ambiguity.
    eid = await _entity(
        db, symbol="AAPL", name="Apple Inc",
        identifiers={"cik": "0000320193", "polygon": "AAPL"},
    )
    row = await resolve(db.pool, "AAPL")
    assert row is not None and row["id"] == eid


# --- resolve: the honest failures --------------------------------------------


async def test_no_match_returns_none(db):
    await _entity(db, symbol="AAPL", name="Apple Inc")
    assert await resolve(db.pool, "NOPE") is None


async def test_ambiguous_symbol_across_kinds_returns_none(db):
    # The unique constraint is (kind, symbol), so the same symbol can belong to
    # two entities. Picking either would be a guess.
    await _entity(db, kind="company", symbol="AAPL", name="Apple Inc")
    await _entity(db, kind="etf", symbol="AAPL", name="AAPL Bull Fund")
    assert await resolve(db.pool, "AAPL") is None


async def test_ambiguous_across_symbol_and_identifier_returns_none(db):
    # "FUND" is entity A's symbol and entity B's cik. Two distinct entities ->
    # None, even though the two matches came through different criteria.
    await _entity(db, kind="company", symbol="FUND", name="Fund Co")
    await _entity(
        db, kind="company", symbol="OTHER", name="Other Co",
        identifiers={"cik": "FUND"},
    )
    assert await resolve(db.pool, "FUND") is None


async def test_ambiguous_identifier_value_across_two_entities_returns_none(db):
    await _entity(
        db, symbol="A", name="A", identifiers={"cik": "0000999999"},
    )
    await _entity(
        db, symbol="B", name="B", identifiers={"cik": "0000999999"},
    )
    assert await resolve(db.pool, "0000999999") is None


async def test_name_substring_does_not_match(db):
    # Exact match only; "Apple" must not resolve to "Apple Inc" -- that would
    # be a guess about which entity the caller meant.
    await _entity(db, symbol="AAPL", name="Apple Inc")
    assert await resolve(db.pool, "Apple") is None


@pytest.mark.parametrize("blank", ["", "   ", "\t"])
async def test_blank_text_raises_unavailable(db, blank):
    with pytest.raises(Unavailable):
        await resolve(db.pool, blank)


async def test_none_text_raises_unavailable(db):
    with pytest.raises(Unavailable):
        await resolve(db.pool, None)


# --- key_for: the provider keys ----------------------------------------------


async def test_key_for_sec_edgar_returns_cik(db):
    await _entity(
        db, symbol="AAPL", name="Apple Inc",
        identifiers={"cik": "0000320193", "polygon": "AAPL"},
    )
    row = await resolve(db.pool, "AAPL")
    assert key_for(row, "sec_edgar") == "0000320193"


async def test_key_for_polygon_returns_polygon_identifier(db):
    await _entity(
        db, symbol="AAPL", name="Apple Inc",
        identifiers={"cik": "0000320193", "polygon": "AAPL"},
    )
    row = await resolve(db.pool, "AAPL")
    assert key_for(row, "polygon") == "AAPL"


async def test_key_for_coingecko_returns_coin_id(db):
    await _entity(
        db, kind="crypto", symbol="BTC", name="Bitcoin",
        identifiers={"coingecko": "bitcoin"},
    )
    row = await resolve(db.pool, "BTC")
    assert key_for(row, "coingecko") == "bitcoin"


async def test_key_for_reads_jsonb_returned_as_text(db):
    # asyncpg returns the identifiers column as a JSON string (no codec on the
    # pool). key_for must decode it rather than blow up on a str.
    await _entity(
        db, symbol="AAPL", name="Apple Inc",
        identifiers={"cik": "0000320193"},
    )
    row = await db.pool.fetchrow(
        "SELECT id, kind, symbol, name, identifiers FROM entity WHERE symbol='AAPL'"
    )
    assert isinstance(row["identifiers"], str)  # the condition this test guards
    assert key_for(row, "sec_edgar") == "0000320193"


async def test_key_for_unknown_provider_raises(db):
    await _entity(
        db, symbol="AAPL", name="Apple Inc",
        identifiers={"cik": "0000320193"},
    )
    row = await resolve(db.pool, "AAPL")
    with pytest.raises(Unavailable):
        key_for(row, "not_a_real_provider")


async def test_key_for_missing_identifier_raises(db):
    # Entity has identifiers but no "polygon" entry. Asking for the polygon key
    # must fail honestly -- never fall back to the symbol.
    await _entity(
        db, symbol="AAPL", name="Apple Inc",
        identifiers={"cik": "0000320193"},
    )
    row = await resolve(db.pool, "AAPL")
    with pytest.raises(Unavailable):
        key_for(row, "polygon")


async def test_key_for_crypto_without_polygon_id_does_not_substitute_symbol(db):
    # The footgun this module exists to avoid: BTC's symbol is "BTC", but
    # Polygon's crypto key is "X:BTC". A symbol fallback would silently hand
    # Polygon the wrong string; key_for must refuse instead.
    await _entity(
        db, kind="crypto", symbol="BTC", name="Bitcoin",
        identifiers={"coingecko": "bitcoin"},
    )
    row = await resolve(db.pool, "BTC")
    with pytest.raises(Unavailable):
        key_for(row, "polygon")


async def test_key_for_works_on_a_plain_mapping(db):
    # The contract is "anything keyed like a row", so callers can unit-test
    # without a database. identifiers as a dict (already decoded) must work too.
    entity = {"identifiers": {"cik": "0000320193"}}
    assert key_for(entity, "sec_edgar") == "0000320193"


async def test_key_for_empty_identifier_value_raises(db):
    await _entity(
        db, symbol="AAPL", name="Apple Inc",
        identifiers={"cik": ""},
    )
    row = await resolve(db.pool, "AAPL")
    with pytest.raises(Unavailable):
        key_for(row, "sec_edgar")
