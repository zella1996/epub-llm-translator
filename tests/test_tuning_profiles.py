from dataclasses import replace

import pytest

import translator.cli as cli
from translator.cli import (
    QUALITY_PAYLOAD_VERSION,
    _clients,
    _effective_stanza_package,
    _syntax_profile_key,
    build_parser,
)
from translator.epub_processor import EpubError
from translator.languages import language_profile
from translator.openrouter_client import OpenRouterClient
from translator.openrouter_model_profiles import (
    OPENROUTER_MODEL_PROFILES,
    openrouter_model_profile,
)
from translator.provider_profiles import PROVIDER_PROFILES
from translator.tuning_profiles import tuning_profile


def test_glm52_grounded_tuning_separates_local_evidence_prompt_and_request_defaults():
    tuning = tuning_profile("glm52-grounded")
    language = language_profile(tuning.language_profile)

    assert tuning.fast_model == "glm-5.2"
    assert tuning.quality_model == "glm-5.2"
    assert tuning.provider_profile == "zhipu"
    assert tuning.do_sample is False
    assert tuning.required_syntax_analyzer == "stanza"
    assert tuning.required_stanza_package == "default_accurate"
    assert tuning.version == 12
    assert language.key == "en-zh-Hans-v16-compact-envelope"
    assert language.min_effortful_card_words == 40
    assert "文学色彩" in language.fast_system
    assert "低频词" in language.fast_system
    assert "人物关系、亲属关系和指代断言" in language.quality_system
    assert "心理动机" in language.quality_system
    assert "不要输出任何 meaning 之外的学习卡字段" in language.quality_system


def test_glm53flash_grounded_uses_required_thinking_and_official_sampling_defaults():
    tuning = tuning_profile("glm53flash-grounded")
    language = language_profile(tuning.language_profile)
    parser = build_parser()
    args = parser.parse_args(
        [
            "translate",
            "source.epub",
            "output.epub",
            "--base-url",
            "https://open.bigmodel.cn/api/paas/v4",
            "--fast-model",
            "glm-5.3-flash",
            "--tuning-profile",
            "glm53flash-grounded",
            "--stream-response",
        ]
    )

    fast, quality, profile_key, _ = _clients(args)
    payload = fast.trace_request_payload("return JSON", "return JSON")

    assert tuning.provider_profile == "zhipu-glm53"
    assert tuning.fast_model == "glm-5.3-flash"
    assert tuning.quality_model == "glm-5.3-flash"
    assert tuning.thinking is True
    assert tuning.temperature == 1.0
    assert tuning.top_p == 0.95
    assert tuning.do_sample is None
    assert tuning.reasoning_effort == "max"
    assert tuning.max_output_tokens == 8192
    assert language.key == "en-zh-Hans-v16-compact-envelope"
    assert payload["model"] == "glm-5.3-flash"
    assert payload["temperature"] == 1.0
    assert payload["top_p"] == 0.95
    assert payload["thinking"] == {"type": "enabled", "clear_thinking": False}
    assert payload["reasoning_effort"] == "max"
    assert payload["max_tokens"] == 8192
    assert payload["stream"] is True
    assert "do_sample" not in payload
    assert quality.config.model == "glm-5.3-flash"
    assert fast.config.stream_response is True
    assert PROVIDER_PROFILES["zhipu-glm53"].fingerprint == "zhipu-glm53-v1"
    assert profile_key


def test_glm53flash_grounded_rejects_disabling_required_thinking():
    parser = build_parser()
    args = parser.parse_args(
        [
            "translate",
            "source.epub",
            "output.epub",
            "--base-url",
            "https://open.bigmodel.cn/api/paas/v4",
            "--fast-model",
            "glm-5.3-flash",
            "--tuning-profile",
            "glm53flash-grounded",
            "--thinking",
            "off",
        ]
    )

    with pytest.raises(EpubError, match="GLM-5.3-Flash 不支持关闭思考"):
        _clients(args)


@pytest.mark.parametrize("name,expected_language", [
    ("glm53flash-structured-meaning", "en-zh-Hans-v17-structured-meaning"),
    ("glm53flash-structured-meaning-v2", "en-zh-Hans-v18-direct-meaning"),
    ("glm53flash-literal-baseline", "en-zh-Hans-v19-literal-meaning"),
])
def test_glm53flash_structured_meaning_uses_structured_prompt_and_extra_headroom(name, expected_language):
    tuning = tuning_profile(name)
    language = language_profile(tuning.language_profile)
    parser = build_parser()
    args = parser.parse_args(
        [
            "translate",
            "source.epub",
            "output.epub",
            "--base-url",
            "https://open.bigmodel.cn/api/paas/v4",
            "--fast-model",
            "glm-5.3-flash",
            "--tuning-profile",
            name,
        ]
    )

    fast, quality, _, _ = _clients(args)
    payload = fast.trace_request_payload("return JSON", "return JSON")

    assert tuning.language_profile == expected_language
    assert language.key == expected_language
    assert tuning.max_output_tokens == 16384
    assert payload["max_tokens"] == 16384
    assert payload["thinking"] == {"type": "enabled", "clear_thinking": False}
    assert payload["reasoning_effort"] == "max"
    assert quality.config.model == "glm-5.3-flash"


@pytest.mark.parametrize("old_name,new_name", [
    ("glm53flash-structured-meaning", "glm53flash-structured-meaning-v2"),
    ("glm53flash-structured-meaning-v2", "glm53flash-literal-baseline"),
])
def test_direct_meaning_preserves_glm_settings_and_isolates_cache_keys(old_name, new_name):
    old = tuning_profile(old_name)
    new = tuning_profile(new_name)
    assert replace(new, name=old.name, version=old.version, language_profile=old.language_profile) == old
    parser = build_parser()
    clients = [
        _clients(parser.parse_args([
            "translate", "source.epub", "output.epub",
            "--base-url", "https://open.bigmodel.cn/api/paas/v4",
            "--fast-model", "glm-5.3-flash",
            "--tuning-profile", name,
        ]))
        for name in (old.name, new.name)
    ]
    assert clients[0][2] != clients[1][2]
    for stage in (0, 1):
        assert clients[0][stage].trace_request_payload("system", "user") == clients[1][stage].trace_request_payload("system", "user")


def test_glm52_bailian_tuning_reuses_grounded_prompt_with_bailian_wire_defaults():
    tuning = tuning_profile("glm52-bailian-grounded")
    language = language_profile(tuning.language_profile)

    assert tuning.fast_model == "glm-5.2"
    assert tuning.quality_model == "glm-5.2"
    assert tuning.provider_profile == "bailian"
    assert tuning.thinking is False
    assert tuning.do_sample is None
    assert tuning.max_output_tokens == 2048
    assert tuning.required_syntax_analyzer == "stanza"
    assert tuning.required_stanza_package == "default_accurate"
    assert language.key == "en-zh-Hans-v16-compact-envelope"


def test_glm52_structured_meaning_tuning_uses_bigmodel_wire_defaults_without_reader_guides():
    tuning = tuning_profile("glm52-structured-meaning")
    language = language_profile(tuning.language_profile)
    parser = build_parser()
    args = parser.parse_args(
        [
            "translate",
            "source.epub",
            "output.epub",
            "--base-url",
            "https://api.z.ai/api/paas/v4",
            "--fast-model",
            "glm-5.2",
            "--tuning-profile",
            "glm52-structured-meaning",
        ]
    )

    fast, quality, profile_key, _ = _clients(args)
    payload = fast.trace_request_payload("return JSON", "return JSON")

    assert tuning.provider_profile == "zhipu"
    assert tuning.fast_model == "glm-5.2"
    assert tuning.quality_model == "glm-5.2"
    assert tuning.thinking is False
    assert tuning.do_sample is False
    assert tuning.max_output_tokens == 2048
    assert tuning.required_syntax_analyzer == "stanza"
    assert tuning.required_stanza_package == "default_accurate"
    assert language.key == "en-zh-Hans-v17-structured-meaning"
    assert "结构化句意" in language.quality_system
    assert "按英文理解路径组织中文" in language.quality_system
    assert "不要输出任何 meaning 之外的学习卡字段" in language.quality_system
    assert fast.config.thinking_parameter == "thinking"
    assert fast.config.enable_thinking is False
    assert fast.config.do_sample is False
    assert payload["thinking"] == {"type": "disabled"}
    assert payload["do_sample"] is False
    assert payload["max_tokens"] == 2048
    assert quality.config.model == "glm-5.2"
    assert profile_key


def test_glm52_bailian_client_uses_enable_thinking_and_omits_zhipu_only_fields():
    parser = build_parser()
    args = parser.parse_args(
        [
            "translate",
            "source.epub",
            "output.epub",
            "--base-url",
            "https://workspace.cn-beijing.maas.aliyuncs.com/compatible-mode/v1",
            "--fast-model",
            "glm-5.2",
            "--tuning-profile",
            "glm52-bailian-grounded",
        ]
    )

    fast, quality, profile_key, _ = _clients(args)
    payload = fast.trace_request_payload("return JSON", "return JSON")

    assert fast.endpoint.endswith("/compatible-mode/v1/chat/completions")
    assert fast.config.thinking_parameter == "enable_thinking"
    assert fast.config.enable_thinking is False
    assert fast.config.do_sample is None
    assert payload["model"] == "glm-5.2"
    assert payload["enable_thinking"] is False
    assert payload["response_format"] == {"type": "json_object"}
    assert payload["max_tokens"] == 2048
    assert "thinking" not in payload
    assert "do_sample" not in payload
    assert "reasoning_effort" not in payload
    assert quality.config.model == "glm-5.2"
    assert profile_key


def test_glm52_bailian_cache_identity_differs_from_zhipu_direct():
    parser = build_parser()
    common = [
        "translate",
        "source.epub",
        "output.epub",
        "--base-url",
        "https://example.test/v1",
        "--fast-model",
        "glm-5.2",
    ]
    bailian = parser.parse_args(common + ["--tuning-profile", "glm52-bailian-grounded"])
    zhipu = parser.parse_args(common + ["--tuning-profile", "glm52-grounded"])

    assert _clients(bailian)[2] != _clients(zhipu)[2]


def test_glm52_bailian_rejects_zhipu_reasoning_effort_before_request():
    parser = build_parser()
    args = parser.parse_args(
        [
            "translate",
            "source.epub",
            "output.epub",
            "--base-url",
            "https://example.test/v1",
            "--fast-model",
            "glm-5.2",
            "--tuning-profile",
            "glm52-bailian-grounded",
            "--thinking",
            "on",
            "--reasoning-effort",
            "high",
        ]
    )

    with pytest.raises(EpubError, match="百炼.*reasoning-effort"):
        _clients(args)


def test_glm52_openrouter_tuning_requires_json_capable_private_routing():
    tuning = tuning_profile("glm52-openrouter-grounded")
    parser = build_parser()
    args = parser.parse_args(
        [
            "translate",
            "source.epub",
            "output.epub",
            "--base-url",
            "https://openrouter.ai/api/v1",
            "--fast-model",
            "z-ai/glm-5.2",
            "--tuning-profile",
            "glm52-openrouter-grounded",
        ]
    )

    fast, quality, profile_key, _ = _clients(args)
    payload = fast.trace_request_payload("return JSON", "return JSON")

    assert tuning.provider_profile == "openrouter"
    assert tuning.fast_model == "z-ai/glm-5.2"
    assert tuning.quality_model == "z-ai/glm-5.2"
    assert tuning.thinking is None
    assert tuning.do_sample is None
    assert tuning.max_output_tokens == 2048
    assert openrouter_model_profile(tuning.fast_model).reasoning == {
        "enabled": False,
        "exclude": True,
    }
    assert fast.endpoint == "https://openrouter.ai/api/v1/chat/completions"
    assert payload["model"] == "z-ai/glm-5.2"
    assert payload["response_format"] == {
        "type": "json_schema",
        "json_schema": {
            "name": "epub_translator_response",
            "strict": True,
            "schema": {"type": "object", "additionalProperties": True},
        },
    }
    assert payload["max_tokens"] == 2048
    assert payload["provider"] == {
        "data_collection": "deny",
        "require_parameters": True,
    }
    assert payload["reasoning"] == {"enabled": False, "exclude": True}
    assert "thinking" not in payload
    assert "enable_thinking" not in payload
    assert "do_sample" not in payload
    assert "reasoning_effort" not in payload
    assert quality.config.model == "z-ai/glm-5.2"
    assert profile_key


def test_glm52_openrouter_cache_identity_differs_from_direct_glm_profiles():
    parser = build_parser()
    common = [
        "translate",
        "source.epub",
        "output.epub",
        "--base-url",
        "https://example.test/v1",
    ]
    openrouter = parser.parse_args(
        common
        + [
            "--fast-model",
            "z-ai/glm-5.2",
            "--tuning-profile",
            "glm52-openrouter-grounded",
        ]
    )
    zhipu = parser.parse_args(
        common + ["--fast-model", "glm-5.2", "--tuning-profile", "glm52-grounded"]
    )

    assert _clients(openrouter)[2] != _clients(zhipu)[2]


def test_glm52_openrouter_rejects_nonstandard_reasoning_effort_before_request():
    parser = build_parser()
    args = parser.parse_args(
        [
            "translate",
            "source.epub",
            "output.epub",
            "--base-url",
            "https://openrouter.ai/api/v1",
            "--fast-model",
            "z-ai/glm-5.2",
            "--tuning-profile",
            "glm52-openrouter-grounded",
            "--thinking",
            "on",
            "--reasoning-effort",
            "minimal",
        ]
    )

    with pytest.raises(EpubError, match="OpenRouter.*reasoning-effort"):
        _clients(args)


def test_gemini37_openrouter_tuning_uses_low_mandatory_reasoning_with_output_headroom():
    tuning = tuning_profile("gemini37-openrouter-grounded")
    language = language_profile(tuning.language_profile)
    parser = build_parser()
    args = parser.parse_args(
        [
            "translate",
            "source.epub",
            "output.epub",
            "--base-url",
            "https://openrouter.ai/api/v1",
            "--fast-model",
            "google/gemini-3.7-flash",
            "--tuning-profile",
            "gemini37-openrouter-grounded",
        ]
    )

    fast, quality, profile_key, _ = _clients(args)
    payload = fast.trace_request_payload("return JSON", "return JSON")

    assert tuning.version == 2
    assert tuning.provider_profile == "openrouter"
    assert tuning.fast_model == "google/gemini-3.7-flash"
    assert tuning.quality_model == "google/gemini-3.7-flash"
    assert tuning.temperature == 0.1
    assert tuning.max_output_tokens == 4096
    assert openrouter_model_profile(tuning.fast_model).reasoning == {
        "effort": "low",
        "exclude": True,
    }
    assert tuning.required_syntax_analyzer == "stanza"
    assert tuning.required_stanza_package == "default_accurate"
    assert language.key == "en-zh-Hans-v16-compact-envelope"
    assert isinstance(fast, OpenRouterClient)
    assert payload["model"] == "google/gemini-3.7-flash"
    assert payload["max_tokens"] == 4096
    assert payload["provider"] == {
        "data_collection": "deny",
        "require_parameters": True,
    }
    assert payload["reasoning"] == {"effort": "low", "exclude": True}
    assert "enabled" not in payload["reasoning"]
    assert quality.config.model == "google/gemini-3.7-flash"
    assert profile_key


def test_gemini37_openrouter_tuning_rejects_a_different_model():
    parser = build_parser()
    args = parser.parse_args(
        [
            "translate",
            "source.epub",
            "output.epub",
            "--base-url",
            "https://openrouter.ai/api/v1",
            "--fast-model",
            "google/gemini-3.7-pro",
            "--tuning-profile",
            "gemini37-openrouter-grounded",
        ]
    )

    with pytest.raises(EpubError, match="仅适用于 fast 模型 google/gemini-3.7-flash"):
        _clients(args)


def test_qwen3_235b_openrouter_grounded_tuning_preserves_v16_contract_without_reasoning():
    tuning = tuning_profile("qwen3-235b-openrouter-grounded")
    language = language_profile(tuning.language_profile)
    parser = build_parser()
    args = parser.parse_args(
        [
            "translate",
            "source.epub",
            "output.epub",
            "--base-url",
            "https://openrouter.ai/api/v1",
            "--fast-model",
            "qwen/qwen3-235b-a22b-07-25",
            "--tuning-profile",
            "qwen3-235b-openrouter-grounded",
        ]
    )

    fast, quality, profile_key, _ = _clients(args)
    payload = fast.trace_request_payload("return JSON", "return JSON")

    assert tuning.provider_profile == "openrouter"
    assert tuning.fast_model == "qwen/qwen3-235b-a22b-07-25"
    assert tuning.quality_model == "qwen/qwen3-235b-a22b-07-25"
    assert tuning.temperature == 0.1
    assert tuning.max_output_tokens == 4096
    assert tuning.required_syntax_analyzer == "stanza"
    assert tuning.required_stanza_package == "default_accurate"
    assert language.key == "en-zh-Hans-v16-compact-envelope"
    assert isinstance(fast, OpenRouterClient)
    assert payload["model"] == "qwen/qwen3-235b-a22b-07-25"
    assert payload["max_tokens"] == 4096
    assert payload["provider"] == {
        "data_collection": "deny",
        "require_parameters": True,
    }
    assert payload["response_format"]["type"] == "json_schema"
    assert "reasoning" not in payload
    assert quality.config.model == "qwen/qwen3-235b-a22b-07-25"
    assert profile_key


@pytest.mark.parametrize(
    ("option", "value", "message"),
    (
        ("--thinking", "on", "OpenRouter.*--thinking"),
        ("--sampling", "off", "OpenRouter.*--sampling"),
    ),
)
def test_openrouter_rejects_unsupported_explicit_generation_flags(
    option, value, message
):
    parser = build_parser()
    args = parser.parse_args(
        [
            "translate",
            "source.epub",
            "output.epub",
            "--base-url",
            "https://openrouter.ai/api/v1",
            "--fast-model",
            "google/gemini-3.7-flash",
            "--provider-profile",
            "openrouter",
            option,
            value,
        ]
    )

    with pytest.raises(EpubError, match=message):
        _clients(args)


def test_cache_profile_includes_effective_provider_request_policy(monkeypatch):
    parser = build_parser()
    args = parser.parse_args(
        [
            "translate",
            "source.epub",
            "output.epub",
            "--base-url",
            "https://openrouter.ai/api/v1",
            "--fast-model",
            "google/gemini-3.7-flash",
            "--provider-profile",
            "openrouter",
        ]
    )

    _, _, original_key, _ = _clients(args)
    original = PROVIDER_PROFILES["openrouter"]
    monkeypatch.setitem(
        PROVIDER_PROFILES,
        "openrouter",
        replace(original, request_extras={"provider": {"data_collection": "allow"}}),
    )

    _, _, changed_key, _ = _clients(args)

    assert changed_key != original_key


def test_cache_profile_includes_openrouter_model_request_policy(monkeypatch):
    parser = build_parser()
    args = parser.parse_args(
        [
            "translate",
            "source.epub",
            "output.epub",
            "--base-url",
            "https://openrouter.ai/api/v1",
            "--fast-model",
            "google/gemini-3.7-flash",
            "--provider-profile",
            "openrouter",
        ]
    )

    _, _, original_key, _ = _clients(args)
    original = OPENROUTER_MODEL_PROFILES["google/gemini-3.7-flash"]
    monkeypatch.setitem(
        OPENROUTER_MODEL_PROFILES,
        "google/gemini-3.7-flash",
        replace(original, version=original.version + 1),
    )

    _, _, changed_key, _ = _clients(args)

    assert changed_key != original_key


def test_qwen_and_deepseek_tunings_share_the_sentence_meaning_contract():
    for name, fast_model, quality_model in (
        ("qwen37-meaning-only", "qwen3.7-flash", "qwen3.7-plus"),
        ("deepseek-v4-meaning-only", "deepseek-v4-flash-0731", "deepseek-v4-pro"),
    ):
        tuning = tuning_profile(name)
        language = language_profile(tuning.language_profile)

        assert tuning.fast_model == fast_model
        assert tuning.quality_model == quality_model
        assert tuning.provider_profile == "qwen-bailian"
        assert tuning.thinking is False
        assert tuning.required_syntax_analyzer == "stanza"
        assert tuning.required_stanza_package == "default_accurate"
        assert language.key == "en-zh-Hans-v11-personal-threshold"
        assert "不要输出任何 meaning 之外的学习卡字段" in language.quality_system


def test_cli_tuning_profile_applies_defaults_and_explicit_request_arguments_override_them():
    parser = build_parser()
    args = parser.parse_args(
        [
            "translate",
            "source.epub",
            "output.epub",
            "--base-url",
            "https://example.test/v4",
            "--fast-model",
            "glm-5.2",
            "--tuning-profile",
            "glm52-grounded",
            "--temperature",
            "0.2",
            "--sampling",
            "on",
            "--response-max-tokens",
            "1024",
        ]
    )
    fast, quality, profile_key, _ = _clients(args)

    assert fast.config.thinking_parameter == "thinking"
    assert fast.config.enable_thinking is False
    assert fast.config.temperature == 0.2
    assert fast.config.do_sample is True
    assert fast.config.max_output_tokens == 1024
    assert quality.config.model == "glm-5.2"
    assert profile_key


def test_quality_payload_version_is_part_of_learning_cache_identity(monkeypatch):
    parser = build_parser()
    args = parser.parse_args(
        [
            "translate",
            "source.epub",
            "output.epub",
            "--base-url",
            "https://example.test/v4",
            "--fast-model",
            "glm-5.2",
            "--tuning-profile",
            "glm52-grounded",
        ]
    )

    _, _, original_key, _ = _clients(args)
    monkeypatch.setattr(cli, "QUALITY_PAYLOAD_VERSION", "quality-candidates-test")
    _, _, changed_key, _ = _clients(args)
    assert QUALITY_PAYLOAD_VERSION == "quality-candidates-v6-sentence-meaning"
    assert changed_key != original_key


def test_lmstudio_provider_uses_structured_json_and_local_output_budget():
    parser = build_parser()
    args = parser.parse_args(
        [
            "translate",
            "source.epub",
            "output.epub",
            "--base-url",
            "http://127.0.0.1:8080/v1",
            "--fast-model",
            "google/gemma-4-e2b",
            "--provider-profile",
            "lmstudio",
        ]
    )

    fast, quality, _, _ = _clients(args)

    assert fast.config.response_format_type == "json_schema"
    assert fast.config.max_output_tokens == 2048
    assert fast.config.enable_thinking is None
    assert quality.config.response_format_type == "json_schema"


def test_openrouter_provider_uses_native_client():
    parser = build_parser()
    args = parser.parse_args(
        [
            "translate",
            "source.epub",
            "output.epub",
            "--base-url",
            "https://openrouter.ai/api/v1",
            "--fast-model",
            "google/gemini-3.7-flash",
            "--provider-profile",
            "openrouter",
            "--response-max-tokens",
            "2048",
        ]
    )

    fast, quality, _, _ = _clients(args)

    assert isinstance(fast, OpenRouterClient)
    assert isinstance(quality, OpenRouterClient)
    assert fast.config.model == "google/gemini-3.7-flash"
    assert fast.config.max_output_tokens == 2048
    assert fast.config.request_extras == {
        "provider": {"data_collection": "deny", "require_parameters": True},
    }
    assert fast.model_profile.reasoning == {"effort": "low", "exclude": True}


def test_meaning_only_tuning_rejects_explicitly_disabled_syntax_analysis():
    parser = build_parser()
    args = parser.parse_args(
        [
            "translate",
            "source.epub",
            "output.epub",
            "--base-url",
            "https://example.test/v4",
            "--fast-model",
            "glm-5.2",
            "--tuning-profile",
            "glm52-grounded",
            "--syntax-analyzer",
            "off",
        ]
    )

    with pytest.raises(EpubError, match="必须使用 Stanza"):
        _clients(args)


def test_meaning_only_tuning_defaults_to_accurate_stanza_and_rejects_downgrade():
    parser = build_parser()
    base = [
        "translate",
        "source.epub",
        "output.epub",
        "--base-url",
        "https://example.test/v4",
        "--fast-model",
        "glm-5.2",
        "--tuning-profile",
        "glm52-grounded",
    ]

    args = parser.parse_args(base)
    assert _effective_stanza_package(args) == "default_accurate"

    downgraded = parser.parse_args(base + ["--stanza-package", "default"])
    with pytest.raises(EpubError, match="必须使用 Stanza default_accurate"):
        _clients(downgraded)


def test_cli_accepts_an_explicit_huggingface_cache_for_accurate_stanza(tmp_path):
    parser = build_parser()
    cache_dir = tmp_path / "huggingface"
    args = parser.parse_args(
        [
            "translate",
            "source.epub",
            "output.epub",
            "--base-url",
            "https://example.test/v4",
            "--fast-model",
            "glm-5.2",
            "--tuning-profile",
            "glm52-grounded",
            "--stanza-hf-cache-dir",
            str(cache_dir),
        ]
    )

    assert args.stanza_hf_cache_dir == str(cache_dir)


def test_syntax_cache_identity_changes_with_stanza_resources_and_transformer_revision(
    tmp_path,
):
    parser = build_parser()
    stanza_dir = tmp_path / "stanza"
    hf_dir = tmp_path / "huggingface"
    stanza_dir.mkdir()
    revision_file = (
        hf_dir
        / "hub"
        / "models--google--electra-large-discriminator"
        / "refs"
        / "main"
    )
    revision_file.parent.mkdir(parents=True)
    resources_file = stanza_dir / "resources.json"
    resources_file.write_text('{"en":{"packages":{"default_accurate":{"pos":"electra-a"}}}}')
    revision_file.write_text("revision-a")
    args = parser.parse_args(
        [
            "translate",
            "source.epub",
            "output.epub",
            "--base-url",
            "https://example.test/v4",
            "--fast-model",
            "glm-5.2",
            "--tuning-profile",
            "glm52-grounded",
            "--stanza-model-dir",
            str(stanza_dir),
            "--stanza-hf-cache-dir",
            str(hf_dir),
        ]
    )
    tuning = tuning_profile("glm52-grounded")

    original = _syntax_profile_key(args, tuning)
    resources_file.write_text('{"en":{"packages":{"default_accurate":{"pos":"electra-b"}}}}')
    changed_resources = _syntax_profile_key(args, tuning)
    revision_file.write_text("revision-b")
    changed_transformer = _syntax_profile_key(args, tuning)

    assert changed_resources != original
    assert changed_transformer != changed_resources
