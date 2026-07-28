"""Macro perception adapter.

The fixture is ALFRED's actual vintage shape for a daily series: a value is
printed, then revised, then a following period is published as '.' (not yet
available). The whole point of reusing parse_observations is that this contract
is preserved verbatim — and then re-stamped as perception_macro.
"""

from datetime import UTC, datetime

import pytest

from omni.ingest.macro_perception import (
    CLAIM_TYPE,
    PERCEPTION_SERIES,
    MacroPerceptionAdapter,
)
from omni.ingest.protocol import Unavailable

VIX_VINTAGES = [
    {"date": "2024-01-02", "realtime_start": "2024-01-03", "value": "12.95"},
    {"date": "2024-01-02", "realtime_start": "2024-01-04", "value": "13.01"},
    {"date": "2024-01-03", "realtime_start": "2024-01-04", "value": "."},
]


def _at(iso: str) -> datetime:
    return datetime.fromisoformat(iso).replace(tzinfo=UTC)


class TestSeriesMap:
    def test_every_series_has_a_non_empty_description(self):
        for series_id, description in PERCEPTION_SERIES.items():
            assert isinstance(series_id, str) and series_id
            assert isinstance(description, str) and description.strip()

    def test_the_curated_set_matches_the_work_order(self):
        assert set(PERCEPTION_SERIES) == {
            "UMCSENT",
            "VIXCLS",
            "BAMLH0A0HYM2",
            "T10Y2Y",
            "STLFSI4",
            "DTWEXBGS",
        }


class TestAdapter:
    async def test_a_recorded_payload_produces_perception_claims_keyed_by_series(
        self,
    ):
        async def fake(series_id: str) -> list[dict]:
            assert series_id == "VIXCLS"
            return VIX_VINTAGES

        drafts = await MacroPerceptionAdapter(fetch_fn=fake).fetch("VIXCLS")
        assert len(drafts) == 3
        assert {d.key for d in drafts} == {"VIXCLS"}
        assert {d.claim_type for d in drafts} == {"perception_macro"}

    async def test_every_draft_is_restamped_perception_macro_not_macro_series(
        self,
    ):
        async def fake(series_id: str) -> list[dict]:
            return VIX_VINTAGES

        drafts = await MacroPerceptionAdapter(fetch_fn=fake).fetch("VIXCLS")
        assert all(d.claim_type == CLAIM_TYPE == "perception_macro" for d in drafts)

    async def test_a_revision_keeps_the_period_and_changes_the_knowledge_date(self):
        async def fake(series_id: str) -> list[dict]:
            return VIX_VINTAGES

        first, revised, _ = await MacroPerceptionAdapter(fetch_fn=fake).fetch(
            "VIXCLS"
        )
        assert first.event_date == revised.event_date == _at("2024-01-02")
        assert first.knowledge_date == _at("2024-01-03")
        assert revised.knowledge_date == _at("2024-01-04")
        assert first.value == {"value": 12.95}
        assert revised.value == {"value": 13.01}

    async def test_a_not_yet_published_figure_is_kept_as_null_not_dropped(self):
        async def fake(series_id: str) -> list[dict]:
            return VIX_VINTAGES

        drafts = await MacroPerceptionAdapter(fetch_fn=fake).fetch("VIXCLS")
        assert drafts[-1].value == {"value": None}

    async def test_the_evidence_dict_names_what_the_series_measures(self):
        async def fake(series_id: str) -> list[dict]:
            return VIX_VINTAGES

        drafts = await MacroPerceptionAdapter(fetch_fn=fake).fetch("VIXCLS")
        expected = {
            "series": "VIXCLS",
            "measures": PERCEPTION_SERIES["VIXCLS"],
        }
        for d in drafts:
            assert d.evidence == expected

    async def test_no_key_and_no_fetcher_is_unavailable_not_empty(self):
        with pytest.raises(Unavailable, match="no FRED API key"):
            await MacroPerceptionAdapter().fetch("VIXCLS")

    async def test_a_source_error_propagates_rather_than_returning_nothing(self):
        async def broken(series_id: str) -> list[dict]:
            raise Unavailable("ALFRED returned HTTP 429 for VIXCLS")

        with pytest.raises(Unavailable, match="429"):
            await MacroPerceptionAdapter(fetch_fn=broken).fetch("VIXCLS")

    async def test_an_empty_response_yields_no_drafts(self):
        async def empty(series_id: str) -> list[dict]:
            return []

        assert await MacroPerceptionAdapter(fetch_fn=empty).fetch("VIXCLS") == []

    async def test_a_series_outside_the_curated_map_is_refused(self):
        async def fake(series_id: str) -> list[dict]:
            return VIX_VINTAGES

        with pytest.raises(Unavailable, match="not a curated macro-perception"):
            await MacroPerceptionAdapter(fetch_fn=fake).fetch("GDP")

    def test_the_adapter_declares_its_provider_for_licence_lookup(self):
        adapter = MacroPerceptionAdapter()
        assert adapter.source == "fred"
        assert adapter.provider_key == "fred"
