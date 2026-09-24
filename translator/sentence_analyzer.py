"""Deterministic sentence boundaries and conservative local difficulty hints."""

from __future__ import annotations

import os
import re
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol


STANZA_ACCURATE_TRANSFORMER_CACHE = "models--google--electra-large-discriminator"


@dataclass(frozen=True)
class Sentence:
    index: int
    text: str


@dataclass(frozen=True)
class LocalGateResult:
    needs_model: bool
    reason: str
    score: int
    threshold: int


@dataclass(frozen=True)
class ClauseHint:
    """A parser-derived, source-grounded hint for a single clause."""

    relation: str
    clause: str
    target: str
    depth: int

    def prompt_payload(self) -> dict[str, object]:
        return {
            "relation": self.relation,
            "clause": self.clause,
            "target": self.target,
            "depth": self.depth,
        }


@dataclass(frozen=True)
class LocalSyntaxResult:
    """Conservative local evidence used to ground sentence meanings."""

    needs_structure: bool = False
    clause_count: int = 0
    max_clause_depth: int = 0
    has_inversion: bool = False
    long_attachments: int = 0
    reasons: tuple[str, ...] = ()
    hints: tuple[ClauseHint, ...] = ()

    def prompt_payload(self) -> dict[str, object]:
        return {
            "clause_count": self.clause_count,
            "max_clause_depth": self.max_clause_depth,
            "has_inversion": self.has_inversion,
            "long_attachments": self.long_attachments,
            "reasons": list(self.reasons),
            "hints": [hint.prompt_payload() for hint in self.hints],
        }


class SyntaxAnalyzer(Protocol):
    def analyze(self, sentences: list[Sentence]) -> dict[int, LocalSyntaxResult]: ...


class SyntaxAnalyzerUnavailable(RuntimeError):
    """Raised only when the user explicitly enables an unavailable analyzer."""


_ABBREVIATIONS = {
    "mr.", "mrs.", "ms.", "dr.", "prof.", "sr.", "jr.", "st.",
    "vs.", "etc.", "e.g.", "i.e.", "no.", "fig.", "vol.",
}
_CLOSERS = {'"', "'", "’", "”", ")", "]"}
_JAPANESE_CLOSERS = frozenset("」』）】〕］｝〉》」』”’")
_JAPANESE_TERMINALS = frozenset("。！？!?")
_JAPANESE_ELLIPSIS = frozenset("…")
_JAPANESE_ABBREVIATIONS = frozenset({
    "mr.", "mrs.", "ms.", "dr.", "prof.", "sr.", "jr.", "st.",
    "vs.", "etc.", "e.g.", "i.e.", "no.", "fig.", "vol.",
})
_WORD = re.compile(r"[A-Za-z]+(?:['’-][A-Za-z]+)?")
_DIALOGUE_OPENERS = ('"', "'", "“", "‘", "—", "–")
_SHORT_COMPLEXITY = re.compile(
    r"\b(if|because|though|although|unless|until|while|whereas|whether|"
    r"who|whom|whose|which|that|provided|notwithstanding)\b",
    re.IGNORECASE,
)
_WH_QUESTION_START = re.compile(
    r"^\s*[\"'“‘]*(who|whom|whose|which)\b",
    re.IGNORECASE,
)
_ARCHAIC_OR_UNUSUAL = re.compile(
    r"\b(thou|thee|thy|thine|hath|doth|shalt|wilt|wherefore|whence|"
    r"hither|thither|ere|lest|betwixt)\b",
    re.IGNORECASE,
)


def _short_complexity_matches(text: str) -> list[re.Match[str]]:
    """Return clause markers without treating an opening wh-question as a clause."""
    matches = list(_SHORT_COMPLEXITY.finditer(text))
    question_start = _WH_QUESTION_START.match(text)
    if question_start and text.rstrip().rstrip('"\'’”').endswith("?"):
        matches = [
            match
            for match in matches
            if not (
                match.start() == question_start.start(1)
                and match.end() == question_start.end(1)
            )
        ]
    return matches


def split_sentences(text: str) -> list[Sentence]:
    """Split English prose while keeping each returned string exact."""
    stripped = text.strip()
    if not stripped:
        return []
    starts = [0]
    index = 0
    while index < len(stripped):
        char = stripped[index]
        if char not in ".!?":
            index += 1
            continue
        end = index + 1
        while end < len(stripped) and stripped[end] in _CLOSERS:
            end += 1
        token_start = index
        while token_start > 0 and not stripped[token_start - 1].isspace():
            token_start -= 1
        token = stripped[token_start:index + 1].lower()
        decimal = (
            char == "."
            and index > 0
            and end < len(stripped)
            and stripped[index - 1].isdigit()
            and stripped[end].isdigit()
        )
        initial = char == "." and len(token) == 2 and token[0].isalpha()
        if token in _ABBREVIATIONS or decimal or initial:
            index = end
            continue
        if end == len(stripped) or stripped[end].isspace():
            next_start = end
            while next_start < len(stripped) and stripped[next_start].isspace():
                next_start += 1
            if next_start < len(stripped):
                starts.append(next_start)
            index = next_start
        else:
            index = end
    boundaries = starts[1:] + [len(stripped)]
    return [
        Sentence(position + 1, stripped[start:end].rstrip())
        for position, (start, end) in enumerate(zip(starts, boundaries))
        if stripped[start:end].strip()
    ]


def _japanese_period_is_internal(text: str, index: int) -> bool:
    """Keep decimal points and common Latin abbreviations inside a Japanese sentence."""
    if index > 0 and index + 1 < len(text) and text[index - 1].isdigit() and text[index + 1].isdigit():
        return True
    start = index
    while start > 0 and (text[start - 1].isascii() and (text[start - 1].isalpha() or text[start - 1] == ".")):
        start -= 1
    token = text[start:index + 1].casefold()
    if token in _JAPANESE_ABBREVIATIONS:
        return True
    # Initials such as "A.スミス" are name components, not sentence endings.
    return len(token) == 2 and token[0].isalpha()


def _japanese_quote_continues_sentence(text: str, start: int) -> bool:
    """Keep a quoted constituent attached to its following Japanese grammar."""
    remainder = text[start:].lstrip()
    return bool(remainder.startswith((
        "の", "は", "が", "を", "に", "へ", "で", "も", "や", "か", "から", "まで",
        "より", "なら", "ならば", "だ", "です", "と", "って", "とは", "という", "と言",
        "と答", "と呼", "と叫", "と尋",
    )))


def split_japanese_sentences(text: str) -> list[Sentence]:
    """Return exact, contiguous Japanese sentence slices without normalizing text.

    Whitespace belongs to the preceding slice at a boundary.  This preserves a
    lossless offset mapping while leaving internal whitespace untouched.
    """
    if not text or not text.strip():
        return []
    slices: list[tuple[int, int]] = []
    start = 0
    index = 0
    while index < len(text):
        char = text[index]
        if char == "." and _japanese_period_is_internal(text, index):
            index += 1
            continue
        is_terminal = char in _JAPANESE_TERMINALS or char == "."
        if not is_terminal and char not in _JAPANESE_ELLIPSIS and char not in _JAPANESE_CLOSERS:
            index += 1
            continue
        run_end = index if char in _JAPANESE_CLOSERS else index + 1
        if run_end:
            while run_end < len(text) and text[run_end] in (_JAPANESE_TERMINALS | _JAPANESE_ELLIPSIS | {"."}):
                if text[run_end] == "." and _japanese_period_is_internal(text, run_end):
                    break
                run_end += 1
        closer_end = run_end
        while closer_end < len(text) and text[closer_end] in _JAPANESE_CLOSERS:
            closer_end += 1
        has_closer = closer_end > run_end
        # An ellipsis alone is an interruption, not necessarily a boundary;
        # a closing quotation mark does close an otherwise unterminated turn.
        has_explicit_terminal = any(mark in _JAPANESE_TERMINALS or mark == "." for mark in text[index:run_end])
        if not has_explicit_terminal and not has_closer:
            index = run_end
            continue
        # A bare closer inside running prose is not a sentence boundary.  It
        # closes a quoted or parenthetical constituent; only whitespace (the
        # no-punctuation dialogue layout) or end-of-text can terminate it.
        if (
            not has_explicit_terminal
            and has_closer
            and closer_end < len(text)
            and not text[closer_end].isspace()
        ):
            index = closer_end
            continue
        if has_closer and _japanese_quote_continues_sentence(text, closer_end):
            index = closer_end
            continue
        end = closer_end
        while end < len(text) and text[end].isspace():
            end += 1
        slices.append((start, end))
        start = end
        index = end
    if start < len(text):
        slices.append((start, len(text)))
    return [
        Sentence(position, text[begin:end])
        for position, (begin, end) in enumerate(slices, 1)
        if text[begin:end].strip()
    ]


def local_candidate_indices(sentences: list[Sentence]) -> set[int]:
    """Favor recall without allowing heuristics to make the final decision."""
    result: set[int] = set()
    connectors = re.compile(
        r"\b(although|whereas|unless|notwithstanding|inasmuch|thereby|whereby|"
        r"nevertheless|consequently|provided that|even though)\b",
        re.IGNORECASE,
    )
    for sentence in sentences:
        words = re.findall(r"[A-Za-z]+(?:['’-][A-Za-z]+)?", sentence.text)
        if len(words) >= 28 or sentence.text.count(",") >= 3:
            result.add(sentence.index)
        if any(mark in sentence.text for mark in (";", "—", " – ")):
            result.add(sentence.index)
        if connectors.search(sentence.text):
            result.add(sentence.index)
    return result


def short_simple_paragraph_kind(
    text: str,
    *,
    dialogue_words: int = 16,
    prose_words: int = 10,
) -> str | None:
    """Return the locally skippable kind without making any model request."""
    stripped = text.strip()
    sentences = split_sentences(stripped)
    if len(sentences) != 1:
        return None
    dialogue = stripped.startswith(_DIALOGUE_OPENERS)
    analysis_text = re.sub(r"^[—–]\s*", "", stripped)
    analysis_sentences = split_sentences(analysis_text)
    if local_candidate_indices(analysis_sentences):
        return None
    if _short_complexity_matches(analysis_text):
        return None
    words = _WORD.findall(analysis_text)
    threshold = dialogue_words if dialogue else prose_words
    if threshold <= 0 or not words or len(words) > threshold:
        return None
    return "dialogue" if dialogue else "prose"


def short_text_paragraph_kind(text: str, *, max_words: int = 15) -> str | None:
    """Return a length-only preflight skip kind without making a model request."""
    stripped = text.strip()
    words = _WORD.findall(stripped)
    if max_words <= 0 or not words or len(words) > max_words:
        return None
    return "dialogue" if stripped.startswith(_DIALOGUE_OPENERS) else "prose"


def local_difficulty_score(text: str) -> int:
    stripped = text.strip()
    dialogue = stripped.startswith(_DIALOGUE_OPENERS)
    analysis_text = re.sub(r"^[—–]\s*", "", stripped)
    sentences = split_sentences(analysis_text)
    words = _WORD.findall(analysis_text)
    score = len(words) + max(0, len(sentences) - 1) * 8
    score += len(local_candidate_indices(sentences)) * 16
    score += len(_short_complexity_matches(analysis_text)) * 10
    score += max(0, analysis_text.count(",") - 1) * 3
    score += sum(8 for mark in (";", ":", "—", "–", "(", ")") if mark in analysis_text)
    score += sum(
        4
        for word in words
        if len(word) >= 10 and not word[0].isupper()
    )
    if _ARCHAIC_OR_UNUSUAL.search(analysis_text):
        score += 20
    if dialogue:
        score = max(0, score - 3)
    return score


def local_model_gate(
    text: str,
    *,
    mode: str = "conservative",
    dialogue_words: int = 16,
    prose_words: int = 10,
    hard_profile_texts: tuple[str, ...] = (),
) -> LocalGateResult:
    """Decide whether a paragraph is safe enough to skip before any LLM call."""
    if mode not in {"off", "conservative", "balanced"}:
        raise ValueError(f"unknown local gate mode: {mode}")
    score = local_difficulty_score(text)
    if mode == "off":
        return LocalGateResult(True, "gate-off", score, -1)

    base_threshold = 18 if mode == "conservative" else 30
    hard_scores = [local_difficulty_score(value) for value in hard_profile_texts]
    threshold = (
        min(base_threshold, max(-1, min(hard_scores) - 1))
        if hard_scores
        else base_threshold
    )
    stripped = text.strip()
    analysis_text = re.sub(r"^[—–]\s*", "", stripped)
    sentences = split_sentences(analysis_text)
    if not sentences:
        return LocalGateResult(True, "empty", score, threshold)
    if len(sentences) > (2 if mode == "balanced" else 1):
        return LocalGateResult(True, "multiple-sentences", score, threshold)
    if local_candidate_indices(sentences):
        return LocalGateResult(True, "structural-signal", score, threshold)
    if _short_complexity_matches(analysis_text):
        return LocalGateResult(True, "clause-signal", score, threshold)
    if any(mark in analysis_text for mark in (";", ":", "(", ")")):
        return LocalGateResult(True, "punctuation-signal", score, threshold)
    words = _WORD.findall(analysis_text)
    lexical_risk = _ARCHAIC_OR_UNUSUAL.search(analysis_text) or any(
        len(word) >= 10 and not word[0].isupper() for word in words
    )
    if lexical_risk and mode == "conservative":
        return LocalGateResult(True, "lexical-signal", score, threshold)
    if score > threshold:
        return LocalGateResult(True, "score", score, threshold)

    short_kind = short_simple_paragraph_kind(
        text,
        dialogue_words=dialogue_words,
        prose_words=prose_words,
    )
    reason = f"short-{short_kind}" if short_kind else "local-easy"
    return LocalGateResult(False, reason, score, threshold)


_RELATIVE_MARKERS = frozenset({"who", "whom", "whose", "which", "that"})
_NEGATIVE_FRONTING = frozenset(
    {"barely", "hardly", "little", "never", "neither", "nor", "rarely", "seldom"}
)
_CONDITIONAL_INVERSION = frozenset({"had", "should", "were"})


class SpacySyntaxAnalyzer:
    """Optional spaCy-based detector for reader-facing relative-clause links.

    Its output is deliberately a hint, not a grammar authority. It can enrich a
    sentence already selected as difficult, but cannot create a card candidate.
    """

    def __init__(self, model: str = "en_core_web_sm") -> None:
        try:
            import spacy
        except ImportError as exc:
            raise SyntaxAnalyzerUnavailable(
                "未安装 spaCy；请安装可选句法依赖：pip install -e '.[syntax]'，"
                f"然后运行 python -m spacy download {model}"
            ) from exc
        try:
            self._nlp = spacy.load(model, disable=["ner", "lemmatizer"])
        except OSError as exc:
            raise SyntaxAnalyzerUnavailable(
                f"未找到 spaCy 英文模型 {model}；请运行：python -m spacy download {model}"
            ) from exc
        if not self._nlp.has_pipe("parser"):
            raise SyntaxAnalyzerUnavailable(f"spaCy 模型 {model} 不包含 dependency parser")
        self.model = model
        self._lock = threading.Lock()

    def analyze(self, sentences: list[Sentence]) -> dict[int, LocalSyntaxResult]:
        # Parser inference is cheap relative to an API request. Serializing access
        # also avoids relying on undocumented thread-safety guarantees of pipeline
        # components when translate_book uses workers.
        with self._lock:
            docs = list(self._nlp.pipe((item.text for item in sentences), batch_size=32))
        return {
            sentence.index: self._analyze_doc(doc)
            for sentence, doc in zip(sentences, docs)
        }

    def _analyze_doc(self, doc: Any) -> LocalSyntaxResult:
        clause_tokens = [
            token
            for token in doc
            if token.dep_ in {"acl", "relcl"}
            and token.head.pos_ in {"NOUN", "PROPN", "PRON", "NUM"}
            and any(part.lower_ in _RELATIVE_MARKERS for part in token.subtree)
            and self._is_trustworthy_relative_root(token)
        ]
        hints = list(self._clause_hint(token, clause_tokens) for token in clause_tokens)
        hints = self._recover_appositive_relative_chain(doc, clause_tokens, hints)
        max_depth = max((hint.depth for hint in hints), default=0)
        long_attachments = sum(
            abs(token.i - token.head.i) >= 7 for token in clause_tokens
        )
        inversion = self._has_inversion(doc)
        interrupted_main_clause = self._has_interrupted_main_clause(doc, clause_tokens)
        reasons: list[str] = []
        if hints:
            reasons.append("relative-clause")
        if max_depth >= 2:
            reasons.append("nested-clauses")
        if len(clause_tokens) >= 2:
            reasons.append("multiple-clauses")
        if long_attachments:
            reasons.append("long-attachment")
        if interrupted_main_clause:
            reasons.append("interrupted-main-clause")
        return LocalSyntaxResult(
            needs_structure=bool(hints),
            clause_count=len(clause_tokens),
            max_clause_depth=max_depth,
            has_inversion=inversion,
            long_attachments=long_attachments,
            reasons=tuple(reasons),
            hints=tuple(hints),
        )

    @staticmethod
    def _is_trustworthy_relative_root(token: Any) -> bool:
        """Reject parser spans that visibly stop mid-clause or swallow a main clause."""
        subtree = sorted(token.subtree, key=lambda part: part.i)
        content = [part for part in subtree if not part.is_space and not part.is_punct]
        if not content or content[-1].pos_ in {"ADP", "ADV", "CCONJ", "SCONJ"}:
            return False
        # A named subject introducing a nested complement after the relative root
        # is a strong local sign that the dependency parser has consumed the next
        # main clause.  Do not attempt to reconstruct that boundary heuristically.
        for part in subtree:
            if part.i <= token.i or part.dep_ not in {"ccomp", "advcl"}:
                continue
            if any(
                child.dep_ in {"nsubj", "nsubjpass"} and child.pos_ == "PROPN"
                for child in part.children
            ):
                return False
        return True

    @classmethod
    def _recover_appositive_relative_chain(
        cls,
        doc: Any,
        clause_tokens: list[Any],
        hints: list[ClauseHint],
    ) -> list[ClauseHint]:
        """Recover a narrow, parser-fragile ``which … — a N of which …`` chain.

        Literary prose often repeats an appositive antecedent after an em dash.
        In that shape, spaCy can attach the first relative root correctly but
        mislabel the following ``of which`` and coordinated ``which`` as content
        or coordination clauses.  We recover only this explicit surface pattern;
        arbitrary ``ccomp``/``conj`` clauses remain excluded.
        """
        recovered: list[ClauseHint] = []
        replaced: set[tuple[str, str]] = set()
        for root in clause_tokens:
            markers = [part for part in root.subtree if part.lower_ == "which"]
            if not markers:
                continue
            marker = min(markers, key=lambda token: token.i)
            target = cls._target_span(root.head)
            dash_index = cls._next_punctuation(doc, marker.i + 1, "—")
            if dash_index is None:
                continue
            of_index = cls._find_of_which(doc, dash_index + 1)
            if of_index is None:
                continue
            antecedent_index = cls._previous_content(doc, of_index)
            if antecedent_index is None or doc[antecedent_index].pos_ not in {
                "NOUN",
                "PROPN",
                "PRON",
                "NUM",
            }:
                continue
            appositive_target = cls._target_span(doc[antecedent_index])
            comma_index = cls._comma_before_and_which(doc, of_index + 2)
            if comma_index is None:
                continue
            and_index = cls._next_content(doc, comma_index + 1)
            which_index = (
                cls._next_content(doc, and_index + 1) if and_index is not None else None
            )
            if (
                and_index is None
                or doc[and_index].lower_ != "and"
                or which_index is None
                or doc[which_index].lower_ != "which"
            ):
                continue
            end_index = cls._next_terminal_punctuation(doc, which_index + 1)
            if end_index is None:
                continue
            recovered.extend(
                (
                    ClauseHint(
                        "relcl",
                        cls._source_span_from_tokens(doc, marker.i, dash_index),
                        target,
                        1,
                    ),
                    ClauseHint(
                        "relcl",
                        cls._source_span_from_tokens(doc, of_index, comma_index),
                        appositive_target,
                        1,
                    ),
                    ClauseHint(
                        "relcl",
                        cls._source_span_from_tokens(doc, which_index, end_index),
                        appositive_target,
                        1,
                    ),
                )
            )
            replaced.add((marker.text.casefold(), target))

        retained = [
            hint
            for hint in hints
            if (
                hint.clause.lstrip().split(maxsplit=1)[0].casefold().rstrip(",:;")
                ,
                hint.target,
            )
            not in replaced
        ]
        seen = {(hint.clause, hint.target) for hint in retained}
        retained.extend(
            hint for hint in recovered if hint.clause and (hint.clause, hint.target) not in seen
        )
        return retained

    @staticmethod
    def _next_content(doc: Any, index: int) -> int | None:
        for position in range(index, len(doc)):
            if not doc[position].is_space:
                return position
        return None

    @staticmethod
    def _previous_content(doc: Any, index: int) -> int | None:
        for position in range(index - 1, -1, -1):
            if not doc[position].is_space:
                return position
        return None

    @staticmethod
    def _next_punctuation(doc: Any, index: int, value: str) -> int | None:
        return next(
            (position for position in range(index, len(doc)) if doc[position].text == value),
            None,
        )

    @classmethod
    def _find_of_which(cls, doc: Any, index: int) -> int | None:
        for position in range(index, len(doc)):
            if doc[position].text in {".", ";", "—"}:
                return None
            if doc[position].lower_ != "of":
                continue
            following = cls._next_content(doc, position + 1)
            if following is not None and doc[following].lower_ == "which":
                return position
        return None

    @classmethod
    def _comma_before_and_which(cls, doc: Any, index: int) -> int | None:
        for position in range(index, len(doc)):
            if doc[position].text in {".", ";", "—"}:
                return None
            if doc[position].text != ",":
                continue
            and_index = cls._next_content(doc, position + 1)
            which_index = (
                cls._next_content(doc, and_index + 1) if and_index is not None else None
            )
            if (
                and_index is not None
                and doc[and_index].lower_ == "and"
                and which_index is not None
                and doc[which_index].lower_ == "which"
            ):
                return position
        return None

    @staticmethod
    def _next_terminal_punctuation(doc: Any, index: int) -> int | None:
        return next(
            (position for position in range(index, len(doc)) if doc[position].text in {".", "!", "?"}),
            None,
        )

    @staticmethod
    def _source_span_from_tokens(doc: Any, start: int, end: int) -> str:
        """Return exact source text between content tokens, excluding end punctuation."""
        while start < end and doc[start].is_space:
            start += 1
        while end > start and doc[end - 1].is_space:
            end -= 1
        if start >= end:
            return ""
        return re.sub(
            r"\s+",
            " ",
            doc.text[doc[start].idx : doc[end - 1].idx + len(doc[end - 1])],
        ).strip()

    @staticmethod
    def _clause_hint(token: Any, clause_tokens: list[Any]) -> ClauseHint:
        depth = 1 + sum(ancestor in clause_tokens for ancestor in token.ancestors)
        span = re.sub(
            r"\s+", " ", token.doc[token.left_edge.i : token.right_edge.i + 1].text
        ).strip().rstrip(",:;")
        return ClauseHint(token.dep_, span, SpacySyntaxAnalyzer._target_span(token.head), depth)

    @staticmethod
    def _target_span(head: Any) -> str:
        """Return a short reader-facing source span, never a parser-only token."""
        if head.pos_ in {"NOUN", "PROPN", "PRON"}:
            for chunk in head.doc.noun_chunks:
                if chunk.start <= head.i < chunk.end:
                    return re.sub(r"\s+", " ", chunk.text).strip()
        predicate = head
        if head.pos_ in {"ADJ", "NOUN"} and head.head != head:
            predicate = head.head
        subjects = [
            token
            for token in predicate.children
            if token.dep_ in {"nsubj", "nsubjpass", "csubj"}
        ]
        auxiliaries = [
            token
            for token in predicate.children
            if token.dep_ in {"aux", "auxpass", "cop"}
        ]
        focus = [*subjects, *auxiliaries, head]
        if subjects:
            start = min(token.i for token in focus)
            end = max(token.i for token in focus)
            return re.sub(r"\s+", " ", head.doc[start : end + 1].text).strip()
        return re.sub(r"\s+", " ", head.text).strip()

    @staticmethod
    def _has_interrupted_main_clause(doc: Any, clause_tokens: list[Any]) -> bool:
        roots = [token for token in doc if token.dep_ == "ROOT"]
        if not roots:
            return False
        root = roots[0]
        subjects = [token for token in root.children if token.dep_ in {"nsubj", "nsubjpass", "csubj"}]
        if not subjects:
            return False
        subject = subjects[0]
        return (
            subject.i < root.i
            and root.i - subject.i >= 7
            and any(subject.i < token.i < root.i for token in clause_tokens)
        )

    @staticmethod
    def _has_inversion(doc: Any) -> bool:
        content = [token for token in doc if not token.is_space and not token.is_punct]
        if not content:
            return False
        first = content[0].lower_
        roots = [token for token in doc if token.dep_ == "ROOT"]
        root = roots[0] if roots else None
        subjects = (
            [token for token in root.children if token.dep_ in {"nsubj", "nsubjpass"}]
            if root is not None
            else []
        )
        auxiliaries = (
            [token for token in root.children if token.dep_ in {"aux", "auxpass"}]
            if root is not None
            else []
        )
        if subjects and auxiliaries and min(token.i for token in auxiliaries) < min(token.i for token in subjects):
            if first in _NEGATIVE_FRONTING or first in {"only", "so", "such"}:
                return True
        if first in _CONDITIONAL_INVERSION and len(content) >= 2:
            return content[1].dep_ in {"nsubj", "nsubjpass"}
        return False


class StanzaSyntaxAnalyzer:
    """Optional Stanza constituency/dependency analyzer for reader-facing clauses."""

    _RELATIVE_MARKERS = frozenset({"that", "who", "whom", "whose", "which"})
    _CLAUSE_RELATIONS = frozenset(
        {"acl", "acl:relcl", "advcl", "ccomp", "csubj", "csubj:pass", "xcomp"}
    )

    def __init__(
        self,
        package: str = "default_accurate",
        *,
        use_gpu: bool = True,
        model_dir: str | None = None,
        hf_cache_dir: str | None = None,
    ) -> None:
        if hf_cache_dir is not None:
            cache_root = Path(hf_cache_dir)
            if not cache_root.is_dir():
                raise SyntaxAnalyzerUnavailable(
                    f"Hugging Face 缓存目录不存在: {hf_cache_dir}"
                )
            if package == "default_accurate":
                snapshots = (
                    cache_root
                    / "hub"
                    / STANZA_ACCURATE_TRANSFORMER_CACHE
                    / "snapshots"
                )
                complete_snapshot = (
                    any(
                        (snapshot / "config.json").is_file()
                        and any(
                            (snapshot / filename).is_file()
                            for filename in (
                                "model.safetensors",
                                "pytorch_model.bin",
                            )
                        )
                        for snapshot in snapshots.iterdir()
                    )
                    if snapshots.is_dir()
                    else False
                )
                if not complete_snapshot:
                    raise SyntaxAnalyzerUnavailable(
                        "Hugging Face 缓存缺少 default_accurate 所需的 "
                        f"Electra-large 权重: {hf_cache_dir}"
                    )
            os.environ["HF_HOME"] = hf_cache_dir
        try:
            import stanza
        except ImportError as exc:
            raise SyntaxAnalyzerUnavailable(
                "未安装 Stanza；请安装可选依赖：pip install -e '.[stanza]'，"
                "然后下载英文 default_accurate 资源和 Electra-large 缓存"
            ) from exc
        try:
            pipeline_options: dict[str, object] = {}
            if model_dir is not None:
                pipeline_options["dir"] = model_dir
            self._nlp = stanza.Pipeline(
                "en",
                package=package,
                processors="tokenize,pos,lemma,depparse,constituency",
                use_gpu=use_gpu,
                download_method=None,
                verbose=False,
                **pipeline_options,
            )
        except Exception as exc:
            raise SyntaxAnalyzerUnavailable(
                f"无法从本地资源加载 Stanza 英文模型包 {package}；"
                "请检查 --stanza-model-dir 和 --stanza-hf-cache-dir"
            ) from exc
        self.package = package
        self._lock = threading.Lock()

    def analyze(self, sentences: list[Sentence]) -> dict[int, LocalSyntaxResult]:
        with self._lock:
            docs = self._nlp.bulk_process([item.text for item in sentences])
        return {
            item.index: self._analyze_sentence(item.text, doc.sentences[0])
            for item, doc in zip(sentences, docs)
            if doc.sentences
        }

    def _analyze_sentence(self, text: str, sentence: Any) -> LocalSyntaxResult:
        phrase_nodes, inversion = self._phrase_spans(sentence.constituency)
        noun_phrases = [span for label, *span in phrase_nodes if label == "NP"]
        clause_spans = [
            (start, end, depth)
            for label, start, end, depth in phrase_nodes
            if label == "SBAR"
        ]
        hints: list[ClauseHint] = []
        long_attachments = 0
        for start, end, depth in clause_spans:
            words = sentence.words[start:end]
            if not words or words[0].text.lower() == "than":
                continue
            relative_marker = words[0].text.lower() in self._RELATIVE_MARKERS or (
                len(words) >= 2
                and words[0].upos == "ADP"
                and words[1].text.lower() in self._RELATIVE_MARKERS
            )
            if not relative_marker:
                continue
            roots = [
                word
                for word in words
                if word.deprel in self._CLAUSE_RELATIONS
                and (word.head == 0 or not start < word.head <= end)
            ]
            if not roots:
                continue
            root = roots[0]
            if words[0].text.lower() == "that":
                preceding_index = start - 1
                while (
                    preceding_index >= 0
                    and sentence.words[preceding_index].upos == "PUNCT"
                ):
                    preceding_index -= 1
                if root.deprel not in {"acl", "acl:relcl"} or (
                    preceding_index < 0
                    or sentence.words[preceding_index].upos
                    not in {"NOUN", "PROPN", "PRON", "NUM"}
                ):
                    continue
            if root.head == 0:
                continue
            target_index = root.head - 1
            if target_index < 0 or sentence.words[target_index].upos not in {
                "NOUN",
                "PROPN",
                "PRON",
                "NUM",
            }:
                continue
            target = self._target_span(text, sentence.words, noun_phrases, target_index)
            clause = self._source_span(text, words)
            if not clause or not target or clause == target:
                continue
            if abs(root.id - root.head) >= 7:
                long_attachments += 1
            hints.append(ClauseHint(root.deprel, clause, target, depth))

        hints = self._dedupe_reader_hints(hints)
        max_depth = max((hint.depth for hint in hints), default=0)
        reasons: list[str] = []
        if hints:
            reasons.append("relative-clause")
        if max_depth >= 2:
            reasons.append("nested-clauses")
        if len(hints) >= 2:
            reasons.append("multiple-clauses")
        if long_attachments:
            reasons.append("long-attachment")
        return LocalSyntaxResult(
            needs_structure=bool(hints),
            clause_count=len(hints),
            max_clause_depth=max_depth,
            has_inversion=inversion,
            long_attachments=long_attachments,
            reasons=tuple(reasons),
            hints=tuple(hints),
        )

    @staticmethod
    def _dedupe_reader_hints(hints: list[ClauseHint]) -> list[ClauseHint]:
        """Keep coordinated relative clauses intact while removing duplicate spans."""
        selected: list[tuple[int, ClauseHint, str, str]] = []
        for index, hint in sorted(
            enumerate(hints),
            key=lambda item: (
                -len(
                    re.findall(
                        r"\b(?:who|whom|whose|which)\b",
                        item[1].clause,
                        flags=re.IGNORECASE,
                    )
                ),
                len(item[1].clause),
                item[0],
            ),
        ):
            clause = re.sub(r"\s+", " ", hint.clause).strip().casefold()
            target = re.sub(r"\s+", " ", hint.target).strip().casefold()
            if any(
                target == kept_target
                and (clause in kept_clause or kept_clause in clause)
                for _, _, kept_clause, kept_target in selected
            ):
                continue
            selected.append((index, hint, clause, target))
        return [hint for _, hint, _, _ in sorted(selected)]

    @staticmethod
    def _phrase_spans(tree: Any) -> tuple[list[tuple[str, int, int, int]], bool]:
        spans: list[tuple[str, int, int, int]] = []
        cursor = 0

        def visit(node: Any, clause_depth: int = 0) -> tuple[int, int]:
            nonlocal cursor
            children = list(getattr(node, "children", ()))
            if not children:
                start = cursor
                cursor += 1
                return start, cursor
            label = str(getattr(node, "label", ""))
            next_depth = clause_depth + (label == "SBAR")
            child_spans = [visit(child, next_depth) for child in children]
            start, end = child_spans[0][0], child_spans[-1][1]
            spans.append((label, start, end, next_depth))
            return start, end

        visit(tree)
        inversion = any(label in {"SINV", "SQ"} for label, *_ in spans)
        return spans, inversion

    @classmethod
    def _target_span(
        cls,
        text: str,
        words: list[Any],
        noun_phrases: list[list[int]],
        target_index: int,
    ) -> str:
        containing = [
            (start, end)
            for start, end, _depth in noun_phrases
            if start <= target_index < end
        ]
        if containing and words[target_index].upos in {"NOUN", "PROPN", "PRON"}:
            start, end = min(containing, key=lambda span: span[1] - span[0])
            return cls._source_span(text, words[start:end])
        return cls._source_span(text, [words[target_index]])

    @staticmethod
    def _source_span(text: str, words: list[Any]) -> str:
        if not words:
            return ""
        start = getattr(words[0], "start_char", None)
        end = getattr(words[-1], "end_char", None)
        if isinstance(start, int) and isinstance(end, int):
            return re.sub(r"\s+", " ", text[start:end]).strip()
        return re.sub(r"\s+", " ", " ".join(word.text for word in words)).strip()


def create_syntax_analyzer(
    mode: str,
    *,
    model: str = "en_core_web_sm",
    stanza_package: str = "default_accurate",
    stanza_model_dir: str | None = None,
    stanza_hf_cache_dir: str | None = None,
) -> SyntaxAnalyzer | None:
    if mode == "off":
        return None
    if mode == "spacy":
        return SpacySyntaxAnalyzer(model)
    if mode == "stanza":
        return StanzaSyntaxAnalyzer(
            stanza_package,
            model_dir=stanza_model_dir,
            hf_cache_dir=stanza_hf_cache_dir,
        )
    raise ValueError(f"unknown syntax analyzer: {mode}")
