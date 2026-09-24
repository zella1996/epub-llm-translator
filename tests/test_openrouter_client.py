from __future__ import annotations

import io
from concurrent.futures import ThreadPoolExecutor
import threading
from urllib import error

import pytest

from translator.llm_api import LLMError
from translator.openrouter_client import OpenRouterClient, OpenRouterConfig
from translator.openrouter_model_profiles import (
    OpenRouterModelProfile,
    openrouter_model_profile,
)


def test_openrouter_client_sends_schema_request_and_parses_content():
    calls = []

    def transport(endpoint, payload, headers, timeout_seconds):
        calls.append((endpoint, payload, headers, timeout_seconds))
        return {
            "choices": [
                {"message": {"role": "assistant", "content": '{"ok": true}'}}
            ],
            "usage": {
                "prompt_tokens": 3,
                "completion_tokens": 2,
                "total_tokens": 5,
                "prompt_tokens_details": {"cached_tokens": 1},
                "cost": 0.00001,
            },
            "provider": "Google AI Studio",
        }

    client = OpenRouterClient(
        OpenRouterConfig(
            base_url="https://openrouter.ai/api/v1",
            model="google/gemini-3.7-flash",
            api_key="secret",
            max_output_tokens=2048,
        ),
        transport=transport,
    )

    assert client.complete_json("system", "user") == {"ok": True}

    endpoint, payload, headers, timeout_seconds = calls[0]
    assert endpoint == "https://openrouter.ai/api/v1/chat/completions"
    assert headers == {
        "Authorization": "Bearer secret",
        "Content-Type": "application/json",
    }
    assert timeout_seconds == 120.0
    assert payload == {
        "model": "google/gemini-3.7-flash",
        "messages": [
            {"role": "system", "content": "system"},
            {"role": "user", "content": "user"},
        ],
        "temperature": 0.1,
        "max_tokens": 2048,
        "response_format": {
            "type": "json_schema",
            "json_schema": {
                "name": "epub_translator_response",
                "strict": True,
                "schema": {"type": "object", "additionalProperties": True},
            },
        },
        "provider": {
            "data_collection": "deny",
            "require_parameters": True,
        },
        "reasoning": {"effort": "low", "exclude": True},
    }
    assert client.usage.prompt_tokens == 3
    assert client.usage.completion_tokens == 2
    assert client.usage.total_tokens == 5
    assert client.usage.cached_prompt_tokens == 1
    assert client.usage.cost_usd == 0.00001
    assert client.usage.providers == ("Google AI Studio",)


def test_openrouter_qwen_profile_omits_reasoning_but_keeps_schema():
    client = OpenRouterClient(
        OpenRouterConfig(
            base_url="https://openrouter.ai/api/v1",
            model="qwen/qwen3-235b-a22b-07-25",
            max_output_tokens=256,
        )
    )

    payload = client.trace_request_payload("system", "user")

    assert payload["response_format"]["type"] == "json_schema"
    assert payload["temperature"] == 0.1
    assert payload["max_tokens"] == 256
    assert "reasoning" not in payload


def test_openrouter_claude_fable_profile_uses_only_supported_generation_fields():
    client = OpenRouterClient(
        OpenRouterConfig(
            base_url="https://openrouter.ai/api/v1",
            model="anthropic/claude-fable-5",
            max_output_tokens=4096,
        ),
        transport=lambda *_: {},
    )

    payload = client.trace_request_payload("system", "user")

    assert payload["response_format"]["type"] == "json_object"
    assert payload["reasoning"] == {"effort": "low", "exclude": True}
    assert "temperature" not in payload
    assert "max_tokens" not in payload


def test_openrouter_unknown_model_fails_before_transport():
    calls = 0

    def transport(endpoint, payload, headers, timeout_seconds):
        nonlocal calls
        calls += 1
        return {"choices": [{"message": {"content": '{"ok": true}'}}]}

    with pytest.raises(LLMError, match="尚无请求策略"):
        OpenRouterClient(
            OpenRouterConfig(
                base_url="https://openrouter.ai/api/v1",
                model="vendor/unreviewed-model",
            ),
            transport=transport,
        )

    assert calls == 0


def test_openrouter_model_policy_fingerprint_tracks_wire_behavior():
    gemini = openrouter_model_profile("google/gemini-3.7-flash")
    qwen = openrouter_model_profile("qwen/qwen3-235b-a22b-07-25")

    assert gemini.fingerprint != qwen.fingerprint


def test_openrouter_lightweight_profile_can_omit_unsupported_parameters():
    profile = OpenRouterModelProfile(
        "minimal-model",
        1,
        "vendor/minimal-model",
        structured_output="prompt_only",
        send_temperature=False,
        send_max_tokens=False,
        reasoning=None,
    )
    client = OpenRouterClient(
        OpenRouterConfig(
            base_url="https://openrouter.ai/api/v1",
            model="vendor/minimal-model",
            max_output_tokens=256,
        ),
        model_profile=profile,
    )

    payload = client.trace_request_payload("system", "user")

    assert set(payload) == {"model", "messages", "provider"}


def test_openrouter_profile_can_add_reviewed_static_parameters_only():
    profile = OpenRouterModelProfile(
        "top-p-model",
        1,
        "vendor/top-p-model",
        structured_output="json_object",
        send_temperature=False,
        send_max_tokens=True,
        static_parameters={"top_p": 0.8},
    )
    client = OpenRouterClient(
        OpenRouterConfig(
            base_url="https://openrouter.ai/api/v1",
            model="vendor/top-p-model",
            max_output_tokens=128,
        ),
        model_profile=profile,
    )

    payload = client.trace_request_payload("system", "user")

    assert payload["response_format"] == {"type": "json_object"}
    assert payload["max_tokens"] == 128
    assert payload["top_p"] == 0.8
    assert "temperature" not in payload


def test_openrouter_profile_rejects_static_override_of_controlled_fields():
    with pytest.raises(ValueError, match="不能覆盖受控字段: reasoning"):
        OpenRouterModelProfile(
            "invalid-model",
            1,
            "vendor/invalid-model",
            structured_output="prompt_only",
            send_temperature=False,
            send_max_tokens=False,
            static_parameters={"reasoning": {"enabled": True}},
        )


def test_openrouter_client_fails_fast_when_response_has_no_final_content():
    calls = 0

    def transport(endpoint, payload, headers, timeout_seconds):
        nonlocal calls
        calls += 1
        return {
            "choices": [
                {
                    "message": {
                        "role": "assistant",
                        "content": None,
                        "reasoning": "hidden",
                    }
                }
            ]
        }

    client = OpenRouterClient(
        OpenRouterConfig(
            base_url="https://openrouter.ai/api/v1",
            model="google/gemini-3.7-flash",
            max_retries=2,
        ),
        transport=transport,
    )

    with pytest.raises(LLMError, match="content 不是字符串"):
        client.complete_json("system", "user")

    assert calls == 1


def test_openrouter_client_preserves_400_error_envelope_without_retrying():
    calls = 0

    def transport(endpoint, payload, headers, timeout_seconds):
        nonlocal calls
        calls += 1
        raise error.HTTPError(
            endpoint,
            400,
            "Bad Request",
            {},
            io.BytesIO(b'{"error":{"message":"bad","code":400}}'),
        )

    client = OpenRouterClient(
        OpenRouterConfig(
            base_url="https://openrouter.ai/api/v1",
            model="google/gemini-3.7-flash",
            max_retries=2,
        ),
        transport=transport,
    )

    with pytest.raises(LLMError, match="HTTP Error 400"):
        client.complete_json("system", "user")

    assert calls == 1
    exchange = client.consume_trace_exchange()
    assert exchange is not None
    assert exchange["provider_responses"] == [
        {
            "http_status": 400,
            "body": {"error": {"message": "bad", "code": 400}},
        }
    ]


def test_openrouter_client_keeps_trace_and_usage_isolated_per_worker():
    barrier = threading.Barrier(2)

    def transport(endpoint, payload, headers, timeout_seconds):
        request_id = payload["messages"][1]["content"]
        barrier.wait(timeout=2)
        return {
            "choices": [
                {"message": {"content": '{"request_id": "' + request_id + '"}'}}
            ],
            "usage": {"prompt_tokens": 1, "completion_tokens": 2, "total_tokens": 3},
        }

    client = OpenRouterClient(
        OpenRouterConfig(
            base_url="https://openrouter.ai/api/v1",
            model="google/gemini-3.7-flash",
        ),
        transport=transport,
    )

    def complete_and_consume(request_id):
        result = client.complete_json("system", request_id)
        exchange = client.consume_trace_exchange()
        return result, exchange

    with ThreadPoolExecutor(max_workers=2) as executor:
        first, second = list(
            executor.map(complete_and_consume, ("first", "second"))
        )

    for result, exchange in (first, second):
        assert result["request_id"] in {"first", "second"}
        assert exchange is not None
        response = exchange["provider_responses"][0]
        assert response["choices"][0]["message"]["content"] == (
            '{"request_id": "' + result["request_id"] + '"}'
        )
    assert client.usage.total_tokens == 6
