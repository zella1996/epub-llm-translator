"""Explicit, versioned request policies for models served through OpenRouter.

OpenRouter exposes one HTTP API, but its model/provider endpoints do not accept
one universal set of generation parameters.  This module is the only place
where model-specific wire behavior is declared.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from typing import Any, Literal, Mapping

from translator.llm_api import LLMError


StructuredOutputMode = Literal["json_schema", "json_object", "prompt_only"]


@dataclass(frozen=True)
class OpenRouterModelProfile:
    """Small allow-list describing parameters safe for one OpenRouter model."""

    name: str
    version: int
    model: str
    structured_output: StructuredOutputMode
    send_temperature: bool
    send_max_tokens: bool
    reasoning: Mapping[str, Any] | None = None
    static_parameters: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        protected = {
            "model",
            "messages",
            "provider",
            "response_format",
            "temperature",
            "max_tokens",
            "reasoning",
        }
        conflicts = sorted(protected.intersection(self.static_parameters))
        if conflicts:
            raise ValueError(
                "OpenRouter model profile 的 static_parameters 不能覆盖受控字段: "
                + ", ".join(conflicts)
            )

    @property
    def fingerprint(self) -> str:
        payload = json.dumps(
            {
                "name": self.name,
                "version": self.version,
                "model": self.model,
                "structured_output": self.structured_output,
                "send_temperature": self.send_temperature,
                "send_max_tokens": self.send_max_tokens,
                "reasoning": self.reasoning,
                "static_parameters": self.static_parameters,
            },
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        digest = hashlib.sha256(payload.encode("utf-8")).hexdigest()[:12]
        return f"{self.name}-v{self.version}-{digest}"

    def build_payload(
        self,
        *,
        system: str,
        user: str,
        temperature: float,
        max_output_tokens: int | None,
        provider_preferences: Mapping[str, Any],
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "provider": dict(provider_preferences),
        }
        if self.send_temperature:
            payload["temperature"] = temperature
        if self.send_max_tokens and max_output_tokens is not None:
            payload["max_tokens"] = max_output_tokens
        if self.structured_output == "json_schema":
            payload["response_format"] = {
                "type": "json_schema",
                "json_schema": {
                    "name": "epub_translator_response",
                    "strict": True,
                    "schema": {"type": "object", "additionalProperties": True},
                },
            }
        elif self.structured_output == "json_object":
            payload["response_format"] = {"type": "json_object"}
        if self.reasoning is not None:
            payload["reasoning"] = dict(self.reasoning)
        payload.update(self.static_parameters)
        return payload


OPENROUTER_MODEL_PROFILES = {
    "google/gemini-3.7-flash": OpenRouterModelProfile(
        "gemini37-flash",
        1,
        "google/gemini-3.7-flash",
        structured_output="json_schema",
        send_temperature=True,
        send_max_tokens=True,
        reasoning={"effort": "low", "exclude": True},
    ),
    "z-ai/glm-5.2": OpenRouterModelProfile(
        "glm52",
        1,
        "z-ai/glm-5.2",
        structured_output="json_schema",
        send_temperature=True,
        send_max_tokens=True,
        reasoning={"enabled": False, "exclude": True},
    ),
    "qwen/qwen3-235b-a22b-07-25": OpenRouterModelProfile(
        "qwen3-235b-a22b-07-25",
        1,
        "qwen/qwen3-235b-a22b-07-25",
        structured_output="json_schema",
        send_temperature=True,
        send_max_tokens=True,
        reasoning=None,
    ),
    "anthropic/claude-fable-5": OpenRouterModelProfile(
        "claude-fable-5",
        1,
        "anthropic/claude-fable-5",
        structured_output="json_object",
        send_temperature=False,
        send_max_tokens=False,
        reasoning={"effort": "low", "exclude": True},
    ),
}


def openrouter_model_profile(model: str) -> OpenRouterModelProfile:
    try:
        return OPENROUTER_MODEL_PROFILES[model]
    except KeyError as exc:
        raise LLMError(
            f"OpenRouter 模型 {model} 尚无请求策略；"
            "请先声明 structured output、temperature、max_tokens 和 reasoning 能力"
        ) from exc
