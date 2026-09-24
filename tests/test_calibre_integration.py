from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path
from urllib.parse import unquote, urlsplit

import pytest
from lxml import etree

from tests.helpers import build_epub
from translator.epub_processor import (
    EpubBook,
    ReadingAidNote,
    ReadingAssistanceNote,
    ReadingSentenceNote,
)


CALIBRE_APP = Path("/Applications/calibre.app/Contents/MacOS")
EBOOK_CONVERT = shutil.which("ebook-convert") or str(CALIBRE_APP / "ebook-convert")
CALIBRE_DEBUG = shutil.which("calibre-debug") or str(CALIBRE_APP / "calibre-debug")


@pytest.mark.calibre
@pytest.mark.skipif(
    os.environ.get("RUN_CALIBRE_TESTS") != "1"
    or not Path(EBOOK_CONVERT).is_file()
    or not Path(CALIBRE_DEBUG).is_file(),
    reason="set RUN_CALIBRE_TESTS=1 on a machine with Calibre",
)
def test_calibre_azw3_preserves_reading_assistance_and_author_links(tmp_path):
    source = build_epub(tmp_path / "source.epub", plain_note_target="cross-file")
    learning_epub = tmp_path / "learning.epub"
    with EpubBook(source) as book:
        ref = book.paragraph(1, 1)
        book.apply_reading_assistance(
            ref,
            ReadingAssistanceNote(
                "这是原段落的直译。",
                (
                    ReadingSentenceNote(1, ref.text, "这是原段落的直译。"),
                ),
                (ReadingAidNote("sentence", (1,), text="先让步，再给主要判断。"),),
            ),
        )
        book.archive(learning_epub)

    azw3 = tmp_path / "learning.azw3"
    conversion = subprocess.run(
        [EBOOK_CONVERT, str(learning_epub), str(azw3), "-vv"],
        capture_output=True,
        text=True,
        check=False,
        timeout=120,
    )
    assert conversion.returncode == 0, conversion.stdout + conversion.stderr
    assert azw3.is_file()

    exploded = tmp_path / "azw3-exploded"
    extraction = subprocess.run(
        [CALIBRE_DEBUG, "--explode-book", str(azw3), str(exploded)],
        capture_output=True,
        text=True,
        check=False,
        timeout=120,
    )
    assert extraction.returncode == 0, extraction.stdout + extraction.stderr
    html_files = sorted(
        path
        for path in exploded.rglob("*")
        if path.suffix.lower() in {".html", ".xhtml", ".htm"}
    )
    assert html_files
    documents = {
        path.resolve(): etree.parse(
            str(path), etree.XMLParser(recover=False, no_network=True)
        )
        for path in html_files
    }
    combined_text = "\n".join(
        "".join(tree.getroot().itertext()) for tree in documents.values()
    )
    assert "这是原段落的直译。" in combined_text
    assert "先让步，再给主要判断。" in combined_text
    assert "Original author note." in combined_text

    generated_refs = [
        anchor
        for tree in documents.values()
        for anchor in tree.xpath(
            "//*[local-name()='a'][contains(@class, 'epubllmt-analysis-ref')]"
        )
    ]
    assert len(generated_refs) == 1
    assert generated_refs[0].get("type") not in {"noteref", "footnote"}

    backlinks = 0
    generated_source_ids = {
        anchor.get("id") for anchor in generated_refs if anchor.get("id")
    }
    for source_path, tree in documents.items():
        for anchor in tree.xpath("//*[local-name()='a'][@href]"):
            href = anchor.get("href", "")
            parsed = urlsplit(href)
            if parsed.scheme or parsed.netloc or not parsed.fragment:
                continue
            target_path = (
                source_path
                if not parsed.path
                else (source_path.parent / unquote(parsed.path)).resolve()
            )
            assert target_path in documents, f"missing converted target file: {href}"
            targets = documents[target_path].xpath(
                "//*[@id=$fragment or @name=$fragment]",
                fragment=unquote(parsed.fragment),
            )
            assert len(targets) == 1, f"broken converted fragment: {href}"
            assert unquote(parsed.fragment) not in generated_source_ids
            if "↩" in "".join(anchor.itertext()):
                backlinks += 1
    assert backlinks >= 1  # The original author's footnote still returns.
