"""SEC filing events.

The fixture is copied from a real submissions response for CIK 320193, not
written from memory. That distinction has already cost this repo once: the
DefiLlama parser and its invented fixture agreed with each other and matched
nothing the API serves.
"""

from datetime import UTC, datetime

import pytest

from omni.ingest.filings import FilingsAdapter, parse_submissions
from omni.ingest.protocol import Unavailable

SUBMISSIONS = {
    "name": "Apple Inc.",
    "filings": {
        "recent": {
            "form": ["4", "10-Q", "8-K"],
            "filingDate": ["2026-06-17", "2026-05-01", "2026-05-01"],
            "reportDate": ["2026-06-15", "2026-03-28", ""],
            "accessionNumber": [
                "0001140361-26-025622",
                "0000320193-26-000052",
                "0000320193-26-000051",
            ],
        }
    },
}


def _at(iso: str) -> datetime:
    return datetime.fromisoformat(iso).replace(tzinfo=UTC)


class TestParsing:
    def test_each_filing_becomes_a_draft(self):
        assert len(parse_submissions(SUBMISSIONS, cik="320193")) == 3

    def test_the_accession_number_is_the_key(self):
        """Unique per filing, so re-ingestion is idempotent."""
        drafts = parse_submissions(SUBMISSIONS, cik="320193")
        assert drafts[0].key == "0001140361-26-025622"
        assert len({d.key for d in drafts}) == 3

    def test_report_date_is_the_event_and_filing_date_is_the_knowledge(self):
        """The lag between them is the disclosure delay, and it is only
        visible because both are kept."""
        quarterly = parse_submissions(SUBMISSIONS, cik="320193")[1]
        assert quarterly.event_date == _at("2026-03-28")
        assert quarterly.knowledge_date == _at("2026-05-01")

    def test_a_filing_with_no_report_period_uses_its_filing_date(self):
        """An 8-K about that day's event covers no prior period."""
        eight_k = parse_submissions(SUBMISSIONS, cik="320193")[2]
        assert eight_k.event_date == eight_k.knowledge_date == _at("2026-05-01")

    def test_the_form_type_is_carried(self):
        assert [d.value["form"] for d in parse_submissions(SUBMISSIONS, cik="320193")] == [
            "4", "10-Q", "8-K",
        ]

    def test_a_report_date_after_the_filing_date_is_clamped(self):
        """The schema forbids knowing a fact before it happened."""
        payload = {
            "filings": {"recent": {
                "form": ["8-K"], "filingDate": ["2026-01-01"],
                "reportDate": ["2027-01-01"], "accessionNumber": ["x"],
            }}
        }
        d = parse_submissions(payload, cik="1")[0]
        assert d.event_date == d.knowledge_date

    def test_ragged_arrays_stop_rather_than_mispair(self):
        """Truncated response: pairing a form with another filing's date would
        produce confidently wrong claims."""
        payload = {
            "filings": {"recent": {
                "form": ["4", "10-Q", "8-K"],
                "filingDate": ["2026-06-17"],
                "reportDate": ["2026-06-15"],
                "accessionNumber": ["a"],
            }}
        }
        assert len(parse_submissions(payload, cik="1")) == 1

    def test_an_entry_without_a_filing_date_is_skipped(self):
        payload = {
            "filings": {"recent": {
                "form": ["4"], "filingDate": [""], "reportDate": ["2026-01-01"],
                "accessionNumber": ["a"],
            }}
        }
        assert parse_submissions(payload, cik="1") == []

    def test_an_empty_record_yields_nothing(self):
        assert parse_submissions({"filings": {"recent": {}}}, cik="1") == []


class TestAdapter:
    async def test_an_injected_fetcher_needs_no_network(self):
        async def fake(cik):
            assert cik == "320193"
            return SUBMISSIONS

        drafts = await FilingsAdapter(fetch_fn=fake).fetch("320193")
        assert len(drafts) == 3

    async def test_no_user_agent_is_unavailable(self):
        """SEC rejects requests without one; it is not a key and not optional."""
        with pytest.raises(Unavailable, match="User-Agent"):
            await FilingsAdapter().fetch("320193")

    async def test_an_unknown_company_is_unavailable_not_empty(self):
        """Empty would read as 'this company has never filed anything'."""
        async def missing(cik):
            return None

        with pytest.raises(Unavailable, match="no submissions record"):
            await FilingsAdapter(fetch_fn=missing).fetch("999")

    def test_the_adapter_declares_a_shareable_provider(self):
        from omni.credentials.catalog import redistribution_for

        assert FilingsAdapter().provider_key == "sec_edgar"
        assert redistribution_for("sec_edgar") == "allowed"
