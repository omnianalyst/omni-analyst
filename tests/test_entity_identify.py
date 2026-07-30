"""Populating `entity.identifiers->>'cik'` from SEC's published ticker map.

The last test is the reason this module exists: after the step runs against a
company whose symbol is in the map, `resolve.key_for(entity, "sec_edgar")`
returns the CIK rather than raising `Unavailable`. Until that is true, no
`fundamental_metric` gap can be filled by any code path. The earlier tests hold
the guarantees the step makes on the way there -- correct CIK and padding,
honest absence, refusal to guess at ambiguity, preservation of other providers'
keys, idempotency, and a fetch failure that writes nothing and stays quiet.
"""

import json

import pytest

from omni.entities.identify import (
    ABSENT,
    AMBIGUOUS,
    RESOLVED,
    assign_company_ciks,
)
from omni.entities.resolve import key_for
from omni.ingest.protocol import Unavailable

# SEC's company_tickers.json maps an integer key to {cik_str, ticker, title}.
# cik_str is an integer in the real document; the tests mirror that shape so
# parse_ticker_map is exercised against the same types SEC serves.
NVDA_CIK = 1045810
AAPL_CIK = 320193


def _payload(*entries):
    return {
        str(i): {
            "cik_str": cik,
            "ticker": ticker,
            "title": title,
        }
        for i, (cik, ticker, title) in enumerate(entries)
    }


def _fetcher(payload):
    async def fn():
        return payload

    return fn


def _failing_fetcher(exc):
    async def fn():
        raise exc

    return fn


async def _company(db, symbol, *, identifiers=None, name=None):
    return await db.pool.fetchval(
        "INSERT INTO entity (kind, symbol, name, identifiers) "
        "VALUES ('company', $1, $2, $3::jsonb) RETURNING id",
        symbol,
        name or symbol,
        json.dumps(identifiers or {}),
    )


async def _identifiers(db, entity_id):
    raw = await db.pool.fetchval(
        "SELECT identifiers FROM entity WHERE id = $1", entity_id
    )
    return json.loads(raw) if isinstance(raw, str) else raw


@pytest.fixture(autouse=True)
async def _clean(db):
    await db.pool.execute("TRUNCATE entity CASCADE")
    yield


class TestAssignCompanyCiks:
    async def test_symbol_in_map_gets_the_right_cik(self, db):
        entity = await _company(db, "NVDA")
        payload = _payload(
            (NVDA_CIK, "NVDA", "NVIDIA CORP"),
            (AAPL_CIK, "AAPL", "Apple Inc."),
        )

        report = await assign_company_ciks(db.pool, fetch_fn=_fetcher(payload))

        by_symbol = {o.symbol: o for o in report.outcomes}
        assert by_symbol["NVDA"].status == RESOLVED
        assert by_symbol["NVDA"].cik == str(NVDA_CIK)

        identifiers = await _identifiers(db, entity)
        assert identifiers["cik"] == str(NVDA_CIK)

    async def test_stored_cik_is_unpadded_and_edgar_pads_for_its_url(self, db):
        # edgar._fetch_companyfacts does str(cik).zfill(10) to build its URL.
        # The stored form must round-trip through that: an unpadded CIK pads
        # to the 10-digit path; a value already padded must not be mangled.
        entity = await _company(db, "AAPL")
        payload = _payload((AAPL_CIK, "AAPL", "Apple Inc."))

        await assign_company_ciks(db.pool, fetch_fn=_fetcher(payload))

        row = await db.pool.fetchrow(
            "SELECT identifiers FROM entity WHERE id = $1", entity
        )
        cik = key_for(row, "sec_edgar")
        assert cik == str(AAPL_CIK)
        assert len(cik) < 10
        assert cik.zfill(10) == str(AAPL_CIK).zfill(10) == "0000320193"

    async def test_symbol_absent_writes_nothing_and_is_reported(self, db):
        entity = await _company(db, "NOPE")
        payload = _payload((AAPL_CIK, "AAPL", "Apple Inc."))

        report = await assign_company_ciks(db.pool, fetch_fn=_fetcher(payload))

        by_symbol = {o.symbol: o for o in report.outcomes}
        assert by_symbol["NOPE"].status == ABSENT
        assert by_symbol["NOPE"].cik is None
        assert by_symbol["NOPE"].reason

        identifiers = await _identifiers(db, entity)
        assert identifiers == {}

    async def test_ambiguous_ticker_writes_nothing(self, db):
        entity = await _company(db, "DUP")
        payload = _payload(
            (111, "DUP", "First DUP Co"),
            (222, "DUP", "Second DUP Co"),
        )

        report = await assign_company_ciks(db.pool, fetch_fn=_fetcher(payload))

        by_symbol = {o.symbol: o for o in report.outcomes}
        assert by_symbol["DUP"].status == AMBIGUOUS
        assert by_symbol["DUP"].cik is None
        assert by_symbol["DUP"].reason

        identifiers = await _identifiers(db, entity)
        assert identifiers == {}

    async def test_an_existing_polygon_identifier_survives_the_merge(self, db):
        entity = await _company(
            db, "AAPL", identifiers={"polygon": "AAPL"}
        )
        payload = _payload((AAPL_CIK, "AAPL", "Apple Inc."))

        await assign_company_ciks(db.pool, fetch_fn=_fetcher(payload))

        identifiers = await _identifiers(db, entity)
        assert identifiers == {"polygon": "AAPL", "cik": str(AAPL_CIK)}

    async def test_running_twice_leaves_the_row_unchanged(self, db):
        entity = await _company(
            db, "AAPL", identifiers={"polygon": "AAPL"}
        )
        payload = _payload((AAPL_CIK, "AAPL", "Apple Inc."))
        fetch_fn = _fetcher(payload)

        await assign_company_ciks(db.pool, fetch_fn=fetch_fn)
        after_first = await _identifiers(db, entity)

        await assign_company_ciks(db.pool, fetch_fn=fetch_fn)
        after_second = await _identifiers(db, entity)

        assert after_second == after_first == {
            "polygon": "AAPL",
            "cik": str(AAPL_CIK),
        }

    async def test_fetch_failure_writes_nothing_and_does_not_raise(self, db):
        entity = await _company(db, "AAPL")

        report = await assign_company_ciks(
            db.pool, fetch_fn=_failing_fetcher(Unavailable("SEC is down"))
        )

        assert report.fetch_error
        assert report.outcomes == ()
        identifiers = await _identifiers(db, entity)
        assert identifiers == {}

    async def test_key_for_returns_the_cik_after_assignment(self, db):
        # The contract the fill path depends on: after this step runs against a
        # company with a real symbol, resolve.key_for(entity, "sec_edgar")
        # returns a CIK instead of raising Unavailable.
        entity = await _company(db, "NVDA")
        payload = _payload((NVDA_CIK, "NVDA", "NVIDIA CORP"))

        await assign_company_ciks(db.pool, fetch_fn=_fetcher(payload))

        row = await db.pool.fetchrow(
            "SELECT id, kind, symbol, name, identifiers FROM entity "
            "WHERE id = $1",
            entity,
        )
        assert key_for(row, "sec_edgar") == str(NVDA_CIK)
