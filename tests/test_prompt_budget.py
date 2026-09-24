from __future__ import annotations

import json

from translator.prompt_budget import approximate_prompt_tokens
from translator.profile import CalibrationExample, ReadingProfile
from translator.sentence_analyzer import ClauseHint, LocalSyntaxResult
from translator.translator import BookContext, analyze_paragraph
from translator.languages import GLM52_MEANING_ONLY


def test_approximate_prompt_tokens_is_deterministic_for_mixed_prompt_text():
    assert approximate_prompt_tokens("") == 0
    assert approximate_prompt_tokens("abcd") == 1
    assert approximate_prompt_tokens("中文") == 2
    assert approximate_prompt_tokens("!") == 1
    assert approximate_prompt_tokens("abcd", "中文") == 3


class _CaptureModel:
    def __init__(self, response):
        self.response = response
        self.calls = []

    def complete_json(self, system, user):
        self.calls.append((system, user))
        return self.response


class _SyntaxStub:
    def __init__(self, results):
        self.results = results

    def analyze(self, _sentences):
        return self.results


def _legacy_quality_user(
    text, sentence_payload, assessments, profile, book_context, syntax_results
):
    """Historical v20 quality wire fixture, independent of current builders."""
    user = (
        "复核候选句。只能返回候选索引；原句由程序按索引从输入中取回。"
        "候选索引必须全部逐一返回且恰好一次，包括最终判断为 fluent 的候选。"
        "fluent 项只返回 index 和 difficulty；effortful 或 blocking 项只生成一句 meaning。"
        "不要返回 cues、feel、note 或任何其他解释字段。"
        "返回："
        '{"sentences":[{"index":1,"difficulty":"effortful|blocking|fluent",'
        '"basis":"structure|vocabulary|both",'
        '"meaning":"仅概括同一候选原句明确含义的一句中文"}]}。\n'
    )
    candidates = [item["index"] for item in assessments if item["difficulty"] != "fluent"]
    user += (
        f"段落：{text}\n"
        f"书籍上下文：{json.dumps(book_context.prompt_payload(), ensure_ascii=False)}\n"
        "书籍上下文只用于判断年代词义与语气，不得用模型记忆补写当前段落。\n"
        "所有内容只能依据同一候选原句；亲属关系断言必须由同一候选原句明确写出。"
        "不得引用段内其他句、前文、下文或模型记忆补写人物关系、心理动机和情节。\n"
        f"全部句子：{json.dumps(sentence_payload, ensure_ascii=False)}\n"
        f"候选索引：{candidates}\n"
        "初步判断："
        + json.dumps(assessments, ensure_ascii=False)
    )
    user += "\n读者代表样本：" + json.dumps(
        profile.prompt_examples(), ensure_ascii=False
    )
    syntax_evidence = [
        {
            "index": index,
            "reasons": list(result.reasons),
            "hints": [hint.prompt_payload() for hint in result.hints],
        }
        for index, result in sorted(syntax_results.items())
        if index in candidates and result.hints
    ]
    return (
        user
        + "\n本地 Stanza 从句证据："
        + json.dumps(syntax_evidence, ensure_ascii=False)
        + "\n从句证据只用于理解同一句；clause 与 target 是原句中的固定锚点。"
        "不得扩写锚点没有表达的人物关系、动机或情节。"
    )


def test_quality_prompt_budget_is_at_least_25_percent_below_v20_fixture():
    text = (
        "Although tired, brother continued through the long evening. "
        "Because it rained, sister stayed near the window."
    )
    sentence_payload = [
        {"index": 1, "text": "Although tired, brother continued through the long evening."},
        {"index": 2, "text": "Because it rained, sister stayed near the window."},
    ]
    assessments = [
        {
            "index": 1,
            "difficulty": "effortful",
            "reason": "让步从句与较长补语",
            "confidence": 0.8,
        },
        {
            "index": 2,
            "difficulty": "effortful",
            "reason": "因果从句",
            "confidence": 0.8,
        },
    ]
    profile = ReadingProfile(
        examples=tuple(
            CalibrationExample(f"{label} calibration example number {index}.", label)
            for label in ("fluent", "effortful")
            for index in range(1, 4)
        )
    )
    book_context = BookContext("Example Novel", ("Example Author",), 1900)
    syntax_results = {
        1: LocalSyntaxResult(
            reasons=("subordinate-clause",),
            hints=(ClauseHint("advcl", "Although tired", "continued", 1),),
        ),
        2: LocalSyntaxResult(
            reasons=("subordinate-clause",),
            hints=(ClauseHint("advcl", "Because it rained", "stayed", 1),),
        ),
    }
    fast = _CaptureModel(
        {
            "translations": [
                {"index": 1, "translation": "尽管疲惫，brother 仍坚持到漫长的夜晚。"},
                {"index": 2, "translation": "因为下雨，sister 留在窗边。"},
            ],
            "sentences": assessments,
        }
    )
    quality = _CaptureModel(
        {
            "sentences": [
                {
                    "index": 1,
                    "difficulty": "effortful",
                    "basis": "structure",
                    "meaning": "尽管疲惫，brother 仍坚持到了漫长的夜晚。",
                },
                {
                    "index": 2,
                    "difficulty": "effortful",
                    "basis": "structure",
                    "meaning": "因为下雨，sister 留在窗边。",
                },
            ]
        }
    )

    analyze_paragraph(
        text,
        fast,
        quality,
        profile=profile,
        book_context=book_context,
        language=GLM52_MEANING_ONLY,
        syntax_analyzer=_SyntaxStub(syntax_results),
    )

    old_tokens = approximate_prompt_tokens(
        GLM52_MEANING_ONLY.quality_system,
        _legacy_quality_user(
            text,
            sentence_payload,
            assessments,
            profile,
            book_context,
            syntax_results,
        ),
    )
    new_tokens = approximate_prompt_tokens(*quality.calls[0])

    assert new_tokens <= old_tokens * 0.75, (old_tokens, new_tokens)
