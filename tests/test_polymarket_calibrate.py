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


# --- side selection, costs, the edge interval and the labelled sweep ---------


def _est(p_yes: float) -> Estimation:
    common = {"market_id": "m", "cutoff": datetime(2024, 5, 25, tzinfo=UTC)}
    if p_yes >= 0.5:
        return Estimation(
            chosen_bin=p_yes, direction="up", confidence=p_yes,
            raw_choice=f"{p_yes}", **common,
        )
    return Estimation(
        chosen_bin=p_yes, direction="down", confidence=1.0 - p_yes,
        raw_choice=f"{p_yes}", **common,
    )


class TestSideIsTheSideOfTheDisagreement:
    def test_model_below_market_buys_no_even_when_model_is_above_half(self):
        """Model 0.65 against market 0.85: YES is overpriced, so the trade is
        NO at 0.15. Choosing the side from `est.direction` ("up") bought YES at
        0.85 -- a trade the model's own estimate values at -0.20 a share."""
        from omni.polymarket.pnl import side_and_entry

        assert side_and_entry(model_p_yes=0.65, market_p_yes=0.85) == (
            "NO", pytest.approx(0.15),
        )

    def test_model_above_market_buys_yes_even_when_model_is_below_half(self):
        from omni.polymarket.pnl import side_and_entry

        assert side_and_entry(model_p_yes=0.35, market_p_yes=0.15) == (
            "YES", pytest.approx(0.15),
        )

    def test_backtest_trades_no_and_wins_when_the_market_resolves_no(self):
        from omni.polymarket.calibrate import _backtest_pnl

        pair = (_est(0.65), _snap(_market(resolved_yes=False), market_probability=0.85))
        summary = _backtest_pnl([pair], threshold=0.05, size_usd=5.0, taker=False)
        # NO at 0.15: $5 buys 33.33 shares paying $1 each -> +$28.33. The old
        # side (YES at 0.85) lost the whole $5 stake on the same market.
        assert summary.n_closed == 1
        assert summary.net_pnl == pytest.approx(5.0 / 0.15 - 5.0)

    def test_paper_position_takes_the_same_side(self):
        from omni.polymarket.active import ActiveMarket
        from omni.polymarket.paper import _estimation_to_position

        market = ActiveMarket(
            condition_id="0xabc", question="Will X happen?", category="Politics",
            yes_token_id="y", no_token_id="n", yes_price=0.85, neg_risk=False,
            slug="x", volume=5000.0, end_date=datetime(2030, 1, 1, tzinfo=UTC),
            fetched_at=datetime(2026, 1, 1, tzinfo=UTC),
        )
        pos = _estimation_to_position(
            _est(0.65), market, threshold=0.05, size_usd=5.0,
            method="m", model_name="fake", taker=False,
        )
        assert pos is not None
        assert pos.direction == "NO"
        assert pos.entry_price == pytest.approx(0.15)


class TestTakerIsTheDefault:
    def test_default_charges_fee_and_half_spread(self):
        from omni.polymarket.calibrate import _backtest_pnl, sweep_thresholds
        from omni.polymarket.pnl import DEFAULT_HALF_SPREAD

        pair = (_est(0.75), _snap(_market(resolved_yes=True), market_probability=0.50))
        default = sweep_thresholds([pair], thresholds=(0.05,)).by_threshold[0.05]
        maker = _backtest_pnl([pair], threshold=0.05, size_usd=5.0, taker=False)

        entry = 0.50 + DEFAULT_HALF_SPREAD
        shares = 5.0 / entry
        from omni.polymarket.news import derive_category
        from omni.polymarket.pnl import DEFAULT_FEE_RATES

        rate = DEFAULT_FEE_RATES[derive_category(pair[1].market)]
        assert rate > 0.0
        fee = shares * rate * entry * (1.0 - entry)
        assert default.fee_pnl == pytest.approx(-fee)
        assert default.net_pnl == pytest.approx(shares - 5.0 - fee)
        # The free fill at mid is the explicit best case and is strictly better.
        assert maker.fee_pnl == 0.0
        assert maker.net_pnl == pytest.approx(5.0)
        assert maker.net_pnl > default.net_pnl

    async def test_run_stage_a_defaults_to_taker(self):
        snap = _snap(_market(resolved_yes=True), market_probability=0.50)
        model = FakeLanguageModel(
            responses=[ScriptedResponse(fields={"probability_bin": "0.75"})]
        )
        report = await run_stage_a(model, [snap], pnl_threshold=0.05)
        assert report.pnl_summary is not None
        assert report.pnl_summary.fee_pnl < 0.0


class TestBrierEdgeInterval:
    def test_too_few_clusters_is_not_an_interval(self):
        from omni.polymarket.calibrate import paired_brier_edge_interval

        assert paired_brier_edge_interval(
            [0.9] * 5, [0.5] * 5, [True] * 5, list(range(5))
        ) is None

    def test_one_event_counted_many_times_is_one_cluster(self):
        """Forty markets from ONE event are one observation. Treated as
        independent they would give a tight interval excluding zero."""
        from omni.polymarket.calibrate import paired_brier_edge_interval

        p_model, p_market, outcomes = [0.9] * 40, [0.5] * 40, [True] * 40
        assert paired_brier_edge_interval(
            p_model, p_market, outcomes, ["one-event"] * 40
        ) is None
        independent = paired_brier_edge_interval(
            p_model, p_market, outcomes, list(range(40))
        )
        assert independent is not None
        assert independent[1] < 0.0

    def test_interval_brackets_a_noisy_edge_and_includes_zero(self):
        from omni.polymarket.calibrate import paired_brier_edge_interval

        # Model better on even clusters, equally worse on odd ones: mean edge 0.
        p_model, p_market, outcomes, clusters = [], [], [], []
        for c in range(20):
            better = c % 2 == 0
            p_model.append(0.9 if better else 0.3)
            p_market.append(0.5)
            outcomes.append(True)
            clusters.append(c)
        lo, hi = paired_brier_edge_interval(p_model, p_market, outcomes, clusters)
        assert lo < 0.0 < hi


class TestSweepIsLabelledInSample:
    def test_pick_is_flagged_in_sample(self):
        from omni.polymarket.calibrate import sweep_thresholds

        win = (_est(0.75), _snap(_market(True, "0xa"), market_probability=0.50))
        lose = (_est(0.58), _snap(_market(False, "0xb"), market_probability=0.50))
        sweep = sweep_thresholds([win, lose], thresholds=(0.05, 0.20), taker=False)
        assert sweep.in_sample is True
        # 0.20 keeps only the winner; 0.05 also takes the loser.
        assert sweep.best_in_sample == 0.20
        assert sweep.by_threshold[0.05].n_closed == 2
        assert sweep.by_threshold[0.20].n_closed == 1

    def test_no_trades_means_no_pick(self):
        from omni.polymarket.calibrate import sweep_thresholds

        pair = (_est(0.52), _snap(_market(True), market_probability=0.50))
        assert sweep_thresholds([pair], thresholds=(0.10,)).best_in_sample is None
