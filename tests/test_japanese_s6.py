"""S6 offline whole-book write-back contracts; no provider is constructed."""

from __future__ import annotations

from pathlib import Path

from scripts.japanese_s6_offline_writeback import FROZEN_MARKER, write_and_audit


def test_frozen_japanese_writeback_preserves_epub3_structure(tmp_path):
    report = write_and_audit(
        Path("tests/fixtures/japanese-epub3.epub"), tmp_path / "out.epub",
        allow_invalid_source=False,
    )
    assert report["offline"] is True
    assert report["marker"] == FROZEN_MARKER
    assert report["paragraphs_written"] == 6
    assert report["passed"] is True
    assert not report["new_epubcheck_findings"]
    assert not report["new_link_issues"]
    assert not report["unexpected_changed_resources"]
    assert all(all(checks.values()) for checks in report["chapter_comparison"].values())


def test_frozen_japanese_writeback_compares_invalid_epub2_against_its_baseline(tmp_path):
    report = write_and_audit(
        Path("tests/fixtures/japanese-epub2.epub"), tmp_path / "out.epub",
        allow_invalid_source=True,
    )
    assert report["passed"] is True
    assert report["source_epubcheck_findings"] == report["output_epubcheck_findings"]
