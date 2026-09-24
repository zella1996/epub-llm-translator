"""S7 regressions discovered by the bounded Japanese model acceptance run."""

from __future__ import annotations

from pathlib import Path

from translator.cli import main
from translator.epub_processor import EpubBook
from translator.languages import (
    JAPANESE_SOURCE,
    resolve_effective_language_configuration,
    split_source_sentences,
)
from translator.reading_assistance import parse_assistance
from translator.translator import write_reading_frozen_manifest
from translator.tuning_profiles import tuning_profile


FIXTURE = Path("tests/fixtures/japanese-epub3.epub")


def test_japanese_frozen_manifest_renders_offline_reading_preview(tmp_path):
    """A Japanese translate manifest must replay without using English identity."""
    tuning = tuning_profile("glm53flash-japanese-reading")
    configuration = resolve_effective_language_configuration(
        ["ja-JP"],
        tuning_name=tuning.name,
        tuning_source_code=tuning.source_language,
        language_profile_key=tuning.language_profile,
    )
    with EpubBook(FIXTURE, reject_generated=True) as book:
        ref = book.paragraph(1, 1)
    source_sentences = split_source_sentences(JAPANESE_SOURCE, ref.text)
    assistance = parse_assistance(
        {
            "sentences": [
                {
                    "index": sentence.index,
                    "translation": f"译文{sentence.index}。",
                    "difficulty": "fluent",
                }
                for sentence in source_sentences
            ],
            "aids": [],
        },
        ref.text,
        source_language=JAPANESE_SOURCE,
    )
    manifest = tmp_path / "reading.json"
    write_reading_frozen_manifest(
        manifest,
        source=FIXTURE,
        profile_key="s7-japanese-preview",
        results={(1, 1): assistance},
        refs=[ref],
        language_identity=configuration.run_identity("off"),
    )
    output = tmp_path / "japanese-PREVIEW.epub"

    assert main([
        "preview",
        str(FIXTURE),
        str(output),
        "--chapter", "1",
        "--paragraph", "1",
        "--pipeline", "reading-v1",
        "--manifest", str(manifest),
        "--tuning-profile", tuning.name,
        "--provider-profile", "zhipu-glm53",
        "--japanese-short-text-gate", "off",
        "--skip-epubcheck",
    ]) == 0
    assert output.is_file()
