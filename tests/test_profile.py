from __future__ import annotations

import pytest

from tests.helpers import build_epub
from translator.epub_processor import EpubBook, EpubError
from translator.profile import CalibrationExample, ReadingProfile, calibration_sentences


def test_profile_round_trip_and_balanced_prompt_examples(tmp_path):
    examples = tuple(
        CalibrationExample(f"{label}-{index}", label)
        for label in ("fluent", "effortful", "blocking")
        for index in range(5)
    )
    profile = ReadingProfile(examples)
    path = tmp_path / "profile.json"
    profile.save(path)
    loaded = ReadingProfile.load(path)
    assert loaded == profile
    prompt = loaded.prompt_examples(per_label=2)
    assert len(prompt) == 6
    assert {item["difficulty"] for item in prompt} == {
        "fluent",
        "effortful",
        "blocking",
    }
    with pytest.raises(EpubError, match="已存在"):
        profile.save(path)


def test_profile_quality_threshold_uses_one_boundary_anchor_per_label():
    profile = ReadingProfile(
        examples=(
            CalibrationExample("A short fluent sentence.", "fluent"),
            CalibrationExample(
                "A longer fluent sentence with a still manageable relation.",
                "fluent",
            ),
            CalibrationExample(
                "An effortful sentence with enough nested structure to cross the reader threshold.",
                "effortful",
            ),
            CalibrationExample(
                "An even longer effortful sentence whose extra wording should not replace the lower boundary anchor.",
                "effortful",
            ),
            CalibrationExample(
                "A blocking construction remains opaque.",
                "blocking",
            ),
        )
    )

    assert profile.quality_threshold_payload() == {
        "version": 1,
        "thresholds": [
            {
                "difficulty": "fluent",
                "boundary": "upper",
                "examples": 2,
                "words": 9,
                "anchor": "A longer fluent sentence with a still manageable relation.",
            },
            {
                "difficulty": "effortful",
                "boundary": "lower",
                "examples": 2,
                "words": 12,
                "anchor": "An effortful sentence with enough nested structure to cross the reader threshold.",
            },
            {
                "difficulty": "blocking",
                "boundary": "lower",
                "examples": 1,
                "words": 5,
                "anchor": "A blocking construction remains opaque.",
            },
        ],
    }


def test_calibration_samples_are_bounded_and_exact(tmp_path):
    source = build_epub(tmp_path / "source.epub")
    with EpubBook(source) as book:
        samples = calibration_sentences(book.paragraphs(), 3)
    assert 1 <= len(samples) <= 3
    assert all(sample.text for sample in samples)
