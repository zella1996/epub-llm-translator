"""Opt-in, secret-free development traces for model-driven paragraph analysis."""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
import threading
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


def _redact(value: object, secret: str) -> object:
    if not secret:
        return value
    if isinstance(value, str):
        return value.replace(secret, "<redacted>")
    if isinstance(value, list):
        return [_redact(item, secret) for item in value]
    if isinstance(value, tuple):
        return [_redact(item, secret) for item in value]
    if isinstance(value, dict):
        return {key: _redact(item, secret) for key, item in value.items()}
    return value


def _atomic_json(path: Path, payload: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            json.dump(payload, stream, ensure_ascii=False, indent=2, sort_keys=True)
            stream.write("\n")
        temporary.chmod(0o600)
        temporary.replace(path)
    except Exception:
        temporary.unlink(missing_ok=True)
        raise


@dataclass
class TraceRun:
    directory: Path
    _paragraphs: dict[tuple[int, int], "ParagraphTrace"] = field(default_factory=dict)
    _lock: threading.Lock = field(default_factory=threading.Lock)

    @classmethod
    def create(
        cls,
        root: Path,
        *,
        command: str,
        source: Path,
        quality_payload_mode: str,
        metadata: dict[str, object] | None = None,
    ) -> "TraceRun":
        root = root.resolve()
        root.mkdir(parents=True, exist_ok=True, mode=0o700)
        source_digest = hashlib.sha256(source.read_bytes()).hexdigest()
        timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ")
        run_id = f"{timestamp}-{os.getpid()}-{uuid.uuid4().hex[:8]}"
        directory = root / run_id
        directory.mkdir(mode=0o700)
        run = cls(directory)
        _atomic_json(
            directory / "run.json",
            {
                "trace_version": 1,
                "run_id": run_id,
                "command": command,
                "source": str(source.resolve()),
                "source_sha256": source_digest,
                "quality_payload_mode": quality_payload_mode,
                "metadata": metadata or {},
                "created_at": timestamp,
            },
        )
        return run

    def paragraph(self, chapter: int, paragraph: int) -> "ParagraphTrace":
        key = (chapter, paragraph)
        with self._lock:
            trace = self._paragraphs.get(key)
            if trace is None:
                trace = ParagraphTrace(
                    self.directory
                    / f"chapter-{chapter:03d}"
                    / f"paragraph-{paragraph:03d}"
                )
                self._paragraphs[key] = trace
            return trace


@dataclass
class ParagraphTrace:
    directory: Path
    _attempts: dict[str, int] = field(default_factory=dict)
    _secrets: set[str] = field(default_factory=set)
    _lock: threading.Lock = field(default_factory=threading.Lock)

    def write(self, name: str, payload: object) -> None:
        with self._lock:
            secrets = tuple(self._secrets)
        for secret in secrets:
            payload = _redact(payload, secret)
        _atomic_json(self.directory / f"{name}.json", payload)

    def start_call(
        self, stage: str, model: object, system: str, user: str
    ) -> "TraceCall":
        with self._lock:
            config = getattr(model, "config", None)
            api_key = getattr(config, "api_key", "")
            if isinstance(api_key, str) and api_key:
                self._secrets.add(api_key)
            attempt = self._attempts.get(stage, 0) + 1
            self._attempts[stage] = attempt
        call = TraceCall(self, stage, attempt, model, system, user)
        call.write_request()
        return call


@dataclass
class TraceCall:
    paragraph: ParagraphTrace
    stage: str
    attempt: int
    model: object
    system: str
    user: str

    @property
    def prefix(self) -> str:
        return f"{self.stage}-attempt-{self.attempt:02d}"

    def write_request(self) -> None:
        builder = getattr(self.model, "trace_request_payload", None)
        payload = (
            builder(self.system, self.user)
            if callable(builder)
            else {
                "messages": [
                    {"role": "system", "content": self.system},
                    {"role": "user", "content": self.user},
                ]
            }
        )
        self.paragraph.write(
            f"{self.prefix}-request",
            _redact(
                {
                    "stage": self.stage,
                    "attempt": self.attempt,
                    "endpoint": getattr(self.model, "endpoint", None),
                    "system_sha256": hashlib.sha256(
                        self.system.encode("utf-8")
                    ).hexdigest(),
                    "user_sha256": hashlib.sha256(
                        self.user.encode("utf-8")
                    ).hexdigest(),
                    "payload": payload,
                },
                self._api_key(),
            ),
        )

    def complete(self) -> dict[str, Any]:
        try:
            parsed = self.model.complete_json(self.system, self.user)  # type: ignore[attr-defined]
        except BaseException as exc:
            self._write_response(None, exc)
            raise
        self._write_response(parsed, None)
        return parsed

    def _write_response(
        self, parsed: dict[str, Any] | None, error: BaseException | None
    ) -> None:
        consumer = getattr(self.model, "consume_trace_exchange", None)
        exchange = consumer() if callable(consumer) else None
        responses = (
            exchange.get("provider_responses", [])
            if isinstance(exchange, dict)
            else []
        )
        self.paragraph.write(
            f"{self.prefix}-response",
            _redact(
                {
                    "stage": self.stage,
                    "attempt": self.attempt,
                    "status": "failure" if error else "success",
                    "provider_responses": responses,
                    "usage": [
                        response["usage"]
                        for response in responses
                        if isinstance(response, dict)
                        and isinstance(response.get("usage"), dict)
                    ],
                    "parsed_content": parsed,
                    "error": (
                        {"type": type(error).__name__, "message": str(error)}
                        if error
                        else None
                    ),
                },
                self._api_key(),
            ),
        )

    def write_normalized(self, payload: object) -> None:
        self.paragraph.write(
            f"{self.prefix}-normalized", _redact(payload, self._api_key())
        )

    def _api_key(self) -> str:
        config = getattr(self.model, "config", None)
        value = getattr(config, "api_key", "")
        return value if isinstance(value, str) else ""
