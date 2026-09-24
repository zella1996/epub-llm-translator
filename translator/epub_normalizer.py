"""Offline, fail-fast normalization for a small set of unambiguous EPUB defects."""

from __future__ import annotations

import hashlib
import tempfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import unquote, urlsplit

from lxml import etree

from translator.epub_processor import EpubBook, EpubError, SUPPORTED_CONTENT_TYPES, run_epubcheck


@dataclass(frozen=True)
class NormalizationChange:
    rule: str
    path: str
    detail: str


@dataclass(frozen=True)
class NormalizationResult:
    source: Path
    output: Path | None
    changes: tuple[NormalizationChange, ...]
    input_errors: int
    output_errors: int | None


def normalize_epub(
    source: Path,
    output: Path,
    *,
    dry_run: bool = False,
    now: datetime | None = None,
) -> NormalizationResult:
    """Normalize only proven-safe package/navigation defects.

    The source is never modified.  A requested output is published only after it
    has no EPUBCheck errors, preserves every non-target resource byte-for-byte,
    and passes the application's strict reader/link preflight.
    """
    source = source.resolve()
    output = output.resolve()
    _validate_output_target(source, output)
    input_check = run_epubcheck(source, required=True, allow_invalid=True)

    with EpubBook(source) as book:
        before_hashes = _file_hashes(book.root)
        changes, changed_paths = _apply_supported_repairs(book, input_check.output, now=now)
        if dry_run:
            return NormalizationResult(
                source, None, tuple(changes), input_check.returncode, None
            )
        if not changes:
            raise EpubError("没有可安全应用的规范化规则；未创建输出文件")

        output.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix=f".{output.stem}-normalize-", dir=output.parent) as temporary_dir:
            candidate = Path(temporary_dir) / output.name
            book.archive(candidate)
            _validate_normalized_output(
                candidate,
                before_hashes,
                changed_paths,
            )
            candidate.replace(output)

    output_check = run_epubcheck(output, required=True, allow_invalid=False)
    return NormalizationResult(
        source, output, tuple(changes), input_check.returncode, output_check.returncode
    )


def _apply_supported_repairs(
    book: EpubBook, epubcheck_output: str, *, now: datetime | None
) -> tuple[list[NormalizationChange], set[str]]:
    codes = _epubcheck_codes(epubcheck_output)
    changes: list[NormalizationChange] = []
    changed_paths: set[str] = set()

    if "OPF-032" in codes:
        change = _drop_invalid_thumbnail_guide(book)
        if change:
            changes.append(change)
            changed_paths.add(change.path)
    if "RSC-012" in codes:
        change = _strip_missing_guide_fragment(book)
        if change:
            changes.append(change)
            changed_paths.add(change.path)
    if "NCX-001" in codes:
        change = _sync_ncx_uid(book)
        if change:
            changes.append(change)
            changed_paths.add(change.path)
    if "OPF-096" in codes:
        change = _promote_unreachable_cover(book, now=now)
        if change:
            changes.append(change)
            changed_paths.add(change.path)
    return changes, changed_paths


def _drop_invalid_thumbnail_guide(book: EpubBook) -> NormalizationChange | None:
    if book.is_epub3:
        return None
    references = book.opf_tree.xpath(
        "//*[local-name()='guide']/*[local-name()='reference' and @type='thumbimagestandard']"
    )
    if len(references) != 1:
        return None
    reference = references[0]
    target = _manifest_item_for_href(book, reference.get("href", ""))
    valid_cover = book.opf_tree.xpath(
        "//*[local-name()='guide']/*[local-name()='reference' and @type='cover']"
    )
    if (
        target is None
        or not (target.get("media-type") or "").startswith("image/")
        or len(valid_cover) != 1
        or _manifest_item_for_href(book, valid_cover[0].get("href", "")) is None
    ):
        return None
    cover_item = _manifest_item_for_href(book, valid_cover[0].get("href", ""))
    if cover_item is None or cover_item.get("media-type") not in SUPPORTED_CONTENT_TYPES:
        return None
    reference.getparent().remove(reference)
    book._write_tree(book.opf_tree, book.opf_path)
    return NormalizationChange(
        "epub2-drop-invalid-thumbnail-guide",
        _relative(book, book.opf_path),
        "删除指向图片的非标准 EPUB 2 guide thumbnail 引用；标准封面引用保留。",
    )


def _strip_missing_guide_fragment(book: EpubBook) -> NormalizationChange | None:
    if book.is_epub3:
        return None
    references = book.opf_tree.xpath(
        "//*[local-name()='guide']/*[local-name()='reference' and @type='text' and contains(@href, '#')]"
    )
    if len(references) != 1:
        return None
    reference = references[0]
    parsed = urlsplit(reference.get("href", ""))
    if not parsed.path or not parsed.fragment:
        return None
    target = _manifest_item_for_href(book, parsed.path)
    if target is None or target.get("media-type") not in SUPPORTED_CONTENT_TYPES:
        return None
    path = book._resolve(book.opf_dir, parsed.path)
    if not path.is_file() or path not in {item_path for _, _, item_path in book.spine_items}:
        return None
    tree = book._parse_xhtml(path)
    if tree.xpath("//*[@id=$fragment or @name=$fragment]", fragment=unquote(parsed.fragment)):
        return None
    reference.set("href", parsed.path)
    book._write_tree(book.opf_tree, book.opf_path)
    return NormalizationChange(
        "epub2-strip-missing-guide-fragment",
        _relative(book, book.opf_path),
        "移除指向存在起始文档、但不存在锚点的 EPUB 2 guide fragment。",
    )


def _sync_ncx_uid(book: EpubBook) -> NormalizationChange | None:
    if book.is_epub3:
        return None
    unique_id = (book.opf_root.get("unique-identifier") or "").strip()
    if not unique_id:
        return None
    identifiers = book.opf_tree.xpath(
        "//*[local-name()='metadata']/*[local-name()='identifier' and @id=$id]",
        id=unique_id,
    )
    if len(identifiers) != 1 or not (identifiers[0].text or "").strip():
        return None
    spines = book.opf_tree.xpath("/*[local-name()='package']/*[local-name()='spine']")
    if len(spines) != 1:
        return None
    ncx_id = (spines[0].get("toc") or "").strip()
    ncx_item = book.manifest.get(ncx_id)
    if ncx_item is None or ncx_item.get("media-type") != "application/x-dtbncx+xml":
        return None
    ncx_path = book._resolve(book.opf_dir, ncx_item.get("href", ""))
    if not ncx_path.is_file():
        return None
    try:
        ncx_tree = etree.parse(str(ncx_path), book._parser())
    except etree.XMLSyntaxError:
        return None
    uid_nodes = ncx_tree.xpath("//*[local-name()='meta' and @name='dtb:uid']")
    identifier = identifiers[0].text.strip()
    if len(uid_nodes) != 1 or uid_nodes[0].get("content") == identifier:
        return None
    uid_nodes[0].set("content", identifier)
    book._write_tree(ncx_tree, ncx_path)
    return NormalizationChange(
        "epub2-sync-ncx-uid",
        _relative(book, ncx_path),
        "将 NCX dtb:uid 同步为 OPF unique-identifier 指向的唯一值。",
    )


def _promote_unreachable_cover(
    book: EpubBook, *, now: datetime | None
) -> NormalizationChange | None:
    if not book.is_epub3:
        return None
    spines = book.opf_tree.xpath("/*[local-name()='package']/*[local-name()='spine']")
    if len(spines) != 1:
        return None
    itemrefs = spines[0].xpath("./*[local-name()='itemref']")
    if not itemrefs or itemrefs[0].get("linear", "yes").lower() != "no":
        return None
    cover_ref = itemrefs[0]
    if cover_ref.get("idref") != "cover":
        return None
    cover_item = book.manifest.get("cover")
    if cover_item is None or cover_item.get("media-type") not in SUPPORTED_CONTENT_TYPES:
        return None
    cover_path = book._resolve(book.opf_dir, cover_item.get("href", ""))
    if not _is_image_only_cover(book, cover_path):
        return None
    guide_refs = book.opf_tree.xpath(
        "//*[local-name()='guide']/*[local-name()='reference' and @type='cover']"
    )
    if len(guide_refs) != 1 or _manifest_item_for_href(book, guide_refs[0].get("href", "")) is not cover_item:
        return None
    cover_images = [
        item
        for item in book.manifest.values()
        if "cover-image" in set((item.get("properties") or "").split())
        and (item.get("media-type") or "").startswith("image/")
    ]
    if len(cover_images) != 1:
        return None
    modified = book.opf_tree.xpath(
        "//*[local-name()='metadata']/*[local-name()='meta' and @property='dcterms:modified']"
    )
    if len(modified) != 1:
        return None
    cover_ref.set("linear", "yes")
    instant = now or datetime.now(timezone.utc)
    modified[0].text = instant.astimezone(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")
    book._write_tree(book.opf_tree, book.opf_path)
    return NormalizationChange(
        "epub3-promote-unreachable-cover",
        _relative(book, book.opf_path),
        "将首个、仅含封面图片且已有 guide cover 的 EPUB 3 spine 项设为 linear=yes。",
    )


def _is_image_only_cover(book: EpubBook, path: Path) -> bool:
    if not path.is_file():
        return False
    try:
        tree = book._parse_xhtml(path)
    except EpubError:
        return False
    bodies = tree.xpath("//*[local-name()='body']")
    if len(bodies) != 1:
        return False
    body = bodies[0]
    return (
        len(body.xpath(".//*[local-name()='img']")) == 1
        and not body.xpath(".//*[local-name()='a']")
        and not "".join(body.itertext()).strip()
    )


def _manifest_item_for_href(book: EpubBook, href: str) -> etree._Element | None:
    parsed = urlsplit(href)
    if parsed.scheme or parsed.netloc or not parsed.path:
        return None
    try:
        target = book._resolve(book.opf_dir, parsed.path)
    except EpubError:
        return None
    matches = [
        item
        for item in book.manifest.values()
        if book._resolve(book.opf_dir, item.get("href", "")) == target
    ]
    return matches[0] if len(matches) == 1 else None


def _validate_normalized_output(
    candidate: Path,
    before_hashes: dict[str, str],
    changed_paths: set[str],
) -> None:
    output_check = run_epubcheck(candidate, required=True, allow_invalid=True)
    if output_check.returncode != 0:
        raise EpubError(
            "规范化后 EPUBCheck 仍有错误；未发布输出文件:\n" + output_check.output.strip()
        )
    with EpubBook(candidate) as generated:
        generated.paragraphs()
        issues = generated.link_issues()
        after_hashes = _file_hashes(generated.root)
    if issues:
        raise EpubError("规范化后存在内部链接问题；未发布输出文件:\n" + "\n".join(sorted(issues)))
    if set(before_hashes) != set(after_hashes):
        raise EpubError("规范化改变了 EPUB 文件集合；未发布输出文件")
    changed = {name for name, digest in before_hashes.items() if after_hashes[name] != digest}
    if changed != changed_paths:
        raise EpubError(
            "规范化修改了计划外资源；未发布输出文件: " + ", ".join(sorted(changed - changed_paths))
        )


def _file_hashes(root: Path) -> dict[str, str]:
    return {
        path.relative_to(root).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in root.rglob("*")
        if path.is_file()
    }


def _epubcheck_codes(output: str) -> set[str]:
    import re

    return set(re.findall(r"\b(?:ERROR|FATAL)\(([A-Z]+-\d+)\)", output))


def _relative(book: EpubBook, path: Path) -> str:
    return path.resolve().relative_to(book.root).as_posix()


def _validate_output_target(source: Path, output: Path) -> None:
    if output == source:
        raise EpubError("输出路径不能覆盖输入 EPUB")
    if output.exists():
        raise EpubError(f"输出文件已存在: {output}")
