from datetime import UTC, datetime

import pytest

from omni.llm.fake import FakeLanguageModel, ScriptedResponse
from omni.llm.protocol import UnusableCompletion
from omni.polymarket.elicitation import PROBABILITY_BINS, UNCERTAIN_BIN, parse_bin
from omni.polymarket.estimator import _direction_and_confidence, estimate, estimation_id
from omni.polymarket.types import Document, Estimation, MarketAtCutoff, ResolvedMarket


def _market(resolved_yes=True):
    return ResolvedMarket(
        condition_id="0x1",
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
        cutoff=datetime(2024, 5, 25, tzinfo=UTC),
        market_probability=market_probability,
        documents=(
            Document(
                at=datetime(2024, 5, 2, tzinfo=UTC),
                source="polymarket:market:0x1",
                text="Will X happen?",
            ),
        ),
    )


class TestDirectionAndConfidenceMapping:
    @pytest.mark.parametrize("bin_str,exp_direction,exp_conf", [
        ("0.95", "up", 0.95),
        ("0.55", "up", 0.55),
        ("0.05", "down", 0.95),
        ("0.45", "down", 0.55),
        ("0.15", "down", 0.85),
    ])
    def test_mapping(self, bin_str, exp_direction, exp_conf):
        bin_value = parse_bin(bin_str)
        direction, confidence = _direction_and_confidence(bin_value)
        assert direction == exp_direction
        assert confidence == exp_conf

    def test_uncertain_bin_is_up_at_half(self):
        direction, confidence = _direction_and_confidence(parse_bin(UNCERTAIN_BIN))
        assert direction == "up"
        assert confidence == 0.5

    def test_confidence_never_below_half(self):
        for bin_str in PROBABILITY_BINS:
            _, conf = _direction_and_confidence(parse_bin(bin_str))
            assert conf >= 0.5


class TestParseBin:
    def test_valid_bins_parse(self):
        assert parse_bin("0.05") == 0.05
        assert parse_bin("0.95") == 0.95

    def test_uncertain_bin_parses(self):
        assert parse_bin(UNCERTAIN_BIN) == 0.5

    def test_out_of_set_string_refused(self):
        with pytest.raises(ValueError, match="never a valid answer"):
            parse_bin("0.42")

    def test_freeform_string_refused(self):
        with pytest.raises(ValueError, match="never a valid answer"):
            parse_bin("around 0.7")

    def test_number_not_in_bin_centres_refused(self):
        with pytest.raises(ValueError, match="never a valid answer"):
            parse_bin("0.6")


class TestEstimatorEndToEnd:
    async def test_clean_completion_produces_estimation(self):
        fake = FakeLanguageModel(
            responses=[
                ScriptedResponse(
                    fields={"probability_bin": "0.65"}
                )
            ]
        )
        est = await estimate(fake, _snap(_market(resolved_yes=True)))
        assert est.chosen_bin == 0.65
        assert est.direction == "up"
        assert est.confidence == 0.65
        assert est.raw_choice == "0.65"
        assert est.market_id == "0x1"
        assert fake.call_count == 1

    async def test_request_carries_benchmark_measurement(self):
        fake = FakeLanguageModel(
            responses=[
                ScriptedResponse(
                    fields={"probability_bin": "0.5"}
                )
            ]
        )
        await estimate(fake, _snap(_market(), market_probability=0.71))
        request = fake.requests[0]
        measurements = {m.name: m.value for m in request.measurements}
        assert "market_yes_price_at_cutoff" in measurements
        from decimal import Decimal
        assert measurements["market_yes_price_at_cutoff"] == Decimal("0.71")

    async def test_request_anchors_on_market_price(self):
        """The market's current YES price appears as an explicit anchor in the
        task itself (not just in measurements). Anchoring is the single
        highest-leverage elicitation change for LLM probability calibration;
        losing it would silently revert to the unanchored baseline."""
        fake = FakeLanguageModel(
            responses=[ScriptedResponse(fields={"probability_bin": "0.65"})]
        )
        await estimate(fake, _snap(_market(), market_probability=0.71))
        prompt = fake.prompts[0]
        assert "71.0%" in prompt
        assert "anchored on the market" in prompt
        assert "UNDERPRICING YES" in prompt
        assert "OVERPRICING YES" in prompt

    async def test_request_schema_uses_choicefield_only_for_probability(self):
        fake = FakeLanguageModel(
            responses=[
                ScriptedResponse(
                    fields={"probability_bin": "0.5"}
                )
            ]
        )
        await estimate(fake, _snap(_market()))
        schema = fake.requests[0].schema
        prob_field = schema.field("probability_bin")
        assert set(prob_field.options) >= set(PROBABILITY_BINS)
        assert UNCERTAIN_BIN in prob_field.options
        # Single-field schema: only probability_bin, no rationale. The schema
        # is intentionally minimal so the model has no TextField in which to
        # mention a fabricated figure.
        assert schema.names == ("probability_bin",)

    async def test_completion_with_extra_field_refused(self):
        fake = FakeLanguageModel(
            responses=[
                ScriptedResponse(
                    fields={
                        "probability_bin": "0.5",
                        "rationale": "extra",  # schema has no such field
                    }
                )
            ]
        )
        with pytest.raises(UnusableCompletion):
            await estimate(fake, _snap(_market()))

    async def test_completion_with_off_bin_choice_refused(self):
        fake = FakeLanguageModel(
            responses=[
                ScriptedResponse(
                    fields={
                        "probability_bin": "0.42",  # not a bin
                    }
                )
            ]
        )
        with pytest.raises(UnusableCompletion):
            await estimate(fake, _snap(_market()))

    async def test_unavailable_propagates_as_exclusion_signal(self):
        fake = FakeLanguageModel(raises=UnusableCompletion("provider refused"))
        with pytest.raises(UnusableCompletion):
            await estimate(fake, _snap(_market()))


class TestEstimationId:
    def test_id_contains_market_and_cutoff(self):
        cutoff = datetime(2024, 5, 25, 12, 0, 0, tzinfo=UTC)
        est = Estimation(
            chosen_bin=0.65,
            direction="up",
            confidence=0.65,
            raw_choice="0.65",
            market_id="0x1",
            cutoff=cutoff,
        )
        eid = estimation_id(est)
        assert eid.startswith(f"0x1:{int(cutoff.timestamp())}:")


class TestDiscrimination:
    """Stub a deliberately wrong estimator and confirm tests catch it."""

    def test_wrong_mapping_fails_this_test(self):
        def wrong_mapping(bin_value):
            return ("up", bin_value)  # never inverts for low bins

        direction, confidence = wrong_mapping(parse_bin("0.05"))
        assert direction == "up" and confidence == 0.05
        direction_right, confidence_right = _direction_and_confidence(parse_bin("0.05"))
        assert (direction, confidence) != (direction_right, confidence_right), (
            "wrong_mapping should disagree with the real one for bin=0.05"
        )
