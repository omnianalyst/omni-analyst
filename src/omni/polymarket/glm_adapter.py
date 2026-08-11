"""GLM provider adapter via Zhipu's public API (bigmodel.cn).

Matches the existing Omni Analyst convention so the same `GLM_API_KEY`,
`GLM_BASE_URL`, `GLM_MODEL` env vars work across both the v1 deployment and
this Stage A harness.

Lazy import: `openai` is imported inside `__init__`. Install once:

    uv pip install openai

and set `GLM_API_KEY` in the environment.

`thinking="max"` is on by default — the user explicitly requested max
thinking. Zhipu's documented parameter is `{"thinking": {"type": "max"}}`
in the request body; the openai SDK passes arbitrary request-body keys
through `extra_body`. If Zhipu's format changes, override `thinking_type`
or pass `thinking_type=None` to disable.

**Why this lives in `polymarket/` and not `llm/`.** Same reason as the
Anthropic sibling: `llm/protocol.py` defers concrete adapters. Promoting
either would reverse that decision; both therefore live where they are used.
"""

from __future__ import annotations

import json
import os
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

DEFAULT_BASE_URL = "https://open.bigmodel.cn/api/paas/v4"
DEFAULT_MODEL = "glm-5.2"

_JSON_OBJECT = re.compile(r"\{[^{}]*\}", re.DOTALL)


def _system_prompt(schema: ResponseSchema) -> str:
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
    """Same parser as the Anthropic sibling. Duplicated, not shared: coupling
    the two adapters via a helper creates a coordination point for two files
    that are otherwise independent, and the body is ten lines.
    """
    match = _JSON_OBJECT.search(text)
    if match is None:
        raise UnusableCompletion(f"no JSON object in response: {text!r}")
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


class OpenAICompatibleLanguageModel:
    """A `LanguageModel` backed by Zhipu's GLM public API.

    Defaults match the existing Omni Analyst convention
    (`GLM_API_KEY` / `open.bigmodel.cn` / `glm-5.2`). `thinking_type="max"`
    is on by default per the user's explicit request; pass `thinking_type=None`
    to disable, or `"auto"` for the model's default behaviour.

    `api_key` falls back to `$GLM_API_KEY` then `$OPENAI_API_KEY` so the same
    constructor works for either gateway with no surprises.
    """

    def __init__(
        self,
        *,
        api_key: str | None = None,
        base_url: str = DEFAULT_BASE_URL,
        model: str = DEFAULT_MODEL,
        name: str = "glm",
        timeout: float = 60.0,
        thinking_type: str | None = "max",
    ) -> None:
        try:
            from openai import AsyncOpenAI  # type: ignore[import-not-found]
        except ImportError as exc:
            raise ImportError(
                "openai is not installed; run `uv pip install openai` to use "
                "OpenAICompatibleLanguageModel"
            ) from exc

        if not model.strip():
            raise ValueError("model must be non-empty")
        if not base_url.strip():
            raise ValueError("base_url must be non-empty")
        if timeout <= 0:
            raise ValueError(f"timeout must be positive, got {timeout}")

        resolved_key = api_key or os.environ.get("GLM_API_KEY") or os.environ.get("OPENAI_API_KEY")
        if not resolved_key:
            raise ValueError(
                "no api_key supplied and neither GLM_API_KEY nor OPENAI_API_KEY "
                "is set; a keyless client would fail at the first request"
            )

        self.name = name
        self._model = model
        self._thinking_type = thinking_type
        self._client = AsyncOpenAI(api_key=resolved_key, base_url=base_url, timeout=timeout)

    async def complete(self, request: CompletionRequest) -> Completion:
        system = _system_prompt(request.schema)
        user = request.render()
        extra_body: dict[str, Any] | None = None
        if self._thinking_type is not None:
            extra_body = {"thinking": {"type": self._thinking_type}}
        try:
            response = await self._client.chat.completions.create(
                model=self._model,
                max_tokens=request.max_output_tokens,
                messages=[
                    {"role": "system", "content": system},
                    {"role": "user", "content": user},
                ],
                extra_body=extra_body,
            )
        except Exception as exc:
            from omni.ingest.protocol import Unavailable
            raise Unavailable(f"openai-compatible chat.completions failed: {exc}") from exc

        choice = response.choices[0] if response.choices else None
        content = getattr(getattr(choice, "message", None), "content", "") if choice else ""
        if not content:
            raise UnusableCompletion("gateway returned no content")
        parsed = _extract_json_object(content)

        fields: dict[str, str] = {}
        for spec in request.schema.fields:
            value = parsed.get(spec.name)
            if value is None:
                raise UnusableCompletion(f"response is missing field {spec.name!r}")
            if not isinstance(value, str):
                value = str(value)
            fields[spec.name] = value

        usage = getattr(response, "usage", None)
        prompt_tokens = int(getattr(usage, "prompt_tokens", 0) or 0)
        completion_tokens = int(getattr(usage, "completion_tokens", 0) or 0)

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
