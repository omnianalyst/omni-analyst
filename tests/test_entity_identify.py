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
import logging

import httpx
import pytest

from omni.entities.identify import (
    ABSENT,
    AMBIGUOUS,
    RESOLVED,
    assign_company_ciks,
    run,
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


# The non-JSON and wrong-shape tests exercise the real `_fetch_ticker_map` (not
# an injected fetch_fn), because the fix lives there: it is the translation of
# `response.json()` / payload shape into Unavailable that must be proven. No
# network call is made -- httpx.AsyncClient is replaced with a fake whose
# `.json()` decodes `text` exactly like the real one, so a non-JSON body raises
# json.JSONDecodeError and a JSON body of any shape is returned verbatim.
class _FakeResponse:
    def __init__(self, status_code, text):
        self.status_code = status_code
        self.text = text

    def json(self):
        return json.loads(self.text)


class _FakeAsyncClient:
    def __init__(self, response):
        self._response = response

    def __call__(self, *args, **kwargs):
        return self

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def get(self, url, headers=None):
        return self._response


def _patch_httpx(monkeypatch, response):
    monkeypatch.setattr(httpx, "AsyncClient", _FakeAsyncClient(response))


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

    async def test_a_200_with_non_json_body_is_a_source_failure(
        self, db, monkeypatch
    ):
        # SEC serves an HTML notice (or a truncated/gzip-broken payload) as a
        # 200. response.json() raises json.JSONDecodeError, which is not an
        # httpx.HTTPError; before the fix that escaped every guard and crashed
        # the scheduler. It must be translated to Unavailable, contained here,
        # written nowhere, and surfaced as a fetch_error.
        _patch_httpx(
            monkeypatch, _FakeResponse(200, "<html>SEC throttled notice</html>")
        )
        entity = await _company(db, "AAPL")

        report = await assign_company_ciks(db.pool, user_agent="test-agent")

        assert report.fetch_error
        assert "non-JSON" in report.fetch_error
        assert report.outcomes == ()
        assert await _identifiers(db, entity) == {}

    async def test_a_json_payload_of_the_wrong_shape_is_a_source_failure(
        self, db, monkeypatch
    ):
        # A 200 body that is valid JSON but not the dict-of-dicts SEC ticker map
        # (an error object, a wrapped response, or a schema change). Deciding
        # this is a source failure: the structure is what SEC served. The guard
        # is structural only -- it never swallows a logic bug, which would show
        # as zero resolved symbols, not as an exception.
        _patch_httpx(
            monkeypatch,
            _FakeResponse(200, '{"error": "rate limited", "message": "stop"}'),
        )
        entity = await _company(db, "AAPL")

        report = await assign_company_ciks(db.pool, user_agent="test-agent")

        assert report.fetch_error
        assert "not a JSON object" in report.fetch_error
        assert report.outcomes == ()
        assert await _identifiers(db, entity) == {}

    async def test_a_top_level_json_array_is_a_source_failure(
        self, db, monkeypatch
    ):
        # The other structural failure: payload itself is not a JSON object.
        _patch_httpx(monkeypatch, _FakeResponse(200, "[1, 2, 3]"))
        entity = await _company(db, "AAPL")

        report = await assign_company_ciks(db.pool, user_agent="test-agent")

        assert report.fetch_error
        assert "not a JSON object" in report.fetch_error
        assert report.outcomes == ()
        assert await _identifiers(db, entity) == {}

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


class TestEntryPoint:
    # `run` is the entry point both the CLI (`python -m omni.entities.identify`)
    # and the scheduler startup call. It runs the step against a pool and logs
    # the report, containing every SEC failure so a caller that treats CIK
    # population as best-effort -- the scheduler at boot -- is never blocked.

    async def test_run_reports_resolved_absent_ambiguous_counts(self, db, caplog):
        aapl = await _company(db, "AAPL")
        nvda = await _company(db, "NVDA")
        await _company(db, "NOPE")
        await _company(db, "DUP")
        payload = _payload(
            (AAPL_CIK, "AAPL", "Apple Inc."),
            (NVDA_CIK, "NVDA", "NVIDIA CORP"),
            (111, "DUP", "First DUP"),
            (222, "DUP", "Second DUP"),
        )

        with caplog.at_level(logging.INFO, logger="omni.entities.identify"):
            await run(db.pool, fetch_fn=_fetcher(payload))

        summary = [
            r for r in caplog.records if "identifiers populated" in r.message
        ]
        assert summary, "run() logged no summary line"
        line = summary[0].message
        assert "2 resolved" in line
        assert "1 absent" in line
        assert "1 ambiguous" in line
        # The step actually ran, not just logged: both resolved symbols got CIKs.
        assert (await _identifiers(db, aapl))["cik"] == str(AAPL_CIK)
        assert (await _identifiers(db, nvda))["cik"] == str(NVDA_CIK)

    async def test_a_fetch_failure_does_not_propagate_and_is_reported(
        self, db, caplog
    ):
        # The guard that keeps the scheduler bootable: a SEC fetch failure must
        # not escape the entry point, and must be surfaced in the log.
        entity = await _company(db, "AAPL")

        with caplog.at_level(logging.WARNING, logger="omni.entities.identify"):
            await run(
                db.pool, fetch_fn=_failing_fetcher(Unavailable("SEC is down"))
            )

        assert any("SEC is down" in r.message for r in caplog.records)
        assert await _identifiers(db, entity) == {}

    async def test_a_missing_user_agent_does_not_propagate(self, db, caplog):
        # No fetch_fn and no user_agent -> assign_company_ciks raises
        # Unavailable before fetching; run() must contain that too. This is the
        # most likely real failure (operator forgot SEC_USER_AGENT).
        entity = await _company(db, "AAPL")

        with caplog.at_level(logging.WARNING, logger="omni.entities.identify"):
            await run(db.pool)

        assert any("skipped" in r.message for r in caplog.records)
        assert await _identifiers(db, entity) == {}

    async def test_run_survives_a_200_non_json_body_and_logs(
        self, db, monkeypatch, caplog
    ):
        # The scheduler-bootability property: the exact defect (a 200 whose body
        # is not JSON) must not escape `run`, which is what scheduler startup
        # awaits. It returns normally and logs the failure.
        _patch_httpx(
            monkeypatch, _FakeResponse(200, "<html>SEC throttled notice</html>")
        )
        entity = await _company(db, "AAPL")

        with caplog.at_level(logging.WARNING, logger="omni.entities.identify"):
            await run(db.pool, user_agent="test-agent")

        assert any(
            "identifier population failed" in r.message for r in caplog.records
        )
        assert any(
            "non-JSON" in r.message for r in caplog.records
        )
        assert await _identifiers(db, entity) == {}

    async def test_an_entity_created_after_first_run_gets_its_cik_on_second_run(
        self, db
    ):
        # The self-healing property that makes boot-time invocation sufficient:
        # an entity created while the scheduler is already up gets its CIK on
        # the next boot (or manual run), because the step re-reads every
        # company row each time.
        payload = _payload(
            (AAPL_CIK, "AAPL", "Apple Inc."),
            (NVDA_CIK, "NVDA", "NVIDIA CORP"),
        )
        fetch_fn = _fetcher(payload)

        aapl = await _company(db, "AAPL")
        await run(db.pool, fetch_fn=fetch_fn)
        assert (await _identifiers(db, aapl))["cik"] == str(AAPL_CIK)

        nvda = await _company(db, "NVDA")
        assert await _identifiers(db, nvda) == {}

        await run(db.pool, fetch_fn=fetch_fn)
        assert (await _identifiers(db, nvda))["cik"] == str(NVDA_CIK)
