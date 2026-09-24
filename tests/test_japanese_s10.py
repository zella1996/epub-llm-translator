"""S10: conservative Japanese short-text gating and migration boundaries."""

from __future__ import annotations

import pytest
import json
from pathlib import Path
from types import SimpleNamespace

from translator import cli
from translator.cli import main
from translator.epub_processor import EpubBook
from translator.llm_api import Usage
from translator.languages import JAPANESE_SOURCE, JAPANESE_TO_CHINESE, japanese_short_text_gate
from translator.languages import resolve_effective_language_configuration
from translator.epub_processor import EpubError
from translator.translator import translate_book, translate_reading_book
from translator.translator import TranslationEstimate, reading_assistance_from_manifest
from tests.helpers import build_epub


@pytest.mark.parametrize(
    "text",
    [
        "はい。",
        "「はい。」",
        "ありがとうございます。",
        "おはようございます。",
        "わかりました。",
    ],
)
def test_japanese_short_text_gate_skips_only_fixed_high_confidence_responses(text):
    decision = japanese_short_text_gate(text, mode="safe-v1", short_text_chars=0)

    assert not decision.needs_model
    assert decision.reason == "japanese-fixed-response"


@pytest.mark.parametrize(
    ("text", "metadata"),
    [
        ("行きますか。", {}),                         # question
        ("そうか……", {}),                           # ellipsis
        ("いいえ。", {}),                           # negation
        ("行かない。", {}),                         # negative predicate
        ("もし晴れたら。", {}),                     # condition
        ("しかし。", {}),                           # contrast
        ("わかりました", {}),                       # unfinished / no terminal mark
        ("それです。", {}),                         # demonstrative/reference
        ("太郎です。", {}),                         # proper name
        ("はい。", {"has_ruby": True}),             # ruby/proper-name metadata
        ("古池や\n蛙飛びこむ\n水の音", {"is_poetry": True}),  # poetry
    ],
)
def test_japanese_short_text_gate_keeps_every_required_risk_for_the_model(text, metadata):
    decision = japanese_short_text_gate(
        text, mode="safe-v1", short_text_chars=0, **metadata
    )

    assert decision.needs_model


def test_japanese_short_text_gate_off_never_skips_even_a_fixed_response():
    decision = japanese_short_text_gate("はい。", mode="off", short_text_chars=0)

    assert decision.needs_model
    assert decision.reason == "disabled"


def test_japanese_character_fallback_skips_short_text_regardless_of_type():
    decision = japanese_short_text_gate(
        "行きますか。", mode="off", short_text_chars=6, has_ruby=True
    )

    assert not decision.needs_model
    assert decision.reason == "japanese-short-text-chars"


def test_japanese_character_fallback_zero_disables_only_the_character_guard():
    decision = japanese_short_text_gate(
        "行きますか。", mode="off", short_text_chars=0
    )

    assert decision.needs_model
    assert decision.reason == "disabled"


def test_epub_paragraph_exposes_ruby_presence_to_the_gate_without_ruby_text():
    with EpubBook(Path("tests/fixtures/japanese-epub3.epub")) as book:
        plain, ruby = book.paragraphs()[:2]

    assert not plain.has_ruby
    assert ruby.has_ruby
    assert "に" not in ruby.text


def test_both_dry_run_pipelines_report_model_and_japanese_skip_coordinates(tmp_path):
    source = build_epub(
        tmp_path / "japanese-gate.epub",
        language_code="ja",
        custom_paragraphs=(
            "はい。",
            "行きますか。",
            "<ruby>はい<rt>ハイ</rt></ruby>。",
            "ありがとうございます。",
        ),
    )

    legacy = translate_book(
        source,
        tmp_path / "unused-legacy.epub",
        fast_model=object(),
        quality_model=object(),
        profile_key="stable-generation",
        cache_path=tmp_path / "legacy.sqlite",
        require_epubcheck=False,
        dry_run=True,
        language=JAPANESE_TO_CHINESE,
        source_language=JAPANESE_SOURCE,
    )
    reading = translate_reading_book(
        source,
        tmp_path / "unused-reading.epub",
        model=object(),
        profile_key="stable-generation",
        cache_path=tmp_path / "reading.sqlite",
        require_epubcheck=False,
        dry_run=True,
        source_language=JAPANESE_SOURCE,
    )

    for estimate in (legacy, reading):
        assert estimate.source_paragraphs == 4
        assert estimate.paragraphs == 0
        assert estimate.skipped_japanese_short_text == 4
        assert estimate.japanese_short_text_coordinates == (
            (1, 1), (1, 2), (1, 3), (1, 4)
        )
        assert estimate.skipped_japanese_short_text_chars == 4
        assert estimate.japanese_short_text_char_coordinates == (
            (1, 1), (1, 2), (1, 3), (1, 4)
        )
        assert estimate.skipped_japanese_fixed_responses == 0
        assert estimate.japanese_fixed_response_coordinates == ()


def test_cli_dry_run_reports_japanese_gate_separately_and_warns_there_is_no_fallback(
    monkeypatch, capsys
):
    estimate = TranslationEstimate(
        source_paragraphs=4,
        paragraphs=2,
        skipped_short_dialogue=0,
        skipped_short_prose=0,
        skipped_local_easy=0,
        approximate_requests=2,
        approximate_input_tokens=100,
        approximate_output_tokens=50,
        approximate_cost=None,
        approximate_seconds=None,
        skipped_japanese_short_text=2,
        japanese_short_text_coordinates=((1, 1), (1, 4)),
        skipped_japanese_short_text_chars=2,
        japanese_short_text_char_coordinates=((1, 1), (1, 4)),
        japanese_short_text_chars=6,
        skipped_japanese_fixed_responses=0,
        japanese_fixed_response_coordinates=(),
    )
    client = SimpleNamespace(usage=Usage())
    captured = {}
    monkeypatch.setattr(cli, "_clients", lambda args: (client, client, "stable", None))

    def fake_translate(*args, **kwargs):
        captured.update(kwargs)
        kwargs["on_estimate"](estimate)
        return estimate

    monkeypatch.setattr(cli, "translate_reading_book", fake_translate)

    assert main([
        "translate", "tests/fixtures/japanese-epub3.epub", "out.epub", "--pipeline", "reading-v1",
        "--base-url", "https://example.invalid/v1", "--fast-model", "fake",
        "--japanese-short-text-gate", "safe-v1", "--japanese-short-text-chars", "6", "--dry-run",
    ]) == 0

    output = capsys.readouterr().out
    assert captured["japanese_short_text_gate_mode"] == "safe-v1"
    assert captured["japanese_short_text_chars"] == 6
    assert "模型段落：2" in output
    assert "日文短文本跳过：2（1:1, 1:4）" in output
    assert "日文字数保底阈值：6" in output
    assert "日文字数保底跳过：2（1:1, 1:4）" in output
    assert "日文固定应答跳过：0（无）" in output
    assert "跳过段落不会生成译文或兜底内容" in output


def test_gate_run_identity_is_independent_from_generation_cache_identity():
    configuration = resolve_effective_language_configuration(["ja"])

    assert configuration.cache_identity["gate"] == "japanese-short-text-safe-v1"
    assert configuration.generation_identity["gate"] == "off-v1"
    assert configuration.run_identity("off", 0) == configuration.generation_identity
    assert configuration.run_identity("safe-v1", 0) == {
        **configuration.cache_identity,
        "gate_mode": "safe-v1",
    }
    assert configuration.run_identity("safe-v1", 30)["short_text_chars"] == "30"
    assert configuration.run_identity("safe-v1", 30)["short_text_char_gate"] == (
        "japanese-short-text-chars-v1"
    )


class _ReadingModel:
    def __init__(self):
        self.calls = 0

    def complete_json(self, _system, user):
        self.calls += 1
        payload = json.loads(user)
        return {
            "sentences": [
                {
                    "index": sentence["index"],
                    "translation": "测试译文。",
                    "difficulty": "fluent",
                }
                for sentence in payload["sentences"]
            ],
            "aids": [],
        }


def test_new_gate_reuses_old_generation_cache_with_zero_calls_but_rejects_old_manifest(
    tmp_path,
):
    source = build_epub(
        tmp_path / "migration.epub",
        language_code="ja",
        custom_paragraphs=(
            "はい。",
            "これは三十文字を超えるためモデル呼び出しのキャッシュ移行を確認する本文です。",
        ),
    )
    cache = tmp_path / "reading.sqlite"
    manifest = tmp_path / "old-off.manifest.json"
    configuration = resolve_effective_language_configuration(["ja"])
    first_model = _ReadingModel()

    first = translate_reading_book(
        source,
        tmp_path / "off.epub",
        model=first_model,
        profile_key="stable-generation",
        cache_path=cache,
        require_epubcheck=False,
        source_language=JAPANESE_SOURCE,
        japanese_short_text_gate_mode="off",
        japanese_short_text_chars=0,
        frozen_manifest=manifest,
        language_identity=configuration.run_identity("off", 0),
    )
    second_model = _ReadingModel()
    second = translate_reading_book(
        source,
        tmp_path / "safe.epub",
        model=second_model,
        profile_key="stable-generation",
        cache_path=cache,
        require_epubcheck=False,
        source_language=JAPANESE_SOURCE,
        japanese_short_text_gate_mode="safe-v1",
        language_identity=configuration.run_identity("safe-v1", 30),
    )

    assert first.generated_paragraphs == first_model.calls == 2
    assert second.cached_paragraphs == 1
    assert second.generated_paragraphs == second_model.calls == 0
    with EpubBook(source) as book:
        ref = book.paragraph(1, 2)
    with pytest.raises(EpubError, match="语言身份"):
        reading_assistance_from_manifest(
            manifest,
            source=source,
            ref=ref,
            language_identity=configuration.run_identity("safe-v1", 30),
            source_language=JAPANESE_SOURCE,
        )


def test_cli_gate_mode_changes_manifest_identity_not_generation_profile_key(
    monkeypatch, tmp_path
):
    estimate = TranslationEstimate(
        source_paragraphs=6,
        paragraphs=6,
        skipped_short_dialogue=0,
        skipped_short_prose=0,
        skipped_local_easy=0,
        approximate_requests=6,
        approximate_input_tokens=100,
        approximate_output_tokens=50,
        approximate_cost=None,
        approximate_seconds=None,
    )
    calls = []
    monkeypatch.setattr(cli, "_api_key", lambda args: "offline-test-key")

    def fake_translate(*args, **kwargs):
        calls.append(kwargs)
        return estimate

    monkeypatch.setattr(cli, "translate_reading_book", fake_translate)
    common = [
        "translate", "tests/fixtures/japanese-epub3.epub",
        str(tmp_path / "unused.epub"), "--pipeline", "reading-v1",
        "--base-url", "https://offline.invalid/v1", "--fast-model", "fake",
        "--dry-run", "--skip-epubcheck", "--progress", "off",
    ]

    assert main(common + ["--japanese-short-text-gate", "off"]) == 0
    assert main(common + ["--japanese-short-text-gate", "safe-v1"]) == 0

    assert calls[0]["profile_key"] == calls[1]["profile_key"]
    assert calls[0]["language_identity"]["gate"] == "off-v1"
    assert calls[1]["language_identity"]["gate"] == "japanese-short-text-safe-v1"
    assert calls[1]["language_identity"]["gate_mode"] == "safe-v1"
