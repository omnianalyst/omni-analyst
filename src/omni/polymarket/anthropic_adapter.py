"""Opt-in Anthropic provider adapter for the existing `LanguageModel` protocol.

This module is **not** imported by anything else in `src/omni/`. It exists so
Stage A can run against a real model without a separate work order, and so a
caller has a worked example of how to wire a provider into the protocol.

Lazy import: `anthropic` is imported inside `__init__`. Tests that use
`FakeLanguageModel` never touch this module and never need the SDK installed.
To run calibration against a real model, install the SDK once:

    uv pip install anthropic

and set `ANTHROPIC_API_KEY` in the environment.

**Why this lives in `polymarket/` and not `llm/`.** `llm/protocol.py` is
explicit that "a concrete provider adapter is deferred until its wire
protocol is known". Promoting this to `llm/anthropic.py` would reverse that
decision and is out of scope for this work order; the adapter therefore lives
where it is used, and a future work order can lift it if and when a general
adapter tier is established.
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Any

from omni.llm.protocol import (
    Completion,
    CompletionRequest,
    ResponseSchema,
    TextField,
    TokenUsage,
    UnusableCompletion,
)

DEFAULT_MODEL = "claude-3-5-sonnet-latest"

_JSON_OBJECT = re.compile(r"\{[^{}]*\}", re.DOTALL)


def _system_prompt(schema: ResponseSchema) -> str:
    """Instruct the model to return one JSON object whose keys match the
    schema's field names. The protocol's own validation catches any field the
    model fabricates; this prompt is only how we ask for parseable text.
    """
    lines = [
        "Respond with exactly one JSON object on a single line. Nothing else.",
        "No markdown, no prose, no preamble, no trailing text.",
        "The object keys are:",
    ]
    for field in schema.fields:
        if isinstance(field, TextField):
            lines.append(f'  "{field.name}": a string of at most {field.max_chars} characters')
        else:
            lines.append(
                f'  "{field.name}": one of {", ".join(field.options)}, as a string'
            )
    return "\n".join(lines)


def _extract_json_object(text: str) -> Mapping[str, Any]:
    """Find the first `{...}` blob and parse it. Refuse on any failure.

    The model occasionally wraps JSON in markdown or adds trailing
    commentary despite the instruction; a tolerant-but-strict extractor
    handles that without licensing multi-document or nested-object responses
    (the `[^{}]*` body refuses nested braces, which a schema field never
    needs).
    """
    match = _JSON_OBJECT.search(text)
    if match is None:
        raise UnusableCompletion(
            f"no JSON object in response: {text!r}"
        )
    try:
        parsed = json.loads(match.group(0))
    except json.JSONDecodeError as exc:
        raise UnusableCompletion(
            f"response is not valid JSON: {match.group(0)!r}"
        ) from exc
    if not isinstance(parsed, Mapping):
        raise UnusableCompletion(
            f"parsed JSON is {type(parsed).__name__}, not an object"
        )
    return parsed


class AnthropicLanguageModel:
    """A `LanguageModel` backed by the Anthropic messages API.

    The constructor lazy-imports `anthropic` and raises a plain `ImportError`
    if it is missing — the same exception Python raises for any missing
    dependency, with the same expectation: install it, or use a different
    adapter. Wrapping it in a custom error would make the failure mode
    *less* debuggable, not more.
    """

    def __init__(
        self,
        *,
        api_key: str | None = None,
        model: str = DEFAULT_MODEL,
        name: str = "anthropic",
        timeout: float = 30.0,
    ) -> None:
        try:
            import anthropic  # type: ignore[import-not-found]
        except ImportError as exc:
            raise ImportError(
                "anthropic is not installed; run `uv pip install anthropic` "
                "to use AnthropicLanguageModel"
            ) from exc

        if not model.strip():
            raise ValueError("model must be non-empty")
        if timeout <= 0:
            raise ValueError(f"timeout must be positive, got {timeout}")
        self.name = name
        self._model = model
        self._client = anthropic.AsyncAnthropic(api_key=api_key, timeout=timeout)

    async def complete(self, request: CompletionRequest) -> Completion:
        system = _system_prompt(request.schema)
        user = request.render()
        try:
            response = await self._client.messages.create(
                model=self._model,
                max_tokens=request.max_output_tokens,
                system=system,
                messages=[{"role": "user", "content": user}],
            )
        except Exception as exc:
            from omni.ingest.protocol import Unavailable
            raise Unavailable(f"anthropic messages.create failed: {exc}") from exc

        text_parts = [
            block.text
            for block in response.content
            if getattr(block, "type", None) == "text" and getattr(block, "text", "")
        ]
        if not text_parts:
            raise UnusableCompletion("anthropic returned no text blocks")
        parsed = _extract_json_object("".join(text_parts))

        fields: dict[str, str] = {}
        for spec in request.schema.fields:
            value = parsed.get(spec.name)
            if value is None:
                raise UnusableCompletion(
                    f"response is missing field {spec.name!r}"
                )
            if not isinstance(value, str):
                value = str(value)
            fields[spec.name] = value

        usage = getattr(response, "usage", None)
        prompt_tokens = int(getattr(usage, "input_tokens", 0) or 0)
        completion_tokens = int(getattr(usage, "output_tokens", 0) or 0)

        return Completion(
            request=request,
            model=self._model,
            created_at=datetime.now(UTC),
            fields=fields,
            usage=TokenUsage(
                prompt_tokens=prompt_tokens,
                completion_tokens=completion_tokens,
                cost_usd=None,
            ),
        )
