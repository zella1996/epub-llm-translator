"""S6-only frozen-result write-back and structure audit for Japanese EPUBs.

This harness never constructs a provider client and never sends book text over
the network.  Its marker is deliberately not a translation: it proves that the
same complete paragraph set can be safely written back with a frozen result.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import zipfile
from pathlib import Path

from translator.epub_processor import EpubBook, ParagraphNote, run_epubcheck

FROZEN_MARKER = "【S6 离线冻结写回；非真实翻译】"


def _stable_findings(findings: frozenset[str]) -> frozenset[str]:
    """Also remove EPUBCheck's relative input-directory spelling."""
    return frozenset(
        re.sub(r": .*?<book>/", ": <book>/", finding)
        for finding in findings
    )


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _zip_hashes(path: Path) -> dict[str, str]:
    with zipfile.ZipFile(path) as archive:
        return {item.filename: hashlib.sha256(archive.read(item)).hexdigest()
                for item in archive.infolist() if not item.is_dir()}


def _chapter_facts(book: EpubBook) -> dict[int, dict[str, object]]:
    facts: dict[int, dict[str, object]] = {}
    for chapter, _, path in book.content_spines():
        tree = book._parse_xhtml(path)
        ruby = tree.xpath("//*[local-name()='ruby']")
        facts[chapter] = {
            "text": book.chapter_text_snapshot(chapter),
            "structure": book.chapter_structure_snapshot(chapter),
            "ruby_count": len(ruby),
            "ruby_attributes": tuple(sorted(
                tuple(sorted((str(key), str(value)) for key, value in node.attrib.items()))
                for node in ruby
            )),
        }
    return facts


def _package_facts(book: EpubBook) -> dict[str, object]:
    spine_elements = book.opf_tree.xpath("//*[local-name()='spine']")
    spine_element = spine_elements[0] if len(spine_elements) == 1 else None
    overlays = tuple(sorted(
        (item.get("id", ""), item.get("media-overlay", ""))
        for item in book.opf_tree.xpath("//*[local-name()='manifest']/*[local-name()='item']")
        if item.get("media-overlay")
    ))
    spine = tuple(
        (item.get("idref", ""), item.get("linear", ""))
        for item in book.opf_tree.xpath("//*[local-name()='spine']/*[local-name()='itemref']")
    )
    return {
        "opf_hash": _sha256(book.opf_path),
        "spine": spine,
        "page_progression_direction": (
            spine_element.get("page-progression-direction") if spine_element is not None else None
        ),
        "media_overlays": overlays,
    }


def write_and_audit(source: Path, output: Path, *, allow_invalid_source: bool) -> dict[str, object]:
    """Write a complete frozen placeholder result, then compare all S6 invariants."""
    source = source.resolve()
    output = output.resolve()
    source_hashes = _zip_hashes(source)
    source_check = run_epubcheck(source, required=True, allow_invalid=allow_invalid_source)
    with EpubBook(source, reject_generated=True) as book:
        before_chapters = _chapter_facts(book)
        before_package = _package_facts(book)
        baseline_links = book.link_issues()
        refs = book.paragraphs()
        book.apply_notes([(ref, ParagraphNote(FROZEN_MARKER)) for ref in refs])
        book.archive(output)

    output_check = run_epubcheck(output, required=True, allow_invalid=allow_invalid_source)
    output_hashes = _zip_hashes(output)
    with EpubBook(output) as book:
        after_chapters = _chapter_facts(book)
        after_package = _package_facts(book)
        output_links = book.link_issues()

    chapter_comparison = {
        str(chapter): {
            "base_text": before["text"] == after_chapters[chapter]["text"],
            "structure": before["structure"] == after_chapters[chapter]["structure"],
            "ruby": (before["ruby_count"], before["ruby_attributes"])
                    == (after_chapters[chapter]["ruby_count"], after_chapters[chapter]["ruby_attributes"]),
        }
        for chapter, before in before_chapters.items()
    }
    changed_resources = sorted(
        name for name, digest in source_hashes.items() if output_hashes.get(name) != digest
    )
    unexpected_resources = [name for name in changed_resources if not name.lower().endswith((".xhtml", ".html"))]
    package_equal = before_package == after_package
    report = {
        "source": str(source), "output": str(output), "offline": True,
        "marker": FROZEN_MARKER, "paragraphs_written": len(refs),
        "source_epubcheck_findings": sorted(_stable_findings(source_check.findings)),
        "output_epubcheck_findings": sorted(_stable_findings(output_check.findings)),
        "new_epubcheck_findings": sorted(
            _stable_findings(output_check.findings) - _stable_findings(source_check.findings)
        ),
        "new_link_issues": sorted(output_links - baseline_links),
        "chapter_comparison": chapter_comparison,
        "package_equal": package_equal,
        "changed_resources": changed_resources,
        "unexpected_changed_resources": unexpected_resources,
        "non_ascii_paths": sorted(name for name in source_hashes if not name.isascii()),
    }
    report["passed"] = (
        all(all(values.values()) for values in chapter_comparison.values())
        and package_equal and not unexpected_resources
        and not report["new_epubcheck_findings"] and not report["new_link_issues"]
        and set(source_hashes) == set(output_hashes)
    )
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source", type=Path)
    parser.add_argument("output", type=Path)
    parser.add_argument("--allow-invalid-source", action="store_true")
    parser.add_argument("--report", type=Path, required=True)
    args = parser.parse_args()
    report = write_and_audit(args.source, args.output, allow_invalid_source=args.allow_invalid_source)
    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({key: report[key] for key in ("paragraphs_written", "passed", "new_epubcheck_findings", "new_link_issues")}, ensure_ascii=False))
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
