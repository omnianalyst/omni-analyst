"""FRED/ALFRED adapter.

The fixture is the shape ALFRED actually returns for a revised series: Q4-2007
US GDP was first printed at 0.6 and later revised to -0.2. A point-in-time
system that cannot tell those apart is the whole problem this layer exists to
solve.
"""

from datetime import UTC, datetime

import pytest

from omni.ingest.fred import FredAdapter, parse_observations
from omni.ingest.protocol import ClaimDraft, Unavailable

GDP_VINTAGES = [
    {"date": "2007-10-01", "realtime_start": "2008-01-30", "value": "0.6"},
    {"date": "2007-10-01", "realtime_start": "2008-02-28", "value": "0.6"},
    {"date": "2007-10-01", "realtime_start": "2008-03-27", "value": "-0.2"},
    {"date": "2008-01-01", "realtime_start": "2008-04-30", "value": "."},
]


def _at(iso: str) -> datetime:
    return datetime.fromisoformat(iso).replace(tzinfo=UTC)


class TestParsing:
    def test_every_vintage_becomes_its_own_draft(self):
        drafts = parse_observations(GDP_VINTAGES, series_id="GDP")
        assert len(drafts) == 4

    def test_a_revision_keeps_the_period_and_changes_the_knowledge_date(self):
        drafts = parse_observations(GDP_VINTAGES, series_id="GDP")
        first, _, revised, _ = drafts
        assert first.event_date == revised.event_date == _at("2007-10-01")
        assert first.knowledge_date == _at("2008-01-30")
        assert revised.knowledge_date == _at("2008-03-27")
        assert first.value == {"value": 0.6}
        assert revised.value == {"value": -0.2}

    def test_a_not_yet_published_figure_is_recorded_as_null_not_dropped(self):
        """Otherwise the gap engine re-requests a known hole forever."""
        drafts = parse_observations(GDP_VINTAGES, series_id="GDP")
        assert drafts[-1].value == {"value": None}

    def test_unparseable_rows_are_skipped_rather_than_guessed(self):
        drafts = parse_observations(
            [
                {"date": "not-a-date", "realtime_start": "2008-01-30", "value": "1"},
                {"realtime_start": "2008-01-30", "value": "1"},
                {"date": "2007-10-01", "value": "1"},
            ],
            series_id="GDP",
        )
        assert drafts == []

    def test_knowledge_never_precedes_the_event(self):
        """The schema forbids it, so the parser must not emit it."""
        drafts = parse_observations(
            [{"date": "2008-01-01", "realtime_start": "2007-06-01", "value": "1"}],
            series_id="X",
        )
        assert drafts[0].knowledge_date == drafts[0].event_date

    def test_the_series_id_is_the_claim_key(self):
        drafts = parse_observations(GDP_VINTAGES, series_id="UNRATE")
        assert {d.key for d in drafts} == {"UNRATE"}
        assert {d.claim_type for d in drafts} == {"macro_series_point"}


class TestAdapter:
    async def test_an_injected_fetcher_needs_no_network_or_key(self):
        async def fake(series_id: str) -> list[dict]:
            assert series_id == "GDP"
            return GDP_VINTAGES

        drafts = await FredAdapter(fetch_fn=fake).fetch("GDP")
        assert len(drafts) == 4

    async def test_no_key_and_no_fetcher_is_unavailable_not_empty(self):
        """Honest failure. An empty list would read as 'FRED has no data'."""
        with pytest.raises(Unavailable, match="no FRED API key"):
            await FredAdapter().fetch("GDP")

    async def test_a_source_error_propagates_rather_than_returning_nothing(self):
        async def broken(series_id: str) -> list[dict]:
            raise Unavailable("ALFRED returned HTTP 429 for GDP")

        with pytest.raises(Unavailable, match="429"):
            await FredAdapter(fetch_fn=broken).fetch("GDP")

    async def test_an_empty_response_yields_no_drafts(self):
        async def empty(series_id: str) -> list[dict]:
            return []

        assert await FredAdapter(fetch_fn=empty).fetch("GDP") == []

    def test_the_adapter_declares_its_provider_for_licence_lookup(self):
        adapter = FredAdapter()
        assert adapter.source == "fred"
        assert adapter.provider_key == "fred"


class TestDraftInvariants:
    def test_a_draft_cannot_know_a_fact_before_it_happened(self):
        with pytest.raises(ValueError, match="precedes"):
            ClaimDraft(
                claim_type="macro_series_point",
                key="GDP",
                value={},
                event_date=_at("2008-01-01"),
                knowledge_date=_at("2007-01-01"),
                confidence=1.0,
            )

    @pytest.mark.parametrize("confidence", [-0.1, 1.1])
    def test_confidence_stays_in_range(self, confidence):
        with pytest.raises(ValueError, match="confidence"):
            ClaimDraft(
                claim_type="macro_series_point",
                key="GDP",
                value={},
                event_date=_at("2008-01-01"),
                knowledge_date=_at("2008-01-01"),
                confidence=confidence,
            )
