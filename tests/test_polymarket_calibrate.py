from datetime import UTC, datetime, timedelta

import httpx
import pytest

from omni.calibration import Outcome
from omni.llm.fake import FakeLanguageModel, ScriptedResponse
from omni.llm.protocol import UnusableCompletion
from omni.polymarket.calibrate import (
    DEFAULT_HORIZON,
    Exclusion,
    _brier,
    _log_loss,
    _outcome_of,
    _p_yes_of,
    prepare_snapshot,
    run_stage_a,
)
from omni.polymarket.gamma import fetch_price_history  # noqa: F401  (proves the import path)
from omni.polymarket.types import Document, Estimation, MarketAtCutoff, ResolvedMarket


def _market(resolved_yes=True, condition_id="0x1"):
    return ResolvedMarket(
        condition_id=condition_id,
        question="Will X happen?",
        category="Politics",
        resolved_yes=resolved_yes,
        resolution_date=datetime(2024, 6, 1, tzinfo=UTC),
        created_at=datetime(2024, 5, 1, tzinfo=UTC),
        yes_token_id="tok-y",
    )


def _snap(market, market_probability=0.5):
    return MarketAtCutoff(
        market=market,
        cutoff=market.resolution_date - DEFAULT_HORIZON,
        market_probability=market_probability,
        documents=(
            Document(
                at=market.created_at + timedelta(days=1),
                source=f"polymarket:market:{market.condition_id}",
                text=market.question,
            ),
        ),
    )


class TestOutcomeMapping:
    def test_yes_resolves_to_upper(self):
        assert _outcome_of(True) is Outcome.UPPER

    def test_no_resolves_to_lower(self):
        assert _outcome_of(False) is Outcome.LOWER


class TestPYesRecovery:
    def test_up_direction_uses_confidence_directly(self):
        est = Estimation(
            chosen_bin=0.65, direction="up", confidence=0.65, raw_choice="0.65",
            market_id="m", cutoff=datetime(2024, 5, 25, tzinfo=UTC),
        )
        assert _p_yes_of(est) == 0.65

    def test_down_direction_inverts(self):
        est = Estimation(
            chosen_bin=0.25, direction="down", confidence=0.75, raw_choice="0.25",
            market_id="m", cutoff=datetime(2024, 5, 25, tzinfo=UTC),
        )
        assert _p_yes_of(est) == pytest.approx(0.25)


class TestBrierAndLogLoss:
    def test_perfect_predictions_score_zero(self):
        assert _brier([1.0, 0.0], [True, False]) == 0.0
        assert _log_loss([1.0, 0.0], [True, False]) == pytest.approx(0.0, abs=1e-5)

    def test_uniform_half_predictions_brier_quarter(self):
        assert _brier([0.5, 0.5], [True, False]) == 0.25

    def test_empty_returns_none(self):
        assert _brier([], []) is None
        assert _log_loss([], []) is None

    def test_mismatched_lengths_returns_none(self):
        assert _brier([0.5], [True, False]) is None

    def test_log_loss_clamps_extreme_predictions(self):
        ll = _log_loss([1.0 - 1e-9], [False])
        assert ll is not None and ll > 0


class TestPrepareSnapshot:
    async def test_happy_path_returns_snapshot(self):
        market = _market()
        cutoff = market.resolution_date - DEFAULT_HORIZON
        ts_at_cutoff = int(cutoff.timestamp())

        def handler(req: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json={"history": [{"t": ts_at_cutoff, "p": "0.6"}]})

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as c:
            snap = await prepare_snapshot(c, market)
        assert isinstance(snap, MarketAtCutoff)
        assert snap.market_probability == 0.6
        assert len(snap.documents) == 1

    async def test_missing_yes_token_excluded(self):
        market = _market()
        market_no_token = ResolvedMarket(
            condition_id=market.condition_id,
            question=market.question,
            category=market.category,
            resolved_yes=market.resolved_yes,
            resolution_date=market.resolution_date,
            created_at=market.created_at,
            yes_token_id=None,
        )
        async with httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(200, json={}))) as c:
            result = await prepare_snapshot(c, market_no_token)
        assert isinstance(result, Exclusion)
        assert "no yes_token_id" in result.reason

    async def test_no_price_near_cutoff_excluded(self):
        market = _market()

        def handler(req):
            return httpx.Response(200, json={"history": []})

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as c:
            result = await prepare_snapshot(c, market, tolerance=timedelta(hours=1))
        assert isinstance(result, Exclusion)
        assert "no price sample" in result.reason

    async def test_document_provider_invoked(self):
        market = _market()
        cutoff = market.resolution_date - DEFAULT_HORIZON
        ts_at_cutoff = int(cutoff.timestamp())
        called = {"n": 0}

        def handler(req):
            return httpx.Response(200, json={"history": [{"t": ts_at_cutoff, "p": "0.6"}]})

        async def provider(client, m, cutoff_dt):
            called["n"] += 1
            return (
                Document(
                    at=m.created_at + timedelta(days=1),
                    source="test-provider",
                    text="evidence",
                ),
            )

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as c:
            snap = await prepare_snapshot(c, market, document_provider=provider)
        assert called["n"] == 1
        assert isinstance(snap, MarketAtCutoff)
        assert snap.documents[0].source == "test-provider"


class TestRunStageA:
    async def test_perfect_predictions_brier_zero(self):
        snapshots = [
            _snap(_market(resolved_yes=True, condition_id="0x1"), market_probability=0.6),
            _snap(_market(resolved_yes=False, condition_id="0x2"), market_probability=0.4),
        ]
        fake = FakeLanguageModel(
            responses=[
                ScriptedResponse(
                    fields={"probability_bin": "0.95"}
                ),
                ScriptedResponse(
                    fields={"probability_bin": "0.05"}
                ),
            ]
        )
        report = await run_stage_a(fake, snapshots)
        assert report.n_estimated == 2
        assert report.n_excluded == 0
        assert report.brier_score == pytest.approx(0.0025, abs=1e-6)
        assert report.brier_edge is not None
        assert report.brier_edge < 0

    async def test_ignorant_predictions_brier_quarter(self):
        snap = _snap(_market(resolved_yes=True), market_probability=0.5)
        fake = FakeLanguageModel(
            responses=[
                ScriptedResponse(
                    fields={"probability_bin": "0.5"}
                )
            ]
        )
        report = await run_stage_a(fake, [snap])
        assert report.n_estimated == 1
        assert report.brier_score == pytest.approx(0.25)

    async def test_llm_refusal_recorded_as_exclusion(self):
        snap = _snap(_market(resolved_yes=True))
        fake = FakeLanguageModel(raises=UnusableCompletion("provider refused"))
        report = await run_stage_a(fake, [snap])
        assert report.n_estimated == 0
        assert report.n_excluded == 1
        assert "LLM unavailable" in report.exclusions[0].reason
        assert report.brier_score is None

    async def test_method_buckets_populated(self):
        snapshots = [
            _snap(_market(resolved_yes=True, condition_id=f"0x{i}"), market_probability=0.5)
            for i in range(15)
        ]
        fake = FakeLanguageModel(
            responses=[
                ScriptedResponse(
                    fields={"probability_bin": "0.55"}
                )
                for _ in range(15)
            ]
        )
        report = await run_stage_a(fake, snapshots, method="polymarket_test_method")
        assert "polymarket_test_method" in report.method_buckets
        buckets = report.method_buckets["polymarket_test_method"]
        total = sum(b.n for b in buckets)
        assert total == 15

    async def test_market_brier_independent_of_llm(self):
        snapshots = [
            _snap(_market(resolved_yes=True, condition_id="0x1"), market_probability=0.9),
            _snap(_market(resolved_yes=False, condition_id="0x2"), market_probability=0.1),
        ]
        fake = FakeLanguageModel(
            responses=[
                ScriptedResponse(fields={"probability_bin": "0.5"})
                for _ in range(2)
            ]
        )
        report = await run_stage_a(fake, snapshots)
        assert report.market_brier_score == pytest.approx(0.01, abs=1e-6)

    async def test_empty_snapshots_returns_empty_report(self):
        fake = FakeLanguageModel(responses=[])
        report = await run_stage_a(fake, [])
        assert report.n_estimated == 0
        assert report.brier_score is None


class TestDiscrimination:
    """Sanity checks that the metrics actually discriminate. A test that a
    perfect predictor and an ignorant predictor score the same would prove
    the metric describes nothing."""

    async def test_perfect_and_ignorant_score_differently(self):
        snap = _snap(_market(resolved_yes=True), market_probability=0.5)

        perfect = FakeLanguageModel(
            responses=[
                ScriptedResponse(
                    fields={"probability_bin": "0.95"}
                )
            ]
        )
        ignorant = FakeLanguageModel(
            responses=[
                ScriptedResponse(
                    fields={"probability_bin": "0.5"}
                )
            ]
        )
        r_perf = await run_stage_a(perfect, [snap])
        r_igno = await run_stage_a(ignorant, [snap])
        assert r_perf.brier_score < r_igno.brier_score
