from __future__ import annotations

import hashlib
import os
import shutil
import subprocess
from pathlib import Path
from urllib.parse import unquote, urlsplit

import pytest
from lxml import etree

from translator.epub_processor import (
    EpubBook,
    ReadingAidNote,
    ReadingAssistanceNote,
    ReadingSentenceNote,
    run_epubcheck,
)


REAL_EPUB = os.environ.get("REAL_EPUB_PATH")
CALIBRE_APP = Path("/Applications/calibre.app/Contents/MacOS")
EBOOK_CONVERT = shutil.which("ebook-convert") or str(CALIBRE_APP / "ebook-convert")
CALIBRE_DEBUG = shutil.which("calibre-debug") or str(CALIBRE_APP / "calibre-debug")


@pytest.mark.real_epub
@pytest.mark.skipif(
    not REAL_EPUB
    or not shutil.which("epubcheck")
    or not Path(EBOOK_CONVERT).is_file()
    or not Path(CALIBRE_DEBUG).is_file(),
    reason="set REAL_EPUB_PATH on a machine with EPUBCheck and Calibre",
)
def test_real_epub_preserves_resources_and_survives_calibre(tmp_path):
    source = Path(REAL_EPUB)
    assert run_epubcheck(source, required=True, allow_invalid=False).returncode == 0
    output = tmp_path / "real-learning.epub"

    with EpubBook(source) as book:
        refs = book.paragraphs()
        assert refs
        assert all("Release date:" not in ref.text for ref in refs)
        ref = refs[0]
        before_text = book.chapter_text_snapshot(ref.chapter)
        before_structure = book.chapter_structure_snapshot(ref.chapter)
        before_hashes = _file_hashes(book.root)
        book.apply_reading_assistance(
            ref,
            ReadingAssistanceNote(
                "达什伍德一家很早以前就定居在苏塞克斯。",
                (
                    ReadingSentenceNote(
                        1, ref.text, "达什伍德一家很早以前就定居在苏塞克斯。"
                    ),
                ),
                (ReadingAidNote("sentence", (1,), text="先说家庭，再说明定居地点。"),),
            ),
        )
        assert book.chapter_text_snapshot(ref.chapter) == before_text
        assert book.chapter_structure_snapshot(ref.chapter) == before_structure
        after_hashes = _file_hashes(book.root)
        changed = {
            name
            for name in before_hashes
            if before_hashes[name] != after_hashes.get(name)
        }
        expected_changed = (book.opf_dir / ref.href).resolve().relative_to(book.root).as_posix()
        assert changed == {expected_changed}
        book.archive(output)

    assert run_epubcheck(output, required=True, allow_invalid=False).returncode == 0
    azw3 = tmp_path / "real-learning.azw3"
    conversion = subprocess.run(
        [EBOOK_CONVERT, str(output), str(azw3), "-vv"],
        capture_output=True,
        text=True,
        check=False,
        timeout=180,
    )
    assert conversion.returncode == 0, conversion.stdout + conversion.stderr

    exploded = tmp_path / "real-azw3"
    extraction = subprocess.run(
        [CALIBRE_DEBUG, "--explode-book", str(azw3), str(exploded)],
        capture_output=True,
        text=True,
        check=False,
        timeout=120,
    )
    assert extraction.returncode == 0, extraction.stdout + extraction.stderr
    documents = {
        path.resolve(): etree.parse(
            str(path), etree.XMLParser(recover=False, no_network=True)
        )
        for path in exploded.rglob("*")
        if path.suffix.lower() in {".html", ".xhtml", ".htm"}
    }
    assert documents
    combined = "\n".join(
        "".join(tree.getroot().itertext()) for tree in documents.values()
    )
    assert "达什伍德一家很早以前就定居在苏塞克斯。" in combined
    assert "提示" in combined

    backlinks = 0
    generated_source_ids = {
        anchor.get("id")
        for tree in documents.values()
        for anchor in tree.xpath(
            "//*[local-name()='a'][starts-with(@id, 'epubllmt-src-')]"
        )
    }
    assert generated_source_ids
    for source_path, tree in documents.items():
        for anchor in tree.xpath("//*[local-name()='a'][@href]"):
            href = anchor.get("href", "")
            parsed = urlsplit(href)
            if parsed.scheme or parsed.netloc or not parsed.fragment:
                continue
            target = (
                source_path
                if not parsed.path
                else (source_path.parent / unquote(parsed.path)).resolve()
            )
            assert target in documents, f"missing AZW3 target: {href}"
            matches = documents[target].xpath(
                "//*[@id=$fragment or @name=$fragment]",
                fragment=unquote(parsed.fragment),
            )
            assert len(matches) == 1, f"broken AZW3 fragment: {href}"
            assert unquote(parsed.fragment) not in generated_source_ids
            if "↩" in "".join(anchor.itertext()):
                backlinks += 1
    assert backlinks == 0


def _file_hashes(root: Path) -> dict[str, str]:
    return {
        path.relative_to(root).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in root.rglob("*")
        if path.is_file()
    }
