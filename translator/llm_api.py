"""Small OpenAI-compatible JSON client used by the translation pipeline."""

from __future__ import annotations

import json
import threading
import time
from dataclasses import dataclass
from typing import Any, Callable, Mapping
from urllib import error, request
from urllib.parse import urlsplit


class LLMError(RuntimeError):
    """Raised when a model request cannot produce a usable response."""


class InvalidJSONResponse(LLMError):
    """Completed provider content that needs explicit, audited core repair."""

    def __init__(self, content: str, detail: str) -> None:
        super().__init__(f"Invalid model JSON: {detail}")
        self.content = content


class _OutputBudgetExhausted(LLMError):
    """The provider spent the output envelope without returning final content."""


@dataclass(frozen=True)
class OpenAIConfig:
    base_url: str
    model: str
    api_key: str = ""
    timeout_seconds: float = 120.0
    max_retries: int = 2
    temperature: float = 0.1
    top_p: float | None = None
    enable_thinking: bool | None = None
    thinking_parameter: str = "enable_thinking"
    thinking_clear_thinking: bool | None = None
    do_sample: bool | None = None
    max_output_tokens: int | None = None
    length_retry_max_output_tokens: int | None = None
    reasoning_effort: str | None = None
    response_format_type: str = "json_object"
    request_extras: Mapping[str, Any] | None = None
    stream_response: bool = False


@dataclass(frozen=True)
class Usage:
    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0
    cached_prompt_tokens: int = 0
    cost_usd: float = 0.0
    providers: tuple[str, ...] = ()


JSONTransport = Callable[
    [str, Mapping[str, Any], Mapping[str, str], float], Mapping[str, Any]
]


class RateLimiter:
    """Thread-safe reservation-based request pacing shared by model clients."""

    def __init__(self, requests_per_minute: float) -> None:
        if requests_per_minute <= 0:
            raise ValueError("requests_per_minute must be positive")
        self.interval = 60.0 / requests_per_minute
        self._lock = threading.Lock()
        self._next_allowed = 0.0

    def acquire(self) -> None:
        with self._lock:
            now = time.monotonic()
            scheduled = max(now, self._next_allowed)
            self._next_allowed = scheduled + self.interval
        delay = scheduled - now
        if delay > 0:
            time.sleep(delay)


class OpenAICompatibleClient:
    def __init__(
        self,
        config: OpenAIConfig,
        *,
        rate_limiter: RateLimiter | None = None,
        transport: JSONTransport | None = None,
    ) -> None:
        parsed = urlsplit(config.base_url)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise LLMError("--base-url 必须是完整的 http(s) URL")
        if not config.model.strip():
            raise LLMError("模型名称不能为空")
        if config.timeout_seconds <= 0:
            raise LLMError("请求超时必须大于 0 秒")
        if config.max_retries < 0:
            raise LLMError("请求重试次数不能小于 0")
        if config.max_output_tokens is not None and config.max_output_tokens <= 0:
            raise LLMError("单次输出 token 上限必须大于 0")
        if (config.length_retry_max_output_tokens is not None
                and config.length_retry_max_output_tokens <= 0):
            raise LLMError("长度耗尽重试 token 上限必须大于 0")
        if config.top_p is not None and not 0 <= config.top_p <= 1:
            raise LLMError("top_p 必须在 0 到 1 之间")
        if config.response_format_type not in {"json_object", "json_schema", "text"}:
            raise LLMError("响应格式必须是 json_object、json_schema 或 text")
        self.config = config
        self.rate_limiter = rate_limiter
        self._transport = transport
        self._usage = Usage()
        self._usage_lock = threading.Lock()
        self._trace_local = threading.local()

    @property
    def usage(self) -> Usage:
        with self._usage_lock:
            return self._usage

    @property
    def endpoint(self) -> str:
        base = self.config.base_url.rstrip("/")
        if base.endswith("/chat/completions"):
            return base
        return f"{base}/chat/completions"

    def complete_json(self, system: str, user: str) -> dict[str, Any]:
        payload = self.trace_request_payload(system, user)
        self._trace_local.exchange = {
            "request_payload": payload,
            "provider_responses": [],
        }
        headers = {"Content-Type": "application/json"}
        if self.config.api_key:
            headers["Authorization"] = f"Bearer {self.config.api_key}"

        last_error: Exception | None = None
        for attempt in range(self.config.max_retries + 1):
            try:
                if self.rate_limiter is not None:
                    self.rate_limiter.acquire()
                body = self._send_request(payload, headers)
                self._trace_local.exchange["provider_responses"].append(body)
                self._record_usage(body)
                content = body["choices"][0]["message"]["content"]
                if isinstance(content, list):
                    content = "".join(
                        part.get("text", "")
                        for part in content
                        if isinstance(part, dict)
                    )
                if not isinstance(content, str):
                    raise LLMError("模型响应 content 不是字符串")
                choice = body["choices"][0]
                if not content.strip() and choice.get("finish_reason") == "length":
                    raise _OutputBudgetExhausted("输出 token 余量耗尽且正文为空")
                try:
                    return self._parse_json_content(content)
                except (json.JSONDecodeError, LLMError) as exc:
                    if content.strip() and choice.get("finish_reason") == "stop":
                        raise InvalidJSONResponse(content, str(exc)) from exc
                    raise
            except InvalidJSONResponse:
                # Repeating the same prompt is not a JSON repair. Let the
                # caller's explicit recovery policy handle completed content.
                raise
            except (error.HTTPError, error.URLError, TimeoutError, KeyError, json.JSONDecodeError, LLMError) as exc:
                if isinstance(exc, error.HTTPError):
                    raw_error = exc.read()
                    try:
                        error_body: object = json.loads(raw_error.decode("utf-8"))
                    except (UnicodeDecodeError, json.JSONDecodeError):
                        error_body = raw_error.decode("utf-8", errors="replace")
                    self._trace_local.exchange["provider_responses"].append(
                        {"http_status": exc.code, "body": error_body}
                    )
                last_error = exc
                retryable = self._is_retryable(exc)
                if isinstance(exc, _OutputBudgetExhausted):
                    current = payload.get("max_tokens")
                    retry_max = self.config.length_retry_max_output_tokens
                    if (isinstance(current, int) and isinstance(retry_max, int)
                            and retry_max > current):
                        payload = {**payload, "max_tokens": retry_max}
                    else:
                        retryable = False
                if attempt >= self.config.max_retries or not retryable:
                    break
                time.sleep(min(2**attempt, 4))
        raise LLMError(f"模型请求在有限重试后失败: {last_error}") from last_error

    @staticmethod
    def _is_retryable(exc: Exception) -> bool:
        return (
            not isinstance(exc, error.HTTPError)
            or exc.code == 429
            or exc.code >= 500
        )

    def _send_request(
        self, payload: Mapping[str, Any], headers: Mapping[str, str]
    ) -> Mapping[str, Any]:
        if self._transport is not None:
            return self._transport(
                self.endpoint, payload, headers, self.config.timeout_seconds
            )
        encoded = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        req = request.Request(self.endpoint, data=encoded, headers=dict(headers))
        with request.urlopen(req, timeout=self.config.timeout_seconds) as response:
            if self.config.stream_response:
                return self._read_stream_response(response)
            raw_response = response.read().decode("utf-8", errors="replace")
        try:
            body = json.loads(raw_response)
        except json.JSONDecodeError:
            self._trace_local.exchange["provider_responses"].append(
                {"raw_body": raw_response}
            )
            raise
        if not isinstance(body, Mapping):
            raise LLMError("模型响应顶层不是对象")
        return body

    def _read_stream_response(self, response: Any) -> Mapping[str, Any]:
        content_parts: list[str] = []
        reasoning_parts: list[str] = []
        response_id: str | None = None
        finish_reason: object = None
        usage: object = None
        provider: object = None

        for raw_line in response:
            line = raw_line.decode("utf-8", errors="replace").strip()
            if not line or line.startswith(":"):
                continue
            if not line.startswith("data:"):
                continue
            data = line[5:].strip()
            if data == "[DONE]":
                break
            try:
                chunk = json.loads(data)
            except json.JSONDecodeError:
                self._trace_local.exchange["provider_responses"].append(
                    {"raw_body": data}
                )
                raise
            if not isinstance(chunk, Mapping):
                raise LLMError("模型流式响应事件顶层不是对象")
            if isinstance(chunk.get("error"), Mapping):
                raise LLMError(
                    "模型流式响应返回错误: "
                    + json.dumps(chunk["error"], ensure_ascii=False)
                )
            if isinstance(chunk.get("id"), str):
                response_id = chunk["id"]
            if chunk.get("usage") is not None:
                usage = chunk["usage"]
            if chunk.get("provider") is not None:
                provider = chunk["provider"]
            choices = chunk.get("choices")
            if not isinstance(choices, list) or not choices:
                continue
            choice = choices[0]
            if not isinstance(choice, Mapping):
                continue
            delta = choice.get("delta")
            if isinstance(delta, Mapping):
                content = delta.get("content")
                if isinstance(content, str):
                    content_parts.append(content)
                reasoning = delta.get("reasoning_content")
                if isinstance(reasoning, str):
                    reasoning_parts.append(reasoning)
            if choice.get("finish_reason") is not None:
                finish_reason = choice["finish_reason"]

        message: dict[str, Any] = {"content": "".join(content_parts)}
        if reasoning_parts:
            message["reasoning_content"] = "".join(reasoning_parts)
        body: dict[str, Any] = {
            "choices": [
                {"message": message, "finish_reason": finish_reason}
            ]
        }
        if response_id is not None:
            body["id"] = response_id
        if usage is not None:
            body["usage"] = usage
        if provider is not None:
            body["provider"] = provider
        return body

    def trace_request_payload(self, system: str, user: str) -> dict[str, Any]:
        response_format: dict[str, Any] = {"type": self.config.response_format_type}
        if self.config.response_format_type == "json_schema":
            response_format["json_schema"] = {
                "name": "epub_translator_response",
                "strict": True,
                "schema": {
                    "type": "object",
                    "additionalProperties": True,
                },
            }
        payload = {
            "model": self.config.model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "temperature": self.config.temperature,
            "response_format": response_format,
        }
        if self.config.top_p is not None:
            payload["top_p"] = self.config.top_p
        if self.config.request_extras is not None:
            payload.update(self.config.request_extras)
        if self.config.enable_thinking is not None:
            if self.config.thinking_parameter == "thinking":
                thinking = {
                    "type": "enabled" if self.config.enable_thinking else "disabled"
                }
                if self.config.thinking_clear_thinking is not None:
                    thinking["clear_thinking"] = self.config.thinking_clear_thinking
                payload["thinking"] = thinking
            else:
                payload["enable_thinking"] = self.config.enable_thinking
        if self.config.do_sample is not None:
            payload["do_sample"] = self.config.do_sample
        if self.config.max_output_tokens is not None:
            payload["max_tokens"] = self.config.max_output_tokens
        if self.config.reasoning_effort is not None:
            payload["reasoning_effort"] = self.config.reasoning_effort
        if self.config.stream_response:
            payload["stream"] = True
        return payload

    def consume_trace_exchange(self) -> dict[str, Any] | None:
        exchange = getattr(self._trace_local, "exchange", None)
        self._trace_local.exchange = None
        return exchange

    def _record_usage(self, body: object) -> None:
        if not isinstance(body, dict):
            return
        raw = body.get("usage")
        if not isinstance(raw, dict):
            return
        values = []
        for name in ("prompt_tokens", "completion_tokens", "total_tokens"):
            value = raw.get(name, 0)
            values.append(value if isinstance(value, int) and value >= 0 else 0)
        details = raw.get("prompt_tokens_details")
        cached_tokens = (
            details.get("cached_tokens", 0) if isinstance(details, dict) else 0
        )
        cached_tokens = (
            cached_tokens if isinstance(cached_tokens, int) and cached_tokens >= 0 else 0
        )
        cost = raw.get("cost", 0.0)
        cost = cost if isinstance(cost, (int, float)) and cost >= 0 else 0.0
        provider = body.get("provider")
        providers = (provider,) if isinstance(provider, str) and provider else ()
        addition = Usage(*values, cached_tokens, float(cost), providers)
        with self._usage_lock:
            self._usage = Usage(
                self._usage.prompt_tokens + addition.prompt_tokens,
                self._usage.completion_tokens + addition.completion_tokens,
                self._usage.total_tokens + addition.total_tokens,
                self._usage.cached_prompt_tokens + addition.cached_prompt_tokens,
                self._usage.cost_usd + addition.cost_usd,
                tuple(dict.fromkeys(self._usage.providers + addition.providers)),
            )

    @staticmethod
    def _parse_json_content(content: str) -> dict[str, Any]:
        text = content.strip()
        if text.startswith("```"):
            lines = text.splitlines()
            if lines and lines[0].startswith("```"):
                lines = lines[1:]
            if lines and lines[-1].strip() == "```":
                lines = lines[:-1]
            text = "\n".join(lines).strip()
        value = json.loads(text)
        if not isinstance(value, dict):
            raise LLMError("模型 JSON 顶层必须是对象")
        return value
