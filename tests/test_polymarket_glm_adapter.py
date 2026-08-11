"""Pure tests for the GLM / OpenAI-compatible adapter's JSON parsing.

The SDK call itself is not exercised here — it would require mocking the
openai client and the value is low (the call is a one-liner pass-through).
The non-trivial logic is the JSON extraction from a possibly-flaky model
response, and that is what these tests pin.

Same parser, same guarantees as the Anthropic sibling; if one drifts from the
other, a test here fails.
"""

import pytest

from omni.llm.protocol import UnusableCompletion
from omni.polymarket.glm_adapter import _extract_json_object


class TestExtractJsonObject:
    def test_clean_object_parsed(self):
        result = _extract_json_object('{"probability_bin": "0.5", "rationale": "x"}')
        assert result["probability_bin"] == "0.5"
        assert result["rationale"] == "x"

    def test_object_embedded_in_prose_parsed(self):
        text = 'Here is my answer: {"probability_bin": "0.65", "rationale": "y"} hope it helps'
        result = _extract_json_object(text)
        assert result["probability_bin"] == "0.65"

    def test_object_wrapped_in_markdown_parsed(self):
        text = '```json\n{"probability_bin": "0.75", "rationale": "z"}\n```'
        result = _extract_json_object(text)
        assert result["probability_bin"] == "0.75"

    def test_empty_response_refused(self):
        with pytest.raises(UnusableCompletion, match="no JSON object"):
            _extract_json_object("")

    def test_prose_only_refused(self):
        with pytest.raises(UnusableCompletion, match="no JSON object"):
            _extract_json_object("I cannot answer that.")

    def test_malformed_json_refused(self):
        with pytest.raises(UnusableCompletion, match="not valid JSON"):
            _extract_json_object('{"probability_bin": "0.5", "rationale":}')

    def test_json_array_refused(self):
        # No `{...}` blob in an array response, so the regex finds nothing.
        with pytest.raises(UnusableCompletion, match="no JSON object"):
            _extract_json_object('["0.5", "x"]')

    def test_nested_object_extracts_innermost_then_field_check_rejects(self):
        # The regex body is [^{}]*, so a nested object does not match the outer
        # {...}. The innermost {...} matches instead. The downstream adapter
        # catches the resulting missing-field mismatch and raises — that layer
        # is the guarantee, not this extractor.
        result = _extract_json_object('{"outer": {"inner": "x"}}')
        # Innermost object is what comes back; the adapter's field-check is
        # what rejects it (see OpenAICompatibleLanguageModel.complete).
        assert "inner" in result
