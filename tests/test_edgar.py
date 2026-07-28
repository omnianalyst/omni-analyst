"""SEC EDGAR companyfacts adapter.

The fixture is the shape EDGAR actually returns: Assets reported for FY2022,
then restated by a 10-K/A (same period end, later filing date, different
value). A point-in-time system that cannot tell the original from the
restatement is the whole problem this layer exists to solve.
"""

from datetime import UTC, datetime

import pytest

from omni.ingest.edgar import EdgarAdapter, parse_companyfacts
from omni.ingest.protocol import Unavailable

COMPANYFACTS = {
    "cik": 320193,
    "entityName": "Apple Inc.",
    "facts": {
        "us-gaap": {
            "Assets": {
                "units": {
                    "USD": [
                        {"end": "2022-09-24", "val": 352755000000, "fy": 2022,
                         "fp": "FY", "form": "10-K", "filed": "2022-10-28"},
                        # A 10-K/A restatement of the SAME period end.
                        {"end": "2022-09-24", "val": 352000000000, "fy": 2022,
                         "fp": "FY", "form": "10-K/A", "filed": "2023-02-15"},
                    ]
                }
            },
            "Revenues": {
                "units": {
                    "USD": [
                        {"end": "2022-09-24", "val": 394328000000, "fy": 2022,
                         "fp": "FY", "form": "10-K", "filed": "2022-10-28"},
                    ]
                }
            },
            # A real us-gaap concept that is NOT in DEFAULT_CONCEPTS. It must
            # be filtered out -- the adapter is not a firehose.
            "ResearchAndDevelopmentExpense": {
                "units": {
                    "USD": [
                        {"end": "2022-09-24", "val": 26251000000, "fy": 2022,
                         "fp": "FY", "form": "10-K", "filed": "2022-10-28"},
                    ]
                }
            },
        }
    },
}


def _at(iso: str) -> datetime:
    return datetime.fromisoformat(iso).replace(tzinfo=UTC)


class TestParsing:
    def test_every_fact_becomes_its_own_draft(self):
        drafts = parse_companyfacts(COMPANYFACTS, cik="320193")
        assert len(drafts) == 3  # 2 Assets (incl. restatement) + 1 Revenues

    def test_a_restatement_keeps_the_period_and_changes_the_knowledge_date(self):
        drafts = [
            d for d in parse_companyfacts(COMPANYFACTS, cik="320193")
            if d.key == "Assets"
        ]
        original, restated = sorted(drafts, key=lambda d: d.knowledge_date)
        assert original.event_date == restated.event_date == _at("2022-09-24")
        assert original.knowledge_date == _at("2022-10-28")
        assert restated.knowledge_date == _at("2023-02-15")
        assert original.value == {"value": 352755000000}
        assert restated.value == {"value": 352000000000}

    def test_the_concept_name_lands_in_the_claim_key(self):
        drafts = parse_companyfacts(COMPANYFACTS, cik="320193")
        assert {d.key for d in drafts} == {"Assets", "Revenues"}
        assert {d.claim_type for d in drafts} == {"fundamental_metric"}

    def test_the_xbrl_unit_lands_in_unit(self):
        drafts = parse_companyfacts(COMPANYFACTS, cik="320193")
        assert {d.unit for d in drafts} == {"USD"}

    def test_a_non_usd_unit_is_preserved(self):
        facts = {"facts": {"us-gaap": {"CommonStockSharesOutstanding": {
            "units": {"shares": [
                {"end": "2022-09-24", "val": 15942, "fy": 2022,
                 "fp": "FY", "form": "10-K", "filed": "2022-10-28"},
            ]},
        }}}}
        drafts = parse_companyfacts(
            facts, cik="320193", concepts=["CommonStockSharesOutstanding"]
        )
        assert len(drafts) == 1
        assert drafts[0].unit == "shares"

    def test_concepts_outside_the_list_are_not_emitted(self):
        drafts = parse_companyfacts(COMPANYFACTS, cik="320193")
        assert all(
            d.key != "ResearchAndDevelopmentExpense" for d in drafts
        )

    def test_facts_missing_end_or_filed_are_skipped_rather_than_guessed(self):
        bad = {"facts": {"us-gaap": {"Assets": {"units": {"USD": [
            {"end": "2022-09-24", "val": 1, "filed": "2022-10-28"},  # valid
            {"end": None, "val": 1, "filed": "2022-10-28"},          # no end
            {"end": "2022-09-24", "val": 1},                          # no filed
            {"end": "2022-09-24", "filed": "2022-10-28"},             # no val
        ]}}}}}
        drafts = parse_companyfacts(bad, cik="320193")
        assert len(drafts) == 1

    def test_an_empty_but_valid_payload_yields_no_drafts(self):
        drafts = parse_companyfacts({"facts": {"us-gaap": {}}}, cik="320193")
        assert drafts == []


class TestAdapter:
    async def test_an_injected_fetcher_needs_no_network_or_user_agent(self):
        async def fake(cik: str) -> dict:
            assert cik == "320193"
            return COMPANYFACTS

        drafts = await EdgarAdapter(fetch_fn=fake).fetch("320193")
        assert len(drafts) == 3

    async def test_no_user_agent_and_no_fetcher_is_unavailable_not_empty(self):
        """Honest failure. An empty list would read as 'EDGAR has no data'."""
        with pytest.raises(Unavailable, match="User-Agent"):
            await EdgarAdapter().fetch("320193")

    async def test_a_source_error_propagates_rather_than_returning_nothing(self):
        async def broken(cik: str) -> dict:
            raise Unavailable("EDGAR companyfacts returned HTTP 403 for CIK 0000320193")

        with pytest.raises(Unavailable, match="403"):
            await EdgarAdapter(fetch_fn=broken).fetch("320193")

    async def test_a_fetcher_returning_none_is_unavailable_not_empty(self):
        async def none_fetch(cik: str):
            return None

        with pytest.raises(Unavailable):
            await EdgarAdapter(fetch_fn=none_fetch).fetch("320193")

    async def test_an_empty_response_yields_no_drafts(self):
        async def empty(cik: str) -> dict:
            return {"facts": {"us-gaap": {}}}

        assert await EdgarAdapter(fetch_fn=empty).fetch("320193") == []

    def test_the_adapter_declares_its_provider_for_licence_lookup(self):
        adapter = EdgarAdapter()
        assert adapter.source == "sec_edgar"
        assert adapter.provider_key == "sec_edgar"


def test_a_fact_filed_before_its_period_is_skipped_not_fatal():
    """One bad row must not abort a whole company's ingestion."""
    facts = {
        "cik": 320193,
        "facts": {"us-gaap": {"Assets": {"units": {"USD": [
            {"end": "2023-12-31", "filed": "2023-06-01", "val": 1, "form": "10-K"},
            {"end": "2022-09-24", "filed": "2022-10-28", "val": 2, "form": "10-K"},
        ]}}}},
    }
    drafts = parse_companyfacts(facts, cik="320193")
    assert [d.value for d in drafts] == [{"value": 2}]
