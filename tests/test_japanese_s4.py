"""S4: versioned Japanese prompts and language-bound offline validation."""

from __future__ import annotations

import json

import pytest

from translator.epub_processor import EpubError
from translator.languages import (
    JAPANESE_SOURCE,
    JAPANESE_TO_CHINESE,
    resolve_effective_language_configuration,
)
from translator.reading_assistance import AssistanceError, parse_assistance
from translator.reading_eval import prepare_reading_input
from translator.reading_prompts import JAPANESE_PROMPT_VERSION, reading_system
from translator.trace import ParagraphTrace
from translator.translator import analyze_paragraph, generate_reading_assistance, replay_quality


class _QueueModel:
    def __init__(self, responses):
        self.responses = iter(responses)
        self.calls: list[tuple[str, str]] = []

    def complete_json(self, system, user):
        self.calls.append((system, user))
        return next(self.responses)


def _reading_payload(translation="太郎说你好。"):
    return {
        "sentences": [{"index": 1, "translation": translation, "difficulty": "effortful"}],
        "aids": [],
    }


def test_japanese_task_profile_and_prompts_are_independent_and_versioned():
    resolved = resolve_effective_language_configuration(["JA_jp"])

    assert resolved.language_profile == JAPANESE_TO_CHINESE.key
    assert resolved.cache_identity["validator"] == "japanese-output-v2"
    assert "敬语、谦让语和授受表达" in JAPANESE_TO_CHINESE.fast_system
    assert "主语、宾语和施事" in JAPANESE_TO_CHINESE.fast_system
    assert "拟声拟态词" in JAPANESE_TO_CHINESE.fast_system
    assert "汉字同形异义" in JAPANESE_TO_CHINESE.fast_system
    assert "ruby" in JAPANESE_TO_CHINESE.quality_system
    prompt = reading_system("review", source_language=JAPANESE_SOURCE)
    assert prompt.startswith(JAPANESE_PROMPT_VERSION)
    assert "giving/receiving" in prompt and "ruby base text" in prompt
    assert "Translate English" not in prompt
    assert "his sister" not in prompt


def test_japanese_default_pipeline_uses_shared_schema_and_japanese_validator():
    text = "先生に本を読んでもらった。太郎はにこにこ笑った。"
    fast = _QueueModel([{
        "translations": [
            {"index": 1, "translation": "老师给我读了书。"},
            {"index": 2, "translation": "太郎笑眯眯地笑了。"},
        ],
        "sentences": [
            {"index": 1, "difficulty": "effortful", "reason": "授受关系", "confidence": 0.8},
            {"index": 2, "difficulty": "fluent", "reason": "", "confidence": 1.0},
        ],
    }])
    quality = _QueueModel([{
        "sentences": [{"index": 1, "difficulty": "effortful", "meaning": "说话人接受老师替自己读书这一帮助。"}],
    }])

    result = analyze_paragraph(
        text, fast, quality, language=JAPANESE_TO_CHINESE, source_language=JAPANESE_SOURCE,
    )

    assert result.paragraph_translation == "老师给我读了书。太郎笑眯眯地笑了。"
    assert result.cards[0].sentence == "先生に本を読んでもらった。"
    assert "敬语、谦让语" in fast.calls[0][0]
    assert json.loads(quality.calls[0][1])["candidates"][0]["source"] == "先生に本を読んでもらった。"


@pytest.mark.parametrize("translation", ["<ruby>太郎<rt>たろう</rt></ruby>说话。", "｜太郎《たろう》说话。"])
def test_japanese_default_pipeline_rejects_ruby_readings(translation):
    fast = _QueueModel([{
        "translations": [{"index": 1, "translation": translation}],
        "sentences": [{"index": 1, "difficulty": "fluent", "reason": "", "confidence": 1.0}],
    }])
    with pytest.raises(EpubError, match="ruby"):
        analyze_paragraph("太郎が話した。", fast, _QueueModel([]),
                          language=JAPANESE_TO_CHINESE, source_language=JAPANESE_SOURCE)


def test_japanese_reading_v1_all_core_stages_use_japanese_prompt_and_only_japanese_validation():
    prepared = prepare_reading_input(
        {"paragraph": "太郎は言った。", "syntax": {}, "stanza_identity": "off-v1"},
        source_language=JAPANESE_SOURCE,
    )
    # An English-looking name/token is permitted for Japanese; English-only token
    # checks are not a proxy for Japanese translation quality.
    single = _QueueModel([_reading_payload("太郎说 hello。")])
    result = generate_reading_assistance(
        prepared, single, pipeline="single", trace=None, source_language=JAPANESE_SOURCE,
    )
    assert result.paragraph_translation == "太郎说 hello。"
    assert single.calls[0][0].startswith(JAPANESE_PROMPT_VERSION)

    two_stage = _QueueModel([_reading_payload(), _reading_payload()])
    generate_reading_assistance(prepared, two_stage, pipeline="two-stage", trace=None,
                                source_language=JAPANESE_SOURCE)
    assert "Generate initial sentence translations" in two_stage.calls[0][0]
    assert "Review the original paragraph and draft" in two_stage.calls[1][0]

    repair = _QueueModel([{"sentences": []}, _reading_payload()])
    repaired = generate_reading_assistance(prepared, repair, pipeline="single", trace=None,
                                           repair_core=True, source_language=JAPANESE_SOURCE)
    assert repaired.diagnostics[-1]["reason"] == "core_repair"
    assert repair.calls[1][0].startswith(JAPANESE_PROMPT_VERSION)


def test_japanese_reading_v1_keeps_shared_schema_errors_and_rejects_ruby_output():
    with pytest.raises(AssistanceError, match="missing sentence translations"):
        parse_assistance({"sentences": [], "aids": []}, "太郎が来た。", source_language=JAPANESE_SOURCE)
    with pytest.raises(AssistanceError, match="invalid sentence fields"):
        parse_assistance({"sentences": [{"index": 1, "translation": "太郎来了。"}], "aids": []},
                         "太郎が来た。", source_language=JAPANESE_SOURCE)
    with pytest.raises(AssistanceError, match="ruby"):
        parse_assistance(_reading_payload("<rt>たろう</rt>"), "太郎が来た。", source_language=JAPANESE_SOURCE)


def test_japanese_reading_v1_rejects_untranslated_deictic_reference():
    """A Japanese deictic must not become the nonsensical Chinese subject ``先``."""
    with pytest.raises(AssistanceError, match="日文指代词"):
        parse_assistance(
            _reading_payload("先并不是普通的女子。"),
            "先はただの女とは違う。",
            source_language=JAPANESE_SOURCE,
        )


def test_japanese_frozen_inputs_and_quality_replay_bind_off_v1_identity(tmp_path):
    with pytest.raises(AssistanceError, match="off-v1"):
        prepare_reading_input({"paragraph": "太郎が来た。", "syntax": {}}, source_language=JAPANESE_SOURCE)
    with pytest.raises(AssistanceError, match="must be off"):
        prepare_reading_input({"paragraph": "太郎が来た。", "syntax": {"1": {}}, "stanza_identity": "off-v1"}, source_language=JAPANESE_SOURCE)

    trace = ParagraphTrace(tmp_path / "trace")
    fast = _QueueModel([{
        "translations": [{"index": 1, "translation": "太郎来了。"}],
        "sentences": [{"index": 1, "difficulty": "effortful", "reason": "专名", "confidence": 0.7}],
    }])
    analyze_paragraph("太郎が来た。", fast, _QueueModel([{
        "sentences": [{"index": 1, "difficulty": "effortful", "meaning": "太郎来了。"}],
    }]), trace=trace, language=JAPANESE_TO_CHINESE, source_language=JAPANESE_SOURCE)
    replay = _QueueModel([{
        "sentences": [{"index": 1, "difficulty": "effortful", "meaning": "太郎到来了。"}],
    }])
    assert replay_quality(trace.directory / "quality-replay-input.json", replay,
                          language=JAPANESE_TO_CHINESE).cards[0].meaning == "太郎到来了。"

    payload_path = trace.directory / "quality-replay-input.json"
    payload = json.loads(payload_path.read_text(encoding="utf-8"))
    payload["language"]["syntax_identity"] = "english-syntax-v1"
    payload_path.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(EpubError, match="冻结语法身份"):
        replay_quality(payload_path, _QueueModel([]), language=JAPANESE_TO_CHINESE)
