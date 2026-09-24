"""Strict, minimally mutating EPUB container support."""

from __future__ import annotations

import hashlib
import copy
import os
import re
import shutil
import subprocess
import tempfile
import unicodedata
import zipfile
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from urllib.parse import unquote, urlsplit

from lxml import etree

from translator.languages import (
    LanguageConfigurationError,
    normalize_source_language_tag,
    resolve_effective_language_configuration,
)
from translator.sentence_analyzer import short_text_paragraph_kind


CONTAINER_NS = "urn:oasis:names:tc:opendocument:xmlns:container"
EPUB_NS = "http://www.idpf.org/2007/ops"
XHTML_NS = "http://www.w3.org/1999/xhtml"
GENERATED_PREFIX = "epubllmt-"
DEFAULT_ANALYSIS_LINK_TEXT = "›››"
AID_BULLET = "•"
ANALYSES_PER_FILE = 16
SUPPORTED_CONTENT_TYPES = {"application/xhtml+xml", "text/html"}

etree.register_namespace("epub", EPUB_NS)


class EpubError(RuntimeError):
    """The input cannot be processed without risking book integrity."""


def validate_analysis_link_text(value: str) -> str:
    """Keep the source-paragraph link a short, visible text label."""
    if (not isinstance(value, str) or not value or value != value.strip()
            or len(value) > 16 or any(
                unicodedata.category(char) in {"Cc", "Cf", "Cs", "Zl", "Zp"}
                for char in value
            )):
        raise EpubError("解析入口文字必须是 1–16 个可见字符，且不能含首尾空白或控制字符")
    return value


@dataclass(frozen=True)
class ParagraphRef:
    chapter: int
    paragraph: int
    href: str
    text: str
    tag: str
    has_ruby: bool = False


@dataclass(frozen=True)
class LearningCard:
    sentence: str
    difficulty: str
    meaning: str


@dataclass(frozen=True)
class ParagraphNote:
    paragraph_translation: str
    cards: tuple[LearningCard, ...] = ()


@dataclass(frozen=True)
class ReadingSentenceNote:
    index: int
    source: str
    translation: str


@dataclass(frozen=True)
class ReadingAidNote:
    scope: str
    sentence_indices: tuple[int, ...]
    quote: str | None = None
    text: str | None = None
    show_translation: bool = False


@dataclass(frozen=True)
class ReadingAssistanceNote:
    paragraph_translation: str
    sentences: tuple[ReadingSentenceNote, ...]
    aids: tuple[ReadingAidNote, ...]


@dataclass(frozen=True)
class EpubcheckResult:
    ran: bool
    returncode: int
    output: str
    findings: frozenset[str] = frozenset()
    warnings: tuple[str, ...] = ()


def run_epubcheck(path: Path, *, required: bool, allow_invalid: bool) -> EpubcheckResult:
    if not required:
        return EpubcheckResult(False, 0, "epubcheck explicitly disabled", frozenset(), ())
    executable = shutil.which("epubcheck")
    if executable is None:
        raise EpubError(
            "未找到 epubcheck。安装 EPUBCheck，或仅在开发/诊断时显式使用 --skip-epubcheck。"
        )
    completed = subprocess.run(
        [executable, str(path)],
        capture_output=True,
        text=True,
        check=False,
    )
    output = "\n".join(part for part in (completed.stdout, completed.stderr) if part)
    if completed.returncode != 0 and not allow_invalid:
        raise EpubError(f"EPUBCheck 拒绝该文件:\n{output.strip()}")
    return EpubcheckResult(
        True,
        completed.returncode,
        output,
        _normalize_epubcheck_findings(output, path),
        tuple(
            line.strip()
            for line in output.splitlines()
            if re.search(r"\bWARNING\b", line, re.IGNORECASE)
        ),
    )


def _normalize_epubcheck_findings(output: str, path: Path) -> frozenset[str]:
    """Compare findings without unstable archive paths or line/column numbers."""
    findings: set[str] = set()
    archive_names = {str(path), str(path.resolve()), path.name}
    for line in output.splitlines():
        if not re.search(r"\b(?:FATAL|ERROR)\b", line, re.IGNORECASE):
            continue
        normalized = line.strip()
        for archive_name in sorted(archive_names, key=len, reverse=True):
            normalized = normalized.replace(archive_name, "<book>")
        normalized = re.sub(r"\(\d+(?:,\d+)?\)", "(line)", normalized)
        normalized = re.sub(r":\d+(?::\d+)?(?=[:\s])", ":line", normalized)
        findings.add(normalized)
    return frozenset(findings)


class EpubBook:
    """Extract an EPUB safely, inspect its spine, and write a new artifact."""

    def __init__(
        self, source: Path, *, reject_generated: bool = False,
        analysis_link_text: str = DEFAULT_ANALYSIS_LINK_TEXT,
    ) -> None:
        self.source = source.resolve()
        self.reject_generated = reject_generated
        self.analysis_link_text = validate_analysis_link_text(analysis_link_text)
        if not self.source.is_file():
            raise EpubError(f"输入文件不存在: {source}")
        self._temporary = tempfile.TemporaryDirectory(prefix="epubllmt-")
        self.root = Path(self._temporary.name).resolve()
        try:
            self._safe_extract()
            self._load_package()
            self._preflight()
            self.note_targets = self._discover_note_targets()
        except Exception:
            self.close()
            raise

    def __enter__(self) -> "EpubBook":
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    def close(self) -> None:
        temporary = getattr(self, "_temporary", None)
        if temporary is not None:
            temporary.cleanup()
            self._temporary = None

    def metadata_values(self, name: str) -> tuple[str, ...]:
        """Return non-empty Dublin Core values without guessing their meaning."""
        values = self.opf_root.xpath(
            "./*[local-name()='metadata']/*[local-name()=$name]/text()",
            name=name,
        )
        return tuple(str(value).strip() for value in values if str(value).strip())

    @staticmethod
    def _parser() -> etree.XMLParser:
        return etree.XMLParser(
            recover=False,
            resolve_entities=False,
            no_network=True,
            remove_blank_text=False,
            huge_tree=False,
        )

    def _safe_extract(self) -> None:
        try:
            archive = zipfile.ZipFile(self.source)
        except (OSError, zipfile.BadZipFile) as exc:
            raise EpubError(f"无法打开 EPUB ZIP 容器: {exc}") from exc
        with archive:
            seen: set[str] = set()
            for info in archive.infolist():
                normalized = info.filename.replace("\\", "/")
                member = PurePosixPath(normalized)
                if (
                    not normalized
                    or member.is_absolute()
                    or ".." in member.parts
                    or normalized in seen
                ):
                    raise EpubError(f"EPUB 包含不安全或重复路径: {info.filename}")
                seen.add(normalized)
                file_type = (info.external_attr >> 16) & 0o170000
                if file_type == 0o120000:
                    raise EpubError(f"EPUB 包含符号链接: {info.filename}")
                destination = (self.root / Path(*member.parts)).resolve()
                if self.root not in destination.parents and destination != self.root:
                    raise EpubError(f"EPUB 路径逃逸: {info.filename}")
                if info.is_dir():
                    destination.mkdir(parents=True, exist_ok=True)
                    continue
                destination.parent.mkdir(parents=True, exist_ok=True)
                with archive.open(info) as source_file, destination.open("wb") as target:
                    shutil.copyfileobj(source_file, target)

    def _load_package(self) -> None:
        mimetype = self.root / "mimetype"
        if not mimetype.is_file() or mimetype.read_bytes() != b"application/epub+zip":
            raise EpubError("mimetype 缺失或内容不正确")
        container = self.root / "META-INF" / "container.xml"
        if not container.is_file():
            raise EpubError("缺少 META-INF/container.xml")
        try:
            container_tree = etree.parse(str(container), self._parser())
        except etree.XMLSyntaxError as exc:
            raise EpubError(f"container.xml 无法严格解析: {exc}") from exc
        rootfiles = container_tree.xpath(
            "//c:rootfile", namespaces={"c": CONTAINER_NS}
        )
        if not rootfiles:
            raise EpubError("container.xml 没有 rootfile")
        package_name = rootfiles[0].get("full-path", "")
        self.opf_path = self._resolve(self.root, package_name)
        if not self.opf_path.is_file():
            raise EpubError(f"找不到 package document: {package_name}")
        try:
            self.opf_tree = etree.parse(str(self.opf_path), self._parser())
        except etree.XMLSyntaxError as exc:
            raise EpubError(f"package document 无法严格解析: {exc}") from exc
        self.opf_root = self.opf_tree.getroot()
        self.version = (self.opf_root.get("version") or "2").strip()
        self.is_epub3 = self.version.startswith("3")
        self.opf_dir = self.opf_path.parent

        languages = [
            str(value).strip()
            for value in self.opf_root.xpath(
                "./*[local-name()='metadata']/*[local-name()='language']/text()"
            )
            if str(value).strip()
        ]
        try:
            self.effective_language_configuration = (
                resolve_effective_language_configuration(languages)
            )
        except LanguageConfigurationError as exc:
            raise EpubError(str(exc)) from exc
        self.source_language = (
            self.effective_language_configuration.source.source_code
        )
        self.original_languages = (
            self.effective_language_configuration.original_languages
        )

        manifest_nodes = self.opf_root.xpath("./*[local-name()='manifest']/*[local-name()='item']")
        self.manifest = {
            node.get("id"): node
            for node in manifest_nodes
            if node.get("id") and node.get("href")
        }
        spine_nodes = self.opf_root.xpath("./*[local-name()='spine']/*[local-name()='itemref']")
        self.spine_items: list[tuple[str, etree._Element, Path]] = []
        self.non_linear_ids: set[str] = set()
        for itemref in spine_nodes:
            idref = itemref.get("idref")
            item = self.manifest.get(idref)
            if item is None:
                raise EpubError(f"spine 引用了不存在的 manifest id: {idref}")
            if (itemref.get("linear") or "yes").lower() == "no":
                self.non_linear_ids.add(idref)
            href = item.get("href", "")
            path = self._resolve(self.opf_dir, href)
            self.spine_items.append((href, item, path))
        if not self.spine_items:
            raise EpubError("EPUB spine 为空")

    def _resolve(self, base: Path, href: str) -> Path:
        parsed = urlsplit(href)
        if parsed.scheme or parsed.netloc:
            raise EpubError(f"不支持外部 package 路径: {href}")
        relative = PurePosixPath(unquote(parsed.path))
        if relative.is_absolute():
            raise EpubError(f"EPUB 内部路径不安全: {href}")
        resolved = (base / Path(*relative.parts)).resolve()
        if self.root not in resolved.parents and resolved != self.root:
            raise EpubError(f"EPUB 内部路径逃逸: {href}")
        return resolved

    def _preflight(self) -> None:
        if (self.root / "META-INF" / "encryption.xml").exists():
            raise EpubError("检测到加密资源；当前版本拒绝处理")
        metadata = self.opf_root.xpath("./*[local-name()='metadata']")
        if metadata:
            for meta in metadata[0].xpath("./*[local-name()='meta']"):
                key = (meta.get("property") or meta.get("name") or "").lower()
                value = (meta.text or meta.get("content") or "").lower()
                if key == "rendition:layout" and value == "pre-paginated":
                    raise EpubError("当前版本不支持固定版式 EPUB")
        for item in self.manifest.values():
            href = item.get("href", "")
            # Media Overlay resources are preserved verbatim by extraction.
            # S6 owns write-back compatibility validation for them.
            if "scripted" in set((item.get("properties") or "").split()):
                raise EpubError(f"当前版本不支持脚本内容: {href}")
        for href, item, path in self.spine_items:
            if item.get("media-type") not in SUPPORTED_CONTENT_TYPES:
                continue
            if not path.is_file():
                raise EpubError(f"spine 文件不存在: {href}")
            tree = self._parse_xhtml(path)
            if tree.xpath("//*[local-name()='script']"):
                raise EpubError(f"当前版本不支持含脚本的章节: {href}")
            generated_ids = tree.xpath(
                "//*[@id and starts-with(@id, 'epubllmt-')]"
            )
            generated_classes = any(
                token.startswith(GENERATED_PREFIX)
                for value in tree.xpath("//@class")
                for token in str(value).split()
            )
            if self.reject_generated and (generated_ids or generated_classes):
                raise EpubError(f"检测到已生成的学习注释，拒绝重复处理: {href}")

    def _discover_note_targets(self) -> dict[Path, set[str]]:
        """Map author-note destinations without relying on publisher class names."""
        targets: dict[Path, set[str]] = {}
        seen_paths: set[Path] = set()
        for _, item, path in self.spine_items:
            if item.get("media-type") not in SUPPORTED_CONTENT_TYPES or path in seen_paths:
                continue
            seen_paths.add(path)
            tree = self._parse_xhtml(path)
            for anchor in tree.xpath("//*[local-name()='a'][@href]"):
                if not self._is_note_reference(anchor):
                    continue
                parsed = urlsplit(anchor.get("href", ""))
                if parsed.scheme or parsed.netloc or not parsed.fragment:
                    continue
                target_path = (
                    path if not parsed.path else self._resolve(path.parent, parsed.path)
                )
                targets.setdefault(target_path, set()).add(unquote(parsed.fragment))
        return targets

    def _is_note_reference(self, anchor: etree._Element) -> bool:
        current: etree._Element | None = anchor
        while current is not None:
            if self._local_name(current) == "sup":
                return True
            tokens = self._semantic_tokens(current)
            if "noteref" in tokens or "doc-noteref" in tokens:
                return True
            if any(
                token in {"footnote-ref", "footnote-reference", "note-ref"}
                or token.endswith("-noteref")
                for token in tokens
            ):
                return True
            current = current.getparent()
        return False

    def _parse_xhtml(self, path: Path) -> etree._ElementTree:
        try:
            return etree.parse(str(path), self._parser())
        except etree.XMLSyntaxError as exc:
            relative = path.relative_to(self.root)
            raise EpubError(f"正文无法严格解析 ({relative}): {exc}") from exc

    def content_spines(self) -> list[tuple[int, str, Path]]:
        result = []
        chapter = 0
        for href, item, path in self.spine_items:
            if item.get("id") in self.non_linear_ids:
                continue
            if item.get("media-type") not in SUPPORTED_CONTENT_TYPES:
                continue
            if "nav" in set((item.get("properties") or "").split()):
                continue
            chapter += 1
            result.append((chapter, href, path))
        return result

    def paragraphs(self) -> list[ParagraphRef]:
        refs: list[ParagraphRef] = []
        for chapter, href, path in self.content_spines():
            tree = self._parse_xhtml(path)
            for paragraph, (_, element) in enumerate(
                self._candidate_elements(tree, path), 1
            ):
                refs.append(
                    ParagraphRef(
                        chapter=chapter,
                        paragraph=paragraph,
                        href=href,
                        text=self._text_without_generated(element).strip(),
                        tag=self._local_name(element),
                        has_ruby=bool(element.xpath(".//*[local-name()='ruby']")),
                    )
                )
        return refs

    def paragraph(self, chapter: int, paragraph: int) -> ParagraphRef:
        for ref in self.paragraphs():
            if ref.chapter == chapter and ref.paragraph == paragraph:
                return ref
        raise EpubError(f"找不到正文段落 chapter={chapter}, paragraph={paragraph}")

    def _candidate_elements(
        self, tree: etree._ElementTree, path: Path | None = None
    ) -> list[tuple[int, etree._Element]]:
        body = tree.xpath("//*[local-name()='body']")
        if not body:
            raise EpubError("正文 XHTML 缺少 body")
        body_tokens = self._semantic_tokens(body[0])
        if body_tokens & {
            "frontmatter",
            "backmatter",
            "titlepage",
            "copyright-page",
            "toc",
            "index",
        }:
            return []
        result: list[tuple[int, etree._Element]] = []
        for element in body[0].iter():
            if not isinstance(element.tag, str):
                continue
            name = self._local_name(element)
            if (
                name not in {"p", "li", "blockquote"}
                or self._inside_note(element)
                or self._inside_excluded_content(element)
                or self._contains_note_target(element, path)
            ):
                continue
            if name == "li" and element.xpath(".//*[local-name()='p']"):
                continue
            if name == "blockquote" and element.xpath(
                ".//*[local-name()='p' or local-name()='li']"
            ):
                continue
            text = self._text_without_generated(element).strip()
            if text:
                result.append((len(result) + 1, element))
        # Some Japanese EPUBs represent each displayed line as text between
        # ``br`` elements rather than as ``p`` elements.  Detached wrappers
        # deliberately make this extraction-only until S6 supplies a safe
        # write-back locator for a text range.
        for element in body[0].iter():
            if not isinstance(element.tag, str):
                continue
            if self._local_name(element) in {"p", "li", "blockquote"}:
                continue
            if self._inside_note(element) or self._inside_excluded_content(element):
                continue
            for segment in self._br_segments(element):
                if self._text_without_generated(segment).strip():
                    result.append((len(result) + 1, segment))
        return result

    def _inside_excluded_content(self, element: etree._Element) -> bool:
        current: etree._Element | None = element
        nearest_language_found = False
        excluded_tokens = {
            "caption",
            "figcaption",
            "poem",
            "poetry",
            "stanza",
            "verse",
            "verses",
            "line",
            "lines",
        }
        while current is not None:
            if self._local_name(current) in {
                "figure",
                "figcaption",
                "table",
                "pre",
            }:
                return True
            if self._semantic_tokens(current) & excluded_tokens:
                return True
            if not nearest_language_found:
                language = (
                    current.get("{http://www.w3.org/XML/1998/namespace}lang")
                    or current.get("lang")
                )
                if language:
                    nearest_language_found = True
                    if not self._is_book_language(language):
                        return True
            current = current.getparent()
        return False

    def _contains_note_target(self, element: etree._Element, path: Path | None) -> bool:
        if path is None:
            return False
        target_ids = self.note_targets.get(path, set())
        if not target_ids:
            return False
        current: etree._Element | None = element
        while current is not None:
            if current.get("id") in target_ids:
                return True
            current = current.getparent()
        descendant_ids = set(element.xpath(".//*[@id]/@id"))
        return bool(target_ids & descendant_ids)

    @staticmethod
    def _local_name(element: etree._Element) -> str:
        """Return an element name while safely ignoring comments/PIs."""
        if not isinstance(element.tag, str):
            return ""
        return etree.QName(element).localname.lower()

    def _is_book_language(self, value: str) -> bool:
        try:
            return normalize_source_language_tag(value) == self.source_language
        except LanguageConfigurationError:
            return False

    def _inside_note(self, element: etree._Element) -> bool:
        current: etree._Element | None = element
        excluded_words = {
            "footnote",
            "footnotes",
            "endnote",
            "endnotes",
            "doc-footnote",
            "header",
            "footer",
            "pgheader",
            "pg-boilerplate",
            "frontmatter",
            "backmatter",
            "titlepage",
            "copyright-page",
            "toc",
            "index",
        }
        while current is not None:
            if self._local_name(current) in {"aside", "nav", "header", "footer"}:
                return True
            epub_type = current.get(f"{{{EPUB_NS}}}type", "")
            role = current.get("role", "")
            classes = current.get("class", "")
            identifier = current.get("id", "")
            tokens = set((epub_type + " " + role + " " + classes).lower().split())
            if tokens & excluded_words or identifier.startswith(GENERATED_PREFIX):
                return True
            if any(token.startswith(GENERATED_PREFIX) for token in tokens):
                return True
            current = current.getparent()
        return False

    @staticmethod
    def _semantic_tokens(element: etree._Element) -> set[str]:
        if not isinstance(element.tag, str):
            return set()
        values = " ".join(
            (
                element.get(f"{{{EPUB_NS}}}type", ""),
                element.get("role", ""),
                element.get("class", ""),
            )
        )
        return set(values.lower().split())

    def _text_without_generated(self, element: etree._Element) -> str:
        pieces: list[str] = []

        def visit(node: etree._Element, inherited_language: bool = True) -> None:
            if not isinstance(node.tag, str):
                if node.tail:
                    pieces.append(node.tail)
                return
            declared_language = (
                node.get("{http://www.w3.org/XML/1998/namespace}lang")
                or node.get("lang")
            )
            visible_language = (
                self._is_book_language(declared_language)
                if declared_language
                else inherited_language
            )
            classes = set((node.get("class") or "").split())
            if f"{GENERATED_PREFIX}phrase-ref" in classes:
                if node.text:
                    pieces.append(node.text)
                for child in node:
                    visit(child, visible_language)
                if node.tail:
                    pieces.append(node.tail)
                return
            if node.get("id", "").startswith(GENERATED_PREFIX) or any(
                value.startswith(GENERATED_PREFIX) for value in classes
            ):
                if node.tail:
                    pieces.append(node.tail)
                return
            if self._local_name(node) in {"rt", "rp"}:
                if node.tail:
                    pieces.append(node.tail)
                return
            if not visible_language:
                for child in node:
                    visit(child, False)
                if node.tail:
                    pieces.append(node.tail)
                return
            if (
                self._local_name(node) == "sup"
                and node.xpath(".//*[local-name()='a'][@href]")
            ):
                if node.tail:
                    pieces.append(node.tail)
                return
            if "noteref" in (node.get(f"{{{EPUB_NS}}}type", "")).split():
                if node.tail:
                    pieces.append(node.tail)
                return
            if node.text:
                pieces.append(node.text)
            for child in node:
                visit(child, visible_language)
            if node.tail:
                pieces.append(node.tail)

        # The root element's tail is not part of its content.
        tail = element.tail
        element.tail = None
        try:
            visit(element)
        finally:
            element.tail = tail
        return "".join(pieces)

    def _br_segments(self, element: etree._Element) -> list[etree._Element]:
        """Return model-text wrappers for direct ``br``-delimited prose.

        The original tree is never changed.  Only inline children are copied;
        nested block content is its own structural unit and cannot leak into a
        neighbouring line merely because the surrounding container uses br.
        """
        children = list(element)
        if not any(self._local_name(child) == "br" for child in children):
            return []
        block_names = {
            "address", "article", "aside", "blockquote", "div", "dl", "figure",
            "footer", "form", "h1", "h2", "h3", "h4", "h5", "h6", "header",
            "li", "main", "nav", "ol", "p", "pre", "section", "table", "ul",
        }
        namespace = etree.QName(element).namespace if isinstance(element.tag, str) else None
        segments: list[etree._Element] = []
        wrapper: etree._Element | None = None

        parent_path = element.getroottree().getpath(element)

        def start(text: str | None = None) -> etree._Element:
            node = etree.Element(self._qname(namespace, "span"))
            node.set("data-epubllmt-virtual-br-segment", "true")
            # These wrappers are deliberately detached.  Preserve an exact,
            # structural range locator so write-back never has to search for
            # duplicated Japanese prose in the chapter.
            node.set("data-epubllmt-virtual-br-parent", parent_path)
            node.set("{http://www.w3.org/XML/1998/namespace}lang", self.source_language)
            node.text = text
            return node

        wrapper = start(element.text)
        contains_nested_block = False
        for child_index, child in enumerate(children):
            if self._local_name(child) == "br":
                if not contains_nested_block and self._text_without_generated(wrapper).strip():
                    wrapper.set("data-epubllmt-virtual-br-end", str(child_index))
                    segments.append(wrapper)
                wrapper = start(child.tail)
                contains_nested_block = False
                continue
            if self._local_name(child) in block_names:
                contains_nested_block = True
                wrapper.text = (wrapper.text or "") + (child.tail or "")
                continue
            copied = copy.deepcopy(child)
            wrapper.append(copied)
        if not contains_nested_block and self._text_without_generated(wrapper).strip():
            # An empty end means the range extends to the parent's end.
            wrapper.set("data-epubllmt-virtual-br-end", "")
            segments.append(wrapper)
        return segments

    def chapter_text_snapshot(self, chapter: int) -> tuple[str, ...]:
        _, _, path = self._chapter(chapter)
        tree = self._parse_xhtml(path)
        return tuple(
            self._text_without_generated(element).strip()
            for _, element in self._candidate_elements(tree, path)
        )

    def chapter_structure_snapshot(self, chapter: int) -> bytes:
        """Canonical XHTML after removing only nodes generated by this tool."""
        _, _, path = self._chapter(chapter)
        tree = self._parse_xhtml(path)
        root = copy.deepcopy(tree.getroot())
        generated = root.xpath(
            "//*[@id and starts-with(@id, $prefix)]"
            " | //*[@class and contains(concat(' ', normalize-space(@class), ' '), $token)]"
            " | //*[@class and contains(concat(' ', normalize-space(@class), ' '), $phrase)]",
            prefix=GENERATED_PREFIX,
            token=f" {GENERATED_PREFIX}noteref ",
            phrase=f" {GENERATED_PREFIX}phrase-ref ",
        )
        for node in reversed(generated):
            parent = node.getparent()
            if parent is None:
                continue
            inline_text = (
                node.text or ""
                if f"{GENERATED_PREFIX}phrase-ref" in set((node.get("class") or "").split())
                else ""
            )
            content = inline_text + (node.tail or "")
            if content:
                previous = node.getprevious()
                if previous is not None:
                    previous.tail = (previous.tail or "") + content
                else:
                    parent.text = (parent.text or "") + content
            parent.remove(node)
        return etree.tostring(root, method="c14n", with_comments=True)

    def apply_note(self, ref: ParagraphRef, note: ParagraphNote) -> None:
        tree, path, namespace, analysis, _, _ = self._start_analysis(ref, "解析")
        self._append_note(analysis, namespace, note)
        self._write_tree(tree, path)

    def apply_notes(self, notes: list[tuple[ParagraphRef, ParagraphNote]]) -> None:
        """Apply notes chapter-by-chapter without changing reference semantics.

        This is primarily important for complete-book offline replay: each
        reference is still revalidated against the same parsed chapter, while
        avoiding a fresh XML parse and disk write for every paragraph.
        """
        by_chapter: dict[int, list[tuple[ParagraphRef, ParagraphNote]]] = {}
        for ref, note in notes:
            by_chapter.setdefault(ref.chapter, []).append((ref, note))
        for chapter, chapter_notes in by_chapter.items():
            _, _, path = self._chapter(chapter)
            tree = self._parse_xhtml(path)
            candidates = self._candidate_elements(tree, path)
            for ref, note in chapter_notes:
                _, _, namespace, analysis, _, _ = self._start_analysis(
                    ref, "解析", tree=tree, path=path, candidates=candidates
                )
                self._append_note(analysis, namespace, note)
            self._write_tree(tree, path)

    def _append_note(
        self, analysis: etree._Element, namespace: str | None, note: ParagraphNote
    ) -> None:
        translation = etree.SubElement(analysis, self._qname(namespace, "p"))
        translation.set("class", f"{GENERATED_PREFIX}translation")
        translation.set(
            "style", "display:block;text-indent:0;margin:0.8em 0 1.2em;"
            "margin-block:0.8em 1.2em"
        )
        translation_label = etree.SubElement(
            translation, self._qname(namespace, "strong")
        )
        translation_label.text = "段译："
        translation_label.tail = note.paragraph_translation
        for card in note.cards:
            card_div = etree.SubElement(analysis, self._qname(namespace, "div"))
            card_div.set("class", f"{GENERATED_PREFIX}card")
            card_div.set(
                "style",
                "display:block;margin:1em",
            )
            self._add_text_line(card_div, namespace, "原句", card.sentence)
            self._add_text_line(card_div, namespace, "句意", card.meaning)

    def apply_reading_assistance(
        self, ref: ParagraphRef, note: ReadingAssistanceNote, *,
        layout: str = "translation-first",
        inline_phrases: bool = False,
        short_sentence_words: int = 0,
        paragraph_aids: bool = False,
    ) -> None:
        """Render P1's shared translations and aids without using legacy cards."""
        sentence_by_index = {sentence.index: sentence for sentence in note.sentences}
        if not note.paragraph_translation or len(sentence_by_index) != len(note.sentences):
            raise EpubError("阅读辅助结果格式无效")
        if layout not in {"translation-first", "aids-first"}:
            raise EpubError("未知阅读辅助布局")
        if short_sentence_words < 0:
            raise EpubError("阅读辅助短句跳过词数不能为负数")
        for aid in note.aids:
            if aid.scope not in {"phrase", "sentence", "paragraph"}:
                raise EpubError("阅读辅助 scope 无效")
            if not aid.sentence_indices or any(index not in sentence_by_index for index in aid.sentence_indices):
                raise EpubError("阅读辅助引用了不存在的句子")

        tree, path, namespace, analysis, source_element, virtual_source = self._start_analysis(ref, "")
        excluded_sentence_indices = {
            sentence.index
            for sentence in note.sentences
            if short_text_paragraph_kind(
                sentence.source, max_words=short_sentence_words
            ) is not None
        }

        def render_translation() -> None:
            translation = etree.SubElement(analysis, self._qname(namespace, "p"))
            translation.set("class", f"{GENERATED_PREFIX}translation")
            translation.set("style", "display:block;text-indent:0;margin:0.8em 0 1.2em;"
                            "margin-block:0.8em 1.2em")
            label = etree.SubElement(translation, self._qname(namespace, "strong"))
            label.text = "段译："
            label.tail = note.paragraph_translation

        def render_aids() -> None:
            indexed_aids = tuple(enumerate((
                aid
                for aid in note.aids
                if (
                    (paragraph_aids or aid.scope != "paragraph")
                    and (
                        aid.scope == "paragraph"
                        or not any(
                            index in excluded_sentence_indices
                            for index in aid.sentence_indices
                        )
                    )
                )
            ), 1))
            local_by_sentence: dict[int, list[tuple[int, ReadingAidNote]]] = {}
            for position, aid in indexed_aids:
                if aid.scope != "paragraph":
                    local_by_sentence.setdefault(aid.sentence_indices[0], []).append((position, aid))

            for sentence in note.sentences:
                local_aids = local_by_sentence.get(sentence.index)
                if not local_aids:
                    continue
                card = etree.SubElement(analysis, self._qname(namespace, "div"))
                card.set("class", f"{GENERATED_PREFIX}reading-sentence-card")
                card.set("style", "display:block;margin:1em")
                self._add_text_line(card, namespace, "原句", sentence.source)
                self._add_text_line(card, namespace, "句译", sentence.translation)
                for position, aid in local_aids:
                    if aid.scope == "sentence" and not aid.text:
                        continue  # The shared sentence translation is already visible on the card.
                    aid_div = etree.SubElement(card, self._qname(namespace, "div"))
                    aid_div.set(
                        "class",
                        f"{GENERATED_PREFIX}reading-aid {GENERATED_PREFIX}reading-{aid.scope}",
                    )
                    aid_div.set("style", "display:block;margin:0.7em 0 0")
                    if aid.scope == "phrase":
                        if inline_phrases and not virtual_source:
                            aid_id = f"{analysis.get('id')}-phrase-{position}"
                            aid_div.set("id", aid_id)
                            self._link_unique_text(
                                source_element, aid.quote or "", f"#{aid_id}", namespace
                            )
                        quote = etree.SubElement(
                            aid_div, self._qname(namespace, "strong")
                        )
                        quote.set("style", "white-space:nowrap")
                        quote.text = f"{AID_BULLET}{aid.quote or ''}"
                        quote.tail = f"：{aid.text}" if aid.text else ""
                    elif aid.text:
                        aid_div.text = f"{AID_BULLET}{aid.text}"

            for _, aid in indexed_aids:
                if aid.scope != "paragraph":
                    continue
                aid_div = etree.SubElement(analysis, self._qname(namespace, "div"))
                aid_div.set(
                    "class",
                    f"{GENERATED_PREFIX}reading-aid {GENERATED_PREFIX}reading-paragraph",
                )
                aid_div.set(
                    "style",
                    "display:block;margin:1em",
                )
                for index in aid.sentence_indices:
                    self._add_text_line(
                        aid_div, namespace, "原句", sentence_by_index[index].source
                    )
                if aid.text:
                    explanation = etree.SubElement(
                        aid_div, self._qname(namespace, "div")
                    )
                    explanation.set("style", "display:block;margin:0.7em 0 0")
                    explanation.text = f"{AID_BULLET}{aid.text}"

        if layout == "translation-first":
            render_translation()
            render_aids()
        else:
            render_aids()
            render_translation()
        self._write_tree(tree, path)

    def _link_unique_text(
        self, element: etree._Element, quote: str, href: str, namespace: str | None
    ) -> bool:
        """Wrap one model-visible, node-local phrase; never guess across markup.

        In particular, ruby pronunciation nodes are not model text.  A phrase
        spanning ruby/inline nodes is intentionally left as the sentence-card
        aid rather than changing the source DOM on a guess.
        """
        matches: list[tuple[etree._Element, str]] = []

        def visit(node: etree._Element, allow_tail: bool = True) -> None:
            if not isinstance(node.tag, str):
                return
            name = self._local_name(node)
            classes = set((node.get("class") or "").split())
            hidden = (
                name in {"rt", "rp", "a"}
                or node.get("id", "").startswith(GENERATED_PREFIX)
                or any(value.startswith(GENERATED_PREFIX) for value in classes)
            )
            if not hidden:
                if node.text and node.text.count(quote):
                    matches.extend((node, "text") for _ in range(node.text.count(quote)))
                for child in node:
                    visit(child)
            if allow_tail and node.tail and node.tail.count(quote) and node.getparent() is not None:
                matches.extend((node, "tail") for _ in range(node.tail.count(quote)))

        visit(element, allow_tail=False)
        if len(matches) != 1:
            return False
        node, field = matches[0]
        value = getattr(node, field)
        before, after = value.split(quote, 1)
        anchor = etree.Element(self._qname(namespace, "a"))
        anchor.set("href", href)
        anchor.set("class", f"{GENERATED_PREFIX}phrase-ref")
        anchor.text = quote
        anchor.tail = after
        if field == "text":
            node.text = before
            node.insert(0, anchor)
        else:
            node.tail = before
            parent = node.getparent()
            parent.insert(parent.index(node) + 1, anchor)
        return True

    def _start_analysis(
        self, ref: ParagraphRef, title: str, *,
        tree: etree._ElementTree | None = None, path: Path | None = None,
        candidates: list[tuple[int, etree._Element]] | None = None,
    ) -> tuple[etree._ElementTree, Path, str | None, etree._Element, etree._Element, bool]:
        _, href, chapter_path = self._chapter(ref.chapter)
        if path is None:
            path = chapter_path
        elif path != chapter_path:
            raise EpubError("段落引用与章节路径不匹配")
        if href != ref.href:
            raise EpubError("段落引用与章节不匹配")
        if tree is None:
            tree = self._parse_xhtml(path)
        if candidates is None:
            candidates = self._candidate_elements(tree, path)
        if ref.paragraph < 1 or ref.paragraph > len(candidates):
            raise EpubError("预览段落在修改前已发生变化")
        element = candidates[ref.paragraph - 1][1]
        current_text = self._text_without_generated(element).strip()
        if current_text != ref.text:
            raise EpubError("预览段落文本在模型调用期间发生变化")

        existing_ids = {
            value
            for value in tree.xpath("//@id")
            if isinstance(value, str)
        }
        digest = hashlib.sha256(
            f"{ref.href}\0{ref.paragraph}\0{ref.text}".encode("utf-8")
        ).hexdigest()[:12]
        source_id = self._unique_id(f"{GENERATED_PREFIX}src-{digest}", existing_ids)
        existing_ids.add(source_id)
        analysis_id = self._unique_id(
            f"{GENERATED_PREFIX}analysis-{digest}", existing_ids
        )

        virtual_source = element.get("data-epubllmt-virtual-br-segment") == "true"
        source_element = element
        if virtual_source:
            parent_path = element.get("data-epubllmt-virtual-br-parent")
            end = element.get("data-epubllmt-virtual-br-end")
            parents = tree.xpath(parent_path) if parent_path else []
            if len(parents) != 1 or not isinstance(parents[0].tag, str):
                raise EpubError("虚拟 <br> 段的范围定位已失效")
            source_element = parents[0]
            if end not in (None, ""):
                try:
                    end_node = source_element[int(end)]
                except (IndexError, ValueError) as exc:
                    raise EpubError("虚拟 <br> 段的结束范围已失效") from exc
                if self._local_name(end_node) != "br":
                    raise EpubError("虚拟 <br> 段的结束范围不是 br")

        namespace = etree.QName(source_element).namespace
        anchor = etree.Element(self._qname(namespace, "a"))
        anchor.set("id", source_id)
        anchor.set("href", f"#{analysis_id}")
        anchor.set("class", f"{GENERATED_PREFIX}analysis-ref")
        # EPUB 2 content documents use XHTML 1.1, which does not permit the
        # HTML5 aria-label attribute. The visible marker remains the same;
        # EPUB 3 readers additionally receive its accessible name.
        if self.is_epub3:
            anchor.set("aria-label", "查看本段学习解析")
        anchor.set("style", "text-decoration:underline;margin:0.25em")
        anchor.text = self.analysis_link_text
        if virtual_source:
            if end not in (None, ""):
                end_node.addprevious(anchor)
            else:
                source_element.append(anchor)
        else:
            source_element.append(anchor)

        body = tree.xpath("//*[local-name()='body']")[0]
        container = self._generated_container(body, namespace)
        analysis = etree.SubElement(container, self._qname(namespace, "div"))
        analysis.set("id", analysis_id)
        analysis.set("class", f"{GENERATED_PREFIX}analysis")
        analysis.set("style", "display:block;writing-mode:inherit;"
                     "text-orientation:inherit;margin:1em")
        heading = etree.SubElement(analysis, self._qname(namespace, "h3"))
        heading.text = f"第 {ref.paragraph} 段{title}"
        return tree, path, namespace, analysis, source_element, virtual_source

    def _generated_container(
        self, body: etree._Element, namespace: str | None
    ) -> etree._Element:
        found = body.xpath(
            f"./*[@id='{GENERATED_PREFIX}analyses']"
        )
        if found:
            return found[0]
        name = "section" if self.is_epub3 else "div"
        container = etree.Element(self._qname(namespace, name))
        container.set("id", f"{GENERATED_PREFIX}analyses")
        container.set("class", f"{GENERATED_PREFIX}analyses")
        container.set(
            "style",
            "display:block;writing-mode:inherit;text-orientation:inherit;"
            "margin:1em",
        )
        heading = etree.SubElement(container, self._qname(namespace, "h2"))
        heading.text = "学习解析"

        insertion_index = len(body)
        for index in range(len(body) - 1, -1, -1):
            if not self._is_trailing_boilerplate(body[index]):
                break
            insertion_index = index
        body.insert(insertion_index, container)
        return container

    def _is_trailing_boilerplate(self, element: etree._Element) -> bool:
        if self._local_name(element) in {"footer", "header", "nav"}:
            return True
        tokens = self._semantic_tokens(element)
        return bool(
            tokens
            & {
                "footer",
                "header",
                "pgfooter",
                "pgheader",
                "pg-boilerplate",
                "copyright-page",
            }
        )

    @staticmethod
    def _add_text_line(
        parent: etree._Element, namespace: str | None, label: str, value: str
    ) -> None:
        paragraph = etree.SubElement(
            parent, etree.QName(namespace, "p") if namespace else "p"
        )
        paragraph.set("style", "display:block;text-indent:0;margin:0.55em 0")
        strong = etree.SubElement(
            paragraph, etree.QName(namespace, "strong") if namespace else "strong"
        )
        strong.text = f"{label}："
        strong.tail = value

    @staticmethod
    def _add_plain_text_line(
        parent: etree._Element, namespace: str | None, value: str
    ) -> None:
        paragraph = etree.SubElement(
            parent, etree.QName(namespace, "p") if namespace else "p"
        )
        paragraph.set("style", "display:block;text-indent:0;margin:0.25em 0 0 1em")
        paragraph.text = value

    @staticmethod
    def _qname(namespace: str | None, name: str) -> str:
        return str(etree.QName(namespace, name)) if namespace else name

    @staticmethod
    def _unique_id(candidate: str, existing: set[str]) -> str:
        if candidate not in existing:
            return candidate
        suffix = 2
        while f"{candidate}-{suffix}" in existing:
            suffix += 1
        return f"{candidate}-{suffix}"

    def _chapter(self, chapter: int) -> tuple[int, str, Path]:
        chapters = self.content_spines()
        if chapter < 1 or chapter > len(chapters):
            raise EpubError(f"章节编号超出范围: {chapter}")
        return chapters[chapter - 1]

    @staticmethod
    def _write_tree(
        tree: etree._ElementTree, path: Path, *, doctype: str | None = None
    ) -> None:
        doctype = doctype or tree.docinfo.doctype or None
        tree.write(
            str(path),
            encoding="utf-8",
            xml_declaration=True,
            pretty_print=False,
            doctype=doctype,
        )

    def link_issues(self) -> set[str]:
        issues: set[str] = set()
        for _, _, path in self.content_spines():
            tree = self._parse_xhtml(path)
            source_rel = path.relative_to(self.root).as_posix()
            ids = tree.xpath("//@id")
            duplicates = {value for value in ids if ids.count(value) > 1}
            for duplicate in duplicates:
                issues.add(f"{source_rel}: duplicate id #{duplicate}")
            for anchor in tree.xpath("//*[local-name()='a'][@href]"):
                href = anchor.get("href", "")
                parsed = urlsplit(href)
                if parsed.scheme or parsed.netloc or href.startswith(("mailto:", "tel:")):
                    continue
                target_path = (
                    path
                    if not parsed.path
                    else self._resolve(path.parent, parsed.path)
                )
                if not target_path.exists():
                    issues.add(f"{source_rel}: missing target {href}")
                    continue
                if parsed.fragment:
                    try:
                        target_tree = tree if target_path == path else self._parse_xhtml(target_path)
                        matches = target_tree.xpath("//*[@id=$fragment]", fragment=unquote(parsed.fragment))
                    except EpubError:
                        issues.add(f"{source_rel}: unparseable target {href}")
                        continue
                    if len(matches) != 1:
                        issues.add(f"{source_rel}: fragment resolves {len(matches)} times {href}")
        return issues

    def _detach_generated_analyses(self) -> None:
        """Move generated notes out of reading-flow chapters, keeping their links."""
        package_ns = etree.QName(self.opf_root).namespace
        manifest = self.opf_root.xpath("./*[local-name()='manifest']")[0]
        spine = self.opf_root.xpath("./*[local-name()='spine']")[0]
        changed = False
        for _, href, path in self.content_spines():
            tree = self._parse_xhtml(path)
            sections = tree.xpath("//*[@id=$id]", id=f"{GENERATED_PREFIX}analyses")
            if not sections:
                continue
            if len(sections) != 1:
                raise EpubError(f"章节中的解析区 ID 不唯一: {href}")
            section = sections[0]
            digest = hashlib.sha256(href.encode("utf-8")).hexdigest()[:12]
            source_root = tree.getroot()
            head = source_root.xpath("./*[local-name()='head']")
            body = source_root.xpath("./*[local-name()='body']")
            if len(head) != 1 or len(body) != 1:
                raise EpubError(f"章节缺少唯一 head/body: {href}")
            analyses = list(section)
            if analyses and self._local_name(analyses[0]) == "h2":
                heading = analyses.pop(0)
            else:
                heading = None
            if not analyses:
                raise EpubError(f"生成解析区为空: {href}")

            target_files: dict[str, str] = {}
            for offset in range(0, len(analyses), ANALYSES_PER_FILE):
                part = offset // ANALYSES_PER_FILE + 1
                stem = f"{GENERATED_PREFIX}analyses-{digest}-{part:03d}"
                notes_path = path.with_name(f"{stem}.xhtml")
                notes_href = Path(os.path.relpath(notes_path, self.opf_dir)).as_posix()
                if notes_path.exists() or stem in self.manifest:
                    raise EpubError(f"生成解析页与原书资源冲突: {notes_href}")

                notes_root = etree.Element(source_root.tag, nsmap=source_root.nsmap)
                for key, value in source_root.attrib.items():
                    notes_root.set(key, value)
                notes_root.append(copy.deepcopy(head[0]))
                notes_body = etree.SubElement(notes_root, body[0].tag)
                for key, value in body[0].attrib.items():
                    notes_body.set(key, value)
                notes_section = etree.SubElement(notes_body, section.tag)
                for key, value in section.attrib.items():
                    notes_section.set(key, value)
                if heading is not None:
                    notes_section.append(copy.deepcopy(heading))
                for analysis in analyses[offset:offset + ANALYSES_PER_FILE]:
                    copied = copy.deepcopy(analysis)
                    notes_section.append(copied)
                    for element_id in copied.xpath(".//@id"):
                        if element_id in target_files:
                            raise EpubError(f"生成解析目标 ID 不唯一: {element_id}")
                        target_files[element_id] = notes_path.name
                self._write_tree(
                    etree.ElementTree(notes_root), notes_path,
                    doctype=tree.docinfo.doctype or None,
                )

                item = etree.SubElement(manifest, self._qname(package_ns, "item"))
                item.set("id", stem)
                item.set("href", notes_href)
                item.set("media-type", "application/xhtml+xml")
                itemref = etree.SubElement(spine, self._qname(package_ns, "itemref"))
                itemref.set("idref", stem)
                itemref.set("linear", "no")
                self.manifest[stem] = item

            for anchor in tree.xpath("//*[local-name()='a'][@href]"):
                target = urlsplit(anchor.get("href", ""))
                if target.scheme or target.netloc or target.path or not target.fragment:
                    continue
                fragment = unquote(target.fragment)
                if fragment in target_files:
                    anchor.set("href", f"{target_files[fragment]}#{target.fragment}")
            section.getparent().remove(section)
            self._write_tree(tree, path)
            changed = True
        if changed:
            self._write_tree(self.opf_tree, self.opf_path)

    def archive(self, output: Path, *, detach_analyses: bool = False) -> None:
        output = output.resolve()
        if output == self.source:
            raise EpubError("输出路径不能覆盖输入 EPUB")
        if output.exists():
            raise EpubError(f"输出文件已存在: {output}")
        if detach_analyses:
            self._detach_generated_analyses()
        output.parent.mkdir(parents=True, exist_ok=True)
        descriptor, temporary_name = tempfile.mkstemp(
            prefix=f".{output.name}.", suffix=".tmp", dir=output.parent
        )
        os.close(descriptor)
        temporary = Path(temporary_name)
        try:
            with zipfile.ZipFile(temporary, "w") as archive:
                mime_info = zipfile.ZipInfo("mimetype")
                mime_info.compress_type = zipfile.ZIP_STORED
                mime_info.extra = b""
                archive.writestr(mime_info, b"application/epub+zip")
                files = sorted(
                    path
                    for path in self.root.rglob("*")
                    if path.is_file() and path.relative_to(self.root).as_posix() != "mimetype"
                )
                for path in files:
                    archive.write(
                        path,
                        path.relative_to(self.root).as_posix(),
                        compress_type=zipfile.ZIP_DEFLATED,
                    )
            temporary.replace(output)
        except Exception:
            temporary.unlink(missing_ok=True)
            raise
