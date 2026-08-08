"""What the language-model seam must refuse.

A model is the one source in this system that answers every time, plausibly,
whether or not it knows anything. So the tests worth having are almost all
refusals, and each one names the fabricated artefact it prevents:

1. **A figure the model made up.** Generated text may only restate a
   measurement the caller passed in. "rose 43%" against no measurement of 43 is
   refused, and so is "fell 3.2" against a measurement of -3.2, because an
   inverted sign is a fabricated direction wearing a real number.

2. **An empty or partial answer read as a short one.** A missing field, blank
   text and an unrequested field all refuse. There is no path from a failed
   call to an object a caller can read.

3. **An untraceable one.** No model name, or a naive timestamp, means a
   sentence that cannot be tied back to what produced it.

4. **A cost that looks free.** A float cost, or an adapter defaulting an
   unreported bill to zero, corrupts the netting against edge in basis points.

The fake is held to the same rules as a provider, which is the only reason a
test using it proves anything about a provider.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from typing import get_args

import pytest

from omni.ingest.protocol import Unavailable
from omni.llm import (
    ChoiceField,
    Completion,
    CompletionRequest,
    FakeLanguageModel,
    InvalidRequest,
    LanguageModel,
    Measurement,
    OutputField,
    ResponseSchema,
    ScriptedResponse,
    TextField,
    TokenUsage,
    UnusableCompletion,
)

NOW = datetime(2026, 8, 7, 12, 0, tzinfo=UTC)

SCHEMA = ResponseSchema(
    (
        TextField(name="summary", max_chars=200),
        ChoiceField(name="direction", options=("up", "down", "flat")),
    )
)

GROWTH = Measurement(
    name="revenue_growth",
    value=Decimal(43),
    unit="percent",
    source="claim:edgar:revenue",
)


def _request(**overrides) -> CompletionRequest:
    base = {
        "task": "narrate_finding",
        "instructions": "Describe the finding in one sentence.",
        "schema": SCHEMA,
        "measurements": (GROWTH,),
    }
    return CompletionRequest(**{**base, **overrides})


def _completion(fields: dict[str, str], **overrides) -> Completion:
    base = {
        "request": _request(),
        "model": "test-model",
        "created_at": NOW,
        "fields": fields,
        "usage": TokenUsage(prompt_tokens=10, completion_tokens=5),
    }
    return Completion(**{**base, **overrides})


class TestNoNumberOriginatesFromTheModel:
    def test_a_figure_no_measurement_carries_is_refused(self):
        with pytest.raises(UnusableCompletion, match="no measurement"):
            _completion({"summary": "Revenue rose 61% this quarter.", "direction": "up"})

    def test_the_measured_figure_may_be_restated(self):
        completion = _completion(
            {"summary": "Revenue rose 43% this quarter.", "direction": "up"}
        )
        assert completion.fields["summary"] == "Revenue rose 43% this quarter."

    def test_an_inverted_sign_is_refused(self):
        request = _request(
            measurements=(
                Measurement(
                    name="drawdown",
                    value=Decimal("-3.2"),
                    unit="percent",
                    source="claim:internal:drawdown",
                ),
            )
        )
        with pytest.raises(UnusableCompletion, match="'3.2'"):
            _completion(
                {"summary": "The position fell 3.2 percent.", "direction": "down"},
                request=request,
            )
        allowed = _completion(
            {"summary": "The position moved -3.2 percent.", "direction": "down"},
            request=request,
        )
        assert "-3.2" in allowed.fields["summary"]

    def test_a_trailing_zero_is_the_same_number(self):
        request = _request(
            measurements=(
                Measurement(
                    name="revenue_growth",
                    value=Decimal("43.0"),
                    unit="percent",
                    source="claim:edgar:revenue",
                ),
            )
        )
        completion = _completion(
            {"summary": "Growth of 43 percent.", "direction": "up"}, request=request
        )
        assert completion.fields["direction"] == "up"

    def test_a_thousands_separator_is_the_same_number(self):
        request = _request(
            measurements=(
                Measurement(
                    name="volume",
                    value=Decimal("1234.5"),
                    unit="contracts",
                    source="claim:polygon:volume",
                ),
            )
        )
        completion = _completion(
            {"summary": "Volume of 1,234.5 contracts.", "direction": "flat"},
            request=request,
        )
        assert "1,234.5" in completion.fields["summary"]

    def test_a_digit_hidden_inside_a_word_is_still_a_figure(self):
        with pytest.raises(UnusableCompletion, match="no measurement"):
            _completion({"summary": "Strong Q3 for the name.", "direction": "up"})

    def test_a_number_the_caller_wrote_into_an_option_is_not_the_models(self):
        schema = ResponseSchema(
            (ChoiceField(name="bucket", options=("below 43", "above 43")),)
        )
        completion = _completion(
            {"bucket": "above 43"}, request=_request(schema=schema, measurements=())
        )
        assert completion.fields["bucket"] == "above 43"

    def test_the_schema_offers_no_way_to_ask_for_a_number(self):
        # The absence is the design: a consumer that wants a figure out of a
        # model finds no field kind that can express the request. Adding one
        # here is the change this asserts against.
        assert set(get_args(OutputField)) == {TextField, ChoiceField}


class TestRefusalIsTheOnlyAlternativeToAnAnswer:
    @pytest.mark.parametrize(
        ("fields", "why"),
        [
            ({"direction": "up"}, "a missing field"),
            ({"summary": "Fine.", "direction": "up", "extra": "x"}, "an extra field"),
            ({"summary": "", "direction": "up"}, "empty text"),
            ({"summary": "   ", "direction": "up"}, "whitespace text"),
            ({"summary": "x" * 201, "direction": "up"}, "over-long text"),
            ({"summary": "Fine.", "direction": "sideways"}, "an invented choice"),
            ({"summary": "Fine.", "direction": ""}, "an empty choice"),
            ({"summary": "Up 61%.", "direction": "up"}, "an invented figure"),
            ({}, "nothing at all"),
        ],
    )
    def test_no_malformed_answer_yields_a_usable_object(self, fields, why):
        with pytest.raises(Unavailable):
            _completion(fields)

    def test_a_schema_violation_is_an_unavailable_so_existing_handlers_catch_it(self):
        assert issubclass(UnusableCompletion, Unavailable)

    def test_a_non_text_field_value_is_refused(self):
        with pytest.raises(UnusableCompletion, match="not text"):
            _completion({"summary": 43, "direction": "up"})


class TestProvenance:
    def test_the_fingerprint_is_of_the_prompt_actually_sent(self):
        from hashlib import sha256

        request = _request()
        assert (
            request.fingerprint
            == sha256(request.render().encode("utf-8")).hexdigest()
        )

    def test_changing_a_measurement_changes_the_fingerprint(self):
        other = Measurement(
            name="revenue_growth",
            value=Decimal(44),
            unit="percent",
            source="claim:edgar:revenue",
        )
        assert _request().fingerprint != _request(measurements=(other,)).fingerprint

    def test_provenance_names_the_model_the_prompt_and_the_measurements(self):
        completion = _completion({"summary": "Up 43%.", "direction": "up"})
        provenance = completion.provenance()
        assert provenance["origin"] == "language_model"
        assert provenance["model"] == "test-model"
        assert provenance["prompt_sha256"] == completion.request.fingerprint
        assert provenance["created_at"] == NOW.isoformat()
        assert provenance["measurements"] == [
            {
                "name": "revenue_growth",
                "value": "43",
                "unit": "percent",
                "source": "claim:edgar:revenue",
            }
        ]

    def test_an_answer_that_cannot_name_its_model_is_refused(self):
        with pytest.raises(UnusableCompletion, match="which model"):
            _completion({"summary": "Up 43%.", "direction": "up"}, model="")

    def test_a_naive_timestamp_is_refused(self):
        with pytest.raises(UnusableCompletion, match="naive"):
            _completion(
                {"summary": "Up 43%.", "direction": "up"},
                created_at=datetime(2026, 8, 7, 12, 0),  # noqa: DTZ001 -- the point
            )


class TestCost:
    def test_a_float_cost_is_refused(self):
        with pytest.raises(TypeError, match="Decimal"):
            TokenUsage(prompt_tokens=1, completion_tokens=1, cost_usd=0.0001)

    def test_a_negative_cost_is_refused(self):
        with pytest.raises(ValueError, match="negative"):
            TokenUsage(
                prompt_tokens=1, completion_tokens=1, cost_usd=Decimal("-0.01")
            )

    def test_a_negative_token_count_is_refused(self):
        with pytest.raises(ValueError, match="negative"):
            TokenUsage(prompt_tokens=-1, completion_tokens=0)

    def test_an_unreported_cost_stays_unknown_rather_than_becoming_zero(self):
        completion = _completion(
            {"summary": "Up 43%.", "direction": "up"},
            usage=TokenUsage(prompt_tokens=10, completion_tokens=5),
        )
        assert completion.usage.cost_usd is None
        assert completion.provenance()["cost_usd"] is None

    def test_a_reported_cost_survives_as_an_exact_decimal(self):
        completion = _completion(
            {"summary": "Up 43%.", "direction": "up"},
            usage=TokenUsage(
                prompt_tokens=10, completion_tokens=5, cost_usd=Decimal("0.0001")
            ),
        )
        assert completion.provenance()["cost_usd"] == "0.0001"
        assert completion.usage.total_tokens == 15


class TestRequestValidation:
    def test_a_measurement_must_carry_a_decimal(self):
        with pytest.raises(InvalidRequest, match="Decimal"):
            Measurement(name="x", value=0.1, unit="percent", source="claim:x")

    def test_a_measurement_must_name_its_source(self):
        with pytest.raises(InvalidRequest, match="source"):
            Measurement(name="x", value=Decimal(1), unit="percent", source="  ")

    def test_a_single_option_choice_is_not_a_selection(self):
        with pytest.raises(InvalidRequest, match="not a selection"):
            ChoiceField(name="direction", options=("up",))

    def test_duplicate_measurement_names_are_refused(self):
        with pytest.raises(InvalidRequest, match="duplicate measurement"):
            _request(measurements=(GROWTH, GROWTH))

    def test_a_request_with_no_instructions_is_refused(self):
        with pytest.raises(InvalidRequest, match="no instructions"):
            _request(instructions="   ")

    def test_the_prompt_states_the_measurements_and_the_fields(self):
        rendered = _request().render()
        assert "revenue_growth = 43 percent (source: claim:edgar:revenue)" in rendered
        assert "summary: free text, at most 200 characters" in rendered
        assert "direction: choose exactly one of: up | down | flat" in rendered


class TestFakeLanguageModel:
    def test_it_satisfies_the_protocol(self):
        assert isinstance(FakeLanguageModel(), LanguageModel)

    async def test_it_records_the_prompt_it_was_given(self):
        model = FakeLanguageModel(
            [ScriptedResponse({"summary": "Up 43%.", "direction": "up"})]
        )
        request = _request()
        await model.complete(request)
        assert model.requests == [request]
        assert model.prompts == [request.render()]

    async def test_a_refused_call_is_still_recorded(self):
        model = FakeLanguageModel(raises=Unavailable("provider down"))
        with pytest.raises(Unavailable, match="provider down"):
            await model.complete(_request())
        assert model.call_count == 1

    async def test_it_reads_no_clock(self):
        script = [ScriptedResponse({"summary": "Up 43%.", "direction": "up"})]
        first = await FakeLanguageModel(script).complete(_request())
        second = await FakeLanguageModel(script).complete(_request())
        assert first.provenance() == second.provenance()
        assert first.created_at.tzinfo is not None

    async def test_it_cannot_script_past_the_schema(self):
        model = FakeLanguageModel(
            [ScriptedResponse({"summary": "Up 61%.", "direction": "up"})]
        )
        with pytest.raises(UnusableCompletion, match="no measurement"):
            await model.complete(_request())

    async def test_an_exhausted_script_is_a_test_defect_not_an_outage(self):
        model = FakeLanguageModel()
        with pytest.raises(RuntimeError, match="script exhausted"):
            await model.complete(_request())

    async def test_scripted_answers_are_returned_in_order(self):
        model = FakeLanguageModel(
            [
                ScriptedResponse({"summary": "Up 43%.", "direction": "up"}),
                ScriptedResponse({"summary": "Flat.", "direction": "flat"}),
            ]
        )
        first = await model.complete(_request())
        second = await model.complete(_request())
        assert first.fields["direction"] == "up"
        assert second.fields["direction"] == "flat"

    def test_a_non_exception_raises_argument_is_refused(self):
        with pytest.raises(TypeError, match="exception instance"):
            FakeLanguageModel(raises=Unavailable)
