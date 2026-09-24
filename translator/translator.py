"""Learning-content orchestration and the first end-to-end preview workflow."""

from __future__ import annotations

import json
import hashlib
import math
import os
import re
import tempfile
import threading
from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Protocol

from translator.cache import LearningCache
from translator.epub_processor import (
    DEFAULT_ANALYSIS_LINK_TEXT,
    EpubBook,
    EpubError,
    EpubcheckResult,
    LearningCard,
    ParagraphNote,
    ParagraphRef,
    run_epubcheck,
)
from translator.sentence_analyzer import (
    ClauseHint,
    LocalSyntaxResult,
    Sentence,
    SyntaxAnalyzer,
    local_model_gate,
    short_text_paragraph_kind,
    split_sentences,
)
from translator.profile import CalibrationExample, ReadingProfile
from translator.languages import (
    ENGLISH_SOURCE,
    ENGLISH_TO_CHINESE,
    JAPANESE_SOURCE,
    LanguageModule,
    SourceLanguage,
    japanese_short_text_gate,
    source_uses_english_local_heuristics,
    split_source_sentences,
    validate_source_card_text,
    validate_default_translation,
)
from translator.llm_api import InvalidJSONResponse, LLMError
from translator.trace import ParagraphTrace, TraceRun

VALIDATION_VERSION = "learning-validation-v2-p0"
READING_REVIEW_POLICY_VERSION = "fronted-inverted-conditional-v1"
FRONTED_INVERTED_CONDITIONAL = re.compile(
    r"\bFor\b[^.!?;]{1,240},\s*(?:had|were|should)\b[^.!?;]{1,240},"
    r"\s*[^.!?;]{0,160}\bwould\s+have\b",
    re.IGNORECASE | re.DOTALL,
)


class JSONModel(Protocol):
    def complete_json(self, system: str, user: str) -> dict: ...


def generate_reading_assistance(prepared: dict, model: JSONModel, *,
                                pipeline: str, trace: ParagraphTrace | None,
                                repair_core: bool = False,
                                source_language: SourceLanguage = ENGLISH_SOURCE):
    """Explicit P1 path: one or two calls, no legacy filters, cache or retries."""
    from translator.reading_assistance import (
        AssistanceError,
        ParagraphAssistance,
        parse_assistance,
        without_paragraph_aids,
    )
    from translator.reading_prompts import reading_system

    if pipeline not in ("single", "two-stage"):
        raise AssistanceError("invalid reading pipeline")
    common = {key: value for key, value in prepared.items() if key != "expected_aids"}
    if prepared["mode"] == "generation":
        common["required_aids"] = prepared["expected_aids"]
    stages = ("single",) if pipeline == "single" else ("draft", "review")
    draft = None
    last_valid_result = None
    core_repair_used = False
    paragraph_aids = prepared.get("paragraph_aids", True)
    for stage in stages:
        user = json.dumps({**common, **({"draft": draft} if draft else {})}, ensure_ascii=False)
        system = reading_system(
            stage, paragraph_aids=paragraph_aids, source_language=source_language,
        )
        call = trace.start_call(f"reading-{stage}", model, system, user) if trace else None
        raw = None
        try:
            raw = call.complete() if call else model.complete_json(system, user)
        except InvalidJSONResponse as exc:
            if not repair_core or core_repair_used:
                raise
            result = repair_reading_core(
                prepared, exc.content, str(exc), model, trace=trace,
                source_language=source_language,
            )
            core_repair_used = True
            if pipeline == "single" or stage == "review":
                return result
            draft = result.to_dict()
            last_valid_result = result
            continue
        except LLMError:
            # Transport/provider failures have no invalid content to repair.
            # The HTTP client owns bounded retries for these failures.
            raise
        try:
            result = parse_assistance(raw, prepared["paragraph"], source_language=source_language)
        except AssistanceError as exc:
            if call and isinstance(exc, AssistanceError):
                call.write_normalized({"status": "core_failure", "error": str(exc)})
            if repair_core and not core_repair_used:
                result = repair_reading_core(
                    prepared, raw, str(exc), model, trace=trace, source_language=source_language,
                )
                core_repair_used = True
                if pipeline == "single" or stage == "review":
                    return result
            elif (
                stage == "review"
                and repair_core
                and core_repair_used
                and last_valid_result is not None
            ):
                return ParagraphAssistance(
                    last_valid_result.schema_version,
                    last_valid_result.input_identity,
                    last_valid_result.source_sentences,
                    last_valid_result.sentences,
                    last_valid_result.aids,
                    last_valid_result.status,
                    tuple((*last_valid_result.diagnostics, {
                        "reason": "review_fallback",
                        "review_error": str(exc),
                    })),
                )
            else:
                raise
        if not paragraph_aids:
            result = without_paragraph_aids(result)
        if call:
            call.write_normalized(result.to_dict())
        last_valid_result = result
        draft = {"sentences": result.to_dict()["sentences"], "aids": []}
    return result


def repair_reading_core(prepared: dict, invalid_draft: object, validation_error: str,
                        model: JSONModel, *, trace: ParagraphTrace | None,
                        source_language: SourceLanguage = ENGLISH_SOURCE):
    """Make one explicit replacement call after a failed core result; never recurse."""
    from translator.reading_assistance import (
        AssistanceError,
        ParagraphAssistance,
        parse_assistance,
        without_paragraph_aids,
    )
    from translator.reading_prompts import reading_system

    user = json.dumps({
        **{key: value for key, value in prepared.items() if key != "expected_aids"},
        "invalid_draft": invalid_draft,
        "validation_error": validation_error,
    }, ensure_ascii=False)
    paragraph_aids = prepared.get("paragraph_aids", True)
    system = reading_system(
        "repair", paragraph_aids=paragraph_aids, source_language=source_language,
    )
    call = trace.start_call(
        "reading-core-repair", model, system, user,
    ) if trace else None
    raw = call.complete() if call else model.complete_json(system, user)
    try:
        parsed = parse_assistance(raw, prepared["paragraph"], source_language=source_language)
    except AssistanceError as exc:
        if call:
            call.write_normalized({"status": "core_failure", "error": str(exc)})
        raise
    repaired = ParagraphAssistance(
        parsed.schema_version, parsed.input_identity, parsed.source_sentences,
        parsed.sentences, parsed.aids, parsed.status,
        tuple((*parsed.diagnostics, {
            "reason": "core_repair", "initial_error": validation_error,
        })),
    )
    if not paragraph_aids:
        repaired = without_paragraph_aids(repaired)
    if call:
        call.write_normalized(repaired.to_dict())
    return repaired


def _reading_review_focus(paragraph: str) -> str | None:
    if not FRONTED_INVERTED_CONDITIONAL.search(paragraph):
        return None
    return (
        f"{READING_REVIEW_POLICY_VERSION}: In the English pattern "
        '“For ..., had/were/should ..., ... would have ...”, the fronted phrase '
        "modifies the counterfactual main clause. Verify that translations and aids "
        "do not attach it to an actual action stated outside that conditional."
    )


def needs_reading_review(result, prepared: dict) -> bool:
    """P4's deterministic review gate; never review every successful paragraph."""
    if result.status in {"partial_aids", "translation_only"}:
        return True
    if _reading_review_focus(prepared["paragraph"]):
        return True
    expected = prepared.get("expected_aids", [])
    return any(not any(
        aid.scope == target["scope"]
        and list(aid.sentence_indices) == target["sentence_indices"]
        and aid.quote == target.get("quote")
        for aid in result.aids
    ) for target in expected)


def review_reading_assistance(
    prepared: dict, draft, model: JSONModel, *, trace: ParagraphTrace | None,
    source_language: SourceLanguage = ENGLISH_SOURCE,
):
    """Run one explicit P4 revision, returning a complete replacement result."""
    from translator.reading_assistance import (
        AssistanceError,
        ParagraphAssistance,
        without_paragraph_aids,
    )
    from translator.reading_prompts import reading_system
    request = {
        **{k: v for k, v in prepared.items() if k != "expected_aids"},
        "draft": {"sentences": draft.to_dict()["sentences"], "aids": draft.to_dict()["aids"]},
    }
    if focus := _reading_review_focus(prepared["paragraph"]):
        request["review_focus"] = focus
    user = json.dumps(request, ensure_ascii=False)
    paragraph_aids = prepared.get("paragraph_aids", True)
    system = reading_system(
        "review", paragraph_aids=paragraph_aids, source_language=source_language,
    )
    call = trace.start_call("reading-directed-review", model, system, user) if trace else None
    raw = call.complete() if call else model.complete_json(system, user)
    try:
        parsed = __import__("translator.reading_assistance", fromlist=["parse_assistance"]).parse_assistance(
            raw, prepared["paragraph"], source_language=source_language,
        )
    except AssistanceError as exc:
        if call:
            call.write_normalized({"status": "core_failure", "error": str(exc)})
        raise
    revised = ParagraphAssistance(parsed.schema_version, parsed.input_identity, parsed.source_sentences,
                                  parsed.sentences, parsed.aids, parsed.status,
                                  tuple((*parsed.diagnostics, {"reason": "directed_review", "draft_identity": draft.input_identity})))
    if not paragraph_aids:
        revised = without_paragraph_aids(revised)
    if call:
        call.write_normalized(revised.to_dict())
    return revised


class _TranslationCancelled(RuntimeError):
    """Internal cooperative cancellation after another paragraph fails."""


def _payload_sha256(payload: object) -> str:
    serialized = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(serialized.encode("utf-8")).hexdigest()


def _raise_if_cancelled(cancel_event: threading.Event | None) -> None:
    if cancel_event is not None and cancel_event.is_set():
        raise _TranslationCancelled("另一个段落失败，停止当前段落")


@dataclass(frozen=True)
class BookContext:
    title: str = ""
    authors: tuple[str, ...] = ()
    work_year: int | None = None

    def prompt_payload(self) -> dict[str, object]:
        return {
            "title": self.title or None,
            "authors": list(self.authors),
            "work_year": self.work_year,
        }

    def quality_prompt_payload(self) -> dict[str, object]:
        """Return only the book metadata relevant to sentence-local review."""
        return {"work_year": self.work_year} if self.work_year is not None else {}


@dataclass(frozen=True)
class Assessment:
    index: int
    text: str
    difficulty: str
    reason: str = ""
    confidence: float = 0.5


@dataclass(frozen=True)
class ParagraphLearning:
    paragraph_translation: str
    assessments: tuple[Assessment, ...]
    cards: tuple[LearningCard, ...]

    def as_note(self) -> ParagraphNote:
        return ParagraphNote(self.paragraph_translation, self.cards)


@dataclass(frozen=True)
class PreviewResult:
    output: Path
    paragraph: ParagraphRef
    learning: ParagraphLearning
    epubcheck_input_ran: bool
    epubcheck_output_ran: bool
    epubcheck_input_warnings: tuple[str, ...] = ()
    epubcheck_output_warnings: tuple[str, ...] = ()


@dataclass(frozen=True)
class ReadingPreviewResult:
    output: Path
    paragraph: ParagraphRef
    epubcheck_input_ran: bool
    epubcheck_output_ran: bool
    epubcheck_input_warnings: tuple[str, ...] = ()
    epubcheck_output_warnings: tuple[str, ...] = ()


@dataclass(frozen=True)
class ReadingTranslationResult:
    output: Path
    paragraphs: int
    cached_paragraphs: int
    generated_paragraphs: int
    estimate: TranslationEstimate
    epubcheck_output_warnings: tuple[str, ...] = ()
    frozen_manifest: Path | None = None


@dataclass(frozen=True)
class TranslationEstimate:
    source_paragraphs: int
    paragraphs: int
    skipped_short_dialogue: int
    skipped_short_prose: int
    skipped_local_easy: int
    approximate_requests: int
    approximate_input_tokens: int
    approximate_output_tokens: int
    approximate_cost: float | None
    approximate_seconds: float | None
    syntax_requests: int = 0
    epubcheck_input_warnings: tuple[str, ...] = ()
    skipped_japanese_short_text: int = 0
    japanese_short_text_coordinates: tuple[tuple[int, int], ...] = ()
    skipped_japanese_short_text_chars: int = 0
    japanese_short_text_char_coordinates: tuple[tuple[int, int], ...] = ()
    japanese_short_text_chars: int = 0
    skipped_japanese_fixed_responses: int = 0
    japanese_fixed_response_coordinates: tuple[tuple[int, int], ...] = ()

    @property
    def approximate_total_tokens(self) -> int:
        return self.approximate_input_tokens + self.approximate_output_tokens


@dataclass(frozen=True)
class TranslationResult:
    output: Path
    paragraphs: int
    cached_paragraphs: int
    generated_paragraphs: int
    estimate: TranslationEstimate
    epubcheck_output_warnings: tuple[str, ...] = ()


@dataclass(frozen=True)
class TranslationProgress:
    """A safe, text-free snapshot of a full translation run."""

    phase: str
    completed_paragraphs: int
    total_paragraphs: int
    cached_paragraphs: int
    generated_paragraphs: int
    source_paragraphs: int = 0
    skipped_paragraphs: int = 0
    chapter: int | None = None
    paragraph: int | None = None
    retry: int | None = None


def analyze_paragraph(
    text: str,
    fast_model: JSONModel,
    quality_model: JSONModel,
    *,
    profile: ReadingProfile | None = None,
    density: str = "medium",
    language: LanguageModule = ENGLISH_TO_CHINESE,
    book_context: BookContext | None = None,
    syntax_analyzer: SyntaxAnalyzer | None = None,
    syntax_results: dict[int, LocalSyntaxResult] | None = None,
    quality_payload_mode: str = "compact-v5",
    cancel_event: threading.Event | None = None,
    trace: ParagraphTrace | None = None,
    on_stage: Callable[[str, int | None], None] | None = None,
    source_language: SourceLanguage = ENGLISH_SOURCE,
) -> ParagraphLearning:
    if density not in {"low", "medium", "high"}:
        raise EpubError(f"无效辅助密度: {density}")
    if quality_payload_mode != "compact-v5":
        raise EpubError(f"无效 quality payload 模式: {quality_payload_mode}")
    sentences = split_source_sentences(source_language, text)
    if not sentences:
        raise EpubError("所选段落没有可处理的英文句子")
    sentence_payload = [
        {"index": sentence.index, "text": sentence.text} for sentence in sentences
    ]
    resolved_syntax = (
        syntax_results
        if syntax_results is not None
        else syntax_analyzer.analyze(sentences) if syntax_analyzer else {}
    )
    if trace is not None:
        trace.write("validation-policy", {"version": VALIDATION_VERSION})
        trace.write("stanza", _syntax_to_dict(resolved_syntax))
    _raise_if_cancelled(cancel_event)
    fast_user = (
        "处理下面一个段落。返回："
        '{"translations":[{"index":1,"translation":"该索引原句的完整中文译文"}],"sentences":['
        '{"index":1,"difficulty":"fluent|effortful|blocking",'
        '"reason":"简短原因","confidence":0.0}]}。\n'
        "translations 必须覆盖输入的每一个 index，按 index 升序，每个 index 恰好一次；"
        "不得合并、跳过或只翻译第一句。每条 translation 只翻译同 index 的原句，"
        "保留该句末尾标点。程序会按索引把它们拼成段译。\n"
        f"句子：{json.dumps(sentence_payload, ensure_ascii=False)}\n"
        f"书籍上下文：{json.dumps((book_context or BookContext()).prompt_payload(), ensure_ascii=False)}\n"
        "书籍上下文只用于判断语言年代、词义和语气；不得引用记忆中的情节、现成译本或段落之外的信息补写原文。\n"
        "不得擅自补足原文未明确表达的年龄、亲属关系或代词指向；指代不清时采用最少假设的译法。\n"
        "先完整理解句意、指代和逻辑，再组织中文，不要按从句片段逐块翻译。\n"
        "译文自检：中文谓语和搭配必须自然；代词关系必须清楚；"
        "不照搬英文被动、形式主语、名词化或介词链；不额外解释。\n"
    )
    if profile and profile.examples:
        fast_user += (
            "\n下面是该读者亲自标注的代表样本，判断必须以此人的实际水平为准："
            + json.dumps(profile.prompt_examples(), ensure_ascii=False)
    )
    fast_call = (
        trace.start_call("fast", fast_model, language.fast_system, fast_user)
        if trace is not None
        else None
    )
    if on_stage is not None:
        on_stage("fast", None)
    fast = (
        fast_call.complete()
        if fast_call is not None
        else fast_model.complete_json(language.fast_system, fast_user)
    )
    _raise_if_cancelled(cancel_event)
    try:
        translation = _parse_sentence_translations(fast.get("translations"), sentences)
        _validate_translation_sanity(text, translation, source_language)
        assessments = _parse_assessments(fast.get("sentences"), sentences)
    except EpubError as exc:
        if fast_call is not None:
            fast_call.write_normalized(
                {
                    "status": "validation_failure",
                    "parsed_content": fast,
                    "error": {"type": type(exc).__name__, "message": str(exc)},
                }
            )
        raise

    # Syntax analysis is enrichment-only. A parser can suggest useful clause
    # links for a sentence already selected as difficult, but its noisy local
    # heuristics must never create a learning-card candidate by themselves.
    candidates = {item.index for item in assessments if item.difficulty != "fluent"}
    if source_uses_english_local_heuristics(source_language):
        candidates |= language.local_candidates(sentences)
    if fast_call is not None:
        fast_call.write_normalized(
            {
                "paragraph_translation": translation.strip(),
                "assessments": [
                    {
                        "index": item.index,
                        "text": item.text,
                        "difficulty": item.difficulty,
                        "reason": item.reason,
                        "confidence": item.confidence,
                    }
                    for item in assessments
                ],
                "candidate_indices": sorted(candidates),
            }
        )
    if trace is not None:
        context_payload = (book_context or BookContext()).prompt_payload()
        profile_payload = profile.fingerprint_payload() if profile else None
        trace.write(
            "quality-replay-input",
            {
                "version": 1,
                "language": {
                    "key": language.key,
                    "source_code": source_language.source_code,
                    "validator": source_language.validator_version,
                    "syntax_identity": source_language.syntax_identity,
                    "card_format": "sentence-meaning-v1",
                    "quality_system_sha256": hashlib.sha256(
                        language.quality_system.encode("utf-8")
                    ).hexdigest(),
                },
                "paragraph": text,
                "sentences": sentence_payload,
                "assessments": [
                    {
                        "index": item.index,
                        "text": item.text,
                        "difficulty": item.difficulty,
                        "reason": item.reason,
                        "confidence": item.confidence,
                    }
                    for item in assessments
                ],
                "candidate_indices": sorted(candidates),
                "stanza": {} if source_language.syntax_identity == "off-v1" else _syntax_to_dict(resolved_syntax),
                "book_context": context_payload,
                "book_context_sha256": _payload_sha256(context_payload),
                "profile": profile_payload,
                "profile_sha256": _payload_sha256(profile_payload),
                "density": density,
            },
        )
    cards: tuple[LearningCard, ...] = ()
    if candidates:
        quality_user = _build_quality_user(
            text,
            sentences,
            assessments,
            candidates,
            book_context or BookContext(),
            profile,
            resolved_syntax,
            language,
            quality_payload_mode,
        )
        _raise_if_cancelled(cancel_event)
        quality_stage = f"quality-{quality_payload_mode}"
        quality_call = (
            trace.start_call(
                quality_stage, quality_model, language.quality_system, quality_user
            )
            if trace is not None
            else None
        )
        if on_stage is not None:
            on_stage("quality", None)
        quality = (
            quality_call.complete()
            if quality_call is not None
            else quality_model.complete_json(language.quality_system, quality_user)
        )
        _raise_if_cancelled(cancel_event)
        try:
            cards = _parse_cards(
                quality.get("sentences"),
                sentences,
                candidates,
            )
            if source_uses_english_local_heuristics(source_language):
                cards = _suppress_locally_easy_effortful_cards(cards, profile, language)
            _validate_card_grounding(text, cards, source_language)
            cards = _apply_density(cards, density)
            _validate_card_lengths(text, cards)
        except EpubError as exc:
            if quality_call is not None:
                quality_call.write_normalized(
                    {
                        "status": "validation_failure",
                        "candidate_indices": sorted(candidates),
                        "parsed_content": quality,
                        "error": {"type": type(exc).__name__, "message": str(exc)},
                    }
                )
            raise
        if quality_call is not None:
            quality_call.write_normalized(
                {
                    "candidate_indices": sorted(candidates),
                    "model_sentences": quality.get("sentences"),
                    "cards": _learning_to_dict(
                        ParagraphLearning(translation.strip(), assessments, cards)
                    )["cards"],
                }
            )
    learning = ParagraphLearning(translation.strip(), assessments, cards)
    if trace is not None:
        trace.write("paragraph-learning", _learning_to_dict(learning))
    return learning


@dataclass(frozen=True)
class QualityReplayResult:
    candidate_indices: tuple[int, ...]
    cards: tuple[LearningCard, ...]


def replay_quality(
    input_path: Path,
    quality_model: JSONModel,
    *,
    quality_payload_mode: str = "compact-v5",
    language: LanguageModule = ENGLISH_TO_CHINESE,
    trace: ParagraphTrace | None = None,
) -> QualityReplayResult:
    """Review a frozen fast-stage trace without invoking fast or Stanza."""
    try:
        payload = json.loads(input_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise EpubError(f"无法读取 quality replay 输入 {input_path}: {exc}") from exc
    if not isinstance(payload, dict) or payload.get("version") != 1:
        raise EpubError("quality replay 输入格式无效")
    language_payload = payload.get("language")
    if not isinstance(language_payload, dict) or language_payload.get("key") != language.key:
        raise EpubError("quality replay 语言配置不匹配")
    if language_payload.get("card_format") != "sentence-meaning-v1":
        raise EpubError("quality replay 卡片模式不匹配")
    if language_payload.get("quality_system_sha256") != hashlib.sha256(
        language.quality_system.encode("utf-8")
    ).hexdigest():
        raise EpubError("quality replay prompt fingerprint 不匹配")
    replay_source = _source_for_language(language)
    if replay_source.source_code == "ja" and (
        language_payload.get("source_code") != "ja"
        or language_payload.get("validator") != replay_source.validator_version
        or language_payload.get("syntax_identity") != "off-v1"
    ):
        raise EpubError("quality replay 日文语言或冻结语法身份不匹配")
    text = payload.get("paragraph")
    if not isinstance(text, str) or not text.strip():
        raise EpubError("quality replay 缺少原段落")
    sentences = language.split(text)
    serialized_sentences = [{"index": item.index, "text": item.text} for item in sentences]
    if _payload_sha256(payload.get("sentences")) != _payload_sha256(serialized_sentences):
        raise EpubError("quality replay 原句索引与段落不一致")
    assessments = _parse_assessments(payload.get("assessments"), sentences)
    raw_candidates = payload.get("candidate_indices")
    if (
        not isinstance(raw_candidates, list)
        or any(type(value) is not int for value in raw_candidates)
        or raw_candidates != sorted(set(raw_candidates))
    ):
        raise EpubError("quality replay 候选索引格式无效")
    candidates = set(raw_candidates)
    sentence_indices = {item.index for item in sentences}
    if not candidates or not candidates <= sentence_indices:
        raise EpubError("quality replay 候选索引与原句不一致")
    if any(item.difficulty != "fluent" and item.index not in candidates for item in assessments):
        raise EpubError("quality replay 缺少 fast 困难候选")
    raw_context = payload.get("book_context")
    if not isinstance(raw_context, dict):
        raise EpubError("quality replay 书籍上下文格式无效")
    title = raw_context.get("title")
    authors = raw_context.get("authors")
    work_year = raw_context.get("work_year")
    if (
        not isinstance(title, (str, type(None)))
        or not isinstance(authors, list)
        or not all(isinstance(author, str) for author in authors)
        or not isinstance(work_year, (int, type(None)))
    ):
        raise EpubError("quality replay 书籍上下文格式无效")
    context = BookContext(title or "", tuple(authors), work_year)
    if payload.get("book_context_sha256") != _payload_sha256(context.prompt_payload()):
        raise EpubError("quality replay 书籍上下文 fingerprint 不匹配")
    profile = _replay_profile(payload.get("profile"))
    profile_payload = profile.fingerprint_payload() if profile else None
    if payload.get("profile_sha256") != _payload_sha256(profile_payload):
        raise EpubError("quality replay 读者画像 fingerprint 不匹配")
    density = payload.get("density")
    if density not in {"low", "medium", "high"}:
        raise EpubError("quality replay 辅助密度无效")
    raw_syntax = payload.get("stanza")
    if not isinstance(raw_syntax, dict):
        raise EpubError("quality replay 缺少 Stanza 证据")
    if replay_source.source_code == "ja" and raw_syntax != {}:
        raise EpubError("quality replay 日文冻结语法必须为 off")
    syntax_results = {} if replay_source.source_code == "ja" else _syntax_from_dict(raw_syntax, sentences)
    quality_user = _build_quality_user(
        text,
        sentences,
        assessments,
        candidates,
        context,
        profile,
        syntax_results,
        language,
        quality_payload_mode,
    )
    if trace is not None:
        trace.write("validation-policy", {"version": VALIDATION_VERSION})
        trace.write("quality-replay-source", {"input_sha256": hashlib.sha256(input_path.read_bytes()).hexdigest()})
    stage = f"quality-{quality_payload_mode}"
    call = trace.start_call(stage, quality_model, language.quality_system, quality_user) if trace else None
    quality = call.complete() if call else quality_model.complete_json(language.quality_system, quality_user)
    try:
        cards = _parse_cards(
            quality.get("sentences"),
            sentences,
            candidates,
        )
        if source_uses_english_local_heuristics(_source_for_language(language)):
            cards = _suppress_locally_easy_effortful_cards(cards, profile, language)
        _validate_card_grounding(text, cards, _source_for_language(language))
        cards = _apply_density(cards, density)
        _validate_card_lengths(text, cards)
    except EpubError as exc:
        if call is not None:
            call.write_normalized(
                {
                    "status": "validation_failure",
                    "candidate_indices": sorted(candidates),
                    "parsed_content": quality,
                    "error": {"type": type(exc).__name__, "message": str(exc)},
                }
            )
        raise
    if call is not None:
        call.write_normalized(
            {
                "candidate_indices": sorted(candidates),
                "model_sentences": quality.get("sentences"),
                "cards": _learning_to_dict(ParagraphLearning("", assessments, cards))["cards"],
            }
        )
    return QualityReplayResult(tuple(sorted(candidates)), cards)


def _replay_profile(payload: object) -> ReadingProfile | None:
    if payload is None:
        return None
    if not isinstance(payload, dict) or payload.get("version") != 1:
        raise EpubError("quality replay 读者画像格式无效")
    examples = payload.get("examples")
    if not isinstance(examples, list):
        raise EpubError("quality replay 读者画像格式无效")
    parsed: list[CalibrationExample] = []
    for item in examples:
        if (
            not isinstance(item, dict)
            or not isinstance(item.get("text"), str)
            or item.get("label") not in {"fluent", "effortful", "blocking"}
        ):
            raise EpubError("quality replay 读者画像格式无效")
        parsed.append(CalibrationExample(item["text"], item["label"]))
    return ReadingProfile(tuple(parsed))


def _build_quality_user(
    text: str,
    sentences: list[Sentence],
    assessments: tuple[Assessment, ...],
    candidates: set[int],
    book_context: BookContext,
    profile: ReadingProfile | None,
    syntax_results: dict[int, LocalSyntaxResult],
    language: LanguageModule,
    quality_payload_mode: str,
) -> str:
    if quality_payload_mode != "compact-v5":
        raise EpubError(f"无效 quality payload 模式: {quality_payload_mode}")
    user = (
        "复核候选。每个给定 index 必须恰好返回一次，包括 fluent；"
        "fluent 只返回 index 和 difficulty；effortful 或 blocking 只返回 index、difficulty 和一句 meaning。"
        "返回 JSON 对象，顶层键为 \"sentences\"，不要 Markdown。\n"
    )
    assessments_by_index = {item.index: item for item in assessments}
    compact_candidates: list[dict[str, object]] = []
    for index in sorted(candidates):
        result = syntax_results.get(index)
        item: dict[str, object] = {"index": index, "source": assessments_by_index[index].text, "initial_difficulty": assessments_by_index[index].difficulty}
        if result and result.hints:
            item["syntax"] = [hint.prompt_payload() for hint in result.hints]
        compact_candidates.append(item)
    if language.quality_input_format == "json-v1":
        return json.dumps(
            {
                "candidates": compact_candidates,
                "lexical_context": book_context.quality_prompt_payload(),
            },
            ensure_ascii=False,
        )
    user += (
        "候选：" + json.dumps(compact_candidates, ensure_ascii=False)
        + "\n年代词义参考：" + json.dumps(book_context.quality_prompt_payload(), ensure_ascii=False)
        + "\n亲属关系断言必须由该 source 明确写出；从句证据只用于理解同一句。"
    )
    return user


# Consume the entire numeric span first, so regex backtracking cannot turn
# e.g. 12.5A into a standalone 12. Chinese text may directly surround numbers.
_NUMBER_RE = re.compile(r"[-+]?(?:\d+(?:[,.]\d+)*|\.\d+)%?")
_GROUPED_NUMBER_RE = re.compile(r"[-+]?\d{1,3}(?:,\d{3})+(?:\.\d+)?%?")
_HYPOTHETICAL_ATTRIBUTION_RE = re.compile(r"\bas\s+if\b", re.IGNORECASE)
_MOTIVE_INFERENCE_MARKERS = ("敷衍", "脱身", "真心", "目的", "用意")


def _number_items(text: str) -> set[str]:
    items = set()
    for match in _NUMBER_RE.finditer(text):
        adjacent = text[max(0, match.start() - 1):match.start()] + text[match.end():match.end() + 1]
        if any(char.isascii() and (char.isalnum() or char == "_") for char in adjacent):
            continue
        token = match.group()
        # Only thousands separators are normalized: no sign/decimal/percent folding.
        items.add(token.replace(",", "") if _GROUPED_NUMBER_RE.fullmatch(token) else token)
    return items


def _source_for_language(language: LanguageModule) -> SourceLanguage:
    return JAPANESE_SOURCE if language.source_code == "ja" else ENGLISH_SOURCE


def _validate_translation_sanity(source: str, translation: str, source_language: SourceLanguage) -> None:
    if len(translation) > max(500, len(source) * 6):
        raise EpubError("快速模型段落译文长度异常")
    missing_numbers = _number_items(source) - _number_items(translation)
    if missing_numbers:
        raise EpubError("快速模型译文疑似遗漏数字: " + ", ".join(sorted(missing_numbers)))
    try:
        validate_default_translation(source_language, translation)
    except ValueError as exc:
        raise EpubError(str(exc)) from exc


def _parse_sentence_translations(raw: object, sentences: list[Sentence]) -> str:
    if not isinstance(raw, list):
        raise EpubError("快速模型 translations 必须是数组")
    expected = {item.index for item in sentences}
    seen: set[int] = set()
    translations: dict[int, str] = {}
    for item in raw:
        if not isinstance(item, dict) or type(item.get("index")) is not int:
            raise EpubError("快速模型译文项格式无效")
        index = item["index"]
        translation = item.get("translation")
        if index in seen or index not in expected:
            raise EpubError(f"快速模型译文索引无效: index={index}")
        if not isinstance(translation, str) or not translation.strip():
            raise EpubError(f"快速模型译文为空: index={index}")
        seen.add(index)
        translations[index] = translation.strip()
    if seen != expected:
        missing = sorted(expected - seen)
        raise EpubError("快速模型译文缺少句子索引: " + ", ".join(map(str, missing)))
    return "".join(translations[index] for index in sorted(expected))


def _validate_card_lengths(source: str, cards: tuple[LearningCard, ...]) -> None:
    total = 0
    for card in cards:
        if len(card.meaning) > max(500, len(card.sentence) * 8):
            raise EpubError("高质量模型句意长度异常")
        total += len(card.meaning)
    if total > max(1500, len(source) * 15):
        raise EpubError("高质量模型理解卡总长度异常")


def _validate_card_grounding(
    source: str, cards: tuple[LearningCard, ...], source_language: SourceLanguage,
) -> None:
    """Reject motive claims drawn from explicitly hypothetical attribution."""
    for card in cards:
        try:
            validate_source_card_text(source_language, card.meaning)
        except ValueError as exc:
            raise EpubError(str(exc)) from exc
        if source_language.source_code != "en":
            continue
        if not _HYPOTHETICAL_ATTRIBUTION_RE.search(card.sentence):
            continue
        for marker in _MOTIVE_INFERENCE_MARKERS:
            if marker in card.meaning:
                raise EpubError(
                    "高质量模型理解卡把 as if 的假设性归因扩写为心理动机: "
                    + marker
                )


def _suppress_locally_easy_effortful_cards(
    cards: tuple[LearningCard, ...],
    profile: ReadingProfile | None,
    language: LanguageModule,
) -> tuple[LearningCard, ...]:
    """Apply the reader's hard floor after model review without hiding blockers."""
    fluent_profile_texts = {
        _reader_example_key(example.text)
        for example in (profile.examples if profile else ())
        if example.label == "fluent"
    }
    hard_profile_texts = {
        _reader_example_key(example.text)
        for example in (profile.examples if profile else ())
        if example.label != "fluent"
    }
    retained: list[LearningCard] = []
    for card in cards:
        if card.difficulty != "effortful":
            retained.append(card)
            continue
        sentence = _reader_example_key(card.sentence)
        if sentence in fluent_profile_texts:
            continue
        if sentence in hard_profile_texts:
            retained.append(card)
            continue
        minimum_words = language.min_effortful_card_words
        if minimum_words > 0:
            word_count = len(re.findall(r"[A-Za-z]+(?:['’-][A-Za-z]+)?", sentence))
            if word_count >= minimum_words:
                retained.append(card)
            continue
        if local_model_gate(
            sentence,
            hard_profile_texts=tuple(hard_profile_texts),
        ).needs_model:
            retained.append(card)
    return tuple(retained)


def _reader_example_key(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip().strip('"\'“”‘’')


def analyze_paragraph_with_retries(
    text: str,
    fast_model: JSONModel,
    quality_model: JSONModel,
    *,
    profile: ReadingProfile | None = None,
    density: str = "medium",
    schema_retries: int = 1,
    language: LanguageModule = ENGLISH_TO_CHINESE,
    book_context: BookContext | None = None,
    syntax_analyzer: SyntaxAnalyzer | None = None,
    syntax_results: dict[int, LocalSyntaxResult] | None = None,
    quality_payload_mode: str = "compact-v5",
    cancel_event: threading.Event | None = None,
    trace: ParagraphTrace | None = None,
    on_stage: Callable[[str, int | None], None] | None = None,
    source_language: SourceLanguage = ENGLISH_SOURCE,
) -> ParagraphLearning:
    if source_language.syntax_identity == "off-v1" and syntax_analyzer is not None:
        raise EpubError("日文句法分析固定为 off；不能启用英文 Stanza 或 spaCy")
    if schema_retries < 0:
        raise EpubError("结构纠正重试次数不能小于 0")
    resolved_syntax = syntax_results
    if resolved_syntax is None and syntax_analyzer is not None:
        resolved_syntax = syntax_analyzer.analyze(split_source_sentences(source_language, text))
    last_error: EpubError | None = None
    for attempt in range(schema_retries + 1):
        try:
            return analyze_paragraph(
                text,
                fast_model,
                quality_model,
                profile=profile,
                density=density,
                language=language,
                book_context=book_context,
                syntax_results=resolved_syntax,
                quality_payload_mode=quality_payload_mode,
                cancel_event=cancel_event,
                trace=trace,
                on_stage=on_stage,
                source_language=source_language,
            )
        except EpubError as exc:
            last_error = exc
            if attempt < schema_retries and on_stage is not None:
                on_stage("retry", attempt + 1)
    raise EpubError(f"模型结构化结果在有限重试后仍无效: {last_error}") from last_error


def _parse_assessments(raw: object, sentences: list[Sentence]) -> tuple[Assessment, ...]:
    if not isinstance(raw, list):
        raise EpubError("快速模型 sentences 必须是数组")
    expected = {item.index: item.text for item in sentences}
    seen: set[int] = set()
    result: list[Assessment] = []
    for item in raw:
        if not isinstance(item, dict) or type(item.get("index")) is not int:
            raise EpubError("快速模型句子项格式无效")
        index = item["index"]
        if index in seen or index not in expected:
            raise EpubError(f"快速模型句子索引无效: index={index}")
        difficulty = item.get("difficulty")
        if difficulty not in {"fluent", "effortful", "blocking"}:
            raise EpubError(f"快速模型难度标签无效: {difficulty}")
        confidence = item.get("confidence", 0.5)
        if type(confidence) not in (int, float) or (type(confidence) is float and not math.isfinite(confidence)):
            raise EpubError("快速模型 confidence 必须是有限数字，不能是布尔值")
        result.append(
            Assessment(
                index=index,
                text=expected[index],
                difficulty=difficulty,
                reason=str(item.get("reason") or ""),
                confidence=float(max(0.0, min(confidence, 1.0))),
            )
        )
        seen.add(index)
    if seen != set(expected):
        raise EpubError("快速模型没有逐句返回完整判断")
    return tuple(sorted(result, key=lambda value: value.index))


def _parse_cards(
    raw: object,
    sentences: list[Sentence],
    candidates: set[int],
) -> tuple[LearningCard, ...]:
    if not isinstance(raw, list):
        raise EpubError("高质量模型 sentences 必须是数组")
    expected = {item.index: item.text for item in sentences}
    seen: set[int] = set()
    cards: list[LearningCard] = []
    for item in raw:
        if not isinstance(item, dict) or type(item.get("index")) is not int:
            raise EpubError("高质量模型句子项格式无效")
        index = item["index"]
        if index not in candidates or index in seen:
            raise EpubError(f"高质量模型句子索引无效: index={index}")
        seen.add(index)
        difficulty = item.get("difficulty")
        if difficulty not in {"fluent", "effortful", "blocking"}:
            raise EpubError(f"高质量模型难度标签无效: {difficulty}")
        if difficulty == "fluent":
            continue
        meaning = item.get("meaning")
        if not isinstance(meaning, str) or not meaning.strip():
            raise EpubError(f"高质量模型缺少句意: index={index}")
        cards.append(
            LearningCard(
                sentence=expected[index],
                difficulty=difficulty,
                meaning=meaning.strip(),
            )
        )
    if seen != candidates:
        raise EpubError("高质量模型没有复核所有候选句")
    return tuple(cards)


def _apply_density(
    cards: tuple[LearningCard, ...], density: str
) -> tuple[LearningCard, ...]:
    result: list[LearningCard] = []
    for card in cards:
        if card.difficulty == "blocking" or density == "high":
            result.append(card)
        elif card.difficulty == "effortful" and density == "medium":
            result.append(card)
    return tuple(result)


def generate_preview(
    source: Path,
    output: Path,
    *,
    chapter: int,
    paragraph: int,
    fast_model: JSONModel,
    quality_model: JSONModel,
    require_epubcheck: bool = True,
    allow_invalid_source: bool = False,
    profile: ReadingProfile | None = None,
    density: str = "medium",
    schema_retries: int = 1,
    language: LanguageModule = ENGLISH_TO_CHINESE,
    book_title: str | None = None,
    book_authors: tuple[str, ...] = (),
    work_year: int | None = None,
    syntax_analyzer: SyntaxAnalyzer | None = None,
    quality_payload_mode: str = "compact-v5",
    trace_dir: Path | None = None,
    source_language: SourceLanguage = ENGLISH_SOURCE,
    analysis_link_text: str = DEFAULT_ANALYSIS_LINK_TEXT,
) -> PreviewResult:
    if "PREVIEW" not in output.stem.upper():
        raise EpubError("预览输出文件名必须包含 PREVIEW，避免与正式成品混淆")
    _validate_output_target(source, output)
    input_check = run_epubcheck(
        source, required=require_epubcheck, allow_invalid=allow_invalid_source
    )
    trace_run = (
        TraceRun.create(
            trace_dir,
            command="preview",
            source=source,
            quality_payload_mode=quality_payload_mode,
        )
        if trace_dir is not None
        else None
    )
    with EpubBook(source, reject_generated=True, analysis_link_text=analysis_link_text) as book:
        if book.source_language != source_language.source_code:
            raise EpubError("预览源语言与 EPUB 语言不匹配")
        book_context = _resolve_book_context(
            book,
            title=book_title,
            authors=book_authors,
            work_year=work_year,
        )
        baseline_issues = book.link_issues()
        ref = book.paragraph(chapter, paragraph)
        before = book.chapter_text_snapshot(chapter)
        structure_before = book.chapter_structure_snapshot(chapter)
        learning = analyze_paragraph_with_retries(
            ref.text,
            fast_model,
            quality_model,
            profile=profile,
            density=density,
            schema_retries=schema_retries,
            language=language,
            book_context=book_context,
            syntax_analyzer=syntax_analyzer,
            quality_payload_mode=quality_payload_mode,
            trace=(trace_run.paragraph(chapter, paragraph) if trace_run else None),
            source_language=source_language,
        )
        book.apply_note(ref, learning.as_note())
        after = book.chapter_text_snapshot(chapter)
        if before != after:
            raise EpubError("添加解析链接后原正文文本发生变化")
        if book.chapter_structure_snapshot(chapter) != structure_before:
            raise EpubError("添加解析链接后原正文结构或属性发生变化")
        changed_issues = book.link_issues() - baseline_issues
        if changed_issues:
            raise EpubError("生成解析引入了链接错误:\n" + "\n".join(sorted(changed_issues)))
        book.archive(output, detach_analyses=True)
    try:
        with EpubBook(output) as generated:
            output_issues = generated.link_issues()
        new_output_issues = output_issues - baseline_issues
        if new_output_issues:
            raise EpubError(
                "重新打包后引入了链接错误:\n" + "\n".join(sorted(new_output_issues))
            )
        output_check = run_epubcheck(
            output, required=require_epubcheck, allow_invalid=True
        )
        new_findings = output_check.findings - input_check.findings
        if new_findings:
            raise EpubError(
                "PREVIEW EPUB 新增了 EPUBCheck 错误:\n"
                + "\n".join(sorted(new_findings))
            )
    except Exception:
        # The PREVIEW artifact is intentionally retained for diagnosis.
        raise
    return PreviewResult(
        output=output,
        paragraph=ref,
        learning=learning,
        epubcheck_input_ran=input_check.ran,
        epubcheck_output_ran=output_check.ran,
        epubcheck_input_warnings=input_check.warnings,
        epubcheck_output_warnings=output_check.warnings,
    )


def generate_reading_preview(
    source: Path,
    output: Path,
    *,
    chapter: int,
    paragraph: int,
    assistance: object,
    layout: str = "translation-first",
    inline_phrases: bool = False,
    paragraph_aids: bool = False,
    require_epubcheck: bool = True,
    allow_invalid_source: bool = False,
    analysis_link_text: str = DEFAULT_ANALYSIS_LINK_TEXT,
) -> ReadingPreviewResult:
    """Render an already validated P1 result without clients, caches or models."""
    adapter = getattr(assistance, "as_epub_note", None)
    if not callable(adapter):
        raise EpubError("阅读辅助结果不能渲染为 EPUB")
    if "PREVIEW" not in output.stem.upper():
        raise EpubError("预览输出文件名必须包含 PREVIEW，避免与正式成品混淆")
    _validate_output_target(source, output)
    input_check = run_epubcheck(
        source, required=require_epubcheck, allow_invalid=allow_invalid_source
    )
    with EpubBook(source, reject_generated=True, analysis_link_text=analysis_link_text) as book:
        baseline_issues = book.link_issues()
        ref = book.paragraph(chapter, paragraph)
        before_text = book.chapter_text_snapshot(chapter)
        before_structure = book.chapter_structure_snapshot(chapter)
        book.apply_reading_assistance(
            ref,
            adapter(),
            layout=layout,
            inline_phrases=inline_phrases,
            paragraph_aids=paragraph_aids,
        )
        if book.chapter_text_snapshot(chapter) != before_text:
            raise EpubError("添加阅读辅助链接后原正文文本发生变化")
        if book.chapter_structure_snapshot(chapter) != before_structure:
            raise EpubError("添加阅读辅助链接后原正文结构或属性发生变化")
        changed_issues = book.link_issues() - baseline_issues
        if changed_issues:
            raise EpubError("生成阅读辅助引入了链接错误:\n" + "\n".join(sorted(changed_issues)))
        book.archive(output, detach_analyses=True)
    try:
        with EpubBook(output) as generated:
            output_issues = generated.link_issues()
        new_output_issues = output_issues - baseline_issues
        if new_output_issues:
            raise EpubError("重新打包后引入了链接错误:\n" + "\n".join(sorted(new_output_issues)))
        output_check = run_epubcheck(output, required=require_epubcheck, allow_invalid=True)
        new_findings = output_check.findings - input_check.findings
        if new_findings:
            raise EpubError("PREVIEW EPUB 新增了 EPUBCheck 错误:\n" + "\n".join(sorted(new_findings)))
    except Exception:
        # Keep the explicitly named PREVIEW artifact for diagnosis, matching
        # the legacy preview workflow.
        raise
    return ReadingPreviewResult(
        output, ref, input_check.ran, output_check.ran,
        input_check.warnings, output_check.warnings,
    )


READING_FROZEN_MANIFEST_VERSION = "reading-frozen-manifest-v1"
READING_AID_LAYOUT_VERSION = "reading-aid-layout-v1"


def write_reading_frozen_manifest(
    path: Path, *, source: Path, profile_key: str,
    results: dict[tuple[int, int], object], refs: list[ParagraphRef],
    context_identities: dict[tuple[int, int], str] | None = None,
    inline_phrases: bool = False,
    language_identity: dict[str, str] | None = None,
) -> None:
    """Write portable, validated reading results without credentials or traces."""
    if path.exists():
        raise EpubError(f"冻结结果清单已存在: {path}")
    entries = []
    for ref in refs:
        assistance = results[(ref.chapter, ref.paragraph)]
        serializer = getattr(assistance, "to_dict", None)
        if not callable(serializer):
            raise EpubError("阅读辅助结果不能写入冻结清单")
        entries.append({
            "chapter": ref.chapter, "paragraph": ref.paragraph, "href": ref.href,
            "text_sha256": hashlib.sha256(ref.text.encode("utf-8")).hexdigest(),
            "context_identity": (context_identities or {}).get(
                (ref.chapter, ref.paragraph), ""
            ),
            "assistance": serializer(),
        })
    payload = {
        "version": READING_FROZEN_MANIFEST_VERSION,
        "source_book_sha256": LearningCache.book_key(source),
        "profile_key": profile_key,
        "layout_version": READING_AID_LAYOUT_VERSION,
        "inline_phrases": inline_phrases,
        "language_identity": language_identity,
        "paragraphs": entries,
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=path.parent, delete=False) as handle:
        json.dump(payload, handle, ensure_ascii=False, sort_keys=True)
        temporary = Path(handle.name)
    temporary.replace(path)


def reading_assistance_from_manifest(
    path: Path, *, source: Path, ref: ParagraphRef,
    language_identity: dict[str, str] | None = None,
    source_language: SourceLanguage = ENGLISH_SOURCE,
):
    """Load one paragraph from a portable frozen manifest after source checks."""
    from translator.reading_assistance import AssistanceError, ParagraphAssistance
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise EpubError(f"无法读取冻结结果清单: {exc}") from exc
    if payload.get("version") != READING_FROZEN_MANIFEST_VERSION:
        raise EpubError("冻结结果清单版本不受支持")
    if payload.get("source_book_sha256") != LearningCache.book_key(source):
        raise EpubError("冻结结果清单不属于当前源 EPUB")
    if language_identity is not None and payload.get("language_identity") != language_identity:
        raise EpubError("冻结结果清单语言身份与当前 EPUB 不匹配")
    matches = [item for item in payload.get("paragraphs", []) if isinstance(item, dict)
               and item.get("chapter") == ref.chapter and item.get("paragraph") == ref.paragraph]
    if len(matches) != 1:
        raise EpubError("冻结结果清单未包含该段或包含重复条目")
    item = matches[0]
    text_hash = hashlib.sha256(ref.text.encode("utf-8")).hexdigest()
    if item.get("href") != ref.href or item.get("text_sha256") != text_hash:
        raise EpubError("冻结结果清单中的段落与当前源 EPUB 不匹配")
    try:
        return ParagraphAssistance.from_dict(
            item["assistance"], ref.text, source_language=source_language,
        )
    except (AssistanceError, KeyError, TypeError, ValueError) as exc:
        raise EpubError(f"冻结结果清单中的阅读辅助无效: {exc}") from exc


def translate_reading_book(
    source: Path,
    output: Path,
    *,
    model: JSONModel,
    profile_key: str,
    cache_path: Path,
    chapters: set[int] | None = None,
    max_workers: int = 2,
    require_epubcheck: bool = True,
    allow_invalid_source: bool = False,
    max_tokens: int | None = None,
    dry_run: bool = False,
    trace_dir: Path | None = None,
    local_gate: str = "off",
    on_estimate: Callable[[TranslationEstimate], None] | None = None,
    on_progress: Callable[[TranslationProgress], None] | None = None,
    frozen_manifest: Path | None = None,
    prior_context: bool = False,
    directed_review: bool = False,
    inline_phrases: bool = False,
    pipeline: str = "single",
    short_text_words: int = 0,
    short_sentence_words: int = 0,
    skip_paragraphs: set[tuple[int, int]] | None = None,
    reprocess_paragraphs: set[tuple[int, int]] | None = None,
    paragraph_aids: bool = False,
    source_language: SourceLanguage = ENGLISH_SOURCE,
    language_identity: dict[str, str] | None = None,
    analysis_link_text: str = DEFAULT_ANALYSIS_LINK_TEXT,
    japanese_short_text_gate_mode: str = "safe-v1",
    japanese_short_text_chars: int = 30,
) -> TranslationEstimate | ReadingTranslationResult:
    """Reading-v1 path with paragraph concurrency and ordered rendering."""
    from translator.reading_assistance import (
        AssistanceError,
        ParagraphAssistance,
        without_short_sentence_aids,
    )
    from translator.reading_eval import prepare_reading_input

    if max_tokens is not None and max_tokens <= 0:
        raise EpubError("token 上限必须大于 0")
    if max_workers < 1:
        raise EpubError("并发工作数必须大于 0")
    if short_text_words < 0:
        raise EpubError("reading-v1 短文本跳过词数不能为负数")
    if short_sentence_words < 0:
        raise EpubError("reading-v1 短句解析跳过词数不能为负数")
    skip_paragraphs = skip_paragraphs or set()
    reprocess_paragraphs = reprocess_paragraphs or set()
    if any(chapter < 1 or paragraph < 1 for chapter, paragraph in skip_paragraphs):
        raise EpubError("reading-v1 显式跳过坐标必须大于 0")
    if any(chapter < 1 or paragraph < 1 for chapter, paragraph in reprocess_paragraphs):
        raise EpubError("reading-v1 重新生成坐标必须大于 0")
    if overlap := skip_paragraphs & reprocess_paragraphs:
        formatted = ", ".join(
            f"{chapter}:{paragraph}" for chapter, paragraph in sorted(overlap)
        )
        raise EpubError(f"同一段落不能同时跳过和重新生成: {formatted}")
    if local_gate not in {"off", "conservative", "balanced"}:
        raise EpubError("无效本地难度门控模式")
    if pipeline not in {"single", "two-stage"}:
        raise EpubError("无效 reading-v1 生成 pipeline")
    if not dry_run:
        _validate_output_target(source, output)
        if frozen_manifest is not None and frozen_manifest.exists():
            raise EpubError(f"冻结结果清单已存在: {frozen_manifest}")
    input_check = run_epubcheck(source, required=require_epubcheck, allow_invalid=allow_invalid_source)
    with EpubBook(source, reject_generated=True, analysis_link_text=analysis_link_text) as book:
        if book.source_language != source_language.source_code:
            raise EpubError("阅读辅助源语言与 EPUB 语言不匹配")
        source_refs = [ref for ref in book.paragraphs() if chapters is None or ref.chapter in chapters]
        if not source_refs:
            raise EpubError("所选范围没有可处理的正文段落")
        source_coordinates = {(ref.chapter, ref.paragraph) for ref in source_refs}
        if unknown_skips := skip_paragraphs - source_coordinates:
            formatted = ", ".join(f"{chapter}:{paragraph}" for chapter, paragraph in sorted(unknown_skips))
            raise EpubError(f"显式跳过段落不存在或不在所选章节: {formatted}")
        if unknown_reprocess := reprocess_paragraphs - source_coordinates:
            formatted = ", ".join(
                f"{chapter}:{paragraph}"
                for chapter, paragraph in sorted(unknown_reprocess)
            )
            raise EpubError(f"重新生成段落不存在或不在所选章节: {formatted}")
        refs = []
        skipped_dialogue = skipped_prose = skipped_easy = 0
        skipped_japanese: list[tuple[int, int]] = []
        skipped_japanese_chars: list[tuple[int, int]] = []
        for ref in source_refs:
            if (ref.chapter, ref.paragraph) in skip_paragraphs:
                skipped_easy += 1
                continue
            if source_language.source_code == "ja":
                gate = japanese_short_text_gate(
                    ref.text,
                    mode=japanese_short_text_gate_mode,
                    short_text_chars=japanese_short_text_chars,
                    has_ruby=ref.has_ruby,
                )
                if gate.needs_model:
                    refs.append(ref)
                else:
                    target = (ref.chapter, ref.paragraph)
                    if gate.reason == "japanese-short-text-chars":
                        skipped_japanese_chars.append(target)
                    else:
                        skipped_japanese.append(target)
                continue
            if not source_uses_english_local_heuristics(source_language):
                refs.append(ref)
                continue
            short_kind = short_text_paragraph_kind(
                ref.text, max_words=short_text_words
            )
            if short_kind == "dialogue":
                skipped_dialogue += 1
                continue
            if short_kind == "prose":
                skipped_prose += 1
                continue
            gate = local_model_gate(ref.text, mode=local_gate)
            if gate.needs_model:
                refs.append(ref)
            elif gate.reason == "short-dialogue":
                skipped_dialogue += 1
            elif gate.reason == "short-prose":
                skipped_prose += 1
            else:
                skipped_easy += 1
        model_coordinates = {(ref.chapter, ref.paragraph) for ref in refs}
        if excluded_reprocess := reprocess_paragraphs - model_coordinates:
            formatted = ", ".join(
                f"{chapter}:{paragraph}"
                for chapter, paragraph in sorted(excluded_reprocess)
            )
            raise EpubError(f"重新生成段落被当前门控排除: {formatted}")
        calls_per_paragraph = 3 if directed_review else (2 if pipeline == "two-stage" else 1)
        estimate = estimate_translation(refs, calls_per_paragraph=calls_per_paragraph,
                                        skipped_short_dialogue=skipped_dialogue,
                                        skipped_short_prose=skipped_prose,
                                        skipped_local_easy=skipped_easy,
                                        skipped_japanese_short_text=(
                                            len(skipped_japanese) + len(skipped_japanese_chars)
                                        ),
                                        source_language=source_language)
        estimate = TranslationEstimate(
            source_paragraphs=estimate.source_paragraphs, paragraphs=estimate.paragraphs,
            skipped_short_dialogue=skipped_dialogue, skipped_short_prose=skipped_prose,
            skipped_local_easy=skipped_easy,
            approximate_requests=estimate.approximate_requests,
            approximate_input_tokens=estimate.approximate_input_tokens,
            approximate_output_tokens=estimate.approximate_output_tokens,
            approximate_cost=None, approximate_seconds=None, syntax_requests=0,
            epubcheck_input_warnings=input_check.warnings,
            skipped_japanese_short_text=(
                len(skipped_japanese) + len(skipped_japanese_chars)
            ),
            japanese_short_text_coordinates=tuple(
                (*skipped_japanese, *skipped_japanese_chars)
            ),
            skipped_japanese_short_text_chars=len(skipped_japanese_chars),
            japanese_short_text_char_coordinates=tuple(skipped_japanese_chars),
            japanese_short_text_chars=japanese_short_text_chars,
            skipped_japanese_fixed_responses=len(skipped_japanese),
            japanese_fixed_response_coordinates=tuple(skipped_japanese),
        )
        if max_tokens is not None and estimate.approximate_total_tokens > max_tokens:
            raise EpubError(f"预计 token {estimate.approximate_total_tokens} 超过上限 {max_tokens}")
        if on_estimate is not None:
            on_estimate(estimate)
        if dry_run:
            return estimate
        baseline_issues = book.link_issues()
        snapshots = {chapter: book.chapter_text_snapshot(chapter) for chapter in sorted({ref.chapter for ref in refs})}
        structures = {chapter: book.chapter_structure_snapshot(chapter) for chapter in snapshots}
        trace_run = TraceRun.create(
            trace_dir, command="translate-reading-v1", source=source,
            quality_payload_mode="reading-assistance-v1",
            metadata={"cache_profile_key": profile_key, "pipeline": pipeline},
        ) if trace_dir is not None else None
        book_key = LearningCache.book_key(source)
        results: dict[tuple[int, int], ParagraphAssistance] = {}
        source_positions = {
            (ref.chapter, ref.paragraph): position
            for position, ref in enumerate(source_refs)
        }
        prior_by_ref: dict[tuple[int, int], str | None] = {}
        context_identities: dict[tuple[int, int], str] = {}
        for ref in refs:
            position = source_positions[(ref.chapter, ref.paragraph)]
            previous = source_refs[position - 1] if position else None
            prior = (
                previous.text
                if prior_context and previous is not None and previous.chapter == ref.chapter
                else None
            )
            prior_by_ref[(ref.chapter, ref.paragraph)] = prior
            context_identity = hashlib.sha256(prior.encode("utf-8")).hexdigest() if prior else ""
            if focus := _reading_review_focus(ref.text):
                context_identity = hashlib.sha256(json.dumps({
                    "prior_context_identity": context_identity,
                    "review_focus": focus,
                }, sort_keys=True).encode("utf-8")).hexdigest()
            context_identities[(ref.chapter, ref.paragraph)] = context_identity
        cached = generated = 0
        progress_lock = threading.Lock()

        def notify_progress(phase: str, ref: ParagraphRef | None = None) -> None:
            if on_progress is None:
                return
            with progress_lock:
                event = TranslationProgress(
                    phase=phase, completed_paragraphs=cached + generated,
                    total_paragraphs=len(refs), cached_paragraphs=cached,
                    generated_paragraphs=generated, source_paragraphs=estimate.source_paragraphs,
                    skipped_paragraphs=estimate.source_paragraphs - estimate.paragraphs,
                    chapter=ref.chapter if ref else None,
                    paragraph=ref.paragraph if ref else None,
                )
            on_progress(event)

        with LearningCache(cache_path) as cache:
            pending: list[tuple[ParagraphRef, str, dict]] = []
            for ref in refs:
                context_identity = context_identities[(ref.chapter, ref.paragraph)]
                key = cache.key(
                    book_key, profile_key, ref, context_identity=context_identity
                )
                payload = cache.get(key)
                force_reprocess = (ref.chapter, ref.paragraph) in reprocess_paragraphs
                if payload is not None and not force_reprocess:
                    try:
                        assistance = ParagraphAssistance.from_dict(
                            payload, ref.text, source_language=source_language,
                        )
                        results[(ref.chapter, ref.paragraph)] = without_short_sentence_aids(
                            assistance,
                            max_words=(short_sentence_words if source_uses_english_local_heuristics(source_language) else 0),
                        )
                        cached += 1
                        notify_progress("cache", ref)
                        continue
                    except (AssistanceError, KeyError, TypeError, ValueError):
                        cache.delete(key)
                prepared = prepare_reading_input({
                    "paragraph": ref.text,
                    "syntax": {},
                    "stanza_identity": source_language.syntax_identity,
                    "mode": "selection",
                    "paragraph_aids": paragraph_aids,
                    "excluded_aid_sentence_indices": [
                        sentence.index
                        for sentence in split_source_sentences(source_language, ref.text)
                        if source_uses_english_local_heuristics(source_language) and short_text_paragraph_kind(
                            sentence.text, max_words=short_sentence_words
                        ) is not None
                    ],
                    "prior_paragraph": prior_by_ref[(ref.chapter, ref.paragraph)],
                }, source_language=source_language)
                pending.append((ref, key, prepared))

            # A hard token ceiling relies on a per-paragraph usage delta. Keep
            # that bounded mode serial until reservations can be made per
            # in-flight request without weakening the existing stop guarantee.
            effective_workers = 1 if max_tokens is not None else max_workers
            stop_event = threading.Event()
            failure_lock = threading.Lock()
            first_failure: list[BaseException] = []

            def work(
                item: tuple[ParagraphRef, str, dict]
            ) -> tuple[ParagraphRef, ParagraphAssistance]:
                ref, key, prepared = item
                usage_before = getattr(getattr(model, "usage", None), "total_tokens", None)
                try:
                    _raise_if_cancelled(stop_event)
                    notify_progress("reading", ref)
                    assistance = generate_reading_assistance(
                        prepared, model, pipeline=pipeline,
                        trace=trace_run.paragraph(ref.chapter, ref.paragraph) if trace_run else None,
                        repair_core=directed_review,
                        source_language=source_language,
                    )
                    if directed_review and needs_reading_review(assistance, prepared):
                        notify_progress("reading-review", ref)
                        assistance = review_reading_assistance(
                            prepared, assistance, model,
                            trace=trace_run.paragraph(ref.chapter, ref.paragraph) if trace_run else None,
                            source_language=source_language,
                        )
                    assistance = without_short_sentence_aids(
                        assistance,
                        max_words=(short_sentence_words if source_uses_english_local_heuristics(source_language) else 0),
                    )
                    if max_tokens is not None:
                        usage_after = getattr(getattr(model, "usage", None), "total_tokens", None)
                        if not isinstance(usage_before, int) or not isinstance(usage_after, int) or usage_after <= usage_before:
                            raise EpubError("供应商未报告本次请求的实际 API token；停止后续外发")
                        if usage_after > max_tokens:
                            raise EpubError(f"实际 API token 超过运行上限 {max_tokens}")
                    cache.put(key, book_key, profile_key, assistance.to_dict())
                    return ref, assistance
                except AssistanceError as exc:
                    failure = EpubError(f"阅读辅助核心结果无效: {exc}")
                    with failure_lock:
                        if not first_failure:
                            first_failure.append(failure)
                            stop_event.set()
                    raise failure from exc
                except BaseException as exc:
                    with failure_lock:
                        if not first_failure:
                            first_failure.append(exc)
                            stop_event.set()
                    raise

            executor = ThreadPoolExecutor(max_workers=effective_workers)
            futures: dict[
                Future[tuple[ParagraphRef, ParagraphAssistance]],
                tuple[ParagraphRef, str, dict],
            ] = {}
            pending_items = iter(pending)

            def submit_next() -> bool:
                try:
                    item = next(pending_items)
                except StopIteration:
                    return False
                futures[executor.submit(work, item)] = item
                return True

            for _ in range(min(effective_workers, len(pending))):
                submit_next()

            def record_completed(
                ref: ParagraphRef, assistance: ParagraphAssistance
            ) -> None:
                nonlocal generated
                results[(ref.chapter, ref.paragraph)] = assistance
                with progress_lock:
                    generated += 1
                notify_progress("paragraph", ref)

            try:
                while futures:
                    done, _ = wait(futures, return_when=FIRST_COMPLETED)
                    for future in done:
                        futures.pop(future)
                        ref, assistance = future.result()
                        record_completed(ref, assistance)
                        submit_next()
            except BaseException as observed:
                stop_event.set()
                for future in futures:
                    future.cancel()
                # Let already in-flight provider calls finish and persist their
                # successful cache entries before LearningCache closes. Only
                # queued work is cancelled, so an interrupt has a bounded tail
                # of at most max_workers paragraphs.
                executor.shutdown(wait=True, cancel_futures=True)
                for future in tuple(futures):
                    if future.cancelled():
                        continue
                    try:
                        ref, assistance = future.result()
                    except BaseException:
                        continue
                    record_completed(ref, assistance)
                failure = first_failure[0] if first_failure else observed
                if failure is observed:
                    raise
                raise failure
            else:
                executor.shutdown(wait=True)
        notify_progress("render")
        for ref in refs:
            book.apply_reading_assistance(
                ref, results[(ref.chapter, ref.paragraph)].as_epub_note(),
                inline_phrases=inline_phrases,
                short_sentence_words=(
                    short_sentence_words
                    if source_uses_english_local_heuristics(source_language)
                    else 0
                ),
                paragraph_aids=paragraph_aids,
            )
        for chapter, before in snapshots.items():
            if book.chapter_text_snapshot(chapter) != before:
                raise EpubError("添加阅读辅助链接后原正文文本发生变化")
            if book.chapter_structure_snapshot(chapter) != structures[chapter]:
                raise EpubError("添加阅读辅助链接后原正文结构或属性发生变化")
        if issues := book.link_issues() - baseline_issues:
            raise EpubError("生成阅读辅助引入了链接错误:\n" + "\n".join(sorted(issues)))
        output_check = _archive_and_validate(
            book, output, baseline_issues=baseline_issues,
            require_epubcheck=require_epubcheck, input_epubcheck_findings=input_check.findings,
        )
        notify_progress("epubcheck")
    notify_progress("complete")
    if frozen_manifest is not None:
        write_reading_frozen_manifest(
            frozen_manifest, source=source, profile_key=profile_key,
            results=results, refs=refs, context_identities=context_identities,
            inline_phrases=inline_phrases,
            language_identity=language_identity,
        )
    return ReadingTranslationResult(output, len(refs), cached, generated, estimate,
                                   output_check.warnings, frozen_manifest)


def estimate_translation(
    refs: list[ParagraphRef],
    *,
    skipped_short_dialogue: int = 0,
    skipped_short_prose: int = 0,
    skipped_local_easy: int = 0,
    skipped_japanese_short_text: int = 0,
    syntax_requests: int = 0,
    input_price_per_million: float | None = None,
    output_price_per_million: float | None = None,
    requests_per_minute: float | None = None,
    calls_per_paragraph: int = 2,
    source_language: SourceLanguage = ENGLISH_SOURCE,
) -> TranslationEstimate:
    if requests_per_minute is not None and requests_per_minute <= 0:
        raise EpubError("每分钟请求数必须大于 0")
    # Conservative model-agnostic estimate: translation and card review are
    # This is a conservative upper bound: a syntax-only request is possible only
    # when a paragraph has local structure hints and the normal stages retain a
    # corresponding difficult card.
    if calls_per_paragraph < 1:
        raise EpubError("每段调用数必须至少为 1")
    # Japanese is not safely estimable using the historic English ``chars/4``
    # heuristic.  Reserve conservatively for UTF-8 Japanese tokenization plus
    # the translation/review envelopes; this is a budget guard, not billing.
    if source_language.source_code == "ja":
        input_tokens = sum(
            max(1, math.ceil(len(ref.text) * 1.25)) * calls_per_paragraph + 1200
            for ref in refs
        )
        output_tokens = sum(max(1, math.ceil(len(ref.text) * 1.5)) + 800 for ref in refs)
    else:
        input_tokens = sum(max(1, len(ref.text) // 4) * calls_per_paragraph + 900 for ref in refs)
        output_tokens = sum(max(1, len(ref.text) // 2) + 500 for ref in refs)
    requests = len(refs) * calls_per_paragraph + syntax_requests
    approximate_seconds = (
        requests * 60.0 / requests_per_minute
        if requests_per_minute is not None
        else None
    )
    cost = None
    if input_price_per_million is not None and output_price_per_million is not None:
        cost = (
            input_tokens * input_price_per_million
            + output_tokens * output_price_per_million
        ) / 1_000_000
    return TranslationEstimate(
        source_paragraphs=(
            len(refs)
            + skipped_short_dialogue
            + skipped_short_prose
            + skipped_local_easy
            + skipped_japanese_short_text
        ),
        paragraphs=len(refs),
        skipped_short_dialogue=skipped_short_dialogue,
        skipped_short_prose=skipped_short_prose,
        skipped_local_easy=skipped_local_easy,
        approximate_requests=requests,
        approximate_input_tokens=input_tokens,
        approximate_output_tokens=output_tokens,
        approximate_cost=cost,
        approximate_seconds=approximate_seconds,
        syntax_requests=syntax_requests,
        skipped_japanese_short_text=skipped_japanese_short_text,
    )


def translate_book(
    source: Path,
    output: Path,
    *,
    fast_model: JSONModel,
    quality_model: JSONModel,
    profile_key: str,
    cache_path: Path,
    syntax_profile_key: str = "syntax-analysis-v1",
    chapters: set[int] | None = None,
    max_workers: int = 2,
    require_epubcheck: bool = True,
    allow_invalid_source: bool = False,
    input_price_per_million: float | None = None,
    output_price_per_million: float | None = None,
    requests_per_minute: float | None = None,
    max_tokens: int | None = None,
    max_cost: float | None = None,
    dry_run: bool = False,
    profile: ReadingProfile | None = None,
    density: str = "medium",
    schema_retries: int = 1,
    language: LanguageModule = ENGLISH_TO_CHINESE,
    on_estimate: Callable[[TranslationEstimate], None] | None = None,
    on_progress: Callable[[TranslationProgress], None] | None = None,
    local_gate: str = "conservative",
    short_dialogue_words: int = 16,
    short_prose_words: int = 10,
    book_title: str | None = None,
    book_authors: tuple[str, ...] = (),
    work_year: int | None = None,
    syntax_analyzer: SyntaxAnalyzer | None = None,
    quality_payload_mode: str = "compact-v5",
    trace_dir: Path | None = None,
    source_language: SourceLanguage = ENGLISH_SOURCE,
    analysis_link_text: str = DEFAULT_ANALYSIS_LINK_TEXT,
    japanese_short_text_gate_mode: str = "safe-v1",
    japanese_short_text_chars: int = 30,
) -> TranslationEstimate | TranslationResult:
    if max_workers < 1:
        raise EpubError("并发数必须至少为 1")
    if schema_retries < 0:
        raise EpubError("结构纠正重试次数不能小于 0")
    if local_gate not in {"off", "conservative", "balanced"}:
        raise EpubError(f"无效本地难度门控模式: {local_gate}")
    if short_dialogue_words < 0 or short_prose_words < 0:
        raise EpubError("短句跳过词数不能为负数")
    if work_year is not None and not 1 <= work_year <= 9999:
        raise EpubError("作品年代必须在 1 到 9999 之间")
    if max_tokens is not None and max_tokens <= 0:
        raise EpubError("token 上限必须大于 0")
    if max_cost is not None and max_cost <= 0:
        raise EpubError("费用上限必须大于 0")
    if (input_price_per_million is None) != (output_price_per_million is None):
        raise EpubError("输入和输出价格必须同时提供")
    if any(
        value is not None and value < 0
        for value in (input_price_per_million, output_price_per_million)
    ):
        raise EpubError("输入和输出价格不能为负数")
    if source_language.syntax_identity == "off-v1" and syntax_analyzer is not None:
        raise EpubError("日文句法分析固定为 off；不能启用英文 Stanza 或 spaCy")
    if not dry_run:
        _validate_output_target(source, output)
    input_check = run_epubcheck(
        source, required=require_epubcheck, allow_invalid=allow_invalid_source
    )
    with EpubBook(source, reject_generated=True, analysis_link_text=analysis_link_text) as book:
        if book.source_language != source_language.source_code:
            raise EpubError("翻译源语言与 EPUB 语言不匹配")
        book_context = _resolve_book_context(
            book,
            title=book_title,
            authors=book_authors,
            work_year=work_year,
        )
        contextual_profile_key = _contextual_profile_key(profile_key, book_context)
        source_refs = [
            ref
            for ref in book.paragraphs()
            if chapters is None or ref.chapter in chapters
        ]
        if not source_refs:
            raise EpubError("所选范围没有可处理的正文段落")
        refs: list[ParagraphRef] = []
        skipped_short_dialogue = 0
        skipped_short_prose = 0
        skipped_local_easy = 0
        skipped_japanese: list[tuple[int, int]] = []
        skipped_japanese_chars: list[tuple[int, int]] = []
        hard_profile_texts = tuple(
            example.text
            for example in (profile.examples if profile else ())
            if example.label != "fluent"
        )
        for ref in source_refs:
            if source_language.source_code == "ja":
                gate = japanese_short_text_gate(
                    ref.text,
                    mode=japanese_short_text_gate_mode,
                    short_text_chars=japanese_short_text_chars,
                    has_ruby=ref.has_ruby,
                )
                if gate.needs_model:
                    refs.append(ref)
                else:
                    target = (ref.chapter, ref.paragraph)
                    if gate.reason == "japanese-short-text-chars":
                        skipped_japanese_chars.append(target)
                    else:
                        skipped_japanese.append(target)
                continue
            if not source_uses_english_local_heuristics(source_language):
                refs.append(ref)
                continue
            gate = local_model_gate(
                ref.text,
                mode=local_gate,
                dialogue_words=short_dialogue_words,
                prose_words=short_prose_words,
                hard_profile_texts=hard_profile_texts,
            )
            if gate.needs_model:
                refs.append(ref)
            elif gate.reason == "short-dialogue":
                skipped_short_dialogue += 1
            elif gate.reason == "short-prose":
                skipped_short_prose += 1
            else:
                skipped_local_easy += 1
        estimate = estimate_translation(
            refs,
            skipped_short_dialogue=skipped_short_dialogue,
            skipped_short_prose=skipped_short_prose,
            skipped_local_easy=skipped_local_easy,
            skipped_japanese_short_text=(
                len(skipped_japanese) + len(skipped_japanese_chars)
            ),
            syntax_requests=0,
            input_price_per_million=input_price_per_million,
            output_price_per_million=output_price_per_million,
            requests_per_minute=requests_per_minute,
            source_language=source_language,
        )
        estimate = TranslationEstimate(
            source_paragraphs=estimate.source_paragraphs,
            paragraphs=estimate.paragraphs,
            skipped_short_dialogue=estimate.skipped_short_dialogue,
            skipped_short_prose=estimate.skipped_short_prose,
            skipped_local_easy=estimate.skipped_local_easy,
            approximate_requests=estimate.approximate_requests,
            approximate_input_tokens=estimate.approximate_input_tokens,
            approximate_output_tokens=estimate.approximate_output_tokens,
            approximate_cost=estimate.approximate_cost,
            approximate_seconds=estimate.approximate_seconds,
            syntax_requests=estimate.syntax_requests,
            epubcheck_input_warnings=input_check.warnings,
            skipped_japanese_short_text=(
                len(skipped_japanese) + len(skipped_japanese_chars)
            ),
            japanese_short_text_coordinates=tuple(
                (*skipped_japanese, *skipped_japanese_chars)
            ),
            skipped_japanese_short_text_chars=len(skipped_japanese_chars),
            japanese_short_text_char_coordinates=tuple(skipped_japanese_chars),
            japanese_short_text_chars=japanese_short_text_chars,
            skipped_japanese_fixed_responses=len(skipped_japanese),
            japanese_fixed_response_coordinates=tuple(skipped_japanese),
        )
        if max_tokens is not None and estimate.approximate_total_tokens > max_tokens:
            raise EpubError(
                f"预计 token {estimate.approximate_total_tokens} 超过上限 {max_tokens}"
            )
        if max_cost is not None:
            if estimate.approximate_cost is None:
                raise EpubError("使用 --max-cost 时必须同时提供输入和输出价格")
            if estimate.approximate_cost > max_cost:
                raise EpubError(
                    f"预计费用 {estimate.approximate_cost:.4f} 超过上限 {max_cost:.4f}"
                )
        if on_estimate is not None:
            on_estimate(estimate)
        if dry_run:
            return estimate

        trace_run = (
            TraceRun.create(
                trace_dir,
                command="translate",
                source=source,
                quality_payload_mode=quality_payload_mode,
                metadata={"cache_profile_key": contextual_profile_key},
            )
            if trace_dir is not None
            else None
        )

        baseline_issues = book.link_issues()
        snapshots = {
            chapter: book.chapter_text_snapshot(chapter)
            for chapter in sorted({ref.chapter for ref in refs})
        }
        structure_snapshots = {
            chapter: book.chapter_structure_snapshot(chapter)
            for chapter in snapshots
        }
        book_key = LearningCache.book_key(source)
        results: dict[tuple[int, int], ParagraphLearning] = {}
        cached = 0
        generated = 0
        progress_lock = threading.RLock()

        def notify_progress(
            phase: str,
            ref: ParagraphRef | None = None,
            retry: int | None = None,
        ) -> None:
            if on_progress is None:
                return
            with progress_lock:
                on_progress(
                    TranslationProgress(
                        phase=phase,
                        completed_paragraphs=cached + generated,
                        total_paragraphs=len(refs),
                        cached_paragraphs=cached,
                        generated_paragraphs=generated,
                        source_paragraphs=estimate.source_paragraphs,
                        skipped_paragraphs=(
                            estimate.source_paragraphs - estimate.paragraphs
                        ),
                        chapter=ref.chapter if ref else None,
                        paragraph=ref.paragraph if ref else None,
                        retry=retry,
                    )
                )

        with LearningCache(cache_path) as cache:
            pending: list[tuple[ParagraphRef, str]] = []
            for ref in refs:
                cache_key = cache.key(book_key, contextual_profile_key, ref)
                payload = cache.get(cache_key)
                if payload is None:
                    pending.append((ref, cache_key))
                    continue
                try:
                    results[(ref.chapter, ref.paragraph)] = _learning_from_dict(payload)
                    cached += 1
                except (KeyError, TypeError, ValueError, EpubError):
                    cache.delete(cache_key)
                    pending.append((ref, cache_key))
            notify_progress("cache")

            stop_event = threading.Event()
            failure_lock = threading.Lock()
            first_failure: list[BaseException] = []

            def work(item: tuple[ParagraphRef, str]) -> tuple[ParagraphRef, ParagraphLearning]:
                try:
                    _raise_if_cancelled(stop_event)
                    ref, cache_key = item
                    syntax_results: dict[int, LocalSyntaxResult] | None = None
                    if syntax_analyzer is not None:
                        sentences = split_source_sentences(source_language, ref.text)
                        syntax_cache_key = cache.syntax_key(
                            book_key, syntax_profile_key, ref
                        )
                        syntax_payload = cache.get_syntax(syntax_cache_key)
                        if syntax_payload is not None:
                            try:
                                syntax_results = _syntax_from_dict(
                                    syntax_payload, sentences
                                )
                            except (KeyError, TypeError, ValueError, EpubError):
                                cache.delete_syntax(syntax_cache_key)
                        if syntax_results is None:
                            _raise_if_cancelled(stop_event)
                            notify_progress("syntax", ref)
                            syntax_results = syntax_analyzer.analyze(sentences)
                            cache.put_syntax(
                                syntax_cache_key,
                                book_key,
                                syntax_profile_key,
                                _syntax_to_dict(syntax_results),
                            )
                    learning = analyze_paragraph_with_retries(
                        ref.text,
                        fast_model,
                        quality_model,
                        profile=profile,
                        density=density,
                        schema_retries=schema_retries,
                        language=language,
                        book_context=book_context,
                        syntax_results=syntax_results,
                        quality_payload_mode=quality_payload_mode,
                        cancel_event=stop_event,
                        trace=(
                            trace_run.paragraph(ref.chapter, ref.paragraph)
                            if trace_run
                            else None
                        ),
                        on_stage=lambda phase, retry: notify_progress(
                            phase, ref, retry
                        ),
                        source_language=source_language,
                    )
                    cache.put(
                        cache_key,
                        book_key,
                        contextual_profile_key,
                        _learning_to_dict(learning),
                    )
                    return ref, learning
                except BaseException as exc:
                    with failure_lock:
                        if not first_failure:
                            first_failure.append(exc)
                            stop_event.set()
                    raise

            with ThreadPoolExecutor(max_workers=max_workers) as executor:
                pending_items = iter(pending)
                futures: dict[
                    Future[tuple[ParagraphRef, ParagraphLearning]],
                    tuple[ParagraphRef, str],
                ] = {}

                def submit_next() -> bool:
                    try:
                        item = next(pending_items)
                    except StopIteration:
                        return False
                    futures[executor.submit(work, item)] = item
                    return True

                for _ in range(min(max_workers, len(pending))):
                    submit_next()
                try:
                    while futures:
                        done, _ = wait(futures, return_when=FIRST_COMPLETED)
                        completed: list[tuple[ParagraphRef, ParagraphLearning]] = []
                        for future in done:
                            futures.pop(future)
                            completed.append(future.result())
                        for ref, learning in completed:
                            results[(ref.chapter, ref.paragraph)] = learning
                            with progress_lock:
                                generated += 1
                            notify_progress("paragraph", ref)
                        for _ in completed:
                            submit_next()
                except BaseException as observed:
                    stop_event.set()
                    for future in futures:
                        future.cancel()
                    failure = first_failure[0] if first_failure else observed
                    if failure is observed:
                        raise
                    raise failure

        if len(results) != len(refs):
            raise EpubError("并非所有段落都成功生成，正式 EPUB 未创建")
        notify_progress("render")
        for ref in refs:
            book.apply_note(ref, results[(ref.chapter, ref.paragraph)].as_note())
        for chapter, before in snapshots.items():
            if book.chapter_text_snapshot(chapter) != before:
                raise EpubError(f"添加解析链接后第 {chapter} 章原正文文本发生变化")
            if book.chapter_structure_snapshot(chapter) != structure_snapshots[chapter]:
                raise EpubError(f"添加解析链接后第 {chapter} 章原正文结构或属性发生变化")
        new_issues = book.link_issues() - baseline_issues
        if new_issues:
            raise EpubError("全书处理引入了链接错误:\n" + "\n".join(sorted(new_issues)))

        notify_progress("epubcheck")
        output_check = _archive_and_validate(
            book,
            output,
            baseline_issues=baseline_issues,
            require_epubcheck=require_epubcheck,
            input_epubcheck_findings=input_check.findings,
        )
        notify_progress("complete")
        return TranslationResult(
            output=output,
            paragraphs=len(refs),
            cached_paragraphs=cached,
            generated_paragraphs=len(refs) - cached,
            estimate=estimate,
            epubcheck_output_warnings=output_check.warnings,
        )


def _resolve_book_context(
    book: EpubBook,
    *,
    title: str | None,
    authors: tuple[str, ...],
    work_year: int | None,
) -> BookContext:
    if work_year is not None and not 1 <= work_year <= 9999:
        raise EpubError("作品年代必须在 1 到 9999 之间")
    metadata_titles = book.metadata_values("title")
    resolved_title = title.strip() if title and title.strip() else (
        metadata_titles[0] if metadata_titles else ""
    )
    resolved_authors = tuple(
        value.strip() for value in authors if value and value.strip()
    ) or book.metadata_values("creator")
    return BookContext(resolved_title, resolved_authors, work_year)


def _contextual_profile_key(profile_key: str, book_context: BookContext) -> str:
    payload = json.dumps(
        {
            "base_profile_key": profile_key,
            "book_context": book_context.prompt_payload(),
        },
        ensure_ascii=False,
        sort_keys=True,
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _syntax_to_dict(results: dict[int, LocalSyntaxResult]) -> dict:
    return {
        "version": 3,
        "results": [
            {
                "index": index,
                "needs_structure": result.needs_structure,
                "clause_count": result.clause_count,
                "max_clause_depth": result.max_clause_depth,
                "has_inversion": result.has_inversion,
                "long_attachments": result.long_attachments,
                "reasons": list(result.reasons),
                "hints": [hint.prompt_payload() for hint in result.hints],
            }
            for index, result in sorted(results.items())
        ],
    }


def _syntax_from_dict(
    payload: dict, sentences: list[Sentence]
) -> dict[int, LocalSyntaxResult]:
    version = payload.get("version")
    if version != 3 or not isinstance(payload.get("results"), list):
        raise EpubError("句法缓存格式无效")
    expected = {sentence.index for sentence in sentences}
    parsed: dict[int, LocalSyntaxResult] = {}
    for item in payload["results"]:
        if not isinstance(item, dict) or type(item.get("index")) is not int:
            raise EpubError("句法缓存索引无效")
        index = item["index"]
        if index not in expected or index in parsed:
            raise EpubError("句法缓存索引与原句不一致")
        reasons = item.get("reasons")
        raw_hints = item.get("hints")
        integer_fields = (
            item.get("clause_count"),
            item.get("max_clause_depth"),
            item.get("long_attachments"),
        )
        if (
            not isinstance(item.get("needs_structure"), bool)
            or not isinstance(item.get("has_inversion"), bool)
            or not all(isinstance(value, int) and value >= 0 for value in integer_fields)
            or not isinstance(reasons, list)
            or not all(isinstance(reason, str) for reason in reasons)
            or not isinstance(raw_hints, list)
        ):
            raise EpubError("句法缓存字段无效")
        hints: list[ClauseHint] = []
        for raw_hint in raw_hints:
            if not isinstance(raw_hint, dict):
                raise EpubError("句法缓存锚点无效")
            relation = raw_hint.get("relation")
            clause = raw_hint.get("clause")
            target = raw_hint.get("target")
            depth = raw_hint.get("depth")
            if (
                not all(
                    isinstance(value, str) and value
                    for value in (relation, clause, target)
                )
                or not isinstance(depth, int)
                or depth < 0
            ):
                raise EpubError("句法缓存锚点字段无效")
            hints.append(ClauseHint(relation, clause, target, depth))
        parsed[index] = LocalSyntaxResult(
            needs_structure=item["needs_structure"],
            clause_count=item["clause_count"],
            max_clause_depth=item["max_clause_depth"],
            has_inversion=item["has_inversion"],
            long_attachments=item["long_attachments"],
            reasons=tuple(reasons),
            hints=tuple(hints),
        )
    return parsed


def _learning_to_dict(learning: ParagraphLearning) -> dict:
    return {
        "paragraph_translation": learning.paragraph_translation,
        "assessments": [
            {
                "index": item.index,
                "text": item.text,
                "difficulty": item.difficulty,
                "reason": item.reason,
                "confidence": item.confidence,
            }
            for item in learning.assessments
        ],
        "cards": [
            {
                "sentence": card.sentence,
                "difficulty": card.difficulty,
                "meaning": card.meaning,
            }
            for card in learning.cards
        ],
    }


def _learning_from_dict(payload: dict) -> ParagraphLearning:
    translation = payload["paragraph_translation"]
    if not isinstance(translation, str) or not translation:
        raise EpubError("缓存译文无效")
    raw_assessments = payload.get("assessments")
    if not isinstance(raw_assessments, list) or any(
        not isinstance(item, dict) or type(item.get("index")) is not int
        or not isinstance(item.get("text"), str) for item in raw_assessments
    ):
        raise EpubError("缓存句子项格式无效")
    assessments = _parse_assessments(raw_assessments, [
        Sentence(item["index"], item["text"]) for item in raw_assessments
    ])
    cards = tuple(
        LearningCard(
            sentence=item["sentence"],
            difficulty=item["difficulty"],
            meaning=item["meaning"],
        )
        for item in payload["cards"]
    )
    return ParagraphLearning(translation, assessments, cards)


def _archive_and_validate(
    book: EpubBook,
    output: Path,
    *,
    baseline_issues: set[str],
    require_epubcheck: bool,
    input_epubcheck_findings: frozenset[str],
) -> EpubcheckResult:
    output = output.resolve()
    if output.exists():
        raise EpubError(f"输出文件已存在: {output}")
    output.parent.mkdir(parents=True, exist_ok=True)
    descriptor, name = tempfile.mkstemp(
        prefix=f".{output.stem}-", suffix="-INCOMPLETE.epub", dir=output.parent
    )
    os.close(descriptor)
    Path(name).unlink()
    try:
        book.archive(Path(name), detach_analyses=True)
        with EpubBook(Path(name)) as generated:
            new_issues = generated.link_issues() - baseline_issues
        if new_issues:
            raise EpubError(
                "重新打包后引入了链接错误:\n" + "\n".join(sorted(new_issues))
            )
        output_check = run_epubcheck(
            Path(name), required=require_epubcheck, allow_invalid=True
        )
        new_findings = output_check.findings - input_epubcheck_findings
        if new_findings:
            raise EpubError(
                "正式输出新增了 EPUBCheck 错误:\n"
                + "\n".join(sorted(new_findings))
            )
        Path(name).replace(output)
        return output_check
    except Exception as exc:
        diagnostic = Path(name)
        if diagnostic.exists():
            raise EpubError(
                f"{exc}\n未通过验收的诊断产物保留在: {diagnostic}"
            ) from exc
        raise


def _validate_output_target(source: Path, output: Path) -> None:
    if output.resolve() == source.resolve():
        raise EpubError("输出路径不能覆盖输入 EPUB")
    if output.exists():
        raise EpubError(f"输出文件已存在: {output.resolve()}")
