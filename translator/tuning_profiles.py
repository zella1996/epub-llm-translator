"""Named, versioned tuning bundles for particular model combinations."""

from __future__ import annotations

from dataclasses import dataclass
@dataclass(frozen=True)
class ModelTuning:
    name: str
    version: int
    language_profile: str
    provider_profile: str | None = None
    thinking: bool | None = None
    temperature: float | None = None
    top_p: float | None = None
    do_sample: bool | None = None
    max_output_tokens: int | None = None
    reasoning_effort: str | None = None
    fast_model: str | None = None
    quality_model: str | None = None
    required_syntax_analyzer: str | None = None
    required_stanza_package: str | None = None
    source_language: str = "en"

    @property
    def fingerprint(self) -> str:
        return f"{self.name}-v{self.version}"


TUNING_PROFILES = {
    "generic": ModelTuning("generic", 1, "en-zh-Hans-v7-kinship-reference-tokens"),
    "generic-ja": ModelTuning(
        "generic-ja", 3, "ja-zh-Hans-v2-japanese-reading-meaning", source_language="ja"
    ),
    "glm53flash-japanese-reading": ModelTuning(
        "glm53flash-japanese-reading", 2,
        "ja-zh-Hans-v2-japanese-reading-meaning",
        provider_profile="zhipu-glm53", thinking=True, temperature=1.0,
        top_p=0.95, max_output_tokens=16384, reasoning_effort="max",
        fast_model="glm-5.3-flash", quality_model="glm-5.3-flash",
        source_language="ja",
    ),
    "glm52-grounded": ModelTuning(
        "glm52-grounded",
        12,
        "en-zh-Hans-v16-compact-envelope",
        provider_profile="zhipu",
        thinking=False,
        temperature=0.1,
        do_sample=False,
        max_output_tokens=2048,
        fast_model="glm-5.2",
        quality_model="glm-5.2",
        required_syntax_analyzer="stanza",
        required_stanza_package="default_accurate",
    ),
    "glm53flash-grounded": ModelTuning(
        "glm53flash-grounded",
        1,
        "en-zh-Hans-v16-compact-envelope",
        provider_profile="zhipu-glm53",
        thinking=True,
        temperature=1.0,
        top_p=0.95,
        max_output_tokens=8192,
        reasoning_effort="max",
        fast_model="glm-5.3-flash",
        quality_model="glm-5.3-flash",
        required_syntax_analyzer="stanza",
        required_stanza_package="default_accurate",
    ),
    "glm53flash-structured-meaning": ModelTuning(
        "glm53flash-structured-meaning",
        1,
        "en-zh-Hans-v17-structured-meaning",
        provider_profile="zhipu-glm53",
        thinking=True,
        temperature=1.0,
        top_p=0.95,
        max_output_tokens=16384,
        reasoning_effort="max",
        fast_model="glm-5.3-flash",
        quality_model="glm-5.3-flash",
        required_syntax_analyzer="stanza",
        required_stanza_package="default_accurate",
    ),
    "glm53flash-structured-meaning-v2": ModelTuning(
        "glm53flash-structured-meaning-v2",
        1,
        "en-zh-Hans-v18-direct-meaning",
        provider_profile="zhipu-glm53",
        thinking=True,
        temperature=1.0,
        top_p=0.95,
        max_output_tokens=16384,
        reasoning_effort="max",
        fast_model="glm-5.3-flash",
        quality_model="glm-5.3-flash",
        required_syntax_analyzer="stanza",
        required_stanza_package="default_accurate",
    ),
    "glm53flash-literal-baseline": ModelTuning(
        "glm53flash-literal-baseline",
        1,
        "en-zh-Hans-v19-literal-meaning",
        provider_profile="zhipu-glm53",
        thinking=True,
        temperature=1.0,
        top_p=0.95,
        max_output_tokens=16384,
        reasoning_effort="max",
        fast_model="glm-5.3-flash",
        quality_model="glm-5.3-flash",
        required_syntax_analyzer="stanza",
        required_stanza_package="default_accurate",
    ),
    "glm52-bailian-grounded": ModelTuning(
        "glm52-bailian-grounded",
        1,
        "en-zh-Hans-v16-compact-envelope",
        provider_profile="bailian",
        thinking=False,
        temperature=0.1,
        max_output_tokens=2048,
        fast_model="glm-5.2",
        quality_model="glm-5.2",
        required_syntax_analyzer="stanza",
        required_stanza_package="default_accurate",
    ),
    "glm52-structured-meaning": ModelTuning(
        "glm52-structured-meaning",
        1,
        "en-zh-Hans-v17-structured-meaning",
        provider_profile="zhipu",
        thinking=False,
        temperature=0.1,
        do_sample=False,
        max_output_tokens=2048,
        fast_model="glm-5.2",
        quality_model="glm-5.2",
        required_syntax_analyzer="stanza",
        required_stanza_package="default_accurate",
    ),
    "glm52-openrouter-grounded": ModelTuning(
        "glm52-openrouter-grounded",
        3,
        "en-zh-Hans-v16-compact-envelope",
        provider_profile="openrouter",
        temperature=0.1,
        max_output_tokens=2048,
        fast_model="z-ai/glm-5.2",
        quality_model="z-ai/glm-5.2",
        required_syntax_analyzer="stanza",
        required_stanza_package="default_accurate",
    ),
    "gemini37-openrouter-grounded": ModelTuning(
        "gemini37-openrouter-grounded",
        2,
        "en-zh-Hans-v16-compact-envelope",
        provider_profile="openrouter",
        temperature=0.1,
        max_output_tokens=4096,
        fast_model="google/gemini-3.7-flash",
        quality_model="google/gemini-3.7-flash",
        required_syntax_analyzer="stanza",
        required_stanza_package="default_accurate",
    ),
    "qwen3-235b-openrouter-grounded": ModelTuning(
        "qwen3-235b-openrouter-grounded",
        1,
        "en-zh-Hans-v16-compact-envelope",
        provider_profile="openrouter",
        temperature=0.1,
        max_output_tokens=4096,
        fast_model="qwen/qwen3-235b-a22b-07-25",
        quality_model="qwen/qwen3-235b-a22b-07-25",
        required_syntax_analyzer="stanza",
        required_stanza_package="default_accurate",
    ),
    "qwen37-meaning-only": ModelTuning(
        "qwen37-meaning-only",
        4,
        "en-zh-Hans-v11-personal-threshold",
        provider_profile="qwen-bailian",
        thinking=False,
        temperature=0.1,
        do_sample=False,
        max_output_tokens=2048,
        fast_model="qwen3.7-flash",
        quality_model="qwen3.7-plus",
        required_syntax_analyzer="stanza",
        required_stanza_package="default_accurate",
    ),
    "deepseek-v4-meaning-only": ModelTuning(
        "deepseek-v4-meaning-only",
        4,
        "en-zh-Hans-v11-personal-threshold",
        provider_profile="qwen-bailian",
        thinking=False,
        temperature=0.1,
        do_sample=False,
        max_output_tokens=2048,
        fast_model="deepseek-v4-flash-0731",
        quality_model="deepseek-v4-pro",
        required_syntax_analyzer="stanza",
        required_stanza_package="default_accurate",
    ),
}


def tuning_profile(name: str) -> ModelTuning:
    return TUNING_PROFILES[name]
