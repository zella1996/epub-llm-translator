"""Versioned, secret-free provider behavior presets."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping


@dataclass(frozen=True)
class ProviderProfile:
    name: str
    version: int
    default_thinking: bool | None
    thinking_parameter: str = "enable_thinking"
    thinking_clear_thinking: bool | None = None
    default_do_sample: bool | None = None
    default_max_output_tokens: int | None = None
    response_format_type: str = "json_object"
    request_extras: Mapping[str, Any] | None = None

    @property
    def fingerprint(self) -> str:
        return f"{self.name}-v{self.version}"


PROVIDER_PROFILES = {
    "generic": ProviderProfile("generic", 1, None),
    "bailian": ProviderProfile("bailian", 1, None),
    "openrouter": ProviderProfile(
        "openrouter",
        4,
        None,
        request_extras={
            "provider": {
                "data_collection": "deny",
                "require_parameters": True,
            },
        },
    ),
    "qwen-bailian": ProviderProfile("qwen-bailian", 1, False),
    "zhipu": ProviderProfile(
        "zhipu",
        2,
        False,
        thinking_parameter="thinking",
        default_do_sample=False,
        default_max_output_tokens=2048,
    ),
    "zhipu-glm53": ProviderProfile(
        "zhipu-glm53",
        1,
        True,
        thinking_parameter="thinking",
        thinking_clear_thinking=False,
    ),
    "lmstudio": ProviderProfile(
        "lmstudio",
        2,
        None,
        default_max_output_tokens=2048,
        response_format_type="json_schema",
    ),
}


def provider_profile(name: str) -> ProviderProfile:
    return PROVIDER_PROFILES[name]
