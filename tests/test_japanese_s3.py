"""S3 boundaries: Japanese slices and processing scope are lossless and ungated."""

from __future__ import annotations

import pytest
from pathlib import Path

from translator.epub_processor import EpubError
from translator.languages import ENGLISH_TO_CHINESE, JAPANESE_SOURCE, split_source_sentences
from translator.reading_assistance import fingerprint, parse_assistance
from translator.sentence_analyzer import split_japanese_sentences
from translator.translator import analyze_paragraph, translate_book, translate_reading_book


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("「本当！？……」彼は言った。次は何？終わり！", ["「本当！？……」", "彼は言った。", "次は何？", "終わり！"]),
        ("「行く。」と言った。次だ。", ["「行く。」と言った。", "次だ。"]),
        ("所謂「生活」の外にいる。次だ。", ["所謂「生活」の外にいる。", "次だ。"]),
        ("彼は、（姉たちだろう）庭に立った。次だ。", ["彼は、（姉たちだろう）庭に立った。", "次だ。"]),
        ("「行く」\n「いや」", ["「行く」\n", "「いや」"]),
        ("彼は……ためらった。――それでも行く。", ["彼は……ためらった。", "――それでも行く。"]),
        ("価格は3.14。例はe.g.東京。Dr.山田は来た。", ["価格は3.14。", "例はe.g.東京。", "Dr.山田は来た。"]),
    ],
)
def test_japanese_splitter_preserves_exact_contiguous_source_slices(text, expected):
    sentences = split_japanese_sentences(text)

    assert [item.index for item in sentences] == list(range(1, len(expected) + 1))
    assert [item.text for item in sentences] == expected
    assert "".join(item.text for item in sentences) == text


class _QueueModel:
    def __init__(self, responses):
        self.responses = iter(responses)
        self.calls = 0

    def complete_json(self, _system, _user):
        self.calls += 1
        return next(self.responses)


def test_japanese_default_pipeline_does_not_add_english_candidates_or_suppress_cards():
    text = "短い。"
    fast = _QueueModel([{
        "translations": [{"index": 1, "translation": "很短。"}],
        "sentences": [{"index": 1, "difficulty": "fluent", "reason": "", "confidence": 1.0}],
    }])

    result = analyze_paragraph(
        text, fast, _QueueModel([]), language=ENGLISH_TO_CHINESE,
        source_language=JAPANESE_SOURCE,
    )

    assert result.cards == ()
    assert fast.calls == 1


def test_japanese_reading_indices_use_the_same_splitter_and_keep_short_sentence_aids():
    paragraph = "「はい。」次です。"
    payload = {
        "sentences": [
            {"index": 1, "translation": "是。", "difficulty": "fluent"},
            {"index": 2, "translation": "接下来。", "difficulty": "fluent"},
        ],
        "aids": [{"scope": "sentence", "sentence_indices": [1], "text": "短句也保留。"}],
    }

    assistance = parse_assistance(payload, paragraph, source_language=JAPANESE_SOURCE)

    assert [item.text for item in assistance.source_sentences] == ["「はい。」", "次です。"]
    assert assistance.aids[0].sentence_indices == (1,)
    assert assistance.input_identity == fingerprint({
        "paragraph": paragraph,
        "splitter": "japanese-exact-v3",
        "sentences": [
            {"index": 1, "text": "「はい。」"},
            {"index": 2, "text": "次です。"},
        ],
    })


def test_japanese_dry_runs_keep_all_non_explicitly_skipped_paragraphs(tmp_path):
    source = Path("tests/fixtures/japanese-epub3.epub")
    legacy = translate_book(
        source, tmp_path / "unused.epub", fast_model=object(), quality_model=object(),
        profile_key="test", cache_path=tmp_path / "legacy.sqlite3", dry_run=True,
        require_epubcheck=False, language=ENGLISH_TO_CHINESE,
        local_gate="balanced", short_dialogue_words=100, short_prose_words=100,
        source_language=JAPANESE_SOURCE, japanese_short_text_chars=0,
    )
    reading = translate_reading_book(
        source, tmp_path / "unused-reading.epub", model=object(), profile_key="test",
        cache_path=tmp_path / "reading.sqlite3", dry_run=True, require_epubcheck=False,
        local_gate="balanced", short_text_words=100, short_sentence_words=100,
        source_language=JAPANESE_SOURCE, japanese_short_text_chars=0,
    )

    assert legacy.paragraphs == legacy.source_paragraphs == 6
    assert reading.paragraphs == reading.source_paragraphs == 6
    assert legacy.skipped_short_dialogue == legacy.skipped_short_prose == legacy.skipped_local_easy == 0
    assert reading.skipped_short_dialogue == reading.skipped_short_prose == reading.skipped_local_easy == 0


def test_japanese_rejects_english_syntax_before_opening_a_transport(tmp_path):
    with pytest.raises(EpubError, match="固定为 off"):
        translate_book(
            "tests/fixtures/japanese-epub3.epub", tmp_path / "unused.epub",
            fast_model=object(), quality_model=object(), profile_key="test",
            cache_path=tmp_path / "cache.sqlite3", dry_run=True, require_epubcheck=False,
            language=ENGLISH_TO_CHINESE, syntax_analyzer=object(),
            source_language=JAPANESE_SOURCE,
        )


def test_source_strategy_does_not_guess_japanese_from_characters():
    assert [item.text for item in split_source_sentences(JAPANESE_SOURCE, "A.本当。 ")] == ["A.本当。 "]
