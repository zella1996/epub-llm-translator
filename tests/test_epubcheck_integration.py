from __future__ import annotations

import os
import shutil

import pytest

from tests.helpers import build_epub
from translator.epub_processor import EpubBook, ParagraphNote, run_epubcheck
from translator.reading_assistance import parse_assistance
from translator.translator import generate_preview, generate_reading_preview


class _Model:
    def __init__(self, response):
        self.response = response

    def complete_json(self, system, user):
        return self.response


@pytest.mark.epubcheck
@pytest.mark.skipif(
    os.environ.get("RUN_EPUBCHECK_TESTS") != "1" or shutil.which("epubcheck") is None,
    reason="set RUN_EPUBCHECK_TESTS=1 on a machine with EPUBCheck",
)
@pytest.mark.parametrize("version", ["2.0", "3.0"])
@pytest.mark.parametrize("plain_note_target", [None, "cross-file"])
def test_generated_epub_has_no_epubcheck_errors(tmp_path, version, plain_note_target):
    source = build_epub(
        tmp_path / "source.epub",
        version=version,
        plain_note_target=plain_note_target,
    )
    assert run_epubcheck(source, required=True, allow_invalid=False).returncode == 0

    output = tmp_path / "learning.epub"
    with EpubBook(source) as book:
        book.apply_note(book.paragraph(1, 1), ParagraphNote("这是段落直译。"))
        book.archive(output)
    result = run_epubcheck(output, required=True, allow_invalid=False)
    assert result.returncode == 0, result.output


@pytest.mark.epubcheck
@pytest.mark.skipif(
    os.environ.get("RUN_EPUBCHECK_TESTS") != "1" or shutil.which("epubcheck") is None,
    reason="set RUN_EPUBCHECK_TESTS=1 on a machine with EPUBCheck",
)
@pytest.mark.parametrize("version", ["2.0", "3.0"])
def test_frozen_reading_result_has_no_epubcheck_errors(tmp_path, version):
    source = build_epub(tmp_path / "source.epub", version=version)
    assistance = parse_assistance(
        {
            "sentences": [
                {"index": 1, "translation": "这是原段。", "difficulty": "fluent"},
                {"index": 2, "translation": "尽管很长，意思可验证。", "difficulty": "effortful"},
            ],
            "aids": [
                {"scope": "phrase", "sentence_indices": [1], "quote": "original", "text": "原始的。"},
                {"scope": "sentence", "sentence_indices": [2], "show_translation": True, "text": "让步关系。"},
                {"scope": "paragraph", "sentence_indices": [1, 2], "text": "两句构成让步关系。"},
            ],
        },
        "This is the original paragraph. Although it is long, its meaning remains testable.",
    )
    output = tmp_path / "reading-PREVIEW.epub"
    result = generate_reading_preview(
        source, output, chapter=1, paragraph=1, assistance=assistance,
        require_epubcheck=True,
    )
    assert result.epubcheck_input_ran and result.epubcheck_output_ran
    assert run_epubcheck(output, required=True, allow_invalid=False).returncode == 0


@pytest.mark.epubcheck
def test_existing_source_error_is_preserved_but_not_treated_as_new(tmp_path):
    source = build_epub(tmp_path / "invalid-source.epub", omit_language=True)
    input_check = run_epubcheck(source, required=True, allow_invalid=True)
    assert input_check.returncode != 0
    fast = _Model(
        {
            "translations": [
                {"index": 1, "translation": "这是原段落。"},
                {"index": 2, "translation": "尽管它很长，但意思仍可验证。"},
            ],
            "sentences": [
                {
                    "index": 1,
                    "text": "This is the original paragraph.",
                    "difficulty": "fluent",
                },
                {
                    "index": 2,
                    "text": "Although it is long, its meaning remains testable.",
                    "difficulty": "fluent",
                },
            ],
        }
    )
    output = tmp_path / "book-PREVIEW.epub"
    generate_preview(
        source,
        output,
        chapter=1,
        paragraph=1,
        fast_model=fast,
        quality_model=_Model(
            {
                "sentences": [
                    {
                        "index": 2,
                        "text": "Although it is long, its meaning remains testable.",
                        "difficulty": "fluent",
                    }
                ]
            }
        ),
        require_epubcheck=True,
        allow_invalid_source=True,
    )
    output_check = run_epubcheck(output, required=True, allow_invalid=True)
    assert output_check.returncode != 0
    assert output_check.findings == input_check.findings
