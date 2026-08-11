"""Stage A's invariant is point-in-time. This file exists to exercise that
invariant end-to-end: nothing that reaches the LLM may be knowable only
after the cutoff. If a test here fails, the system would be scoring itself
against information it would not have had — the failure mode that makes a
backtest lie.
"""

from datetime import UTC, datetime, timedelta

import httpx
import pytest

from omni.llm.fake import FakeLanguageModel, ScriptedResponse
from omni.polymarket.calibrate import DEFAULT_HORIZON, prepare_snapshot, run_stage_a
from omni.polymarket.estimator import estimation_id
from omni.polymarket.types import Document, Estimation, MarketAtCutoff, ResolvedMarket


def _market(**overrides):
    base = {
        "condition_id": "0x1",
        "question": "Will X happen?",
        "category": "Politics",
        "resolved_yes": True,
        "resolution_date": datetime(2024, 6, 1, tzinfo=UTC),
        "created_at": datetime(2024, 5, 1, tzinfo=UTC),
        "yes_token_id": "tok-y",
    }
    base.update(overrides)
    return ResolvedMarket(**base)


class TestCutoffComputation:
    async def test_default_cutoff_is_resolution_minus_horizon(self):
        market = _market()
        cutoff = market.resolution_date - DEFAULT_HORIZON
        ts = int(cutoff.timestamp())

        def handler(req):
            return httpx.Response(200, json={"history": [{"t": ts, "p": "0.5"}]})

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as c:
            snap = await prepare_snapshot(c, market)
        assert isinstance(snap, MarketAtCutoff)
        assert snap.cutoff == cutoff

    async def test_short_market_falls_back_to_midpoint(self):
        market = _market(
            created_at=datetime(2024, 5, 30, tzinfo=UTC),
            resolution_date=datetime(2024, 6, 1, tzinfo=UTC),
        )
        cutoff = market.created_at + (market.resolution_date - market.created_at) / 2
        ts = int(cutoff.timestamp())

        def handler(req):
            return httpx.Response(200, json={"history": [{"t": ts, "p": "0.5"}]})

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as c:
            snap = await prepare_snapshot(c, market)
        assert isinstance(snap, MarketAtCutoff)
        assert snap.cutoff == cutoff

    async def test_price_sample_outside_tolerance_excluded(self):
        market = _market()
        cutoff = market.resolution_date - DEFAULT_HORIZON
        far_from_cutoff = cutoff + timedelta(days=2)
        ts = int(far_from_cutoff.timestamp())

        def handler(req):
            return httpx.Response(200, json={"history": [{"t": ts, "p": "0.5"}]})

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as c:
            result = await prepare_snapshot(c, market, tolerance=timedelta(hours=6))
        from omni.polymarket.calibrate import Exclusion
        assert isinstance(result, Exclusion)
        assert "no price sample" in result.reason


class TestDocumentCutoff:
    def test_post_cutoff_document_refused_at_construction(self):
        with pytest.raises(ValueError, match="lookahead bias"):
            MarketAtCutoff(
                market=_market(),
                cutoff=datetime(2024, 5, 25, tzinfo=UTC),
                market_probability=0.5,
                documents=(
                    Document(
                        at=datetime(2024, 5, 26, tzinfo=UTC),
                        source="leaked",
                        text="post-cutoff evidence",
                    ),
                ),
            )

    async def test_document_provider_signature_receives_cutoff(self):
        market = _market()
        cutoff = market.resolution_date - DEFAULT_HORIZON
        ts = int(cutoff.timestamp())
        received_cutoff = {}

        def handler(req):
            return httpx.Response(200, json={"history": [{"t": ts, "p": "0.5"}]})

        async def provider(client, m, cutoff_dt):
            received_cutoff["value"] = cutoff_dt
            return (
                Document(
                    at=cutoff_dt - timedelta(days=1),
                    source="ok",
                    text="pre-cutoff",
                ),
            )

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as c:
            await prepare_snapshot(c, market, document_provider=provider)
        assert received_cutoff["value"] == cutoff

    async def test_run_stage_a_never_sees_post_cutoff_evidence(self):
        snapshots = [
            MarketAtCutoff(
                market=_market(condition_id=f"0x{i}"),
                cutoff=datetime(2024, 5, 25, tzinfo=UTC),
                market_probability=0.5,
                documents=(
                    Document(
                        at=datetime(2024, 5, 24, tzinfo=UTC),
                        source="pre",
                        text="pre-cutoff evidence",
                    ),
                ),
            )
            for i in range(3)
        ]
        fake = FakeLanguageModel(
            responses=[
                ScriptedResponse(
                    fields={"probability_bin": "0.5"}
                )
                for _ in range(3)
            ]
        )
        report = await run_stage_a(fake, snapshots)
        for rendered_prompt in fake.prompts:
            assert "post-cutoff" not in rendered_prompt
            assert "2024-05-26" not in rendered_prompt
        assert report.n_estimated == 3


class TestReproducibility:
    def test_estimation_id_deterministic_for_same_market_and_cutoff(self):
        cutoff = datetime(2024, 5, 25, 12, 0, 0, tzinfo=UTC)
        est_a = Estimation(
            chosen_bin=0.65, direction="up", confidence=0.65, raw_choice="0.65",
            market_id="0x1", cutoff=cutoff,
        )
        prefix = f"0x1:{int(cutoff.timestamp())}:"
        assert estimation_id(est_a).startswith(prefix)

    def test_estimation_id_changes_across_markets(self):
        cutoff = datetime(2024, 5, 25, 12, 0, 0, tzinfo=UTC)
        est_a = Estimation(
            chosen_bin=0.5, direction="up", confidence=0.5, raw_choice="0.5",
            market_id="0x1", cutoff=cutoff,
        )
        est_b = Estimation(
            chosen_bin=0.5, direction="up", confidence=0.5, raw_choice="0.5",
            market_id="0x2", cutoff=cutoff,
        )
        assert estimation_id(est_a).split(":")[0] != estimation_id(est_b).split(":")[0]
