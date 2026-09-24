"""S5: Japanese effective configuration reaches offline user entry paths."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from translator import cli
from translator.cli import main
from translator.epub_processor import EpubError
from translator.languages import JAPANESE_SOURCE, JAPANESE_TO_CHINESE, resolve_effective_language_configuration
from translator.translator import estimate_translation, translate_book, translate_reading_book


FIXTURE = Path("tests/fixtures/japanese-epub3.epub")


class _NeverCall:
    calls = 0

    def complete_json(self, *_):
        self.calls += 1
        raise AssertionError("dry-run must not call a model")


class _JapaneseReadingModel:
    def __init__(self):
        self.calls = 0

    def complete_json(self, _system, user):
        self.calls += 1
        sentences = json.loads(user)["sentences"]
        return {
            "sentences": [
                {
                    "index": sentence["index"],
                    "translation": f"译文{sentence['index']}。",
                    "difficulty": "fluent",
                }
                for sentence in sentences
            ],
            "aids": [],
        }


def test_japanese_inspect_prints_the_single_effective_identity_and_safe_gate(capsys):
    assert main(["inspect", str(FIXTURE), "--skip-epubcheck", "--limit", "0"]) == 0
    output = capsys.readouterr().out
    for value in (
        "ja-zh-Hans-v2-japanese-reading-meaning", "japanese-exact-v3",
        "japanese-short-text-safe-v1", '"gate_mode": "safe-v1"',
        "off-v1", "japanese-output-v2", "prompt_fingerprint",
    ):
        assert value in output
    assert "跳过短简单对话 0" in output


def test_japanese_default_and_reading_dry_runs_do_not_call_models_or_create_output(tmp_path):
    config = resolve_effective_language_configuration(["ja-JP"])
    model = _NeverCall()
    output = tmp_path / "not-created.epub"
    default = translate_book(
        FIXTURE, output, fast_model=model, quality_model=model, profile_key="ja-s5",
        cache_path=tmp_path / "cache.sqlite", require_epubcheck=False, dry_run=True,
        language=JAPANESE_TO_CHINESE, source_language=JAPANESE_SOURCE,
        japanese_short_text_chars=0,
    )
    reading = translate_reading_book(
        FIXTURE, output, model=model, profile_key="ja-reading-s5",
        cache_path=tmp_path / "reading-cache.sqlite", require_epubcheck=False, dry_run=True,
        source_language=JAPANESE_SOURCE, language_identity=config.cache_identity,
        japanese_short_text_chars=0,
    )
    assert default.paragraphs == reading.paragraphs == 6
    assert model.calls == 0
    assert not output.exists()


def test_japanese_reading_live_path_constructs_off_v1_frozen_syntax_identity(tmp_path):
    config = resolve_effective_language_configuration(["ja-JP"])
    model = _JapaneseReadingModel()

    result = translate_reading_book(
        FIXTURE,
        tmp_path / "reading.epub",
        model=model,
        profile_key="ja-reading-live-syntax-identity",
        cache_path=tmp_path / "reading-cache.sqlite",
        require_epubcheck=False,
        source_language=JAPANESE_SOURCE,
        language_identity=config.cache_identity,
        japanese_short_text_chars=0,
    )

    assert result.generated_paragraphs == 6
    assert model.calls == 6


def test_japanese_estimate_is_not_the_english_chars_divided_by_four_rule():
    from translator.epub_processor import ParagraphRef

    ref = ParagraphRef(1, 1, "chapter.xhtml", "p", "これは日文 token 見積りです。")
    japanese = estimate_translation([ref], source_language=JAPANESE_SOURCE)
    english = estimate_translation([ref])
    assert japanese.approximate_input_tokens > english.approximate_input_tokens
    assert japanese.approximate_output_tokens > english.approximate_output_tokens


def test_explicit_english_tuning_is_rejected_before_client_construction(monkeypatch, tmp_path):
    called = False

    def forbidden(*_, **__):
        nonlocal called
        called = True
        raise AssertionError("configuration must fail before a client exists")

    monkeypatch.setattr(cli, "_clients", forbidden)
    assert main([
        "translate", str(FIXTURE), str(tmp_path / "out.epub"), "--dry-run",
        "--base-url", "https://offline.invalid/v1", "--fast-model", "test",
        "--tuning-profile", "glm52-grounded", "--skip-epubcheck",
    ]) == 2
    assert not called
