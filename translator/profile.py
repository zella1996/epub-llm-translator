"""Versioned personal reading calibration."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path

from translator.epub_processor import EpubError, ParagraphRef
from translator.sentence_analyzer import Sentence
from translator.languages import ENGLISH_TO_CHINESE, LanguageModule


LABELS = {"fluent", "effortful", "blocking"}
_PROFILE_WORD = re.compile(r"[A-Za-z]+(?:['’-][A-Za-z]+)?")


@dataclass(frozen=True)
class CalibrationExample:
    text: str
    label: str


@dataclass(frozen=True)
class ReadingProfile:
    examples: tuple[CalibrationExample, ...] = ()
    version: int = 1

    @classmethod
    def load(cls, path: Path) -> "ReadingProfile":
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
            if payload.get("version") != 1 or not isinstance(payload.get("examples"), list):
                raise ValueError("unsupported profile")
            examples = tuple(
                CalibrationExample(text=item["text"], label=item["label"])
                for item in payload["examples"]
            )
        except (OSError, json.JSONDecodeError, KeyError, TypeError, ValueError) as exc:
            raise EpubError(f"无法读取个人阅读画像 {path}: {exc}") from exc
        if any(not item.text or item.label not in LABELS for item in examples):
            raise EpubError(f"个人阅读画像包含无效样本: {path}")
        return cls(examples=examples)

    def save(self, path: Path, *, overwrite: bool = False) -> None:
        if path.exists() and not overwrite:
            raise EpubError(f"个人阅读画像已存在: {path}")
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "version": self.version,
            "examples": [
                {"text": item.text, "label": item.label} for item in self.examples
            ],
        }
        temporary = path.with_suffix(path.suffix + ".tmp")
        temporary.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        temporary.replace(path)

    def prompt_examples(self, per_label: int = 3) -> list[dict[str, str]]:
        selected: list[CalibrationExample] = []
        for label in ("fluent", "effortful", "blocking"):
            values = [item for item in self.examples if item.label == label]
            selected.extend(_evenly(values, min(per_label, len(values))))
        return [{"text": item.text, "difficulty": item.label} for item in selected]

    def quality_threshold_payload(self) -> dict[str, object]:
        """Summarize reader-specific difficulty boundaries for quality review."""
        thresholds: list[dict[str, object]] = []
        for label in ("fluent", "effortful", "blocking"):
            values = [item for item in self.examples if item.label == label]
            if not values:
                continue
            counted = [
                (len(_PROFILE_WORD.findall(item.text)), item)
                for item in values
            ]
            boundary = "upper" if label == "fluent" else "lower"
            words, anchor = (
                max(counted, key=lambda item: item[0])
                if label == "fluent"
                else min(counted, key=lambda item: item[0])
            )
            thresholds.append(
                {
                    "difficulty": label,
                    "boundary": boundary,
                    "examples": len(values),
                    "words": words,
                    "anchor": anchor.text,
                }
            )
        return {"version": 1, "thresholds": thresholds}

    def fingerprint_payload(self) -> dict:
        return {
            "version": self.version,
            "examples": [
                {"text": item.text, "label": item.label} for item in self.examples
            ],
        }


def calibration_sentences(
    refs: list[ParagraphRef],
    count: int,
    language: LanguageModule = ENGLISH_TO_CHINESE,
) -> list[Sentence]:
    if count < 1:
        raise EpubError("校准样本数必须至少为 1")
    all_sentences: list[Sentence] = []
    local_candidates: list[Sentence] = []
    ordinary: list[Sentence] = []
    global_index = 1
    for ref in refs:
        paragraph_sentences = language.split(ref.text)
        candidate_indices = language.local_candidates(paragraph_sentences)
        for sentence in paragraph_sentences:
            value = Sentence(global_index, sentence.text)
            all_sentences.append(value)
            if sentence.index in candidate_indices:
                local_candidates.append(value)
            else:
                ordinary.append(value)
            global_index += 1
    if not all_sentences:
        raise EpubError("书籍没有可用于校准的英文句子")
    target = min(count, len(all_sentences))
    difficult_target = min(len(local_candidates), target // 2)
    selected = _evenly(local_candidates, difficult_target)
    remaining = target - len(selected)
    selected.extend(_evenly(ordinary, min(remaining, len(ordinary))))
    remaining = target - len(selected)
    if remaining:
        selected_ids = {item.index for item in selected}
        rest = [item for item in all_sentences if item.index not in selected_ids]
        selected.extend(_evenly(rest, remaining))
    return sorted(selected, key=lambda item: item.index)


def _evenly(values: list, count: int) -> list:
    if count <= 0:
        return []
    if count >= len(values):
        return list(values)
    if count == 1:
        return [values[len(values) // 2]]
    indices = {
        round(position * (len(values) - 1) / (count - 1))
        for position in range(count)
    }
    return [values[index] for index in sorted(indices)]
