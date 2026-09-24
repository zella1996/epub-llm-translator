from __future__ import annotations

import json
import os
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from translator.llm_api import LLMError, OpenAICompatibleClient, OpenAIConfig
from tests.helpers import build_epub
from translator.cli import main
from translator.epub_processor import EpubBook


pytestmark = [
    pytest.mark.http_integration,
    pytest.mark.skipif(
        os.environ.get("RUN_HTTP_TESTS") != "1",
        reason="set RUN_HTTP_TESTS=1 where localhost sockets are allowed",
    ),
]


class _Handler(BaseHTTPRequestHandler):
    calls = 0
    requests = []
    fail_first = False
    status = 200
    response_content = '{"ok": true}'
    response_contents = []
    response_usage = None
    response_provider = None
    raw_response_body = None
    echo_authorization = False
    stream_chunks = []

    def do_POST(self):
        type(self).calls += 1
        length = int(self.headers["Content-Length"])
        payload = json.loads(self.rfile.read(length))
        type(self).requests.append((self.path, self.headers, payload))
        status = 500 if self.fail_first and self.calls == 1 else self.status
        self.send_response(status)
        self.send_header(
            "Content-Type",
            "text/event-stream" if type(self).stream_chunks else "application/json",
        )
        self.end_headers()
        if status == 200:
            if type(self).stream_chunks:
                for chunk in type(self).stream_chunks:
                    self.wfile.write(
                        f"data: {json.dumps(chunk)}\n\n".encode("utf-8")
                    )
                    self.wfile.flush()
                self.wfile.write(b"data: [DONE]\n\n")
                return
            if type(self).raw_response_body is not None:
                self.wfile.write(type(self).raw_response_body)
                return
            content = (
                type(self).response_contents[self.calls - 1]
                if self.calls <= len(type(self).response_contents)
                else type(self).response_content
            )
            body = {
                "choices": [
                    {"message": {"content": content}}
                ]
            }
            if type(self).response_usage is not None:
                body["usage"] = type(self).response_usage
            if type(self).response_provider is not None:
                body["provider"] = type(self).response_provider
            if type(self).echo_authorization:
                body["debug_authorization"] = self.headers.get("Authorization")
            self.wfile.write(json.dumps(body).encode())
        else:
            self.wfile.write(b'{"error":"temporary"}')

    def log_message(self, *_):
        return


@pytest.fixture
def model_server():
    _Handler.calls = 0
    _Handler.requests = []
    _Handler.fail_first = False
    _Handler.status = 200
    _Handler.response_content = '{"ok": true}'
    _Handler.response_contents = []
    _Handler.response_usage = None
    _Handler.response_provider = None
    _Handler.raw_response_body = None
    _Handler.echo_authorization = False
    _Handler.stream_chunks = []
    server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}/v1"
    finally:
        server.shutdown()
        thread.join(timeout=2)


def test_openai_compatible_request_and_json_response(model_server):
    client = OpenAICompatibleClient(
        OpenAIConfig(base_url=model_server, model="test-model", api_key="secret")
    )
    assert client.complete_json("system", "user") == {"ok": True}
    path, headers, payload = _Handler.requests[0]
    assert path == "/v1/chat/completions"
    assert headers["Authorization"] == "Bearer secret"
    assert payload["model"] == "test-model"
    assert payload["response_format"] == {"type": "json_object"}
    assert "enable_thinking" not in payload
    assert "stream" not in payload


def test_openai_compatible_can_disable_provider_thinking(model_server):
    client = OpenAICompatibleClient(
        OpenAIConfig(
            base_url=model_server,
            model="test-model",
            enable_thinking=False,
        )
    )
    assert client.complete_json("system", "user") == {"ok": True}
    assert _Handler.requests[0][2]["enable_thinking"] is False


def test_openai_compatible_can_request_text_for_lmstudio_json_prompts(model_server):
    _Handler.response_content = '```json\n{"ok": true}\n```'
    client = OpenAICompatibleClient(
        OpenAIConfig(
            base_url=model_server,
            model="local-model",
            response_format_type="text",
        )
    )

    assert client.complete_json("system", "user") == {"ok": True}
    assert _Handler.requests[0][2]["response_format"] == {"type": "text"}


def test_openai_compatible_can_enforce_generic_json_schema(model_server):
    client = OpenAICompatibleClient(
        OpenAIConfig(
            base_url=model_server,
            model="local-model",
            response_format_type="json_schema",
        )
    )

    assert client.complete_json("system", "user") == {"ok": True}
    response_format = _Handler.requests[0][2]["response_format"]
    assert response_format["type"] == "json_schema"
    assert response_format["json_schema"] == {
        "name": "epub_translator_response",
        "strict": True,
        "schema": {"type": "object", "additionalProperties": True},
    }


def test_openai_compatible_uses_zhipu_thinking_shape(model_server):
    client = OpenAICompatibleClient(
        OpenAIConfig(
            base_url=model_server,
            model="glm-4.7-flash",
            enable_thinking=False,
            thinking_parameter="thinking",
        )
    )
    assert client.complete_json("system", "user") == {"ok": True}
    payload = _Handler.requests[0][2]
    assert payload["thinking"] == {"type": "disabled"}
    assert "enable_thinking" not in payload


def test_openai_compatible_uses_glm53_thinking_and_sampling_shape(model_server):
    client = OpenAICompatibleClient(
        OpenAIConfig(
            base_url=model_server,
            model="glm-5.3-flash",
            temperature=1.0,
            top_p=0.95,
            enable_thinking=True,
            thinking_parameter="thinking",
            thinking_clear_thinking=False,
            reasoning_effort="max",
        )
    )
    assert client.complete_json("system", "user") == {"ok": True}
    payload = _Handler.requests[0][2]
    assert payload["temperature"] == 1.0
    assert payload["top_p"] == 0.95
    assert payload["thinking"] == {"type": "enabled", "clear_thinking": False}
    assert payload["reasoning_effort"] == "max"


def test_openai_compatible_collects_streamed_content_usage_and_trace(model_server):
    _Handler.stream_chunks = [
        {
            "id": "stream-1",
            "choices": [{"delta": {"reasoning_content": "thinking"}}],
        },
        {
            "id": "stream-1",
            "choices": [{"delta": {"content": '{"ok":'}}],
        },
        {
            "id": "stream-1",
            "choices": [
                {"delta": {"content": " true}"}, "finish_reason": "stop"}
            ],
            "usage": {
                "prompt_tokens": 11,
                "completion_tokens": 7,
                "total_tokens": 18,
            },
            "provider": "zhipu",
        },
    ]
    client = OpenAICompatibleClient(
        OpenAIConfig(
            base_url=model_server,
            model="glm-5.3-flash",
            stream_response=True,
        )
    )

    assert client.complete_json("system", "user") == {"ok": True}
    assert _Handler.requests[0][2]["stream"] is True
    assert client.usage.total_tokens == 18
    assert client.usage.providers == ("zhipu",)
    assert client.consume_trace_exchange()["provider_responses"] == [
        {
            "id": "stream-1",
            "choices": [
                {
                    "message": {
                        "content": '{"ok": true}',
                        "reasoning_content": "thinking",
                    },
                    "finish_reason": "stop",
                }
            ],
            "usage": {
                "prompt_tokens": 11,
                "completion_tokens": 7,
                "total_tokens": 18,
            },
            "provider": "zhipu",
        }
    ]


def test_openai_compatible_can_disable_sampling_limit_output_and_collect_usage(model_server):
    _Handler.response_usage = {
        "prompt_tokens": 11,
        "completion_tokens": 7,
        "total_tokens": 18,
    }
    client = OpenAICompatibleClient(
        OpenAIConfig(
            base_url=model_server,
            model="glm-5.2",
            do_sample=False,
            max_output_tokens=2048,
        )
    )
    assert client.complete_json("system", "user") == {"ok": True}
    payload = _Handler.requests[0][2]
    assert payload["do_sample"] is False
    assert payload["max_tokens"] == 2048
    assert client.usage.prompt_tokens == 11
    assert client.usage.completion_tokens == 7
    assert client.usage.total_tokens == 18


def test_openrouter_usage_collects_cached_tokens_cost_and_actual_provider(model_server):
    _Handler.response_usage = {
        "prompt_tokens": 11,
        "completion_tokens": 7,
        "total_tokens": 18,
        "cost": 0.000123,
        "prompt_tokens_details": {"cached_tokens": 5},
    }
    _Handler.response_provider = "z-ai"
    client = OpenAICompatibleClient(
        OpenAIConfig(base_url=model_server, model="z-ai/glm-5.2")
    )

    assert client.complete_json("system", "user") == {"ok": True}
    assert client.usage.prompt_tokens == 11
    assert client.usage.completion_tokens == 7
    assert client.usage.total_tokens == 18
    assert client.usage.cached_prompt_tokens == 5
    assert client.usage.cost_usd == pytest.approx(0.000123)
    assert client.usage.providers == ("z-ai",)


def test_openai_compatible_retries_500_then_succeeds(model_server):
    _Handler.fail_first = True
    client = OpenAICompatibleClient(
        OpenAIConfig(
            base_url=model_server,
            model="test-model",
            max_retries=1,
        )
    )
    assert client.complete_json("system", "user") == {"ok": True}
    assert _Handler.calls == 2


def test_openai_compatible_does_not_retry_bad_request(model_server):
    _Handler.status = 400
    client = OpenAICompatibleClient(
        OpenAIConfig(base_url=model_server, model="test-model", max_retries=2)
    )
    with pytest.raises(LLMError):
        client.complete_json("system", "user")
    assert _Handler.calls == 1


def test_trace_exchange_keeps_malformed_http_body(model_server):
    _Handler.raw_response_body = b"not-json"
    client = OpenAICompatibleClient(
        OpenAIConfig(base_url=model_server, model="test-model", max_retries=0)
    )
    with pytest.raises(LLMError):
        client.complete_json("system", "user")
    assert client.consume_trace_exchange()["provider_responses"] == [
        {"raw_body": "not-json"}
    ]


def test_cli_preview_round_trip_through_openai_http(
    model_server, tmp_path, capsys, monkeypatch
):
    source = build_epub(tmp_path / "source.epub")
    output = tmp_path / "book-PREVIEW.epub"
    trace_root = tmp_path / "traces"
    monkeypatch.setenv("TRACE_TEST_API_KEY", "must-not-appear-in-trace")
    _Handler.echo_authorization = True
    _Handler.response_usage = {
        "prompt_tokens": 101,
        "completion_tokens": 23,
        "total_tokens": 124,
    }
    _Handler.response_contents = [
        json.dumps(
            {
                "translations": [
                    {"index": 1, "translation": "这是原段落。"},
                    {"index": 2, "translation": "尽管它很长，但意思仍可验证。"},
                ],
                "sentences": [
                    {
                        "index": 1,
                        "text": "This is the original paragraph.",
                        "difficulty": "fluent",
                    },
                    {
                        "index": 2,
                        "text": "Although it is long, its meaning remains testable.",
                        "difficulty": "effortful",
                    },
                ],
            },
            ensure_ascii=False,
        ),
        json.dumps(
            {
                "sentences": [
                    {
                        "index": 2,
                        "text": "Although it is long, its meaning remains testable.",
                        "difficulty": "effortful",
                        "meaning": "尽管它很长，但意思仍可验证。",
                        "cues": "先让步，再给主要判断。",
                        "feel": "",
                        "note": "",
                    }
                ]
            },
            ensure_ascii=False,
        ),
    ]
    result = main(
        [
            "preview",
            str(source),
            str(output),
            "--chapter",
            "1",
            "--paragraph",
            "1",
            "--base-url",
            model_server,
            "--fast-model",
            "fast",
            "--quality-model",
            "quality",
            "--api-key-env",
            "TRACE_TEST_API_KEY",
            "--trace-dir",
            str(trace_root),
            "--provider-profile",
            "qwen-bailian",
            "--work-year",
            "1811",
            "--book-author",
            "Override Author",
            "--skip-epubcheck",
        ]
    )
    assert result == 0
    assert output.is_file()
    assert "段译：这是原段落" in capsys.readouterr().out
    assert [request[2]["model"] for request in _Handler.requests] == [
        "fast",
        "quality",
    ]
    assert all(request[2]["enable_thinking"] is False for request in _Handler.requests)
    fast_user = _Handler.requests[0][2]["messages"][1]["content"]
    assert '"authors": ["Override Author"]' in fast_user
    assert '"work_year": 1811' in fast_user
    run_dir, = trace_root.iterdir()
    paragraph_dir = run_dir / "chapter-001" / "paragraph-001"
    fast_request = json.loads(
        (paragraph_dir / "fast-attempt-01-request.json").read_text()
    )
    fast_response = json.loads(
        (paragraph_dir / "fast-attempt-01-response.json").read_text()
    )
    quality_normalized = json.loads(
        (paragraph_dir / "quality-compact-v5-attempt-01-normalized.json").read_text()
    )
    final_learning = json.loads((paragraph_dir / "paragraph-learning.json").read_text())
    assert fast_request["payload"]["model"] == "fast"
    assert fast_response["provider_responses"][0]["usage"] == _Handler.response_usage
    assert quality_normalized["candidate_indices"] == [2]
    assert final_learning["cards"][0]["meaning"] == "尽管它很长，但意思仍可验证。"
    assert "must-not-appear-in-trace" not in "".join(
        path.read_text() for path in run_dir.rglob("*.json")
    )
    with EpubBook(output) as book:
        assert book.link_issues() == set()


def test_cli_trace_keeps_raw_response_and_validation_error(
    model_server, tmp_path, monkeypatch
):
    source = build_epub(tmp_path / "source.epub")
    output = tmp_path / "failed-PREVIEW.epub"
    trace_root = tmp_path / "traces"
    monkeypatch.setenv("TRACE_TEST_API_KEY", "must-not-appear-in-trace")
    _Handler.response_content = "{}"

    assert main(
        [
            "preview",
            str(source),
            str(output),
            "--chapter",
            "1",
            "--paragraph",
            "1",
            "--base-url",
            model_server,
            "--fast-model",
            "fast",
            "--api-key-env",
            "TRACE_TEST_API_KEY",
            "--trace-dir",
            str(trace_root),
            "--schema-retries",
            "0",
            "--skip-epubcheck",
        ]
    ) == 2

    run_dir, = trace_root.iterdir()
    paragraph_dir = run_dir / "chapter-001" / "paragraph-001"
    response = json.loads(
        (paragraph_dir / "fast-attempt-01-response.json").read_text()
    )
    normalized = json.loads(
        (paragraph_dir / "fast-attempt-01-normalized.json").read_text()
    )
    assert response["provider_responses"][0]["choices"][0]["message"]["content"] == "{}"
    assert normalized["status"] == "validation_failure"
    assert "translations" in normalized["error"]["message"]
    assert not output.exists()


def test_cli_trace_keeps_http_error_body(model_server, tmp_path, monkeypatch):
    source = build_epub(tmp_path / "source.epub")
    trace_root = tmp_path / "traces"
    monkeypatch.setenv("TRACE_TEST_API_KEY", "must-not-appear-in-trace")
    _Handler.status = 400

    assert main(
        [
            "preview",
            str(source),
            str(tmp_path / "failed-PREVIEW.epub"),
            "--chapter",
            "1",
            "--paragraph",
            "1",
            "--base-url",
            model_server,
            "--fast-model",
            "fast",
            "--api-key-env",
            "TRACE_TEST_API_KEY",
            "--trace-dir",
            str(trace_root),
            "--max-retries",
            "0",
            "--skip-epubcheck",
        ]
    ) == 2

    run_dir, = trace_root.iterdir()
    response = json.loads(
        (
            run_dir
            / "chapter-001"
            / "paragraph-001"
            / "fast-attempt-01-response.json"
        ).read_text()
    )
    assert response["status"] == "failure"
    assert response["provider_responses"] == [
        {"http_status": 400, "body": {"error": "temporary"}}
    ]
