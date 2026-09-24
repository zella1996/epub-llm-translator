from __future__ import annotations

import hashlib
import os
import shutil
import zipfile
from base64 import b64decode
from pathlib import Path

import pytest

from tests.helpers import build_epub
from translator.epub_normalizer import normalize_epub
from translator.epub_processor import run_epubcheck


@pytest.mark.epubcheck
@pytest.mark.skipif(
    os.environ.get("RUN_EPUBCHECK_TESTS") != "1" or shutil.which("epubcheck") is None,
    reason="set RUN_EPUBCHECK_TESTS=1 on a machine with EPUBCheck",
)
@pytest.mark.parametrize(
    ("case", "expected_rule", "expected_changed"),
    [
        ("invalid_thumbnail_guide", "epub2-drop-invalid-thumbnail-guide", {"OEBPS/content.opf"}),
        ("missing_guide_fragment", "epub2-strip-missing-guide-fragment", {"OEBPS/content.opf"}),
        ("ncx_uid_mismatch", "epub2-sync-ncx-uid", {"OEBPS/toc.ncx"}),
        ("unreachable_cover", "epub3-promote-unreachable-cover", {"OEBPS/content.opf"}),
    ],
)
def test_normalize_repairs_only_the_supported_structure_error(
    tmp_path, case, expected_rule, expected_changed
):
    source = _normalization_fixture(tmp_path / f"{case}.epub", case)
    output = tmp_path / f"{case}-normalized.epub"
    before = _archive_hashes(source)
    assert run_epubcheck(source, required=True, allow_invalid=True).returncode != 0

    result = normalize_epub(source, output)

    assert [change.rule for change in result.changes] == [expected_rule]
    assert run_epubcheck(output, required=True, allow_invalid=False).returncode == 0
    assert _archive_hashes(source) == before
    after = _archive_hashes(output)
    assert set(after) == set(before)
    changed = {name for name, digest in before.items() if after[name] != digest}
    assert changed == expected_changed


@pytest.mark.epubcheck
@pytest.mark.skipif(
    os.environ.get("RUN_EPUBCHECK_TESTS") != "1" or shutil.which("epubcheck") is None,
    reason="set RUN_EPUBCHECK_TESTS=1 on a machine with EPUBCheck",
)
def test_normalize_dry_run_does_not_create_an_output(tmp_path):
    source = _normalization_fixture(tmp_path / "source.epub", "ncx_uid_mismatch")
    output = tmp_path / "normalized.epub"

    result = normalize_epub(source, output, dry_run=True)

    assert [change.rule for change in result.changes] == ["epub2-sync-ncx-uid"]
    assert not output.exists()


def _normalization_fixture(path: Path, case: str) -> Path:
    epub3 = case == "unreachable_cover"
    build_epub(path, version="3.0" if epub3 else "2.0")
    members = _read_archive(path)
    opf_name = "OEBPS/content.opf"
    opf = members[opf_name].decode("utf-8")
    if case == "invalid_thumbnail_guide":
        opf = opf.replace(
            "</manifest>",
            '<item id="cover" href="cover.xhtml" media-type="application/xhtml+xml"/>'
            '<item id="cover-image" href="cover.png" media-type="image/png"/>'
            '<item id="thumb" href="thumb.png" media-type="image/png"/>'
            "</manifest>",
        ).replace(
            "</spine>",
            "</spine><guide>"
            '<reference type="cover" title="Cover" href="cover.xhtml"/>'
            '<reference type="thumbimagestandard" title="thumbnail" href="thumb.png"/>'
            "</guide>",
        )
        members["OEBPS/cover.xhtml"] = _cover_xhtml()
        members["OEBPS/cover.png"] = _minimal_png()
        members["OEBPS/thumb.png"] = _minimal_png()
    elif case == "missing_guide_fragment":
        opf = opf.replace(
            "</spine>",
            "</spine><guide>"
            '<reference type="text" title="Start" href="chapter.xhtml#missing"/>'
            "</guide>",
        )
    elif case == "ncx_uid_mismatch":
        members["OEBPS/toc.ncx"] = members["OEBPS/toc.ncx"].replace(
            b'content="test-book"', b'content="urn:isbn:test-book"'
        )
    elif case == "unreachable_cover":
        opf = opf.replace(
            "</manifest>",
            '<item id="cover" href="cover.xhtml" media-type="application/xhtml+xml"/>'
            '<item id="cover-image" href="cover.png" media-type="image/png" properties="cover-image"/>'
            "</manifest>",
        ).replace(
            "<spine>", '<spine><itemref idref="cover" linear="no"/>'
        ).replace(
            "</spine>",
            "</spine><guide><reference type=" + '"cover"' + " title=" + '"Cover"' + " href=" + '"cover.xhtml"' + "/></guide>",
        )
        members["OEBPS/cover.xhtml"] = _cover_xhtml()
        members["OEBPS/cover.png"] = _minimal_png()
    else:
        raise AssertionError(case)
    members[opf_name] = opf.encode("utf-8")
    _write_archive(path, members)
    return path


def _cover_xhtml() -> bytes:
    return (
        b'<?xml version="1.0" encoding="utf-8"?>'
        b'<html xmlns="http://www.w3.org/1999/xhtml"><head><title>Cover</title></head>'
        b'<body><div><img src="cover.png" alt=""/></div></body></html>'
    )


def _minimal_png() -> bytes:
    return b64decode(
        "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVQIHWP4z8DwHwAFgAI/ScL6dgAAAABJRU5ErkJggg=="
    )


def _read_archive(path: Path) -> dict[str, bytes]:
    with zipfile.ZipFile(path) as archive:
        return {info.filename: archive.read(info.filename) for info in archive.infolist()}


def _write_archive(path: Path, members: dict[str, bytes]) -> None:
    rewritten = path.with_name(f"{path.stem}-rewritten.epub")
    with zipfile.ZipFile(rewritten, "w") as archive:
        info = zipfile.ZipInfo("mimetype")
        info.compress_type = zipfile.ZIP_STORED
        archive.writestr(info, members.pop("mimetype"))
        for name, value in members.items():
            archive.writestr(name, value, compress_type=zipfile.ZIP_DEFLATED)
    rewritten.replace(path)


def _archive_hashes(path: Path) -> dict[str, str]:
    with zipfile.ZipFile(path) as archive:
        return {
            info.filename: hashlib.sha256(archive.read(info.filename)).hexdigest()
            for info in archive.infolist()
            if not info.is_dir()
        }
