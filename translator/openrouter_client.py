"""OpenRouter request encoder backed by the shared JSON client runtime."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping
from urllib import error

from translator.llm_api import (
    JSONTransport,
    OpenAICompatibleClient,
    OpenAIConfig,
    RateLimiter,
)
from translator.openrouter_model_profiles import (
    OpenRouterModelProfile,
    openrouter_model_profile,
)


@dataclass(frozen=True)
class OpenRouterConfig:
    base_url: str
    model: str
    api_key: str = ""
    timeout_seconds: float = 120.0
    max_retries: int = 2
    temperature: float = 0.1
    max_output_tokens: int | None = None
    provider_preferences: Mapping[str, Any] | None = None


class OpenRouterClient(OpenAICompatibleClient):
    """OpenRouter payload encoder using the shared, thread-safe runtime."""

    def __init__(
        self,
        config: OpenRouterConfig,
        *,
        rate_limiter: RateLimiter | None = None,
        transport: JSONTransport | None = None,
        model_profile: OpenRouterModelProfile | None = None,
    ) -> None:
        self.model_profile = model_profile or openrouter_model_profile(config.model)
        if self.model_profile.model != config.model:
            raise ValueError("OpenRouter model profile 与请求模型不匹配")
        self.provider_preferences = dict(
            config.provider_preferences
            or {"data_collection": "deny", "require_parameters": True}
        )
        super().__init__(
            OpenAIConfig(
                base_url=config.base_url,
                model=config.model,
                api_key=config.api_key,
                timeout_seconds=config.timeout_seconds,
                max_retries=config.max_retries,
                temperature=config.temperature,
                max_output_tokens=config.max_output_tokens,
                response_format_type={
                    "json_schema": "json_schema",
                    "json_object": "json_object",
                    "prompt_only": "text",
                }[self.model_profile.structured_output],
                request_extras={"provider": self.provider_preferences},
            ),
            rate_limiter=rate_limiter,
            transport=transport,
        )

    def trace_request_payload(self, system: str, user: str) -> dict[str, Any]:
        return self.model_profile.build_payload(
            system=system,
            user=user,
            temperature=self.config.temperature,
            max_output_tokens=self.config.max_output_tokens,
            provider_preferences=self.provider_preferences,
        )

    @property
    def request_policy_fingerprint(self) -> str:
        return self.model_profile.fingerprint

    @staticmethod
    def _is_retryable(exc: Exception) -> bool:
        if isinstance(exc, error.HTTPError):
            return exc.code == 429 or exc.code >= 500
        return isinstance(exc, (error.URLError, TimeoutError))
