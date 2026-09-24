from __future__ import annotations

import threading
import time
import json
import hashlib
from types import SimpleNamespace

import pytest
from lxml import etree

from tests.helpers import build_epub
from translator.epub_processor import EpubBook, EpubError
from translator.sentence_analyzer import (
    ClauseHint,
    LocalSyntaxResult,
    Sentence,
    local_candidate_indices,
    local_model_gate,
    short_simple_paragraph_kind,
    short_text_paragraph_kind,
    split_sentences,
)
from translator.profile import CalibrationExample, ReadingProfile
from translator.languages import (
    DIRECT_MEANING,
    ENGLISH_TO_CHINESE,
    GLM52_MEANING_ONLY,
    GLM52_STRUCTURED_MEANING,
)
from translator.trace import ParagraphTrace
from translator.translator import (
    BookContext,
    TranslationEstimate,
    TranslationProgress,
    _syntax_from_dict,
    _syntax_to_dict,
    analyze_paragraph,
    analyze_paragraph_with_retries,
    generate_preview,
    generate_reading_preview,
    translate_reading_book,
    replay_quality,
    translate_book,
)


def _detached_analysis_tree(book: EpubBook) -> etree._ElementTree:
    chapter = etree.parse(str(book.root / "OEBPS/chapter.xhtml"))
    link = chapter.xpath("//*[contains(@class, 'epubllmt-analysis-ref')]")[0]
    return etree.parse(str(book.root / "OEBPS" / link.get("href").split("#", 1)[0]))


def _legacy_fast_fixture(response, user):
    """Keep pre-contract fixtures focused on the behavior they exercise."""
    if "translations 必须覆盖输入的每一个 index" not in user:
        return response
    if not isinstance(response, dict) or "translations" in response:
        return response
    paragraph_translation = response.get("paragraph_translation")
    assessments = response.get("sentences")
    if not isinstance(paragraph_translation, str) or not isinstance(assessments, list):
        return response
    indexes = [item.get("index") for item in assessments if isinstance(item, dict)]
    if not indexes or not all(isinstance(index, int) for index in indexes):
        return response
    return {
        **response,
        "translations": [
            {
                "index": index,
                "translation": paragraph_translation if offset == 0 else "。",
            }
            for offset, index in enumerate(indexes)
        ],
    }


class FakeModel:
    def __init__(self, response, *, adapt_fast=True):
        self.response = response
        self.adapt_fast = adapt_fast
        self.calls = []

    def complete_json(self, system, user):
        self.calls.append((system, user))
        return _legacy_fast_fixture(self.response, user) if self.adapt_fast else self.response


def _reading_payload(aids=None):
    return {"sentences": [
        {"index": 2, "translation": "她留下了。", "difficulty": "fluent"},
        {"index": 1, "translation": "他离开了。", "difficulty": "effortful"},
    ], "aids": [] if aids is None else aids}


def test_reading_synthetic_development_cases_selection_and_fixed_generation(tmp_path):
    from pathlib import Path
    from translator.reading_eval import evaluate_reading, prepare_reading_input
    fixture = Path(__file__).parent / "fixtures" / "reading_eval_cases.json"
    cases = json.loads(fixture.read_text(encoding="utf-8"))
    for case in cases:
        for mode in ("selection", "generation"):
            if mode == "generation" and not case["input"]["expected_aids"]:
                continue
            prepared = prepare_reading_input({**case["input"], "mode": mode})
            report = evaluate_reading(fixture, tmp_path / case["id"] / mode,
                FakeModel(case["response"], adapt_fast=False), pipeline="both",
                prepared=prepared, offline=True)
            assert len(report["runs"]) == 2
            assert all(run["status"] == "complete" and not run["missing_locations"]
                       for run in report["runs"])


@pytest.mark.parametrize("aid", [
    {"scope": "phrase", "sentence_indices": [1], "quote": "left", "text": "离开"},
    {"scope": "sentence", "sentence_indices": [1], "show_translation": True},
    {"scope": "sentence", "sentence_indices": [1], "text": "动作已发生。"},
    {"scope": "sentence", "sentence_indices": [1], "text": "动作已发生。", "show_translation": True},
    {"scope": "paragraph", "sentence_indices": [2, 1], "text": "两人的去留形成对照。"},
])
def test_reading_contract_shared_translation_roundtrip(aid):
    from translator.reading_assistance import ParagraphAssistance, parse_assistance
    result = parse_assistance(_reading_payload([aid, aid]), "He left. She stayed.")
    assert result.paragraph_translation == "他离开了。她留下了。"
    assert len(result.aids) == 1
    assert result.diagnostics[0]["reason"] == "duplicate aid"
    assert ParagraphAssistance.from_dict(result.to_dict(), "He left. She stayed.") == result


def test_reading_contract_ignores_unknown_sentence_fields():
    from translator.reading_assistance import parse_assistance

    payload = _reading_payload()
    payload["sentences"][0]["unexpected"] = "discard me"

    result = parse_assistance(payload, "He left. She stayed.")

    assert result.paragraph_translation == "他离开了。她留下了。"
    assert all("unexpected" not in sentence for sentence in result.to_dict()["sentences"])


def test_reading_short_sentence_filter_keeps_translations_and_paragraph_aids():
    from translator.reading_assistance import (
        parse_assistance,
        without_short_sentence_aids,
    )

    paragraph = (
        "Is she still in town? "
        "Although the unexpected message arrived before breakfast, everyone in "
        "the crowded room understood why she remained unusually silent."
    )
    assistance = parse_assistance({
        "sentences": [
            {"index": 1, "translation": "她还在城里吗？", "difficulty": "fluent"},
            {"index": 2, "translation": "那封意外的消息早餐前便到了，屋里所有人都明白她为何异常沉默。", "difficulty": "effortful"},
        ],
        "aids": [
            {"scope": "phrase", "sentence_indices": [1], "quote": "in town", "text": "在本地。"},
            {"scope": "sentence", "sentence_indices": [2], "text": "先让步，再给主句。"},
            {"scope": "paragraph", "sentence_indices": [1, 2], "text": "问句与后文形成承接。"},
        ],
    }, paragraph)

    filtered = without_short_sentence_aids(assistance, max_words=15)

    assert filtered.sentences == assistance.sentences
    assert filtered.paragraph_translation == assistance.paragraph_translation
    assert [(aid.scope, aid.sentence_indices) for aid in filtered.aids] == [
        ("sentence", (2,)),
        ("paragraph", (1, 2)),
    ]


def test_reading_generation_disables_paragraph_aids_in_prompt_and_result():
    from translator.reading_eval import prepare_reading_input
    from translator.translator import generate_reading_assistance

    model = FakeModel(_reading_payload([
        {
            "scope": "paragraph",
            "sentence_indices": [1, 2],
            "text": "两句构成对照。",
        },
        {
            "scope": "sentence",
            "sentence_indices": [1],
            "text": "动作已经发生。",
        },
    ]), adapt_fast=False)
    prepared = prepare_reading_input({
        "paragraph": "He left. She stayed.",
        "paragraph_aids": False,
    })

    result = generate_reading_assistance(
        prepared, model, pipeline="single", trace=None,
    )

    assert "Paragraph aids are disabled" in model.calls[0][0]
    assert json.loads(model.calls[0][1])["paragraph_aids"] is False
    assert [aid.scope for aid in result.aids] == ["sentence"]


def test_reading_input_binds_excluded_aid_sentence_indices():
    from translator.reading_assistance import AssistanceError
    from translator.reading_eval import prepare_reading_input

    prepared = prepare_reading_input({
        "paragraph": "He left. She stayed.",
        "excluded_aid_sentence_indices": [2, 1],
    })
    assert prepared["excluded_aid_sentence_indices"] == [1, 2]
    with pytest.raises(AssistanceError, match="excluded aid"):
        prepare_reading_input({
            "paragraph": "He left. She stayed.",
            "excluded_aid_sentence_indices": [1, 1],
        })


@pytest.mark.parametrize("aid", [
    {"scope": "phrase", "sentence_indices": [1], "quote": "left. She", "text": "错误"},
    {"scope": "phrase", "sentence_indices": [1], "quote": "missing", "text": "错误"},
    {"scope": "sentence", "sentence_indices": [True], "show_translation": True},
    {"scope": "sentence", "sentence_indices": [1], "show_translation": "true"},
    {"scope": "sentence", "sentence_indices": [1]},
    {"scope": "paragraph", "sentence_indices": [1], "text": "单句不是跨句关系。"},
    {"scope": "paragraph", "sentence_indices": [3], "text": "错误"},
    {"scope": "paragraph", "sentence_indices": [1, 1], "text": "错误"},
    {"scope": [], "sentence_indices": [1]},
    {"scope": "sentence", "sentence_indices": [1], "text": "长" * 4001},
    None,
])
def test_reading_invalid_aids_degrade_without_losing_translation(aid):
    from translator.reading_assistance import ParagraphAssistance, parse_assistance
    valid = {"scope": "sentence", "sentence_indices": [2], "show_translation": True}
    result = parse_assistance(_reading_payload([aid, valid]), "He left. She stayed.")
    assert result.status == "partial_aids"
    assert len(result.aids) == 1
    assert ParagraphAssistance.from_dict(result.to_dict(), "He left. She stayed.") == result


@pytest.mark.parametrize("locator", ["第一句", "第 2 句", "上一句", "下一句"])
def test_reading_aids_reject_sentence_ordinal_locators(locator):
    from translator.reading_assistance import parse_assistance

    aid = {
        "scope": "paragraph",
        "sentence_indices": [1, 2],
        "text": f"{locator}中的代词承接另一句。",
    }
    result = parse_assistance(_reading_payload([aid]), "He left. She stayed.")
    assert result.status == "partial_aids"
    assert not result.aids
    assert result.diagnostics == ({"position": 0, "reason": "aid uses sentence ordinal locator"},)


@pytest.mark.parametrize("aids, status", [([], "complete"), (None, "translation_only"), ({}, "translation_only")])
def test_reading_empty_and_broken_collection_distinct(aids, status):
    from translator.reading_assistance import ParagraphAssistance, parse_assistance
    payload = {**_reading_payload(), "aids": aids}
    result = parse_assistance(payload, "He left. She stayed.")
    assert result.status == status
    assert ParagraphAssistance.from_dict(result.to_dict(), "He left. She stayed.") == result


@pytest.mark.parametrize("change", ["bool", "duplicate", "missing", "outside", "difficulty", "empty"])
def test_reading_core_errors_are_not_success(change):
    from translator.reading_assistance import AssistanceError, parse_assistance
    payload = _reading_payload()
    if change == "bool": payload["sentences"][0]["index"] = True
    if change == "duplicate": payload["sentences"][0]["index"] = 1
    if change == "missing": payload["sentences"].pop()
    if change == "outside": payload["sentences"][0]["index"] = 3
    if change == "difficulty": payload["sentences"][0]["difficulty"] = []
    if change == "empty": payload["sentences"][0]["translation"] = " "
    with pytest.raises(AssistanceError):
        parse_assistance(payload, "He left. She stayed.")


def test_reading_phrase_exact_unique_extracted_text():
    from lxml import etree
    from translator.reading_assistance import parse_assistance
    source = "".join(etree.fromstring(b"<p>He <em>gave</em> up.</p>").itertext())
    payload = {"sentences": [{"index": 1, "translation": "他放弃了。", "difficulty": "fluent"}],
               "aids": [{"scope": "phrase", "sentence_indices": [1], "quote": "gave up", "text": "放弃"}]}
    assert parse_assistance(payload, source).status == "complete"
    payload["aids"][0]["quote"] = "ha"
    assert parse_assistance(payload, "ha ha").status == "partial_aids"
    payload["aids"][0]["quote"] = "aa"
    assert parse_assistance(payload, "aaa").status == "partial_aids"


def test_reading_phrase_quote_matches_layout_whitespace_and_binds_source_text():
    from translator.reading_assistance import parse_assistance

    source = "I will find none in\n  accommodating them."
    payload = {
        "sentences": [
            {"index": 1, "translation": "我接待他们也不会有困难。", "difficulty": "effortful"}
        ],
        "aids": [{
            "scope": "phrase",
            "sentence_indices": [1],
            "quote": "I will find none in accommodating them",
            "text": "排版换行不改变这段原文。",
        }],
    }

    result = parse_assistance(payload, source)

    assert result.status == "complete"
    assert result.aids[0].quote == "I will find none in\n  accommodating them"


def test_reading_fronted_inverted_conditional_forces_focused_review():
    from translator.reading_assistance import parse_assistance
    from translator.translator import needs_reading_review, review_reading_assistance

    paragraph = (
        "For the comfort of her children, had she consulted only her own wishes, "
        "she would have kept it; but Elinor prevailed."
    )
    payload = {
        "sentences": [{
            "index": 1,
            "translation": "为了孩子们舒适，若只顾自己，她本会留下它；但埃莉诺占了上风。",
            "difficulty": "effortful",
        }],
        "aids": [],
    }
    prepared = {"paragraph": paragraph, "mode": "selection", "sentences": []}
    draft = parse_assistance(payload, paragraph)

    assert needs_reading_review(draft, prepared)

    model = FakeModel(payload, adapt_fast=False)
    review_reading_assistance(prepared, draft, model, trace=None)
    request = json.loads(model.calls[0][1])
    assert "counterfactual main clause" in request["review_focus"]
    assert "actual action" in request["review_focus"]


def test_reading_saved_source_and_version_are_bound():
    from translator.reading_assistance import AssistanceError, ParagraphAssistance, parse_assistance
    result = parse_assistance(_reading_payload(), "He left. She stayed.")
    with pytest.raises(AssistanceError, match="identity"):
        ParagraphAssistance.from_dict(result.to_dict(), "She left. He stayed.")
    with pytest.raises(AssistanceError, match="schema"):
        ParagraphAssistance.from_dict({**result.to_dict(), "schema_version": "legacy"}, "He left. She stayed.")


@pytest.mark.parametrize("changes", [
    {"sentences": [{"index": True, "text": "He left."}]},
    {"syntax": {"2": {}}},
    {"syntax": {"1": {}}},
])
def test_reading_frozen_input_rejects_boolean_or_unbound_evidence(changes):
    from translator.reading_assistance import AssistanceError
    from translator.reading_eval import prepare_reading_input
    with pytest.raises(AssistanceError):
        prepare_reading_input({"paragraph": "He left.", **changes})


@pytest.mark.parametrize("mode", ["selection", "generation"])
def test_reading_both_paths_share_evidence_and_report_omissions(tmp_path, mode):
    from translator.reading_eval import evaluate_reading, prepare_reading_input
    source = tmp_path / "input.json"
    prepared = prepare_reading_input({"paragraph": "He left. She stayed.", "mode": mode,
        "sentences": [{"index": 1, "text": "He left."}, {"index": 2, "text": "She stayed."}],
        "syntax": {"1": {"reference": "frozen"}}, "stanza_identity": "test-frozen-v1",
        "expected_aids": [{"scope": "paragraph", "sentence_indices": [1, 2]}]})
    source.write_text(json.dumps(prepared))
    model = FakeModel(_reading_payload(), adapt_fast=False)
    report = evaluate_reading(source, tmp_path / "trace", model, pipeline="both", prepared=prepared, offline=True)
    assert report["call_count"] == 3
    assert len(report["runs"]) == 2
    assert all(run["missing_locations"] == prepared["expected_aids"] for run in report["runs"])
    users = [json.loads(user) for _, user in model.calls]
    assert all(user["syntax"] == prepared["syntax"] for user in users)
    assert all("expected_aids" not in user for user in users)
    assert all(("required_aids" in user) == (mode == "generation") for user in users)
    assert users[2]["draft"]["sentences"][0]["index"] == 1
    assert report["total_tokens"] is None and report["cost"] is None


@pytest.mark.parametrize("responses, expected_calls", [(["not json"], 1), ([_reading_payload(), {}], 2)])
def test_reading_failure_preserves_raw_and_stops(tmp_path, responses, expected_calls):
    from translator.reading_eval import FrozenResponseModel, evaluate_reading, prepare_reading_input
    source = tmp_path / "input.json"
    source.write_text('{}')
    report = evaluate_reading(source, tmp_path / "traces", FrozenResponseModel(responses),
        pipeline="two-stage", prepared=prepare_reading_input({"paragraph": "He left. She stayed."}), offline=True)
    assert report["call_count"] == expected_calls
    assert report["runs"][0]["status"] == "failure"
    assert report["calls"][-1]["provider_responses"][0]["offline_content"] == responses[-1]


def test_reading_provider_usage_failure_and_redaction(tmp_path):
    from translator.llm_api import OpenAICompatibleClient, OpenAIConfig
    from translator.reading_eval import evaluate_reading, prepare_reading_input
    secret = "test-secret-never-persist"
    bodies = iter([
        {"choices": [{"message": {"content": json.dumps(_reading_payload())}}],
         "usage": {"total_tokens": 12, "prompt_tokens": 8, "completion_tokens": 4, "cost": 0.01}},
        {"choices": [{"message": {"content": secret}}],
         "usage": {"total_tokens": 13, "prompt_tokens": 8, "completion_tokens": 5}},
    ])
    model = OpenAICompatibleClient(OpenAIConfig(base_url="https://example.invalid/v1", model="fake",
        api_key=secret, max_retries=0), transport=lambda *args: next(bodies))
    source = tmp_path / "input.json"
    source.write_text('{}')
    report = evaluate_reading(source, tmp_path / "traces", model, pipeline="two-stage",
        prepared=prepare_reading_input({"paragraph": "He left. She stayed."}), offline=False)
    assert report["total_tokens"] == 25
    assert report["cost"] is None  # Missing provider cost is not zero.
    assert report["runs"][0]["status"] == "failure"
    assert secret not in json.dumps(report)


@pytest.mark.parametrize("first_failure", ["empty-json", "timeout"])
def test_openai_client_retries_transient_reading_transport_failures(
    monkeypatch, first_failure,
):
    import translator.llm_api as llm_api
    from translator.llm_api import OpenAICompatibleClient, OpenAIConfig

    calls = 0

    def transport(*args):
        nonlocal calls
        calls += 1
        if calls == 1:
            if first_failure == "timeout":
                raise TimeoutError("The read operation timed out")
            return {"choices": [{"message": {"content": ""}}]}
        return {"choices": [{"message": {"content": '{"ok": true}'}}]}

    monkeypatch.setattr(llm_api.time, "sleep", lambda _seconds: None)
    client = OpenAICompatibleClient(
        OpenAIConfig(
            base_url="https://example.invalid/v1",
            model="fake",
            max_retries=1,
        ),
        transport=transport,
    )

    assert client.complete_json("system", "user") == {"ok": True}
    assert calls == 2


def test_openai_client_escalates_empty_length_response_headroom(monkeypatch):
    import translator.llm_api as llm_api
    from translator.llm_api import OpenAICompatibleClient, OpenAIConfig

    payloads = []

    def transport(_endpoint, payload, _headers, _timeout):
        payloads.append(dict(payload))
        if len(payloads) == 1:
            return {
                "choices": [{
                    "finish_reason": "length",
                    "message": {"content": "", "reasoning_content": "thinking"},
                }],
                "usage": {"completion_tokens": 20000},
            }
        return {"choices": [{"message": {"content": '{"ok": true}'}}]}

    monkeypatch.setattr(llm_api.time, "sleep", lambda _seconds: None)
    client = OpenAICompatibleClient(OpenAIConfig(
        base_url="https://example.invalid/v1",
        model="fake",
        max_retries=3,
        max_output_tokens=20000,
        length_retry_max_output_tokens=30000,
    ), transport=transport)

    assert client.complete_json("system", "user") == {"ok": True}
    assert [payload["max_tokens"] for payload in payloads] == [20000, 30000]


def test_openai_client_stops_when_length_retry_headroom_is_exhausted(monkeypatch):
    import translator.llm_api as llm_api
    from translator.llm_api import LLMError, OpenAICompatibleClient, OpenAIConfig

    payloads = []

    def transport(_endpoint, payload, _headers, _timeout):
        payloads.append(dict(payload))
        return {
            "choices": [{
                "finish_reason": "length",
                "message": {"content": "", "reasoning_content": "thinking"},
            }],
            "usage": {"completion_tokens": payload["max_tokens"]},
        }

    monkeypatch.setattr(llm_api.time, "sleep", lambda _seconds: None)
    client = OpenAICompatibleClient(OpenAIConfig(
        base_url="https://example.invalid/v1",
        model="fake",
        max_retries=3,
        max_output_tokens=20000,
        length_retry_max_output_tokens=30000,
    ), transport=transport)

    with pytest.raises(LLMError, match="输出 token 余量耗尽"):
        client.complete_json("system", "user")
    assert [payload["max_tokens"] for payload in payloads] == [20000, 30000]


class StubSyntaxAnalyzer:
    def __init__(self, result_by_index):
        self.result_by_index = result_by_index
        self.calls = []

    def analyze(self, sentences):
        self.calls.append(sentences)
        return self.result_by_index


class FailingSyntaxAnalyzer:
    def __init__(self, message="stanza stopped"):
        self.message = message
        self.calls = []

    def analyze(self, sentences):
        self.calls.append(sentences)
        raise RuntimeError(self.message)


class SequenceModel:
    def __init__(self, responses, *, adapt_fast=True):
        self.responses = iter(responses)
        self.adapt_fast = adapt_fast
        self.calls = []

    def complete_json(self, system, user):
        self.calls.append((system, user))
        response = next(self.responses)
        return _legacy_fast_fixture(response, user) if self.adapt_fast else response


class FailingModel:
    def __init__(self, message="provider stopped"):
        self.message = message
        self.calls = []

    def complete_json(self, system, user):
        self.calls.append((system, user))
        raise RuntimeError(self.message)


class CoordinatedFailingFastModel:
    def __init__(self):
        self.second_started = threading.Event()
        self.calls = []

    def complete_json(self, system, user):
        self.calls.append((system, user))
        if "This is the original paragraph." in user:
            assert self.second_started.wait(timeout=2)
            raise RuntimeError("provider stopped")
        self.second_started.set()
        time.sleep(0.05)
        return _legacy_fast_fixture(
            {
                "paragraph_translation": "第二段有一条作者注释。",
                "sentences": [
                    {
                        "index": 1,
                        "text": "A second paragraph has an author's note.",
                        "difficulty": "effortful",
                    }
                ],
            },
            user,
        )


class CandidateContractModel:
    """Mimic a model that omits fluent candidates unless explicitly told not to."""

    def __init__(self):
        self.calls = []

    def complete_json(self, system, user):
        self.calls.append((system, user))
        sentences = [
            {
                "index": 1,
                "difficulty": "effortful",
                "meaning": "尽管疲惫，他仍继续。",
            }
        ]
        if "包括 fluent" in user:
            sentences.append(
                {
                    "index": 2,
                    "difficulty": "fluent",
                }
            )
        return {"sentences": sentences}


class BookFastModel:
    def __init__(self, *, fail_on_second=False):
        self.fail_on_second = fail_on_second
        self.calls = []

    def complete_json(self, system, user):
        self.calls.append((system, user))
        if "This is the original paragraph." in user:
            response = {
                "paragraph_translation": "这是原段落。尽管它很长，但意思仍可验证。",
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
            }
            return _legacy_fast_fixture(response, user)
        if self.fail_on_second:
            raise RuntimeError("provider stopped")
        response = {
            "paragraph_translation": "第二段有一条作者注释。",
            "sentences": [
                {
                    "index": 1,
                    "text": "A second paragraph has an author's note.",
                    "difficulty": "fluent",
                }
            ],
        }
        return _legacy_fast_fixture(response, user)


class InterruptingBookFastModel(BookFastModel):
    def complete_json(self, system, user):
        if "A second paragraph has an author's note." in user:
            self.calls.append((system, user))
            raise KeyboardInterrupt()
        return super().complete_json(system, user)


def test_system_prompts_require_reference_preservation():
    assert "he、she、they" not in ENGLISH_TO_CHINESE.fast_system
    assert "sister、brother、mother、father" in ENGLISH_TO_CHINESE.fast_system
    assert "普通人称代词按中文语法" in ENGLISH_TO_CHINESE.fast_system
    assert "直接保留在中文译文中" in ENGLISH_TO_CHINESE.fast_system
    assert "不翻译、不转写、不替换、不省略" in ENGLISH_TO_CHINESE.fast_system
    assert "he、she、they" not in ENGLISH_TO_CHINESE.quality_system
    assert "sister、brother、mother、father" in ENGLISH_TO_CHINESE.quality_system
    assert "普通人称代词按中文语法" in ENGLISH_TO_CHINESE.quality_system
    assert "直接保留" in ENGLISH_TO_CHINESE.quality_system
    assert "不翻译、不转写、不替换、不省略" in ENGLISH_TO_CHINESE.quality_system
    assert "同一候选原句" in ENGLISH_TO_CHINESE.quality_system
    assert "每个索引恰好返回一次" in GLM52_MEANING_ONLY.quality_system
    assert "包括最终判断为 fluent 的候选" in GLM52_MEANING_ONLY.quality_system


def test_quality_book_context_omits_unknown_work_year():
    assert BookContext().quality_prompt_payload() == {}
    assert BookContext(work_year=1811).quality_prompt_payload() == {"work_year": 1811}


def test_quality_request_requires_note_relationship_to_be_stated_in_same_sentence():
    fast = FakeModel(
        {
            "paragraph_translation": "Edward 已抵达。",
            "sentences": [{"index": 1, "difficulty": "effortful"}],
        }
    )
    quality = FakeModel(
        {
            "sentences": [
                {
                    "index": 1,
                    "difficulty": "effortful",
                    "meaning": "Edward 已抵达。",
                    "cues": "简单陈述。",
                    "feel": "",
                    "note": "",
                }
            ]
        }
    )
    analyze_paragraph("Edward arrived.", fast, quality)
    assert "亲属关系断言必须由该 source 明确写出" in quality.calls[0][1]


def test_quality_card_rejects_motive_inference_from_as_if_attribution():
    text = "As if he felt the necessity of instant exertion, he recovered himself."
    fast = FakeModel(
        {
            "paragraph_translation": "仿佛觉得必须立刻振作起来，他恢复了镇定。",
            "sentences": [{"index": 1, "difficulty": "effortful"}],
        }
    )
    quality = FakeModel(
        {
            "sentences": [
                {
                    "index": 1,
                    "difficulty": "effortful",
                    "meaning": "他强撑镇定，敷衍一句后想要脱身。",
                    "cues": "这是他为了脱身的客套话。",
                    "feel": "",
                    "note": "",
                }
            ]
        }
    )

    with pytest.raises(EpubError, match="as if 的假设性归因"):
        analyze_paragraph(text, fast, quality)


def _obsolete_glm_meaning_only_cards_discard_extra_explanation_fields():
    fast = FakeModel(
        {
            "paragraph_translation": "他谄媚的举止令她不安。",
            "sentences": [{"index": 1, "difficulty": "effortful"}],
        }
    )
    quality = FakeModel(
        {
            "sentences": [
                {
                    "index": 1,
                    "difficulty": "effortful",
                    "basis": "vocabulary",
                    "meaning": "他谄媚的举止令她不安。",
                    "cues": "模型擅自返回的线索。",
                    "feel": "模型擅自返回的语感。",
                    "note": "模型擅自返回的提示。",
                }
            ]
        }
    )

    result = analyze_paragraph(
        "His obsequious demeanor troubled her.",
        fast,
        quality,
        language=GLM52_MEANING_ONLY,
        syntax_analyzer=StubSyntaxAnalyzer({}),
    )

    assert len(result.cards) == 1
    assert result.cards[0].meaning == "他谄媚的举止令她不安。"
    assert result.cards[0].cues == result.cards[0].feel == result.cards[0].note == ""
    assert "不要返回 cues、feel、note" in quality.calls[0][1]


def test_quality_request_sends_only_candidate_sources_and_book_year():
    text = (
        "He waited by the door. "
        "Although tired, brother continued. "
        "She smiled at last."
    )
    profile = ReadingProfile(
        examples=(
            CalibrationExample("A clear short sentence.", "fluent"),
            CalibrationExample(
                "A much longer sentence whose layered structure is effortful.",
                "effortful",
            ),
        )
    )
    syntax = StubSyntaxAnalyzer(
        {
            2: LocalSyntaxResult(
                needs_structure=True,
                reasons=("subordinate-clause",),
                hints=(
                    ClauseHint("advcl", "Although tired", "continued", 1),
                ),
            )
        }
    )
    fast = FakeModel(
        {
            "paragraph_translation": "他在门边等着。尽管疲惫，brother 仍继续。他终于笑了。",
            "sentences": [
                {"index": 1, "difficulty": "fluent", "reason": "直接", "confidence": 0.9},
                {"index": 2, "difficulty": "effortful", "reason": "让步从句", "confidence": 0.8},
                {"index": 3, "difficulty": "fluent", "reason": "直接", "confidence": 0.9},
            ],
        }
    )
    quality = FakeModel(
        {
            "sentences": [
                {
                    "index": 2,
                    "difficulty": "effortful",
                    "basis": "structure",
                    "meaning": "尽管疲惫，brother 仍继续。",
                }
            ]
        }
    )

    analyze_paragraph(
        text,
        fast,
        quality,
        profile=profile,
        book_context=BookContext("A Title", ("An Author",), 1811),
        language=GLM52_MEANING_ONLY,
        syntax_analyzer=syntax,
    )

    user = quality.calls[0][1]
    assert "复核候选" in user
    assert "每个给定 index 必须恰好返回一次，包括 fluent" in user
    assert "段落：" not in user
    assert "全部句子：" not in user
    assert "候选索引：" not in user
    assert "初步判断：" not in user
    assert '"source": "Although tired, brother continued."' in user
    assert '"initial_difficulty": "effortful"' in user
    assert "He waited by the door." not in user
    assert "She smiled at last." not in user
    assert '"reason": "让步从句"' not in user
    assert '"confidence": 0.8' not in user
    assert '"title": "A Title"' not in user
    assert '"authors": ["An Author"]' not in user
    assert '"work_year": 1811' in user
    assert "读者难度阈值：" not in user
    assert "A clear short sentence." not in user
    assert '"clause": "Although tired"' in user
    assert '"target": "continued"' in user


def _obsolete_legacy_v20_quality_payload_remains_available_for_controlled_ab_test():
    fast = FakeModel(
        {
            "paragraph_translation": "尽管疲惫，brother 仍继续。她微笑了。",
            "sentences": [
                {"index": 1, "difficulty": "effortful", "reason": "让步", "confidence": 0.8},
                {"index": 2, "difficulty": "fluent", "reason": "直接", "confidence": 0.9},
            ],
        }
    )
    quality = FakeModel(
        {
            "sentences": [
                {
                    "index": 1,
                    "difficulty": "effortful",
                    "basis": "structure",
                    "meaning": "尽管疲惫，brother 仍继续。",
                }
            ]
        }
    )

    analyze_paragraph(
        "Although tired, brother continued. She smiled.",
        fast,
        quality,
        book_context=BookContext("A Title", ("An Author",), 1811),
        language=GLM52_MEANING_ONLY,
        syntax_analyzer=StubSyntaxAnalyzer({}),
        quality_payload_mode="legacy-v20",
    )

    user = quality.calls[0][1]
    assert "段落：Although tired, brother continued. She smiled." in user
    assert "全部句子：" in user
    assert "候选索引：[1]" in user
    assert '"reason": "让步"' in user
    assert '"confidence": 0.8' in user
    assert '"title": "A Title"' in user
    assert '"authors": ["An Author"]' in user


def test_quality_request_filters_stanza_evidence_to_existing_candidates():
    text = (
        "Although tired, he continued. "
        "Because it rained, she stayed. "
        "They later rested."
    )
    syntax = StubSyntaxAnalyzer(
        {
            1: LocalSyntaxResult(
                hints=(ClauseHint("advcl", "Although tired", "continued", 1),),
            ),
            3: LocalSyntaxResult(
                hints=(ClauseHint("advcl", "ignored local hint", "rested", 1),),
            ),
        }
    )
    fast = FakeModel(
        {
            "paragraph_translation": "尽管疲惫，他仍继续。因为下雨，她留了下来。他们后来休息了。",
            "sentences": [
                {"index": 1, "difficulty": "effortful"},
                {"index": 2, "difficulty": "effortful"},
                {"index": 3, "difficulty": "fluent"},
            ],
        }
    )
    quality = FakeModel(
        {
            "sentences": [
                {
                    "index": 1,
                    "difficulty": "effortful",
                    "basis": "structure",
                    "meaning": "尽管疲惫，他仍继续。",
                },
                {
                    "index": 2,
                    "difficulty": "effortful",
                    "basis": "structure",
                    "meaning": "因为下雨，她留了下来。",
                },
            ]
        }
    )

    analyze_paragraph(
        text,
        fast,
        quality,
        language=GLM52_MEANING_ONLY,
        syntax_analyzer=syntax,
    )

    user = quality.calls[0][1]
    assert "复核唯一候选" not in user
    assert "每个给定 index 必须恰好返回一次，包括 fluent" in user
    assert '返回 JSON 对象，顶层键为 "sentences"，不要 Markdown。' in user
    assert '"index": 1, "source": "Although tired, he continued."' in user
    assert '"index": 2, "source": "Because it rained, she stayed."' in user
    assert '"clause": "Although tired"' in user
    assert "ignored local hint" not in user


def test_sentence_split_keeps_abbreviation_and_exact_text():
    sentences = split_sentences("Dr. Smith paused. Although tired, he continued.")
    assert [item.text for item in sentences] == [
        "Dr. Smith paused.",
        "Although tired, he continued.",
    ]
    assert local_candidate_indices(sentences) == {2}


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ('“I really do not know what you expect me to say.”', "dialogue"),
        ("He merely shook his head and looked away.", "prose"),
        ("Although tired, he continued.", None),
        ('“I would answer if I knew what she intended.”', None),
        ("He stopped. She waited.", None),
    ],
)
def test_short_simple_paragraph_policy(text, expected):
    assert short_simple_paragraph_kind(text) == expected


def test_short_simple_paragraph_policy_can_be_disabled_per_threshold():
    assert short_simple_paragraph_kind(
        '“I do not know.”', dialogue_words=0, prose_words=0
    ) is None


def test_conservative_local_gate_skips_only_high_confidence_easy_text():
    easy = local_model_gate(
        "He walked to the window and looked out across the quiet garden."
    )
    assert not easy.needs_model
    assert easy.reason == "local-easy"

    lexical = local_model_gate("His obsequious demeanor troubled her.")
    assert lexical.needs_model
    assert lexical.reason == "lexical-signal"

    structural = local_model_gate("He stayed because she asked him to.")
    assert structural.needs_model
    assert structural.reason == "clause-signal"


def test_short_wh_question_is_locally_easy_not_a_clause_signal():
    result = local_model_gate("Who regards me?")

    assert not result.needs_model
    assert result.reason == "short-prose"


def test_locally_easy_effortful_sentence_does_not_render_a_card():
    text = (
        "Yes, why should I stay here? "
        "I came only for Willoughby's sake—and now who cares for me? "
        "Who regards me?"
    )
    fast = FakeModel(
        {
            "paragraph_translation": "是的，我为什么留下？我只为他而来——谁在乎我？谁关心我？",
            "sentences": [
                {"index": 1, "difficulty": "fluent"},
                {"index": 2, "difficulty": "fluent"},
                {"index": 3, "difficulty": "effortful"},
            ],
        }
    )
    quality = FakeModel(
        {
            "sentences": [
                {
                    "index": 2,
                    "difficulty": "fluent",
                },
                {
                    "index": 3,
                    "difficulty": "effortful",
                    "basis": "structure",
                    "meaning": "谁关心我？",
                }
            ]
        }
    )

    result = analyze_paragraph(
        text,
        fast,
        quality,
        language=GLM52_MEANING_ONLY,
        syntax_analyzer=StubSyntaxAnalyzer({}),
    )

    assert result.cards == ()


@pytest.mark.parametrize(
    ("difficulty", "profile"),
    [
        ("blocking", None),
        (
            "effortful",
            ReadingProfile((CalibrationExample("Who regards me?", "effortful"),)),
        ),
    ],
)
def test_local_easy_card_veto_preserves_blocking_and_profiled_hard_sentences(
    difficulty, profile
):
    fast = FakeModel(
        {
            "paragraph_translation": "谁关心我？",
            "sentences": [{"index": 1, "difficulty": difficulty}],
        }
    )
    quality = FakeModel(
        {
            "sentences": [
                {
                    "index": 1,
                    "difficulty": difficulty,
                    "basis": "structure",
                    "meaning": "谁关心我？",
                }
            ]
        }
    )

    result = analyze_paragraph(
        "“Who regards me?”",
        fast,
        quality,
        profile=profile,
        language=GLM52_MEANING_ONLY,
        syntax_analyzer=StubSyntaxAnalyzer({}),
    )

    assert len(result.cards) == 1


@pytest.mark.parametrize(
    "text",
    [
        (
            "I do assure you that nothing would surprise me more than to hear "
            "of their being going to be married."
        ),
        (
            "Because you are so sly about it yourself, you think nobody else has "
            "any senses; but it is no such thing, I can tell you, for it has been "
            "known all over town this ever so long."
        ),
    ],
)
def test_sub_forty_word_reader_fluent_examples_do_not_render_effortful_cards(text):
    fast = FakeModel(
        {
            "paragraph_translation": "这句话的段译已经足够清楚。",
            "sentences": [{"index": 1, "difficulty": "effortful"}],
        }
    )
    quality = FakeModel(
        {
            "sentences": [
                {
                    "index": 1,
                    "difficulty": "effortful",
                    "basis": "structure",
                    "meaning": "这句话的句意已经足够清楚。",
                }
            ]
        }
    )

    result = analyze_paragraph(
        text,
        fast,
        quality,
        language=GLM52_MEANING_ONLY,
        syntax_analyzer=StubSyntaxAnalyzer({}),
    )

    assert result.cards == ()


def test_local_gate_off_and_profile_hard_examples_prevent_unsafe_skips():
    text = "He simply nodded."
    assert local_model_gate(text, mode="off").needs_model
    baseline = local_model_gate(text)
    assert not baseline.needs_model
    personalized = local_model_gate(
        text,
        hard_profile_texts=("Go.",),
    )
    assert personalized.needs_model


def test_two_stage_learning_requires_exact_source_mapping():
    text = "This is easy. Although it is long, it remains clear."
    fast = FakeModel(
        {
            "paragraph_translation": "这很简单。尽管它很长，但依然清楚。",
            "sentences": [
                {
                    "index": 1,
                    "text": "This is easy.",
                    "difficulty": "fluent",
                    "confidence": 0.9,
                },
                {
                    "index": 2,
                    "text": "Although it is long, it remains clear.",
                    "difficulty": "effortful",
                    "reason": "让步关系",
                    "confidence": 0.8,
                },
            ],
        }
    )
    quality = FakeModel(
        {
            "sentences": [
                {
                    "index": 2,
                    "text": "Although it is long, it remains clear.",
                    "difficulty": "effortful",
                    "meaning": "尽管它很长，但依然清楚。",
                    "cues": "先给出让步，再表达主要判断。",
                    "feel": "",
                    "note": "",
                }
            ]
        }
    )
    result = analyze_paragraph(
        text,
        fast,
        quality,
        book_context=BookContext("Test Book", ("Test Author",), 1811),
    )
    assert result.paragraph_translation.startswith("这很简单")
    assert len(result.cards) == 1
    assert result.cards[0].difficulty == "effortful"
    assert len(fast.calls) == len(quality.calls) == 1
    assert "它只服务于连续阅读" in fast.calls[0][0]
    assert "中文谓语和搭配必须自然" in fast.calls[0][1]
    assert "不要输出任何 meaning 之外的学习卡字段" in quality.calls[0][0]
    assert "不要解释写法、语感或额外提示" in quality.calls[0][0]
    assert "不得引用前文、下文或模型记忆" in quality.calls[0][0]
    assert "年龄" in quality.calls[0][0]
    assert "effortful 或 blocking 只返回 index、difficulty 和一句 meaning" in quality.calls[0][1]
    assert '"work_year": 1811' in fast.calls[0][1]
    assert "不得引用记忆中的情节" in fast.calls[0][1]
    assert "亲属称谓硬规则" not in fast.calls[0][1]


def _obsolete_short_nested_sentence_keeps_quality_evidence_without_rendering_structure():
    text = "The letter which the woman whom I met yesterday had written was lost."
    syntax = StubSyntaxAnalyzer(
        {
            1: LocalSyntaxResult(
                needs_structure=True,
                clause_count=2,
                max_clause_depth=2,
                reasons=("nested-clauses",),
                hints=(
                    ClauseHint("relcl", "which the woman whom I met yesterday had written", "letter", 1),
                    ClauseHint("relcl", "whom I met yesterday", "woman", 2),
                ),
                skeleton="The letter was lost.",
                reader_modifiers=(
                    ReaderModifier(
                        "interruption",
                        "which the woman whom I met yesterday had written",
                        "The letter",
                        "读到 was lost 时回到主干",
                    ),
                    ReaderModifier(
                        "interruption",
                        "whom I met yesterday",
                        "the woman",
                        "补充说明 the woman",
                    ),
                ),
            )
        }
    )
    fast = FakeModel(
        {
            "paragraph_translation": "我昨天见到的那位女士写的信丢了。",
            "sentences": [{"index": 1, "difficulty": "blocking"}],
        }
    )
    quality = FakeModel(
        {
            "sentences": [
                {
                    "index": 1,
                    "difficulty": "blocking",
                    "basis": "structure",
                    "meaning": "我昨天见到的那位女士写的信丢了。",
                }
            ]
        }
    )

    result = analyze_paragraph(
        text,
        fast,
        quality,
        language=GLM52_MEANING_ONLY,
        syntax_analyzer=syntax,
    )

    assert len(syntax.calls) == 1
    assert result.cards[0].syntax is None
    assert len(quality.calls) == 1
    assert '"clause": "whom I met yesterday"' in quality.calls[0][1]
    assert '"target": "woman"' in quality.calls[0][1]
    assert '"depth": 2' in quality.calls[0][1]
    assert "从句证据只用于理解同一句" in quality.calls[0][1]


def _obsolete_reader_structure_uses_separate_modifiers_without_filtering_quality_evidence():
    text = (
        "Mrs. Jennings, who had watched them with pleasure while they were talking, "
        "and who expected to see the effect of Miss Dashwood’s communication, in such "
        "an instantaneous gaiety on Colonel Brandon’s side, as might have become a man "
        "in the bloom of youth, of hope and happiness, saw him, with amazement, remain "
        "the whole evening more serious and thoughtful than usual."
    )
    quality_hint = ClauseHint(
        "acl:relcl",
        (
            "who had watched them with pleasure while they were talking, and who "
            "expected to see the effect of Miss Dashwood’s communication, in such an "
            "instantaneous gaiety on Colonel Brandon’s side, as might have become a man "
            "in the bloom of youth, of hope and happiness"
        ),
        "Mrs. Jennings",
        1,
    )
    syntax = StubSyntaxAnalyzer(
        {
            1: LocalSyntaxResult(
                needs_structure=True,
                clause_count=3,
                reasons=("interrupted-main-clause",),
                hints=(quality_hint,),
                skeleton=(
                    "Mrs. Jennings saw him remain the whole evening more serious and "
                    "thoughtful than usual."
                ),
                reader_modifiers=(
                    ReaderModifier(
                        "interruption",
                        "who had watched … and who expected …",
                        "Mrs. Jennings",
                        "读到 saw him 时回到主干",
                    ),
                    ReaderModifier(
                        "time",
                        "while they were talking",
                        "watched",
                        "说明 watched 发生的时间",
                    ),
                    ReaderModifier(
                        "reason",
                        "because this must not be rendered",
                        "expected",
                        "第三条不得展示",
                    ),
                ),
            )
        }
    )
    fast = FakeModel(
        {
            "paragraph_translation": "Jennings 太太看到他整晚都比平常更加严肃和沉思。",
            "sentences": [{"index": 1, "difficulty": "blocking"}],
        }
    )
    quality = FakeModel(
        {
            "sentences": [
                {
                    "index": 1,
                    "difficulty": "blocking",
                    "basis": "structure",
                    "meaning": "Jennings 太太看到他整晚都比平常更加严肃和沉思。",
                }
            ]
        }
    )

    result = analyze_paragraph(
        text,
        fast,
        quality,
        language=GLM52_MEANING_ONLY,
        syntax_analyzer=syntax,
    )

    assert '"clause": "who had watched them with pleasure' in quality.calls[0][1]
    assert result.cards[0].syntax is not None
    assert result.cards[0].syntax.skeleton == (
        "Mrs. Jennings saw him remain the whole evening more serious and thoughtful "
        "than usual."
    )
    assert [link.label for link in result.cards[0].syntax.links] == [
        "插入说明",
        "时间背景",
    ]
    assert [link.clause for link in result.cards[0].syntax.links] == [
        "who had watched … and who expected …",
        "while they were talking",
    ]


def _obsolete_structured_meaning_keeps_quality_evidence_without_reader_structure_display():
    text = (
        "Mrs. Jennings, who had watched them with pleasure while they were talking, "
        "and who expected to see the effect of Miss Dashwood’s communication, in such "
        "an instantaneous gaiety on Colonel Brandon’s side, as might have become a man "
        "in the bloom of youth, of hope and happiness, saw him, with amazement, remain "
        "the whole evening more serious and thoughtful than usual."
    )
    syntax = StubSyntaxAnalyzer(
        {
            1: LocalSyntaxResult(
                needs_structure=True,
                clause_count=3,
                reasons=("interrupted-main-clause",),
                hints=(
                    ClauseHint(
                        "acl:relcl",
                        "who had watched them with pleasure while they were talking",
                        "Mrs. Jennings",
                        1,
                    ),
                ),
                skeleton=(
                    "Mrs. Jennings saw him remain the whole evening more serious and "
                    "thoughtful than usual."
                ),
                reader_modifiers=(
                    ReaderModifier(
                        "interruption",
                        "who had watched … and who expected …",
                        "Mrs. Jennings",
                        "读到 saw him 时回到主干",
                    ),
                    ReaderModifier(
                        "time",
                        "while they were talking",
                        "watched",
                        "说明 watched 发生的时间",
                    ),
                ),
            )
        }
    )
    fast = FakeModel(
        {
            "paragraph_translation": "Jennings 太太惊讶地看见他整晚都更严肃沉思。",
            "sentences": [{"index": 1, "difficulty": "blocking"}],
        }
    )
    quality = FakeModel(
        {
            "sentences": [
                {
                    "index": 1,
                    "difficulty": "blocking",
                    "basis": "structure",
                    "meaning": "Jennings 太太先是愉快旁观并期待结果；但她惊讶地看到他整晚比平常更严肃沉思。",
                }
            ]
        }
    )

    result = analyze_paragraph(
        text,
        fast,
        quality,
        language=GLM52_STRUCTURED_MEANING,
        syntax_analyzer=syntax,
    )

    assert '"clause": "who had watched them with pleasure' in quality.calls[0][1]
    assert "从句证据只用于理解同一句" in quality.calls[0][1]
    assert result.cards[0].meaning.startswith("Jennings 太太先是")
    assert result.cards[0].syntax is None


def _obsolete_meaning_only_analysis_requires_syntax_before_model_calls():
    unused = FakeModel({})

    with pytest.raises(EpubError, match="必须提供 Stanza"):
        analyze_paragraph(
            "The sentence which needs analysis remains grounded.",
            unused,
            unused,
            language=GLM52_MEANING_ONLY,
        )

    assert unused.calls == []


def test_local_syntax_does_not_create_candidate_for_model_fluent_sentence():
    syntax = StubSyntaxAnalyzer(
        {
            1: LocalSyntaxResult(
                needs_structure=True,
                clause_count=2,
                max_clause_depth=2,
                reasons=("nested-clauses",),
                hints=(
                    ClauseHint(
                        "relcl",
                        "which the woman whom I met yesterday had written",
                        "letter",
                        1,
                    ),
                ),
            )
        }
    )
    fast = FakeModel(
        {
            "paragraph_translation": "我昨天见到的那位女士写的信丢了。",
            "sentences": [{"index": 1, "difficulty": "fluent"}],
        }
    )
    quality = FakeModel({})

    result = analyze_paragraph(
        "The letter which the woman whom I met yesterday had written was lost.",
        fast,
        quality,
        syntax_analyzer=syntax,
    )

    assert result.cards == ()
    assert quality.calls == []


def test_simple_local_syntax_does_not_create_a_quality_candidate():
    syntax = StubSyntaxAnalyzer({1: LocalSyntaxResult()})
    fast = FakeModel(
        {
            "paragraph_translation": "她笑了。",
            "sentences": [{"index": 1, "difficulty": "fluent"}],
        }
    )
    quality = FakeModel({})

    result = analyze_paragraph("She laughed.", fast, quality, syntax_analyzer=syntax)

    assert result.cards == ()
    assert quality.calls == []


def _obsolete_syntax_source_fragments_accept_normalized_epub_line_wrapping():
    sentence = (
        "A room or two can easily be added; and if my friends find\n"
        "no difficulty in travelling so far to see me, I am sure I will find none in\n"
        "accommodating them.”"
    )

    guide = _parse_syntax(
        {
            "skeleton": "I am sure I will find none in accommodating them",
            "links": [
                {
                    "clause": "if my friends find no difficulty in travelling so far to see me",
                    "target": "am",
                    "explanation": "条件从句修饰主句。",
                },
                {
                    "clause": "I will find none in accommodating them",
                    "target": "sure",
                    "explanation": "内容连接到 sure。",
                },
            ],
            "normal_order": "",
            "nesting_path": "",
        },
        sentence,
    )

    assert guide is not None
    assert guide.links[0].clause == "if my friends find\nno difficulty in travelling so far to see me"
    assert guide.links[0].target == "am"
    assert guide.links[1].clause == "I will find none in\naccommodating them"


def _obsolete_syntax_cache_accepts_ordered_source_grounded_reader_abbreviation():
    sentence = (
        "Mrs. Jennings, who had watched them with pleasure, and who expected an "
        "answer, saw him remain serious."
    )

    guide = _parse_syntax(
        {
            "skeleton": "Mrs. Jennings saw him remain serious.",
            "links": [
                {
                    "clause": "who had watched … and who expected …",
                    "target": "Mrs. Jennings",
                    "explanation": (
                        "补充说明 Mrs. Jennings；读到 saw him 时回到主干"
                    ),
                    "label": "插入说明",
                }
            ],
        },
        sentence,
    )

    assert guide is not None
    assert guide.links[0].clause == "who had watched … and who expected …"
    assert guide.links[0].label == "插入说明"


def test_syntax_cache_round_trip_preserves_sentence_meaning_evidence():
    sentences = [Sentence(1, "A deliberately long sentence remains available.")]
    expected = {
        1: LocalSyntaxResult(
            needs_structure=True,
            clause_count=2,
            max_clause_depth=1,
            reasons=("interrupted-main-clause",),
            hints=(ClauseHint("acl:relcl", "who had watched", "A reader", 1),),
        )
    }

    payload = _syntax_to_dict(expected)

    assert payload["version"] == 3
    assert _syntax_from_dict(payload, sentences) == expected


def test_density_controls_effortful_cards_and_profile_reaches_prompt():
    text = "Although tired, he continued."
    fast_response = {
        "paragraph_translation": "尽管疲惫，他仍继续。",
        "sentences": [
            {
                "index": 1,
                "text": text,
                "difficulty": "effortful",
            }
        ],
    }
    quality_response = {
        "sentences": [
            {
                "index": 1,
                "text": text,
                "difficulty": "effortful",
                "meaning": "尽管疲惫，他仍继续。",
                "cues": "先让步，再给主要动作。",
                "feel": "简洁。",
                "note": "",
            }
        ]
    }
    profile = ReadingProfile((CalibrationExample("A hard sample.", "blocking"),))
    fast = FakeModel(fast_response)
    low = analyze_paragraph(
        text, fast, FakeModel(quality_response), profile=profile, density="low"
    )
    assert low.cards == ()
    assert "A hard sample." in fast.calls[0][1]

    medium = analyze_paragraph(
        text,
        FakeModel(fast_response),
        FakeModel(quality_response),
        density="medium",
    )
    assert medium.cards[0].meaning == "尽管疲惫，他仍继续。"

    high = analyze_paragraph(
        text,
        FakeModel(fast_response),
        FakeModel(quality_response),
        density="high",
    )
    assert high.cards[0].meaning == "尽管疲惫，他仍继续。"


def test_model_text_cannot_rewrite_locally_bound_original_sentence():
    fast = FakeModel(
        {
            "paragraph_translation": "翻译",
            "sentences": [
                {
                    "index": 1,
                    "text": "Rewritten.",
                    "difficulty": "fluent",
                }
            ],
        }
    )
    result = analyze_paragraph("Original.", fast, FakeModel({}))
    assert result.assessments[0].text == "Original."


def test_translation_preserves_numbers():
    text = "In 1813, Jane Austen published it."
    response = {
        "paragraph_translation": "1813年，Jane Austen 出版了它。",
        "sentences": [
            {"index": 1, "text": text, "difficulty": "fluent"}
        ],
    }
    fast = FakeModel(response)
    result = analyze_paragraph(text, fast, FakeModel({}))
    assert result.paragraph_translation.startswith("1813")


@pytest.mark.parametrize("stage", ["translations", "assessments", "cards"])
def test_p0_boolean_sentence_indices_rejected(stage):
    from translator.translator import _parse_assessments, _parse_cards, _parse_sentence_translations
    sentences = [Sentence(1, "He left.")]
    with pytest.raises(EpubError, match="格式无效"):
        if stage == "translations":
            _parse_sentence_translations([{"index": True, "translation": "他走了。"}], sentences)
        elif stage == "assessments":
            _parse_assessments([{"index": True, "difficulty": "fluent"}], sentences)
        else:
            _parse_cards([{"index": True, "difficulty": "fluent"}], sentences, {1})


@pytest.mark.parametrize("confidence", [float("nan"), float("inf"), -float("inf"), True])
def test_p0_nonfinite_confidence_rejected(confidence):
    from translator.translator import _parse_assessments
    with pytest.raises(EpubError, match="confidence"):
        _parse_assessments([{"index": 1, "difficulty": "fluent", "confidence": confidence}],
                           [Sentence(1, "He left.")])


@pytest.mark.parametrize("source, translation", [
    ("He paid 12 pounds.", "他付了112英镑。"),
    ("He paid 12 pounds.", "他付了12.5英镑。"),
    ("He paid 12 pounds.", "他付了12,000英镑。"),
    ("He paid 12 pounds.", "编号12.5A。"),
    ("He paid 12 pounds.", "他付了.12英镑。"),
    ("He paid 1200 pounds.", "他付了12,00英镑。"),
    ("It was 12%.", "比例为112%。"),
    ("It was -12.", "数值为-112。"),
])
def test_p0_number_substrings_do_not_count_as_preserved(source, translation):
    fast = FakeModel({"paragraph_translation": translation,
                      "sentences": [{"index": 1, "difficulty": "fluent"}]})
    with pytest.raises(EpubError, match="遗漏数字"):
        analyze_paragraph(source, fast, FakeModel({}))


@pytest.mark.parametrize("source, translation", [
    ("He paid 12 pounds.", "他付了12英镑。"),
    ("He paid 1,200 pounds.", "他付了1200英镑。"),
    ("He paid 1200 pounds.", "他付了1,200英镑。"),
    ("It was -12.5%.", "比例为-12.5%。"),
])
def test_p0_number_items_allow_only_grouping_normalization(source, translation):
    fast = FakeModel({"paragraph_translation": translation,
                      "sentences": [{"index": 1, "difficulty": "fluent"}]})
    assert analyze_paragraph(source, fast, FakeModel({})).paragraph_translation == translation


def test_p0_as_if_in_another_sentence_does_not_reject_grounded_motive():
    source = "He looked as if he were tired. His purpose was to leave."
    fast = FakeModel({"paragraph_translation": "他看起来仿佛累了。他的目的是离开。",
                      "sentences": [{"index": 1, "difficulty": "fluent"},
                                    {"index": 2, "difficulty": "effortful"}]})
    quality = FakeModel({"sentences": [
        {"index": 2, "difficulty": "blocking", "meaning": "他的目的是离开。"}]})
    result = analyze_paragraph(source, fast, quality)
    assert result.cards[0].sentence == "His purpose was to leave."


@pytest.mark.parametrize("confidence, expected", [(0, 0.0), (0.7, 0.7), (-2, 0.0), (10**1000, 1.0)])
def test_p0_finite_confidence_keeps_existing_clamping(confidence, expected):
    from translator.translator import _parse_assessments
    result = _parse_assessments([{"index": 1, "difficulty": "fluent", "confidence": confidence}],
                                [Sentence(1, "He left.")])
    assert result[0].confidence == expected


@pytest.mark.parametrize("change", [{"index": True}, {"confidence": float("nan")}])
def test_p0_cached_assessments_do_not_bypass_validation(change):
    from translator.translator import _learning_from_dict
    with pytest.raises(EpubError):
        _learning_from_dict({"paragraph_translation": "他走了。", "cards": [], "assessments": [
            {"index": 1, "text": "He left.", "difficulty": "fluent", "confidence": 0.5, **change}]})


def test_p0_frozen_syntax_rejects_boolean_sentence_index():
    from translator.translator import _syntax_to_dict
    payload = _syntax_to_dict({1: LocalSyntaxResult()})
    payload["results"][0]["index"] = True
    with pytest.raises(EpubError, match="索引无效"):
        _syntax_from_dict(payload, [Sentence(1, "He left.")])


def test_p0_trace_marks_new_validation_without_changing_prompts(tmp_path):
    from translator.translator import VALIDATION_VERSION
    trace = ParagraphTrace(tmp_path / "trace")
    fast = FakeModel({"paragraph_translation": "他走了。",
                      "sentences": [{"index": 1, "difficulty": "fluent"}]})
    analyze_paragraph("He left.", fast, FakeModel({}), trace=trace)
    assert json.loads((trace.directory / "validation-policy.json").read_text()) == {"version": VALIDATION_VERSION}


def test_reading_p1_does_not_inherit_as_if_keyword_rejection():
    from translator.reading_assistance import parse_assistance
    payload = {"sentences": [{"index": 1, "translation": "他看起来仿佛累了。", "difficulty": "fluent"}],
               "aids": [{"scope": "sentence", "sentence_indices": [1], "text": "这里只是说仿佛疲惫，没有说明他的目的。"}]}
    result = parse_assistance(payload, "He looked as if he were tired.")
    assert result.status == "complete" and len(result.aids) == 1


@pytest.mark.parametrize("translation", ["She 锁上了它。", "他告诉 them 离开。", "It 仍在桌上。"])
def test_reading_core_rejects_untranslated_personal_pronouns(translation):
    from translator.reading_assistance import AssistanceError, parse_assistance

    payload = {
        "sentences": [{"index": 1, "translation": translation, "difficulty": "effortful"}],
        "aids": [],
    }
    with pytest.raises(AssistanceError, match="untranslated personal pronoun"):
        parse_assistance(payload, "She locked it.")


@pytest.mark.parametrize("translation", [
    "他把信 unopened 留在桌上。",
    "她 worked 到很晚。",
    "这是一个 half-finished 计划。",
    "Mara 去找她的 sister。",
    "Jane Austen 去看她的 sister-in-law。",
    "这是独立的 s 标记。",
])
def test_reading_core_rejects_untranslated_lowercase_english_words(translation):
    from translator.reading_assistance import AssistanceError, parse_assistance
    payload = {
        "sentences": [{"index": 1, "translation": translation, "difficulty": "effortful"}],
        "aids": [],
    }
    with pytest.raises(AssistanceError, match="untranslated lowercase English word"):
        parse_assistance(payload, "He left the half-finished letter unopened and worked late.")


@pytest.mark.parametrize("translation", [
    "Charles de Gaulle 到了。",
    "Mara 去找她的姐妹。",
    "她住在 Bartlett's Buildings。",
    "她住在 Bartlett’s Buildings。",
    "她称她为 the Hon. Miss Morton。",
])
def test_reading_core_allows_selected_lowercase_exceptions_and_proper_names(translation):
    from translator.reading_assistance import parse_assistance
    payload = {
        "sentences": [{"index": 1, "translation": translation, "difficulty": "fluent"}],
        "aids": [],
    }
    assert parse_assistance(payload, "Mara visited Jane Austen and Charles de Gaulle.").status == "complete"


def test_reading_p1_prompt_translates_ambiguous_kinship_without_inventing_detail():
    from translator.reading_prompts import SEMANTIC_RULES
    assert '"his sister" can become "他的姐妹"' in SEMANTIC_RULES
    assert "Do not leave a lowercase English kinship noun" in SEMANTIC_RULES
    assert "state the uncertainty rather than resolving" in SEMANTIC_RULES


@pytest.mark.parametrize("pipeline, count", [("single", 1), ("two-stage", 2)])
@pytest.mark.parametrize("raw_aids, status", [
    ([{"scope": "sentence", "sentence_indices": [True], "text": "无效提示"}], "partial_aids"),
    ({"broken": "collection"}, "translation_only"),
])
def test_reading_p1_degradation_keeps_raw_and_never_retranslates(tmp_path, pipeline, count, raw_aids, status):
    from translator.reading_eval import evaluate_reading, prepare_reading_input
    source = tmp_path / "input.json"
    source.write_text(json.dumps({"paragraph": "He left. She stayed."}))
    final = {**_reading_payload(), "aids": raw_aids}
    model = SequenceModel([_reading_payload()] * (count - 1) + [final], adapt_fast=False)
    report = evaluate_reading(source, tmp_path / "traces", model, pipeline=pipeline,
        prepared=prepare_reading_input(json.loads(source.read_text())), offline=True)
    result = report["runs"][0]
    assert result["status"] == status
    assert len(model.calls) == report["call_count"] == count
    assert result["result"]["aids"] == []
    assert report["calls"][-1]["parsed_content"]["aids"] == raw_aids
    assert result["result"]["sentences"][0]["translation"] == "他离开了。"


def test_reading_p1_review_replaces_draft_with_complete_final_result(tmp_path):
    from translator.reading_eval import evaluate_reading, prepare_reading_input
    source = tmp_path / "input.json"
    source.write_text(json.dumps({"paragraph": "He left. She stayed."}))
    draft, final = _reading_payload(), _reading_payload()
    final["sentences"][1]["translation"] = "他已经离开了。"
    model = SequenceModel([draft, final], adapt_fast=False)
    report = evaluate_reading(source, tmp_path / "traces", model, pipeline="two-stage",
        prepared=prepare_reading_input(json.loads(source.read_text())), offline=True)
    assert json.loads(model.calls[1][1])["draft"]["sentences"][0]["translation"] == "他离开了。"
    assert report["runs"][0]["result"]["sentences"][0]["translation"] == "他已经离开了。"
    assert len(model.calls) == 2


def test_reading_core_repair_is_one_bounded_complete_replacement():
    from translator.reading_eval import prepare_reading_input
    from translator.translator import generate_reading_assistance

    invalid = {
        "sentences": [{"index": 1, "translation": "他把信 unopened 留下。", "difficulty": "effortful"}],
        "aids": [],
    }
    valid = {
        "sentences": [{"index": 1, "translation": "他把信留着没有拆开。", "difficulty": "effortful"}],
        "aids": [{"scope": "phrase", "sentence_indices": [1], "quote": "left unopened",
                  "text": "leave + 宾语 + 补语表示让宾语保持某种状态。"}],
    }
    model = SequenceModel([invalid, valid], adapt_fast=False)
    prepared = prepare_reading_input({"paragraph": "He left the letter unopened."})

    result = generate_reading_assistance(
        prepared, model, pipeline="single", trace=None, repair_core=True,
    )

    assert result.sentences[0].translation == "他把信留着没有拆开。"
    assert len(model.calls) == 2
    repair_system, repair_user = model.calls[1]
    repair_payload = json.loads(repair_user)
    assert "Repair" in repair_system
    assert repair_payload["invalid_draft"] == invalid
    assert repair_payload["validation_error"] == "untranslated lowercase English word in core translation"
    assert result.diagnostics[-1]["reason"] == "core_repair"


def test_reading_core_repair_is_opt_in_and_never_loops():
    from translator.reading_assistance import AssistanceError
    from translator.reading_eval import prepare_reading_input
    from translator.translator import generate_reading_assistance

    invalid = {
        "sentences": [{"index": 1, "translation": "他 unopened。", "difficulty": "effortful"}],
        "aids": [],
    }
    prepared = prepare_reading_input({"paragraph": "He left it unopened."})
    without_repair = SequenceModel([invalid], adapt_fast=False)
    with pytest.raises(AssistanceError, match="untranslated lowercase English word"):
        generate_reading_assistance(prepared, without_repair, pipeline="single", trace=None)
    assert len(without_repair.calls) == 1

    bounded = SequenceModel([invalid, invalid, invalid], adapt_fast=False)
    with pytest.raises(AssistanceError, match="untranslated lowercase English word"):
        generate_reading_assistance(
            prepared, bounded, pipeline="single", trace=None, repair_core=True,
        )
    assert len(bounded.calls) == 2


def test_reading_transport_failure_never_uses_content_repair():
    from translator.llm_api import LLMError
    from translator.reading_eval import prepare_reading_input
    from translator.translator import generate_reading_assistance

    class TransportFailureThenValid:
        def __init__(self):
            self.calls = 0

        def complete_json(self, system, user):
            self.calls += 1
            if self.calls == 1:
                raise LLMError("The read operation timed out")
            return {
                "sentences": [
                    {"index": 1, "translation": "他离开了。", "difficulty": "fluent"},
                ],
                "aids": [],
            }

    model = TransportFailureThenValid()
    prepared = prepare_reading_input({"paragraph": "He left."})

    with pytest.raises(LLMError, match="timed out"):
        generate_reading_assistance(
            prepared, model, pipeline="single", trace=None, repair_core=True,
        )

    assert model.calls == 1


def test_reading_two_stage_core_repair_continues_to_review_once():
    from translator.reading_eval import prepare_reading_input
    from translator.translator import generate_reading_assistance

    invalid = {
        "sentences": [{"index": 1, "translation": "他 unopened。", "difficulty": "effortful"}],
        "aids": [],
    }
    repaired = {
        "sentences": [{"index": 1, "translation": "他把它留着没有打开。", "difficulty": "effortful"}],
        "aids": [],
    }
    reviewed = {
        "sentences": [{"index": 1, "translation": "他把它留着，没有打开。", "difficulty": "effortful"}],
        "aids": [{"scope": "phrase", "sentence_indices": [1], "quote": "left it unopened",
                  "text": "leave + 宾语 + 补语表示让宾语保持某种状态。"}],
    }
    model = SequenceModel([invalid, repaired, reviewed], adapt_fast=False)
    result = generate_reading_assistance(
        prepare_reading_input({"paragraph": "He left it unopened."}),
        model, pipeline="two-stage", trace=None, repair_core=True,
    )

    assert len(model.calls) == 3
    assert result.sentences[0].translation == "他把它留着，没有打开。"
    assert "draft" in json.loads(model.calls[2][1])


def test_reading_two_stage_keeps_valid_repair_when_review_regresses_core():
    from translator.reading_eval import prepare_reading_input
    from translator.translator import generate_reading_assistance

    invalid_draft = {
        "sentences": [
            {"index": 1, "translation": "他们住在Mansion-house。", "difficulty": "effortful"},
        ],
        "aids": [],
    }
    repaired = {
        "sentences": [
            {"index": 1, "translation": "他们住在朋友的大宅里。", "difficulty": "effortful"},
        ],
        "aids": [],
    }
    regressed_review = invalid_draft
    model = SequenceModel([invalid_draft, repaired, regressed_review], adapt_fast=False)

    result = generate_reading_assistance(
        prepare_reading_input({"paragraph": "They stayed at the Mansion-house."}),
        model,
        pipeline="two-stage",
        trace=None,
        repair_core=True,
    )

    assert len(model.calls) == 3
    assert result.sentences[0].translation == "他们住在朋友的大宅里。"
    assert result.diagnostics[-1]["reason"] == "review_fallback"
    assert "untranslated lowercase English word" in result.diagnostics[-1]["review_error"]


def test_reading_preview_uses_frozen_result_without_model_calls(tmp_path):
    from translator.reading_assistance import parse_assistance
    source = build_epub(tmp_path / "source.epub")
    output = tmp_path / "reading-PREVIEW.epub"
    assistance = parse_assistance(
        {
            "sentences": [
                {"index": 1, "translation": "这是原段。", "difficulty": "fluent"},
                {"index": 2, "translation": "尽管很长，意思可验证。", "difficulty": "effortful"},
            ],
            "aids": [{"scope": "sentence", "sentence_indices": [2], "show_translation": True, "text": "让步关系。"}],
        },
        "This is the original paragraph. Although it is long, its meaning remains testable.",
    )
    result = generate_reading_preview(
        source, output, chapter=1, paragraph=1, assistance=assistance,
        require_epubcheck=False, analysis_link_text="〔译〕",
    )
    assert result.output == output and output.is_file()
    with EpubBook(output) as book:
        tree = etree.parse(str(book.root / "OEBPS/chapter.xhtml"))
        assert tree.xpath("//*[contains(@class, 'epubllmt-analysis-ref')]")[0].text == "〔译〕"
        analysis = _detached_analysis_tree(book)
        generated = analysis.xpath("//*[contains(@class, 'epubllmt-reading-aid')]")
        assert len(generated) == 1
        cards = analysis.xpath("//*[contains(@class, 'epubllmt-reading-sentence-card')]")
        assert len(cards) == 1
        card_text = "".join(cards[0].itertext())
        assert "原句：Although it is long, its meaning remains testable." in card_text
        assert "句译：尽管很长，意思可验证。" in card_text
        assert generated[0] in cards[0].iterdescendants()


class _ReadingBookModel:
    def __init__(self, *, broken_second=False):
        self.calls = []
        self.broken_second = broken_second

    def complete_json(self, system, user):
        self.calls.append((system, user))
        source = json.loads(user)["sentences"]
        if self.broken_second and len(self.calls) == 2:
            return {"sentences": []}
        return {"sentences": [
            {"index": item["index"], "translation": f"译{item['index']}。", "difficulty": "fluent"}
            for item in source
        ], "aids": []}


class _ReadingAidBookModel(_ReadingBookModel):
    def complete_json(self, system, user):
        self.calls.append((system, user))
        source = json.loads(user)["sentences"]
        return {
            "sentences": [
                {
                    "index": item["index"],
                    "translation": f"译{item['index']}。",
                    "difficulty": "fluent",
                }
                for item in source
            ],
            "aids": [
                {
                    "scope": "sentence",
                    "sentence_indices": [item["index"]],
                    "text": "结构说明。",
                }
                for item in source
            ],
        }


def test_reading_book_dry_run_and_cache_rerender_never_repeat_calls(tmp_path):
    source = build_epub(tmp_path / "source.epub")
    cache = tmp_path / "cache.sqlite3"
    model = _ReadingBookModel()
    common = dict(model=model, profile_key="reading-v1-profile", cache_path=cache,
                  require_epubcheck=False)
    estimate = translate_reading_book(source, tmp_path / "unused-PREVIEW.epub", dry_run=True, **common)
    assert estimate.approximate_requests == 2 and model.calls == []
    first = translate_reading_book(source, tmp_path / "first-PREVIEW.epub", **common)
    assert first.generated_paragraphs == 2 and first.cached_paragraphs == 0
    second = translate_reading_book(source, tmp_path / "second-PREVIEW.epub", **common)
    assert second.generated_paragraphs == 0 and second.cached_paragraphs == 2
    assert len(model.calls) == 2
    with EpubBook(second.output) as book:
        analysis = _detached_analysis_tree(book)
        assert len(analysis.xpath("//*[contains(@class, 'epubllmt-reading-aid')]")) == 0
        assert len(analysis.xpath("//*[contains(@class, 'epubllmt-translation')]")) == 2


def test_reading_book_reprocesses_only_selected_cached_paragraph(tmp_path):
    source = build_epub(tmp_path / "source.epub")
    cache = tmp_path / "cache.sqlite3"
    first_model = _ReadingBookModel()
    first = translate_reading_book(
        source, tmp_path / "first-PREVIEW.epub", model=first_model,
        profile_key="stable-reading-profile", cache_path=cache,
        require_epubcheck=False,
    )
    assert first.generated_paragraphs == 2

    replacement_model = _ReadingBookModel()
    replacement = translate_reading_book(
        source, tmp_path / "replacement-PREVIEW.epub", model=replacement_model,
        profile_key="stable-reading-profile", cache_path=cache,
        require_epubcheck=False, reprocess_paragraphs={(1, 2)},
    )

    assert replacement.cached_paragraphs == 1
    assert replacement.generated_paragraphs == 1
    assert len(replacement_model.calls) == 1
    assert "A second paragraph has an author's note." in replacement_model.calls[0][1]


def test_reading_book_failed_reprocess_preserves_previous_valid_cache(tmp_path):
    source = build_epub(tmp_path / "source.epub")
    cache = tmp_path / "cache.sqlite3"
    first_model = _ReadingBookModel()
    translate_reading_book(
        source, tmp_path / "first-PREVIEW.epub", model=first_model,
        profile_key="stable-reading-profile", cache_path=cache,
        require_epubcheck=False,
    )

    class InvalidReplacement(_ReadingBookModel):
        def complete_json(self, system, user):
            self.calls.append((system, user))
            return {"sentences": [], "aids": []}

    with pytest.raises(EpubError, match="核心结果"):
        translate_reading_book(
            source, tmp_path / "failed-PREVIEW.epub", model=InvalidReplacement(),
            profile_key="stable-reading-profile", cache_path=cache,
            require_epubcheck=False, reprocess_paragraphs={(1, 2)},
        )

    class FailIfCalled:
        def complete_json(self, system, user):
            raise AssertionError("failed reprocess must leave old cache reusable")

    resumed = translate_reading_book(
        source, tmp_path / "resumed-PREVIEW.epub", model=FailIfCalled(),
        profile_key="stable-reading-profile", cache_path=cache,
        require_epubcheck=False,
    )
    assert resumed.cached_paragraphs == 2
    assert resumed.generated_paragraphs == 0


def test_reading_book_skips_explicit_paragraph_without_model_or_rendering(tmp_path):
    source = build_epub(tmp_path / "source.epub")
    model = _ReadingBookModel()

    result = translate_reading_book(
        source,
        tmp_path / "skipped-PREVIEW.epub",
        model=model,
        profile_key="reading-v1-profile",
        cache_path=tmp_path / "cache.sqlite3",
        require_epubcheck=False,
        skip_paragraphs={(1, 1)},
    )

    assert result.paragraphs == 1
    assert result.generated_paragraphs == 1
    assert result.estimate.source_paragraphs == 2
    assert result.estimate.skipped_local_easy == 1
    assert len(model.calls) == 1
    assert "A second paragraph has an author's note." in model.calls[0][1]
    with EpubBook(result.output) as book:
        tree = etree.parse(str(book.root / "OEBPS/chapter.xhtml"))
        translations = _detached_analysis_tree(book).xpath(
            "//*[contains(@class, 'epubllmt-translation')]"
        )
        assert len(translations) == 1
        opening_links = tree.xpath(
            "//*[contains(@class, 'opening')]"
            "//*[contains(@class, 'epubllmt-analysis-ref')]"
        )
        second_links = tree.xpath(
            "//*[@id='second']//*[contains(@class, 'epubllmt-analysis-ref')]"
        )
        assert opening_links == []
        assert len(second_links) == 1


def test_reading_book_filters_short_sentence_aids_from_existing_cache(tmp_path):
    source = build_epub(tmp_path / "source.epub")
    cache = tmp_path / "cache.sqlite3"
    model = _ReadingAidBookModel()
    first = translate_reading_book(
        source,
        tmp_path / "first-PREVIEW.epub",
        model=model,
        profile_key="stable-reading-profile",
        cache_path=cache,
        require_epubcheck=False,
        short_text_words=0,
        short_sentence_words=0,
    )
    assert first.generated_paragraphs == 2

    class FailIfCalled:
        def complete_json(self, system, user):
            raise AssertionError("existing cache should be reused")

    resumed = translate_reading_book(
        source,
        tmp_path / "resumed-PREVIEW.epub",
        model=FailIfCalled(),
        profile_key="stable-reading-profile",
        cache_path=cache,
        require_epubcheck=False,
        short_text_words=0,
        short_sentence_words=6,
    )

    assert resumed.cached_paragraphs == 2
    assert resumed.generated_paragraphs == 0
    with EpubBook(resumed.output) as book:
        analysis = _detached_analysis_tree(book)
        cards = analysis.xpath("//*[contains(@class, 'epubllmt-reading-sentence-card')]")
        rendered = "\n".join("".join(card.itertext()) for card in cards)
        assert "This is the original paragraph." not in rendered
        assert "Although it is long, its meaning remains testable." in rendered


def test_reading_book_tells_model_which_short_sentences_need_no_aids(tmp_path):
    source = build_epub(tmp_path / "source.epub")
    model = _ReadingBookModel()
    translate_reading_book(
        source,
        tmp_path / "reading-PREVIEW.epub",
        model=model,
        profile_key="reading-profile",
        cache_path=tmp_path / "cache.sqlite3",
        require_epubcheck=False,
        short_text_words=0,
        short_sentence_words=6,
    )

    first_paragraph_request = next(
        json.loads(user)
        for _, user in model.calls
        if "original paragraph" in user
    )
    assert first_paragraph_request["excluded_aid_sentence_indices"] == [1]


def test_reading_book_skips_tiny_single_sentence_before_cache_or_model(tmp_path):
    source = build_epub(tmp_path / "source.epub")
    cache = tmp_path / "cache.sqlite3"
    first_model = _ReadingBookModel()
    first = translate_reading_book(
        source,
        tmp_path / "first-PREVIEW.epub",
        model=first_model,
        profile_key="stable-reading-profile",
        cache_path=cache,
        require_epubcheck=False,
        short_text_words=0,
    )
    assert first.generated_paragraphs == 2

    resumed_model = _ReadingBookModel()
    resumed = translate_reading_book(
        source,
        tmp_path / "resumed-PREVIEW.epub",
        model=resumed_model,
        profile_key="stable-reading-profile",
        cache_path=cache,
        require_epubcheck=False,
        short_text_words=15,
    )

    assert resumed.paragraphs == 0
    assert resumed.cached_paragraphs == 0
    assert resumed.generated_paragraphs == 0
    assert resumed.estimate.skipped_short_prose == 2
    assert resumed_model.calls == []

    restored_model = _ReadingBookModel()
    restored = translate_reading_book(
        source,
        tmp_path / "restored-PREVIEW.epub",
        model=restored_model,
        profile_key="stable-reading-profile",
        cache_path=cache,
        require_epubcheck=False,
        short_text_words=0,
    )
    assert restored.cached_paragraphs == 2
    assert restored.generated_paragraphs == 0
    assert restored_model.calls == []


def test_reading_tiny_text_threshold_covers_short_town_question():
    assert short_text_paragraph_kind("Is she still in town?", max_words=15) == "prose"
    fifteen_words = "one two three four five six seven eight nine ten eleven twelve thirteen fourteen fifteen"
    sixteen_words = f"{fifteen_words} sixteen"
    assert short_text_paragraph_kind(fifteen_words) == "prose"
    assert short_text_paragraph_kind(sixteen_words) is None


def test_reading_book_workers_generate_paragraphs_concurrently(tmp_path):
    source = build_epub(tmp_path / "source.epub")
    barrier = threading.Barrier(2)

    class ConcurrentModel:
        def __init__(self):
            self.active = 0
            self.max_active = 0
            self.lock = threading.Lock()

        def complete_json(self, system, user):
            payload = json.loads(user)
            with self.lock:
                self.active += 1
                self.max_active = max(self.max_active, self.active)
            try:
                barrier.wait(timeout=1)
                return {
                    "sentences": [
                        {
                            "index": item["index"],
                            "translation": f"译{item['index']}。",
                            "difficulty": "fluent",
                        }
                        for item in payload["sentences"]
                    ],
                    "aids": [],
                }
            finally:
                with self.lock:
                    self.active -= 1

    model = ConcurrentModel()
    result = translate_reading_book(
        source,
        tmp_path / "concurrent-PREVIEW.epub",
        model=model,
        profile_key="reading-concurrent-profile",
        cache_path=tmp_path / "cache.sqlite3",
        require_epubcheck=False,
        max_workers=2,
    )

    assert result.generated_paragraphs == 2
    assert model.max_active == 2


def test_reading_concurrent_failure_reports_successes_cached_during_shutdown(tmp_path):
    source = build_epub(tmp_path / "source.epub")
    barrier = threading.Barrier(2)
    events = []

    class OneFailureOneSuccess:
        def complete_json(self, system, user):
            payload = json.loads(user)
            barrier.wait(timeout=1)
            if "original paragraph" in payload["paragraph"]:
                return {
                    "sentences": [
                        {
                            "index": item["index"],
                            "translation": "unopened",
                            "difficulty": "effortful",
                        }
                        for item in payload["sentences"]
                    ],
                    "aids": [],
                }
            time.sleep(0.05)
            return {
                "sentences": [
                    {
                        "index": item["index"],
                        "translation": f"译{item['index']}。",
                        "difficulty": "fluent",
                    }
                    for item in payload["sentences"]
                ],
                "aids": [],
            }

    with pytest.raises(EpubError, match="核心结果无效"):
        translate_reading_book(
            source,
            tmp_path / "failed-PREVIEW.epub",
            model=OneFailureOneSuccess(),
            profile_key="reading-concurrent-failure-profile",
            cache_path=tmp_path / "cache.sqlite3",
            require_epubcheck=False,
            max_workers=2,
            on_progress=events.append,
        )

    assert max(event.completed_paragraphs for event in events) == 1


def test_reading_book_cache_isolated_from_legacy_payload_and_resumes_core_failure(tmp_path):
    from translator.cache import LearningCache
    source = build_epub(tmp_path / "source.epub")
    cache = tmp_path / "cache.sqlite3"
    with EpubBook(source) as book, LearningCache(cache) as stored:
        ref = book.paragraph(1, 1)
        stored.put(LearningCache.key(LearningCache.book_key(source), "legacy-profile", ref),
                   LearningCache.book_key(source), "legacy-profile", {"paragraph_translation": "旧格式"})
    failing = _ReadingBookModel(broken_second=True)
    with pytest.raises(EpubError, match="核心结果"):
        translate_reading_book(source, tmp_path / "failed-PREVIEW.epub", model=failing,
                               profile_key="reading-profile", cache_path=cache, require_epubcheck=False)
    resumed = _ReadingBookModel()
    result = translate_reading_book(source, tmp_path / "resumed-PREVIEW.epub", model=resumed,
                                    profile_key="reading-profile", cache_path=cache, require_epubcheck=False)
    assert result.cached_paragraphs == 1 and result.generated_paragraphs == 1
    assert len(resumed.calls) == 1


def test_reading_book_stops_after_actual_budget_exceeded_without_caching(tmp_path):
    from types import SimpleNamespace
    source = build_epub(tmp_path / "source.epub")
    class OverBudget(_ReadingBookModel):
        def __init__(self):
            super().__init__()
            self.usage = SimpleNamespace(total_tokens=0)
        def complete_json(self, system, user):
            result = super().complete_json(system, user)
            self.usage.total_tokens = 9999
            return result
    model = OverBudget()
    with pytest.raises(EpubError, match="实际 API token"):
        translate_reading_book(source, tmp_path / "budget-PREVIEW.epub", model=model,
                               profile_key="reading-profile", cache_path=tmp_path / "cache.sqlite3",
                               require_epubcheck=False, max_tokens=9000)
    assert len(model.calls) == 1


def test_reading_book_stops_when_bounded_run_lacks_usage_delta(tmp_path):
    source = build_epub(tmp_path / "source.epub")
    class MissingUsage(_ReadingBookModel):
        usage = SimpleNamespace(total_tokens=0)
    model = MissingUsage()
    with pytest.raises(EpubError, match="未报告本次请求"):
        translate_reading_book(source, tmp_path / "budget-PREVIEW.epub", model=model,
                               profile_key="reading-profile", cache_path=tmp_path / "cache.sqlite3",
                               require_epubcheck=False, max_tokens=99999)
    assert len(model.calls) == 1


def test_reading_p4_prior_context_and_directed_review_are_bounded_and_cached(tmp_path):
    source = build_epub(tmp_path / "source.epub")
    cache = tmp_path / "cache.sqlite3"

    class P4Model:
        def __init__(self):
            self.calls = []

        def complete_json(self, system, user):
            payload = json.loads(user)
            self.calls.append(payload)
            sentences = payload["sentences"]
            result = {
                "sentences": [
                    {"index": item["index"], "translation": f"译{item['index']}。", "difficulty": "fluent"}
                    for item in sentences
                ],
                "aids": [],
            }
            if len(self.calls) == 1:
                result["aids"] = [
                    {"scope": "sentence", "sentence_indices": [True], "text": "invalid"}
                ]
            return result

    model = P4Model()
    first = translate_reading_book(
        source, tmp_path / "first-PREVIEW.epub", model=model,
        profile_key="p4-profile", cache_path=cache, require_epubcheck=False,
        prior_context=True, directed_review=True,
    )
    assert first.generated_paragraphs == 2
    assert len(model.calls) == 3
    assert "draft" in model.calls[1]
    assert model.calls[0]["prior_paragraph"] is None
    assert model.calls[2]["prior_paragraph"] == model.calls[0]["paragraph"]

    second_model = P4Model()
    second = translate_reading_book(
        source, tmp_path / "second-PREVIEW.epub", model=second_model,
        profile_key="p4-profile", cache_path=cache, require_epubcheck=False,
        prior_context=True, directed_review=True,
    )
    assert second.cached_paragraphs == 2
    assert second_model.calls == []


def test_reading_book_two_stage_is_explicit_bounded_and_cached(tmp_path):
    source = build_epub(tmp_path / "source.epub")
    cache = tmp_path / "cache.sqlite3"

    class TwoStageModel:
        def __init__(self):
            self.calls = []

        def complete_json(self, system, user):
            payload = json.loads(user)
            self.calls.append((system, payload))
            result = {
                "sentences": [
                    {"index": item["index"], "translation": f"译{item['index']}。", "difficulty": "effortful"}
                    for item in payload["sentences"]
                ],
                "aids": [],
            }
            if len(self.calls) == 2:
                result["aids"] = [
                    {"scope": "sentence", "sentence_indices": [True], "text": "invalid"}
                ]
            return result

    model = TwoStageModel()
    first = translate_reading_book(
        source, tmp_path / "two-stage.epub", model=model,
        profile_key="two-stage-profile", cache_path=cache,
        require_epubcheck=False, pipeline="two-stage", directed_review=True,
    )
    assert first.generated_paragraphs == 2
    assert first.estimate.approximate_requests == 6
    assert len(model.calls) == 5
    assert "draft" not in model.calls[0][1]
    assert "draft" in model.calls[1][1]
    assert "draft" in model.calls[2][1]

    cached_model = TwoStageModel()
    second = translate_reading_book(
        source, tmp_path / "two-stage-cached.epub", model=cached_model,
        profile_key="two-stage-profile", cache_path=cache,
        require_epubcheck=False, pipeline="two-stage", directed_review=True,
    )
    assert second.cached_paragraphs == 2
    assert cached_model.calls == []


@pytest.mark.parametrize(
    ("translation", "message"),
    [
        ("简·奥斯汀出版了它。", "遗漏数字"),
    ],
)
def test_translation_rejects_obvious_detail_loss(translation, message):
    text = "In 1813, Jane Austen published it."
    fast = FakeModel(
        {
            "paragraph_translation": translation,
            "sentences": [
                {"index": 1, "text": text, "difficulty": "fluent"}
            ],
        }
    )
    with pytest.raises(EpubError, match=message):
        analyze_paragraph(text, fast, FakeModel({}))


def test_translation_rejects_abnormally_long_output():
    fast = FakeModel(
        {
            "paragraph_translation": "译" * 501,
            "sentences": [
                {"index": 1, "text": "Short.", "difficulty": "fluent"}
            ],
        }
    )
    with pytest.raises(EpubError, match="长度异常"):
        analyze_paragraph("Short.", fast, FakeModel({}))


def test_invalid_schema_is_retried_a_limited_number_of_times():
    syntax = StubSyntaxAnalyzer({1: LocalSyntaxResult()})
    fast = SequenceModel(
        [
            {"paragraph_translation": "", "sentences": []},
            {
                "paragraph_translation": "简单。",
                "sentences": [
                    {
                        "index": 1,
                        "text": "Easy.",
                        "difficulty": "fluent",
                    }
                ],
            },
        ]
    )
    result = analyze_paragraph_with_retries(
        "Easy.",
        fast,
        FakeModel({}),
        schema_retries=1,
        syntax_analyzer=syntax,
    )
    assert result.paragraph_translation == "简单。"
    assert len(fast.calls) == 2
    assert len(syntax.calls) == 1


def test_missing_sentence_translation_is_retried_and_never_silently_accepted():
    fast = SequenceModel(
        [
            {
                "translations": [{"index": 1, "translation": "第一句。"}],
                "sentences": [
                    {"index": 1, "difficulty": "fluent"},
                    {"index": 2, "difficulty": "fluent"},
                ],
            },
            {
                "translations": [
                    {"index": 1, "translation": "第一句。"},
                    {"index": 2, "translation": "第二句。"},
                ],
                "sentences": [
                    {"index": 1, "difficulty": "fluent"},
                    {"index": 2, "difficulty": "fluent"},
                ],
            },
        ],
        adapt_fast=False,
    )

    result = analyze_paragraph_with_retries(
        "First sentence. Second sentence.", fast, FakeModel({}), schema_retries=1
    )

    assert result.paragraph_translation == "第一句。第二句。"
    assert len(fast.calls) == 2
    assert "不得合并、跳过或只翻译第一句" in fast.calls[0][1]


def test_quality_prompt_requires_every_candidate_even_when_finally_fluent():
    text = "Although tired, he continued. Because it rained, she stayed."
    fast = FakeModel(
        {
            "paragraph_translation": "尽管疲惫，他仍继续。因为下雨，她留了下来。",
            "sentences": [
                {"index": 1, "difficulty": "effortful"},
                {"index": 2, "difficulty": "effortful"},
            ],
        }
    )
    quality = CandidateContractModel()

    result = analyze_paragraph(text, fast, quality)

    assert [card.sentence for card in result.cards] == [
        "Although tired, he continued."
    ]


def test_generate_preview_end_to_end_without_network(tmp_path):
    source = build_epub(tmp_path / "source.epub")
    output = tmp_path / "book-PREVIEW.epub"
    paragraph = (
        "This is the original paragraph. "
        "Although it is long, its meaning remains testable."
    )
    fast = FakeModel(
        {
                "paragraph_translation": "这是原段落。尽管它很长，但意思仍可验证。",
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
        }
    )
    quality = FakeModel(
        {
            "sentences": [
                {
                    "index": 2,
                    "text": "Although it is long, its meaning remains testable.",
                    "difficulty": "effortful",
                    "meaning": "尽管它很长，但意思仍可验证。",
                    "cues": "先让步，再给主要判断。",
                }
            ]
        }
    )
    result = generate_preview(
        source,
        output,
        chapter=1,
        paragraph=1,
        fast_model=fast,
        quality_model=quality,
        require_epubcheck=False,
    )
    assert result.paragraph.text == paragraph
    assert result.output == output
    assert output.is_file()
    assert '"title": "Test Book"' in fast.calls[0][1]
    assert '"authors": ["Test Author"]' in fast.calls[0][1]
    assert '"work_year": null' in fast.calls[0][1]
    with EpubBook(output) as book:
        assert book.link_issues() == set()


def test_preview_filename_must_be_visibly_marked(tmp_path):
    source = build_epub(tmp_path / "source.epub")
    with pytest.raises(EpubError, match="必须包含 PREVIEW"):
        generate_preview(
            source,
            tmp_path / "looks-final.epub",
            chapter=1,
            paragraph=1,
            fast_model=FakeModel({}),
            quality_model=FakeModel({}),
            require_epubcheck=False,
        )


def test_preview_trace_write_failure_stops_before_model_calls(tmp_path):
    source = build_epub(tmp_path / "source.epub")
    trace_path = tmp_path / "trace-is-a-file"
    trace_path.write_text("occupied")
    model = FakeModel({})

    with pytest.raises(OSError):
        generate_preview(
            source,
            tmp_path / "book-PREVIEW.epub",
            chapter=1,
            paragraph=1,
            fast_model=model,
            quality_model=model,
            require_epubcheck=False,
            trace_dir=trace_path,
        )
    assert model.calls == []
    assert not (tmp_path / "book-PREVIEW.epub").exists()


def test_output_conflicts_are_rejected_before_model_calls(tmp_path):
    source = build_epub(tmp_path / "source.epub")
    existing = tmp_path / "existing-PREVIEW.epub"
    existing.write_bytes(b"keep")
    model = FakeModel({})
    with pytest.raises(EpubError, match="已存在"):
        generate_preview(
            source,
            existing,
            chapter=1,
            paragraph=1,
            fast_model=model,
            quality_model=model,
            require_epubcheck=False,
        )
    assert model.calls == []
    assert existing.read_bytes() == b"keep"

    with pytest.raises(EpubError, match="覆盖输入"):
        translate_book(
            source,
            source,
            fast_model=model,
            quality_model=model,
            profile_key="profile",
            cache_path=tmp_path / "cache.sqlite3",
            require_epubcheck=False,
        )
    assert model.calls == []


def test_full_run_resumes_from_successful_cache_after_failure(tmp_path):
    source = build_epub(tmp_path / "source.epub")
    cache = tmp_path / "cache.sqlite3"
    quality_response = {
        "sentences": [
            {
                "index": 2,
                "text": "Although it is long, its meaning remains testable.",
                "difficulty": "effortful",
                "meaning": "尽管它很长，但意思仍可验证。",
                "cues": "先让步，再给主要判断。",
            }
        ]
    }
    with pytest.raises(RuntimeError, match="provider stopped"):
        translate_book(
            source,
            tmp_path / "should-not-exist.epub",
            fast_model=BookFastModel(fail_on_second=True),
            quality_model=FakeModel(quality_response),
            profile_key="profile-v1",
            cache_path=cache,
            max_workers=1,
            require_epubcheck=False,
            local_gate="off",
        )
    assert not (tmp_path / "should-not-exist.epub").exists()

    output = tmp_path / "complete.epub"
    result = translate_book(
        source,
        output,
        fast_model=BookFastModel(),
        quality_model=FakeModel(quality_response),
        profile_key="profile-v1",
        cache_path=cache,
        max_workers=1,
        require_epubcheck=False,
        local_gate="off",
    )
    assert result.cached_paragraphs == 1
    assert result.generated_paragraphs == 1
    assert output.is_file()
    with EpubBook(output) as book:
        assert book.link_issues() == set()
        assert len(book.paragraphs()) == 2


def test_full_run_reports_text_free_progress_for_cache_stages_and_completion(tmp_path):
    source = build_epub(tmp_path / "source.epub")
    events: list[TranslationProgress] = []
    result = translate_book(
        source,
        tmp_path / "complete.epub",
        fast_model=BookFastModel(),
        quality_model=FakeModel(
            {
                "sentences": [
                    {
                        "index": 2,
                        "difficulty": "effortful",
                        "meaning": "尽管它很长，但意思仍可验证。",
                        "cues": "",
                        "feel": "",
                        "note": "",
                    }
                ]
            }
        ),
        profile_key="progress-v1",
        cache_path=tmp_path / "cache.sqlite3",
        max_workers=1,
        require_epubcheck=False,
        local_gate="off",
        on_progress=events.append,
    )

    assert result.generated_paragraphs == 2
    assert events[0].phase == "cache"
    assert {event.phase for event in events} >= {
        "fast",
        "quality",
        "paragraph",
        "render",
        "epubcheck",
        "complete",
    }
    assert events[-1] == TranslationProgress(
        phase="complete",
        completed_paragraphs=2,
        total_paragraphs=2,
        cached_paragraphs=0,
        generated_paragraphs=2,
        source_paragraphs=2,
        skipped_paragraphs=0,
    )
    assert all(not hasattr(event, "text") for event in events)


def test_book_context_is_part_of_cache_identity(tmp_path):
    source = build_epub(tmp_path / "source.epub")
    cache = tmp_path / "cache.sqlite3"
    quality_response = {
        "sentences": [
            {
                "index": 2,
                "text": "Although it is long, its meaning remains testable.",
                "difficulty": "effortful",
                "meaning": "尽管它很长，但意思仍可验证。",
                "cues": "先让步，再给主要判断。",
            }
        ]
    }
    first_fast = BookFastModel()
    first = translate_book(
        source,
        tmp_path / "first.epub",
        fast_model=first_fast,
        quality_model=FakeModel(quality_response),
        profile_key="models-v1",
        cache_path=cache,
        max_workers=1,
        require_epubcheck=False,
        local_gate="off",
        work_year=1811,
    )
    assert first.generated_paragraphs == 2

    changed_fast = BookFastModel()
    changed = translate_book(
        source,
        tmp_path / "changed.epub",
        fast_model=changed_fast,
        quality_model=FakeModel(quality_response),
        profile_key="models-v1",
        cache_path=cache,
        max_workers=1,
        require_epubcheck=False,
        local_gate="off",
        work_year=1812,
    )
    assert changed.cached_paragraphs == 0
    assert changed.generated_paragraphs == 2

    same_fast = BookFastModel(fail_on_second=True)
    same = translate_book(
        source,
        tmp_path / "same.epub",
        fast_model=same_fast,
        quality_model=FakeModel({}),
        profile_key="models-v1",
        cache_path=cache,
        max_workers=1,
        require_epubcheck=False,
        local_gate="off",
        work_year=1812,
    )
    assert same.cached_paragraphs == 2
    assert same.generated_paragraphs == 0
    assert same_fast.calls == []


def test_full_run_skips_short_simple_paragraph_without_any_model_request(tmp_path):
    source = build_epub(tmp_path / "source.epub")
    fast = BookFastModel(fail_on_second=True)
    quality = FakeModel(
        {
            "sentences": [
                {
                    "index": 2,
                    "text": "Although it is long, its meaning remains testable.",
                    "difficulty": "effortful",
                    "meaning": "尽管它很长，但意思仍可验证。",
                    "cues": "先让步，再给主要判断。",
                }
            ]
        }
    )
    result = translate_book(
        source,
        tmp_path / "complete.epub",
        fast_model=fast,
        quality_model=quality,
        profile_key="profile-v2",
        cache_path=tmp_path / "cache.sqlite3",
        require_epubcheck=False,
    )
    assert result.paragraphs == 1
    assert result.estimate.skipped_short_prose == 1
    assert len(fast.calls) == 1
    assert len(quality.calls) == 1


def test_full_run_parses_each_uncached_paragraph_once(tmp_path):
    source = build_epub(tmp_path / "source.epub")
    syntax = StubSyntaxAnalyzer({})
    quality = FakeModel(
        {
            "sentences": [
                {
                    "index": 2,
                    "difficulty": "effortful",
                    "meaning": "尽管它很长，但意思仍可验证。",
                    "cues": "",
                    "feel": "",
                    "note": "",
                }
            ]
        }
    )

    result = translate_book(
        source,
        tmp_path / "complete.epub",
        fast_model=BookFastModel(),
        quality_model=quality,
        profile_key="profile-with-required-syntax",
        cache_path=tmp_path / "cache.sqlite3",
        max_workers=1,
        require_epubcheck=False,
        local_gate="off",
        syntax_analyzer=syntax,
    )

    assert result.generated_paragraphs == 2
    assert len(syntax.calls) == 2


def test_full_run_trace_separates_concurrent_paragraphs_without_changing_cache(tmp_path):
    source = build_epub(tmp_path / "source.epub")
    cache = tmp_path / "cache.sqlite3"
    trace_root = tmp_path / "traces"
    quality = FakeModel(
        {
            "sentences": [
                {
                    "index": 2,
                    "difficulty": "effortful",
                    "meaning": "尽管它很长，但意思仍可验证。",
                    "cues": "",
                    "feel": "",
                    "note": "",
                }
            ]
        }
    )

    first = translate_book(
        source,
        tmp_path / "first.epub",
        fast_model=BookFastModel(),
        quality_model=quality,
        profile_key="trace-profile",
        cache_path=cache,
        max_workers=2,
        require_epubcheck=False,
        local_gate="off",
        trace_dir=trace_root,
    )
    assert first.generated_paragraphs == 2
    run_dir, = trace_root.iterdir()
    assert (run_dir / "chapter-001" / "paragraph-001" / "paragraph-learning.json").is_file()
    assert (run_dir / "chapter-001" / "paragraph-002" / "paragraph-learning.json").is_file()
    replay_input = json.loads(
        (
            run_dir
            / "chapter-001"
            / "paragraph-001"
            / "quality-replay-input.json"
        ).read_text()
    )
    assert replay_input["version"] == 1
    assert replay_input["paragraph"] == "This is the original paragraph. Although it is long, its meaning remains testable."
    assert replay_input["candidate_indices"] == [2]
    assert replay_input["assessments"][1]["difficulty"] == "effortful"
    assert replay_input["stanza"]["version"] == 3

    second_fast = BookFastModel(fail_on_second=True)
    second = translate_book(
        source,
        tmp_path / "second.epub",
        fast_model=second_fast,
        quality_model=FakeModel({}),
        profile_key="trace-profile",
        cache_path=cache,
        max_workers=2,
        require_epubcheck=False,
        local_gate="off",
    )
    assert second.cached_paragraphs == 2
    assert second.generated_paragraphs == 0
    assert second_fast.calls == []


@pytest.mark.parametrize("language", [ENGLISH_TO_CHINESE, DIRECT_MEANING])
def test_quality_replay_uses_frozen_trace_without_fast_or_stanza_calls(tmp_path, language):
    fast = FakeModel(
        {
            "paragraph_translation": "这是原段落。尽管它很长，但意思仍可验证。",
            "sentences": [
                {"index": 1, "difficulty": "fluent"},
                {"index": 2, "difficulty": "effortful"},
            ],
        }
    )
    initial_quality = FakeModel(
        {
            "sentences": [
                {"index": 2, "difficulty": "effortful", "meaning": "初始结果。"}
            ]
        },
        adapt_fast=False,
    )
    syntax = StubSyntaxAnalyzer({})
    trace = ParagraphTrace(tmp_path / "source-trace")
    analyze_paragraph(
        "This is the original paragraph. Although it is long, its meaning remains testable.",
        fast,
        initial_quality,
        syntax_analyzer=syntax,
        trace=trace,
        language=language,
        profile=ReadingProfile((CalibrationExample("Although it is long, its meaning remains testable.", "effortful"),)),
    )

    replay_model = FakeModel(
        {
            "sentences": [
                {"index": 2, "difficulty": "effortful", "meaning": "冻结输入的结果。"}
            ]
        },
        adapt_fast=False,
    )
    result = replay_quality(
        trace.directory / "quality-replay-input.json",
        replay_model,
        quality_payload_mode="compact-v5",
        language=language,
    )

    assert result.candidate_indices == (2,)
    assert result.cards[0].meaning == "冻结输入的结果。"
    assert len(replay_model.calls) == 1
    assert len(fast.calls) == 1
    assert len(syntax.calls) == 1
    assert '"index": 2' in replay_model.calls[0][1]
    assert replay_model.calls[0] == initial_quality.calls[0]


@pytest.mark.parametrize("tamper, message", [
    ("missing", "缺少 fast 困难候选"),
    ("boolean_candidate", "候选索引格式无效"),
    ("boolean_source", "原句索引与段落不一致"),
])
def test_quality_replay_rejects_tampered_candidate_indices_before_model_call(tmp_path, tamper, message):
    payload = {
        "version": 1,
        "language": {
            "key": ENGLISH_TO_CHINESE.key,
            "card_format": "sentence-meaning-v1",
            "quality_system_sha256": hashlib.sha256(
                ENGLISH_TO_CHINESE.quality_system.encode("utf-8")
            ).hexdigest(),
        },
        "paragraph": "A short sentence. Another difficult sentence.",
        "sentences": [
            {"index": 1, "text": "A short sentence."},
            {"index": 2, "text": "Another difficult sentence."},
        ],
        "assessments": [
            {"index": 1, "text": "A short sentence.", "difficulty": "fluent", "reason": "", "confidence": 0.5},
            {"index": 2, "text": "Another difficult sentence.", "difficulty": "effortful", "reason": "", "confidence": 0.5},
        ],
        "candidate_indices": [1],
        "stanza": {"version": 3, "results": []},
        "book_context": {"title": None, "authors": [], "work_year": None},
        "profile": None,
        "density": "medium",
    }
    if tamper == "boolean_candidate":
        payload["candidate_indices"] = [True, 2]
    elif tamper == "boolean_source":
        payload["sentences"][0]["index"] = True
    digest = lambda value: hashlib.sha256(
        json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    payload["book_context_sha256"] = digest(payload["book_context"])
    payload["profile_sha256"] = digest(None)
    input_path = tmp_path / "tampered.json"
    input_path.write_text(json.dumps(payload), encoding="utf-8")
    unused = FakeModel({}, adapt_fast=False)

    with pytest.raises(EpubError, match=message):
        replay_quality(input_path, unused)

    assert unused.calls == []


def test_stanza_cache_survives_api_failure_and_is_independent_of_model_profile(tmp_path):
    source = build_epub(tmp_path / "source.epub")
    cache = tmp_path / "cache.sqlite3"
    syntax_result = {
        2: LocalSyntaxResult(
            needs_structure=True,
            clause_count=1,
            max_clause_depth=1,
            reasons=("relative-clause",),
            hints=(
                ClauseHint(
                    "acl:relcl",
                    "Although it is long",
                    "meaning",
                    1,
                ),
            ),
        )
    }
    first_syntax = StubSyntaxAnalyzer(syntax_result)

    with pytest.raises(RuntimeError, match="provider stopped"):
        translate_book(
            source,
            tmp_path / "failed.epub",
            fast_model=FailingModel(),
            quality_model=FakeModel({}),
            profile_key="model-profile-a",
            syntax_profile_key="stanza-accurate-v1",
            cache_path=cache,
            max_workers=1,
            require_epubcheck=False,
            syntax_analyzer=first_syntax,
        )

    assert len(first_syntax.calls) == 1
    assert not (tmp_path / "failed.epub").exists()

    second_syntax = StubSyntaxAnalyzer({})
    quality = FakeModel(
        {
            "sentences": [
                {
                    "index": 2,
                    "difficulty": "effortful",
                    "meaning": "尽管它很长，但意思仍可验证。",
                    "cues": "",
                    "feel": "",
                    "note": "",
                }
            ]
        }
    )
    result = translate_book(
        source,
        tmp_path / "complete.epub",
        fast_model=BookFastModel(),
        quality_model=quality,
        profile_key="model-profile-b",
        syntax_profile_key="stanza-accurate-v1",
        cache_path=cache,
        max_workers=1,
        require_epubcheck=False,
        syntax_analyzer=second_syntax,
    )

    assert result.generated_paragraphs == 1
    assert second_syntax.calls == []
    assert '"clause": "Although it is long"' in quality.calls[0][1]


def test_api_failure_does_not_start_the_next_queued_paragraph(tmp_path):
    source = build_epub(tmp_path / "source.epub")
    syntax = StubSyntaxAnalyzer({})
    fast = FailingModel()

    with pytest.raises(RuntimeError, match="provider stopped"):
        translate_book(
            source,
            tmp_path / "failed.epub",
            fast_model=fast,
            quality_model=FakeModel({}),
            profile_key="bounded-queue-v1",
            syntax_profile_key="stanza-accurate-v1",
            cache_path=tmp_path / "cache.sqlite3",
            max_workers=1,
            require_epubcheck=False,
            local_gate="off",
            syntax_analyzer=syntax,
        )

    assert len(syntax.calls) == 1
    assert len(fast.calls) == 1
    assert not (tmp_path / "failed.epub").exists()


def test_stanza_failure_stops_before_model_and_does_not_start_next_paragraph(tmp_path):
    source = build_epub(tmp_path / "source.epub")
    syntax = FailingSyntaxAnalyzer()
    unused = FakeModel({})

    with pytest.raises(RuntimeError, match="stanza stopped"):
        translate_book(
            source,
            tmp_path / "failed.epub",
            fast_model=unused,
            quality_model=unused,
            profile_key="bounded-queue-v1",
            syntax_profile_key="stanza-accurate-v1",
            cache_path=tmp_path / "cache.sqlite3",
            max_workers=1,
            require_epubcheck=False,
            local_gate="off",
            syntax_analyzer=syntax,
        )

    assert len(syntax.calls) == 1
    assert unused.calls == []
    assert not (tmp_path / "failed.epub").exists()


def test_api_failure_stops_another_inflight_paragraph_before_quality_stage(tmp_path):
    source = build_epub(tmp_path / "source.epub")
    fast = CoordinatedFailingFastModel()
    quality = FakeModel(
        {
            "sentences": [
                {
                    "index": 1,
                    "difficulty": "effortful",
                    "meaning": "第二段有一条作者注释。",
                    "cues": "",
                    "feel": "",
                    "note": "",
                }
            ]
        }
    )

    with pytest.raises(RuntimeError, match="provider stopped"):
        translate_book(
            source,
            tmp_path / "failed.epub",
            fast_model=fast,
            quality_model=quality,
            profile_key="cooperative-stop-v1",
            syntax_profile_key="stanza-accurate-v1",
            cache_path=tmp_path / "cache.sqlite3",
            max_workers=2,
            require_epubcheck=False,
            local_gate="off",
            syntax_analyzer=StubSyntaxAnalyzer({}),
        )

    assert len(fast.calls) == 2
    assert quality.calls == []
    assert not (tmp_path / "failed.epub").exists()


def test_keyboard_interrupt_resumes_with_learning_and_syntax_caches_consistent(tmp_path):
    source = build_epub(tmp_path / "source.epub")
    cache = tmp_path / "cache.sqlite3"
    syntax = StubSyntaxAnalyzer({})
    quality_response = {
        "sentences": [
            {
                "index": 2,
                "difficulty": "effortful",
                "meaning": "尽管它很长，但意思仍可验证。",
                "cues": "",
                "feel": "",
                "note": "",
            }
        ]
    }

    with pytest.raises(KeyboardInterrupt):
        translate_book(
            source,
            tmp_path / "interrupted.epub",
            fast_model=InterruptingBookFastModel(),
            quality_model=FakeModel(quality_response),
            profile_key="resume-profile-v1",
            syntax_profile_key="stanza-accurate-v1",
            cache_path=cache,
            max_workers=1,
            require_epubcheck=False,
            local_gate="off",
            syntax_analyzer=syntax,
        )

    assert len(syntax.calls) == 2
    assert not (tmp_path / "interrupted.epub").exists()

    resumed_syntax = StubSyntaxAnalyzer({})
    resumed_fast = BookFastModel()
    result = translate_book(
        source,
        tmp_path / "complete.epub",
        fast_model=resumed_fast,
        quality_model=FakeModel(quality_response),
        profile_key="resume-profile-v1",
        syntax_profile_key="stanza-accurate-v1",
        cache_path=cache,
        max_workers=1,
        require_epubcheck=False,
        local_gate="off",
        syntax_analyzer=resumed_syntax,
    )

    assert result.cached_paragraphs == 1
    assert result.generated_paragraphs == 1
    assert resumed_syntax.calls == []
    assert len(resumed_fast.calls) == 1


def test_dry_run_enforces_token_limit_without_calling_models(tmp_path):
    source = build_epub(tmp_path / "source.epub")
    unused = FakeModel({})
    syntax = StubSyntaxAnalyzer({})
    observed_estimates = []
    estimate = translate_book(
        source,
        tmp_path / "unused.epub",
        fast_model=unused,
        quality_model=unused,
        profile_key="profile",
        cache_path=tmp_path / "cache.sqlite3",
        require_epubcheck=False,
        dry_run=True,
        requests_per_minute=30,
        on_estimate=lambda value: observed_estimates.append(value),
        syntax_analyzer=syntax,
    )
    assert isinstance(estimate, TranslationEstimate)
    assert estimate.source_paragraphs == 2
    assert estimate.paragraphs == 1
    assert estimate.skipped_short_prose == 1
    assert estimate.approximate_requests == 2
    assert estimate.approximate_seconds == 4.0
    assert observed_estimates == [estimate]
    assert unused.calls == []
    assert syntax.calls == []
    with pytest.raises(EpubError, match="超过上限"):
        translate_book(
            source,
            tmp_path / "unused.epub",
            fast_model=unused,
            quality_model=unused,
            profile_key="profile",
            cache_path=tmp_path / "cache.sqlite3",
            require_epubcheck=False,
            dry_run=True,
            max_tokens=1,
        )


@pytest.mark.parametrize(
    ("options", "message"),
    [
        ({"max_tokens": 0}, "token 上限"),
        ({"max_cost": -1}, "费用上限"),
        ({"input_price_per_million": 1}, "同时提供"),
        (
            {
                "input_price_per_million": -1,
                "output_price_per_million": 1,
            },
            "不能为负数",
        ),
        ({"schema_retries": -1}, "结构纠正"),
        ({"work_year": 0}, "作品年代"),
    ],
)
def test_dry_run_rejects_invalid_budget_and_retry_options(tmp_path, options, message):
    source = build_epub(tmp_path / "source.epub")
    unused = FakeModel({})
    with pytest.raises(EpubError, match=message):
        translate_book(
            source,
            tmp_path / "unused.epub",
            fast_model=unused,
            quality_model=unused,
            profile_key="profile",
            cache_path=tmp_path / "cache.sqlite3",
            require_epubcheck=False,
            dry_run=True,
            **options,
        )
    assert unused.calls == []
