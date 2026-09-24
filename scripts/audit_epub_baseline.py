"""Emit a reproducible, offline JSON baseline for an EPUB source file."""

from __future__ import annotations

import argparse
import copy
import hashlib
import json
import posixpath
import re
import shutil
import subprocess
import zipfile
from pathlib import Path, PurePosixPath
from urllib.parse import unquote, urlsplit

from lxml import etree

from translator.epub_processor import EpubBook


def _xml(data: bytes) -> etree._Element:
    return etree.fromstring(
        data,
        parser=etree.XMLParser(
            recover=False, resolve_entities=False, no_network=True, huge_tree=False
        ),
    )


def _resolve(base: str, href: str) -> str:
    parsed = urlsplit(href)
    if parsed.scheme or parsed.netloc:
        return ""
    return posixpath.normpath(posixpath.join(posixpath.dirname(base), unquote(parsed.path)))


def _base_text(element: etree._Element) -> str:
    pieces: list[str] = []

    def visit(node: etree._Element) -> None:
        if not isinstance(node.tag, str):
            if node.tail:
                pieces.append(node.tail)
            return
        if etree.QName(node).localname.lower() in {"rt", "rp"}:
            if node.tail:
                pieces.append(node.tail)
            return
        if node.text:
            pieces.append(node.text)
        for child in node:
            visit(child)
        if node.tail:
            pieces.append(node.tail)

    tail = element.tail
    element.tail = None
    try:
        visit(element)
    finally:
        element.tail = tail
    return "".join(pieces)


def _break_delimited_texts(element: etree._Element) -> list[str]:
    """Approximate publisher paragraphs when prose is separated only by ``br``."""
    segments: list[list[str]] = [[]]

    def visit(node: etree._Element) -> None:
        if not isinstance(node.tag, str):
            if node.tail:
                segments[-1].append(node.tail)
            return
        name = etree.QName(node).localname.lower()
        if name in {"rt", "rp"}:
            if node.tail:
                segments[-1].append(node.tail)
            return
        if name == "br":
            segments.append([])
            if node.tail:
                segments[-1].append(node.tail)
            return
        if node.text:
            segments[-1].append(node.text)
        for child in node:
            visit(child)
        if node.tail:
            segments[-1].append(node.tail)

    visit(element)
    return [
        re.sub(r"\s+", " ", "".join(segment)).strip()
        for segment in segments
        if re.sub(r"\s+", " ", "".join(segment)).strip()
    ]


def _current_candidates_after_language_only_bypass(
    root: etree._Element,
) -> tuple[int | None, str | None]:
    """Measure today's block selection after changing only Japanese tags to English.

    This is a diagnostic counter, not a proposed implementation.  It separates the
    package/language rejection from unsupported source block markup without altering
    the source archive.
    """
    diagnostic_root = copy.deepcopy(root)
    xml_lang = "{http://www.w3.org/XML/1998/namespace}lang"
    for element in diagnostic_root.iter():
        if not isinstance(element.tag, str):
            continue
        for attribute in (xml_lang, "lang"):
            value = (element.get(attribute) or "").strip()
            if value.lower() == "ja" or value.lower().startswith("ja-"):
                element.set(attribute, "en")
    book = object.__new__(EpubBook)
    book.note_targets = {}
    try:
        return len(book._candidate_elements(etree.ElementTree(diagnostic_root), None)), None
    except Exception as exc:  # Diagnostic only: preserve the implementation failure.
        return None, f"{type(exc).__name__}: {exc}"


def _epubcheck(path: Path) -> dict[str, object]:
    executable = shutil.which("epubcheck")
    if executable is None:
        return {"available": False}
    completed = subprocess.run(
        [executable, str(path)], capture_output=True, text=True, check=False
    )
    output = "\n".join(filter(None, (completed.stdout, completed.stderr)))
    counts = {
        level.lower(): len(
            re.findall(rf"^\s*{level}(?:\(|:)", output, re.IGNORECASE | re.MULTILINE)
        )
        for level in ("FATAL", "ERROR", "WARNING")
    }
    findings = set()
    for line in output.splitlines():
        if not re.search(r"\b(?:FATAL|ERROR)\b", line, re.IGNORECASE):
            continue
        normalized = line.strip().replace(str(path), "<book>").replace(path.name, "<book>")
        normalized = re.sub(r"\(\d+(?:,\d+)?\)", "(line)", normalized)
        normalized = re.sub(r":\d+(?::\d+)?(?=[:\s])", ":line", normalized)
        findings.add(normalized)
    return {
        "available": True,
        "version": subprocess.run(
            [executable, "--version"], capture_output=True, text=True, check=False
        ).stdout.strip(),
        "returncode": completed.returncode,
        "counts": counts,
        "normalized_error_findings": sorted(findings),
    }


def audit(path: Path) -> dict[str, object]:
    raw = path.read_bytes()
    with zipfile.ZipFile(path) as archive:
        infos = archive.infolist()
        names = [info.filename for info in infos]
        duplicate_names = sorted({name for name in names if names.count(name) > 1})
        corrupt_member = archive.testzip()
        container = _xml(archive.read("META-INF/container.xml"))
        rootfiles = container.xpath("//*[local-name()='rootfile']/@full-path")
        if not rootfiles:
            raise ValueError("container.xml has no rootfile")
        opf_path = str(rootfiles[0])
        opf = _xml(archive.read(opf_path))
        manifest = {
            node.get("id"): node
            for node in opf.xpath("./*[local-name()='manifest']/*[local-name()='item']")
            if node.get("id") and node.get("href")
        }
        spine = opf.xpath("./*[local-name()='spine']")[0]
        chapters = []
        documents: dict[str, etree._Element] = {}
        for index, itemref in enumerate(
            opf.xpath("./*[local-name()='spine']/*[local-name()='itemref']"), 1
        ):
            item = manifest.get(itemref.get("idref"))
            if item is None:
                continue
            href = _resolve(opf_path, item.get("href", ""))
            if item.get("media-type") not in {"application/xhtml+xml", "text/html"}:
                continue
            root = _xml(archive.read(href))
            documents[href] = root
            paragraphs = root.xpath("//*[local-name()='body']//*[local-name()='p']")
            texts = [re.sub(r"\s+", " ", _base_text(p)).strip() for p in paragraphs]
            nonempty = [text for text in texts if text]
            bodies = root.xpath("//*[local-name()='body']")
            break_segments = _break_delimited_texts(bodies[0]) if bodies else []
            candidate_units = nonempty if nonempty else break_segments
            candidate_count, candidate_error = _current_candidates_after_language_only_bypass(root)
            chapters.append(
                {
                    "spine_index": index,
                    "href": href,
                    "linear": (itemref.get("linear") or "yes").lower(),
                    "raw_paragraphs": len(nonempty),
                    "br_delimited_segments": len(break_segments),
                    "candidate_units": len(candidate_units),
                    "current_candidates_after_language_only_bypass": candidate_count,
                    "current_candidates_after_language_only_bypass_error": candidate_error,
                    "base_text_characters": sum(len(text) for text in candidate_units),
                    "ruby": len(root.xpath("//*[local-name()='ruby']")),
                    "rt": len(root.xpath("//*[local-name()='rt']")),
                    "rp": len(root.xpath("//*[local-name()='rp']")),
                }
            )
        for item in manifest.values():
            if item.get("media-type") not in {"application/xhtml+xml", "text/html"}:
                continue
            href = _resolve(opf_path, item.get("href", ""))
            if href in names and href not in documents:
                documents[href] = _xml(archive.read(href))

        link_issues: set[str] = set()
        link_count = 0
        for source, document in documents.items():
            for anchor in document.xpath("//*[local-name()='a'][@href]"):
                href = anchor.get("href", "")
                parsed = urlsplit(href)
                if parsed.scheme or parsed.netloc:
                    continue
                link_count += 1
                target = source if not parsed.path else _resolve(source, href)
                if target not in names:
                    link_issues.add(f"{source}: missing target {href}")
                    continue
                if parsed.fragment:
                    target_doc = documents.get(target)
                    if target_doc is None:
                        try:
                            target_doc = _xml(archive.read(target))
                        except (KeyError, etree.XMLSyntaxError):
                            link_issues.add(f"{source}: unreadable target {href}")
                            continue
                    matches = target_doc.xpath(
                        "//*[@id=$fragment or @name=$fragment]", fragment=unquote(parsed.fragment)
                    )
                    if len(matches) != 1:
                        link_issues.add(f"{source}: fragment matches {len(matches)} for {href}")

        return {
            "path": str(path),
            "sha256": hashlib.sha256(raw).hexdigest(),
            "bytes": len(raw),
            "zip": {
                "entries": len(names),
                "corrupt_member": corrupt_member,
                "duplicate_names": duplicate_names,
                "mimetype_first": bool(names and names[0] == "mimetype"),
                "mimetype_stored": bool(
                    infos and infos[0].compress_type == zipfile.ZIP_STORED
                ),
            },
            "package": {
                "path": opf_path,
                "version": opf.get("version"),
                "languages": [
                    str(value).strip()
                    for value in opf.xpath(
                        "./*[local-name()='metadata']/*[local-name()='language']/text()"
                    )
                    if str(value).strip()
                ],
                "page_progression_direction": spine.get("page-progression-direction"),
                "spine_items": len(opf.xpath("./*[local-name()='spine']/*[local-name()='itemref']")),
            },
            "content_chapters": chapters,
            "content_totals": {
                "chapters": len(chapters),
                "raw_paragraphs": sum(int(chapter["raw_paragraphs"]) for chapter in chapters),
                "candidate_units": sum(int(chapter["candidate_units"]) for chapter in chapters),
                "base_text_characters": sum(
                    int(chapter["base_text_characters"]) for chapter in chapters
                ),
                "ruby": sum(int(chapter["ruby"]) for chapter in chapters),
            },
            "links": {"internal": link_count, "issues": sorted(link_issues)},
            "epubcheck": _epubcheck(path),
        }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("epub", type=Path)
    args = parser.parse_args()
    print(json.dumps(audit(args.epub), ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
