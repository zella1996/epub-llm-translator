"""Versioned, DOM-independent reading results. No legacy cache migration."""

from __future__ import annotations

from dataclasses import asdict, dataclass, replace
import hashlib
import json
import re

from translator.sentence_analyzer import (
    Sentence,
    short_text_paragraph_kind,
)
from translator.languages import (
    ENGLISH_SOURCE, SourceLanguage, split_source_sentences,
    validate_source_translation,
)

SCHEMA_VERSION = "reading-assistance-v1"
SPLITTER_VERSION = "english-split-v1"
VALIDATION_VERSION = "reading-validation-v2"
DIFFICULTIES = ("fluent", "effortful", "blocking")
AID_SENTENCE_ORDINAL = re.compile(
    r"(?:第\s*[0-9一二三四五六七八九十百两]+\s*句|上(?:一)?句|下(?:一)?句)"
)


class AssistanceError(ValueError):
    """The core translations or locally bound input are invalid."""


def fingerprint(value: object) -> str:
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
                                     allow_nan=False).encode()).hexdigest()


@dataclass(frozen=True)
class SentenceReading:
    index: int
    translation: str
    difficulty: str


@dataclass(frozen=True)
class ReadingAid:
    scope: str
    sentence_indices: tuple[int, ...]
    quote: str | None = None
    text: str | None = None
    show_translation: bool = False


@dataclass(frozen=True)
class ParagraphAssistance:
    schema_version: str
    input_identity: str
    source_sentences: tuple[Sentence, ...]
    sentences: tuple[SentenceReading, ...]
    aids: tuple[ReadingAid, ...]
    status: str
    diagnostics: tuple[dict, ...]

    @property
    def paragraph_translation(self) -> str:
        return "".join(sentence.translation for sentence in self.sentences)

    def to_dict(self) -> dict:
        return json.loads(json.dumps(asdict(self), ensure_ascii=False))

    def as_epub_note(self):
        """Adapt the validated P1 result at the rendering boundary only."""
        from translator.epub_processor import (
            ReadingAidNote,
            ReadingAssistanceNote,
            ReadingSentenceNote,
        )

        sources = {sentence.index: sentence.text for sentence in self.source_sentences}
        return ReadingAssistanceNote(
            self.paragraph_translation,
            tuple(
                ReadingSentenceNote(sentence.index, sources[sentence.index], sentence.translation)
                for sentence in self.sentences
            ),
            tuple(
                ReadingAidNote(aid.scope, aid.sentence_indices, aid.quote, aid.text, aid.show_translation)
                for aid in self.aids
            ),
        )

    @classmethod
    def from_dict(
        cls, payload: dict, paragraph: str, *, source_language: SourceLanguage = ENGLISH_SOURCE,
    ) -> "ParagraphAssistance":
        if not isinstance(payload, dict) or payload.get("schema_version") != SCHEMA_VERSION:
            raise AssistanceError("unsupported reading schema")
        parsed = parse_assistance(payload, paragraph, source_language=source_language)
        if (payload.get("input_identity") != parsed.input_identity
                or fingerprint(payload.get("source_sentences")) != fingerprint(parsed.to_dict()["source_sentences"])):
            raise AssistanceError("reading source identity mismatch")
        status, diagnostics = payload.get("status"), payload.get("diagnostics")
        if (status not in ("complete", "partial_aids", "translation_only")
                or not isinstance(diagnostics, list)
                or any(not isinstance(d, dict) or not isinstance(d.get("reason"), str)
                       for d in diagnostics)
                or parsed.status != "complete"
                or (status == "translation_only" and parsed.aids)
                or (status != "complete" and not diagnostics)):
            raise AssistanceError("invalid saved reading diagnostics")
        return cls(parsed.schema_version, parsed.input_identity, parsed.source_sentences,
                   parsed.sentences, parsed.aids, status, tuple(diagnostics))


def without_short_sentence_aids(
    assistance: ParagraphAssistance, *, max_words: int
) -> ParagraphAssistance:
    """Drop local aids for short sentences while preserving shared translations."""
    if max_words <= 0:
        return assistance
    excluded = {
        sentence.index
        for sentence in assistance.source_sentences
        if short_text_paragraph_kind(sentence.text, max_words=max_words) is not None
    }
    aids = tuple(
        aid
        for aid in assistance.aids
        if aid.scope == "paragraph"
        or not any(index in excluded for index in aid.sentence_indices)
    )
    return assistance if aids == assistance.aids else replace(assistance, aids=aids)


def without_paragraph_aids(assistance: ParagraphAssistance) -> ParagraphAssistance:
    """Drop cross-sentence aids when the product-level option is disabled."""
    aids = tuple(aid for aid in assistance.aids if aid.scope != "paragraph")
    return assistance if aids == assistance.aids else replace(assistance, aids=aids)


def _text(value: object, limit: int, *, optional: bool = False) -> str | None:
    if value is None and optional:
        return None
    if not isinstance(value, str) or not value.strip() or len(value) > limit:
        raise AssistanceError("invalid or oversized text")
    return value


def _unique_source_quote(source: str, quote: str) -> str | None:
    """Bind a model quote to source text while ignoring layout-only whitespace."""
    normalized = []
    starts = []
    ends = []
    position = 0
    while position < len(source):
        if source[position].isspace():
            end = position + 1
            while end < len(source) and source[end].isspace():
                end += 1
            normalized.append(" ")
            starts.append(position)
            ends.append(end)
            position = end
            continue
        normalized.append(source[position])
        starts.append(position)
        ends.append(position + 1)
        position += 1

    normalized_source = "".join(normalized)
    normalized_quote = " ".join(quote.split())
    if not normalized_quote:
        return None
    matches = [
        position for position in range(len(normalized_source))
        if normalized_source.startswith(normalized_quote, position)
    ]
    if len(matches) != 1:
        return None
    start = matches[0]
    return source[starts[start]:ends[start + len(normalized_quote) - 1]]


def _aid(item: object, sources: dict[int, str]) -> ReadingAid:
    if not isinstance(item, dict) or set(item) - {
        "scope", "sentence_indices", "quote", "text", "show_translation"
    }:
        raise AssistanceError("invalid aid fields")
    scope = item.get("scope")
    indices = item.get("sentence_indices")
    if (not isinstance(scope, str) or scope not in ("phrase", "sentence", "paragraph")
            or not isinstance(indices, list) or not indices
            or any(type(i) is not int or i not in sources for i in indices)
            or len(set(indices)) != len(indices)):
        raise AssistanceError("invalid scope or evidence indices")
    show = item.get("show_translation", False)
    if type(show) is not bool:
        raise AssistanceError("show_translation must be boolean")
    quote = _text(item.get("quote"), 2000, optional=True)
    text = _text(item.get("text"), 4000, optional=True)
    if text and AID_SENTENCE_ORDINAL.search(text):
        raise AssistanceError("aid uses sentence ordinal locator")
    if scope == "phrase":
        if len(indices) != 1 or quote is None or text is None or show:
            raise AssistanceError("phrase requires one source, quote and gloss")
        # HTML source often contains layout line breaks that the model reasonably
        # renders as spaces. Match after collapsing whitespace, but bind the aid
        # back to the exact source substring for later DOM-safe rendering.
        quote = _unique_source_quote(sources[indices[0]], quote)
        if quote is None:
            raise AssistanceError("phrase quote must match exactly once")
    elif scope == "sentence":
        if len(indices) != 1 or quote is not None or not (show or text):
            raise AssistanceError("sentence requires shared translation or hint")
    elif len(indices) < 2 or text is None or quote is not None or show:
        raise AssistanceError("paragraph requires evidence and relation text")
    return ReadingAid(scope, tuple(sorted(indices)), quote, text, show)


def parse_assistance(
    payload: object, paragraph: str, *, source_language: SourceLanguage = ENGLISH_SOURCE,
) -> ParagraphAssistance:
    sources = tuple(split_source_sentences(source_language, paragraph))
    if not sources or not isinstance(payload, dict) or not isinstance(payload.get("sentences"), list):
        raise AssistanceError("missing core sentence translations")
    table = {s.index: s.text for s in sources}
    readings = {}
    for item in payload["sentences"]:
        required_fields = {"index", "translation", "difficulty"}
        if not isinstance(item, dict) or not required_fields.issubset(item):
            raise AssistanceError("invalid sentence fields")
        index, difficulty = item["index"], item["difficulty"]
        if type(index) is not int or index not in table or index in readings:
            raise AssistanceError("duplicate or invalid sentence index")
        if not isinstance(difficulty, str) or difficulty not in DIFFICULTIES:
            raise AssistanceError("invalid difficulty")
        translation = _text(item["translation"], 20000)
        try:
            validate_source_translation(source_language, translation)
        except ValueError as exc:
            raise AssistanceError(str(exc)) from exc
        readings[index] = SentenceReading(index, translation, difficulty)
    if set(readings) != set(table):
        raise AssistanceError("missing sentence translations")
    diagnostics, aids, seen = [], [], set()
    raw_aids = payload.get("aids")
    status = "complete"
    if not isinstance(raw_aids, list):
        status = "translation_only"
        diagnostics.append({"reason": "aids must be an array"})
    else:
        for position, item in enumerate(raw_aids):
            try:
                aid = _aid(item, table)
                if aid in seen:
                    diagnostics.append({"position": position, "reason": "duplicate aid"})
                    continue
                seen.add(aid)
                aids.append(aid)
            except AssistanceError as exc:
                status = "partial_aids"
                diagnostics.append({"position": position, "reason": str(exc)})
    identity = fingerprint({"paragraph": paragraph, "splitter": source_language.splitter_version,
                            "sentences": [asdict(s) for s in sources]})
    return ParagraphAssistance(SCHEMA_VERSION, identity, sources,
                               tuple(readings[i] for i in sorted(readings)),
                               tuple(aids), status, tuple(diagnostics))
