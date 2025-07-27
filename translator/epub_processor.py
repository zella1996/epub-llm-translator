import os
import re
import zipfile
from pathlib import Path
from typing import Generator, Callable, Iterable, cast
from enum import Enum, auto
from io import StringIO
from html import escape
from lxml.etree import parse, Element, QName, tostring, SubElement
from lxml import etree

# =========================
# 文本节点相关工具函数与类型
# =========================


class TextPosition(Enum):
    """文本节点在DOM中的位置类型"""

    WHOLE_DOM = auto()  # 整个DOM片段
    TEXT = auto()  # 元素的text属性
    TAIL = auto()  # 元素的tail属性


# element, position, parent
default_TextDescription = tuple[Element, TextPosition, Element | None]

# 忽略的标签（不处理其文本内容）
_IGNORE_TAGS = (
    "title",
    "link",
    "style",
    "css",
    "img",
    "script",
    "metadata",
    "{http://www.w3.org/1998/Math/MathML}math",  # TODO: 公式是正文，也要读进去，暂时忽略避免扰乱得了。
)

# 认为是文本叶子的标签
_TEXT_LEAF_TAGS = ("a", "b", "br", "hr", "span", "em", "strong", "label", "small")


def _is_not_empty_str(text: str | None) -> bool:
    """判断字符串是否为非空（忽略空格和换行）"""
    if text is None:
        return False
    for char in text:
        if char not in (" ", "\n"):
            return True
    return False


def search_texts(element: Element, parent: Element | None = None):
    """
    遍历DOM树，定位所有可提取的文本节点。
    返回(element, position, parent)元组。
    """
    if element.tag in _IGNORE_TAGS:
        return
    # 如果有任意子节点不是文本叶子标签
    if any(c.tag not in _TEXT_LEAF_TAGS for c in element):
        # 处理当前节点的text
        if _is_not_empty_str(element.text):
            yield element, TextPosition.TEXT, parent
        # 遍历所有子节点
        for child in element:
            if child.tag in _TEXT_LEAF_TAGS:
                yield child, TextPosition.WHOLE_DOM, element
            else:
                yield from search_texts(child, element)
            # 处理子节点的tail
            if _is_not_empty_str(child.tail):
                yield child, TextPosition.TAIL, element
    else:
        # 所有子节点都是文本叶子标签，则整体作为一个片段
        yield element, TextPosition.WHOLE_DOM, parent


def read_texts(root: Element):
    """
    读取DOM树中所有可提取的文本内容，按search_texts顺序yield字符串。
    """
    for element, position, _ in search_texts(root):
        if position == TextPosition.WHOLE_DOM:
            yield _plain_text(element)
        elif position == TextPosition.TEXT:
            yield cast(str, element.text)
        elif position == TextPosition.TAIL:
            yield cast(str, element.tail)


def append_texts(root: Element, texts: Iterable[str | Iterable[str] | None]):
    """
    按search_texts顺序，将texts中的内容写回DOM树。
    支持str或可迭代的str（会拼接）。
    """
    zip_list = list(zip(texts, search_texts(root)))
    for text, (element, position, parent) in reversed(zip_list):
        if text is None:
            continue
        if not isinstance(text, str):
            # TODO: 实现分割文本的写入
            text = "".join(text)
        if position == TextPosition.WHOLE_DOM:
            if parent is not None:
                _append_dom(parent, element, text)
        elif position == TextPosition.TEXT:
            element.text = _append_text(element.text, text)
        elif position == TextPosition.TAIL:
            element.tail = _append_text(element.tail, text)


def _append_dom(parent: Element, origin: Element, text: str):
    """
    在parent下，origin节点后插入一个新节点，内容为text。
    """
    appended = Element(origin.tag, {**origin.attrib})
    for index, child in enumerate(parent):
        if child == origin:
            parent.insert(index + 1, appended)
            break
    appended.attrib.pop("id", None)
    appended.text = text
    appended.tail = origin.tail
    origin.tail = None


def _append_text(left: str | None, right: str) -> str:
    """拼接文本，处理None情况"""
    if left is None:
        return right
    else:
        return left + right


def _plain_text(target: Element):
    """递归获取DOM片段的所有文本内容，拼接为字符串"""
    buffer = StringIO()
    for text in _iter_text(target):
        buffer.write(text)
    return buffer.getvalue()


def _iter_text(parent: Element):
    """递归yield所有text和tail内容"""
    if parent.text is not None:
        yield parent.text
    for child in parent:
        yield from _iter_text(child)
    if parent.tail is not None:
        yield parent.tail


# =========================
# HTMLFile: 单个HTML/XHTML文件处理
# =========================

_FILE_HEAD_PATTERN = re.compile(r"^<\?xml.*?\?>[\s]*<!DOCTYPE.*?>")
_XMLNS_IN_TAG = re.compile(r"\{[^}]+\}")
_BRACES = re.compile(r"(\{|\})")


class HTMLFile:
    """
    代表一个HTML/XHTML文件，支持文本提取与写回。
    """

    def __init__(self, file_content: str):
        # 匹配文件头部（xml声明和DOCTYPE）
        match = re.match(_FILE_HEAD_PATTERN, file_content)
        # 依赖to_xml函数将内容转为xml格式，并去除头部
        xml_content = (
            re.sub(_FILE_HEAD_PATTERN, "", to_xml(file_content))
            if to_xml
            else file_content
        )
        self._head: str = match.group() if match else None
        self._root: Element = Element("root")  # 占位初始化
        self._xmlns: str | None = None
        self._texts_length: int | None = None
        try:
            from xml.etree.ElementTree import fromstring

            # 解析xml内容为Element对象
            self._root = fromstring(xml_content)
            # 提取并清理命名空间
            self._xmlns = self._extract_xmlns(self._root)
        except Exception:
            pass

    def _extract_xmlns(self, root: Element) -> str | None:
        """
        遍历所有元素，提取根命名空间并清理tag中的命名空间
        """
        root_xmlns: str | None = None
        for i, element in enumerate(self._all_elements(root)):
            need_clean_xmlns = True
            match = re.match(_XMLNS_IN_TAG, element.tag)
            if match:
                xmlns = re.sub(_BRACES, "", match.group())
                if i == 0:
                    root_xmlns = xmlns
                elif root_xmlns != xmlns:
                    need_clean_xmlns = False
            if need_clean_xmlns:
                element.tag = re.sub(_XMLNS_IN_TAG, "", element.tag)
        return root_xmlns

    def _all_elements(self, parent: Element):
        """递归遍历所有元素"""
        yield parent
        for child in parent:
            yield from self._all_elements(child)

    def read_texts(self) -> list[str]:
        """读取所有文本节点内容"""
        texts = list(read_texts(self._root))
        self._texts_length = len(texts)
        return texts

    def write_texts(self, texts: Iterable[str]):
        """写入文本节点内容"""
        append_texts(self._root, texts)

    @property
    def texts_length(self) -> int:
        """获取文本节点数量"""
        if self._texts_length is None:
            self._texts_length = 0
            for _ in read_texts(self._root):
                self._texts_length += 1
        return self._texts_length

    @property
    def file_content(self) -> str:
        """
        生成最终的文件内容（带头部和命名空间）
        """
        file_content: str
        if self._xmlns is None:
            # 没有命名空间，直接序列化
            file_content = tostring(self._root, encoding="unicode")
            file_content = to_html(file_content) if to_html else file_content
        else:
            # 有命名空间，重新构造根节点并添加命名空间属性
            root = Element(
                self._root.tag,
                attrib={**self._root.attrib, "xmlns": self._xmlns},
            )
            root.extend(self._root)
            # 保证所有元素的text不为None
            for element in self._all_elements(root):
                if element.text is None:
                    element.text = ""
            file_content = tostring(root, encoding="unicode")
        if self._head is not None:
            file_content = self._head + file_content
        return file_content


# =========================
# EpubContent: EPUB文件整体处理
# =========================


class EpubContent:
    """
    代表一个EPUB文件，支持解包、文本提取、写回、元数据操作等。
    """

    def __init__(self, epub_path: Path, temp_dir: Path = None):
        # 初始化epub文件路径和解压目录
        self.file_path = epub_path
        self.extract_dir = (
            str(epub_path) + "_extracted" if temp_dir is None else str(temp_dir)
        )
        if not os.path.exists(self.extract_dir):
            os.makedirs(self.extract_dir)
        # 解压epub文件到指定目录
        with zipfile.ZipFile(self.file_path, "r") as zip_ref:
            zip_ref.extractall(self.extract_dir)
        self._temp_dir: Path = Path(self.extract_dir)
        # 查找主内容opf文件路径
        self._content_path = self._find_content_path(self.extract_dir)
        # 解析opf文件
        self._tree = parse(self._content_path)
        # 获取命名空间
        self._namespaces = {"ns": self._tree.getroot().nsmap.get(None)}
        # 获取spine、metadata、manifest节点
        self._spine = self._tree.xpath("//ns:spine", namespaces=self._namespaces)[0]
        self._metadata = self._tree.xpath("//ns:metadata", namespaces=self._namespaces)[
            0
        ]
        self._manifest = self._tree.xpath("//ns:manifest", namespaces=self._namespaces)[
            0
        ]

    def _find_content_path(self, path: str) -> str:
        """
        解析container.xml，获取opf主内容文件路径
        """
        root = parse(os.path.join(path, "META-INF", "container.xml")).getroot()
        rootfile = root.xpath(
            "//ns:container/ns:rootfiles/ns:rootfile",
            namespaces={"ns": root.nsmap.get(None)},
        )[0]
        full_path = rootfile.attrib["full-path"]
        joined_path = os.path.join(path, full_path)
        return os.path.abspath(joined_path)

    def archive(self, saved_path: Path):
        """
        将解压目录重新打包为epub文件
        """
        with zipfile.ZipFile(saved_path, "w") as zip_file:
            for file_path in self._temp_dir.rglob("*"):
                if not file_path.is_file():
                    continue
                relative_path = file_path.relative_to(self._temp_dir)
                zip_file.write(
                    filename=file_path,
                    arcname=str(relative_path),
                )

    @property
    def ncx_path(self):
        """
        获取ncx导航文件路径
        """
        ncx_dom = self._manifest.find('.//*[@id="ncx"]')
        if ncx_dom is not None:
            href_path = ncx_dom.get("href")
            base_path = os.path.dirname(self._content_path)
            path = os.path.join(base_path, href_path)
            path = os.path.abspath(path)
            if os.path.exists(path):
                return path
            # 若主路径不存在，尝试用解压目录拼接
            path = os.path.join(self.extract_dir, path)
            path = os.path.abspath(path)
            return path

    @property
    def spines(self) -> list:
        """
        获取spine顺序的内容项信息
        """
        idref_dict = {}
        index = 0
        # 记录spine顺序
        for child in self._spine.iterchildren():
            id = child.get("idref")
            idref_dict[id] = index
            index += 1
        items = [None for _ in range(index)]
        spines = []
        # 按spine顺序整理manifest中的item
        for child in self._manifest.iterchildren():
            id = child.get("id")
            if id in idref_dict:
                index = idref_dict[id]
                items[index] = child
        base_path = os.path.dirname(self._content_path)
        for item in items:
            if item is not None:
                spines.append(
                    {
                        "folder_path": self.extract_dir,
                        "base_path": base_path,
                        "href": item.get("href"),
                        "media_type": item.get("media-type"),
                        "id": item.get("id"),
                    }
                )
        return spines

    def search_spine_paths(self) -> Generator[Path, None, None]:
        """
        遍历所有spine项，返回xhtml类型的文件路径
        """
        for spine in self.spines:
            if spine["media_type"] == "application/xhtml+xml":
                path = os.path.join(spine["base_path"], spine["href"])
                path = os.path.abspath(path)
                if not os.path.exists(path):
                    # 若主路径不存在，尝试用解压目录拼接
                    path = os.path.join(spine["folder_path"], spine["href"])
                    path = os.path.abspath(path)
                yield Path(path)

    def read_spine_file(self, spine_path: Path) -> HTMLFile:
        """
        读取spine文件内容并返回HTMLFile对象
        """
        with open(spine_path, "r", encoding="utf-8") as file:
            return HTMLFile(file.read())

    def write_spine_file(self, spine_path: Path, file: HTMLFile):
        """
        写入spine文件内容
        """
        with open(spine_path, "w", encoding="utf-8") as f:
            f.write(file.file_content)

    def replace_ncx(self, replace: Callable[[list[str]], list[str]]):
        """
        替换ncx文件中的text节点内容
        """
        ncx_path = self.ncx_path
        if ncx_path is None:
            return
        tree = parse(ncx_path)
        root = tree.getroot()
        namespaces = {"ns": root.nsmap.get(None)}
        text_doms = []
        text_list = []
        # 收集所有text节点
        for text_dom in root.xpath("//ns:text", namespaces=namespaces):
            text_doms.append(text_dom)
            text_list.append(text_dom.text or "")
        # 替换text节点内容
        for index, text in enumerate(replace(text_list)):
            text_dom = text_doms[index]
            text_dom.text = self._link_translated(text_dom.text, text)
        tree.write(ncx_path, pretty_print=True)

    def _link_translated(self, origin: str, target: str) -> str:
        """
        若翻译内容与原文一致则不变，否则拼接
        """
        if origin == target:
            return origin
        else:
            return f"{origin} - {target}"

    def save_content(self):
        """
        保存opf主内容文件
        """
        self._tree.write(self._content_path, pretty_print=True)

    @property
    def title(self):
        """
        获取书名
        """
        title_dom = self._get_title()
        if title_dom is None:
            return None
        return title_dom.text

    @title.setter
    def title(self, title: str):
        """
        设置书名
        """
        title_dom = self._get_title()
        if title_dom is not None:
            title_dom.text = _escape_ascii(title)

    def _get_title(self):
        """
        获取metadata中的title节点
        """
        titles = self._metadata.xpath(
            "./dc:title",
            namespaces={
                "dc": self._metadata.nsmap.get("dc"),
            },
        )
        if len(titles) == 0:
            return None
        return titles[0]

    @property
    def authors(self) -> list[str]:
        """
        获取作者列表
        """
        return list(map(lambda x: x.text, self._get_creators()))

    @authors.setter
    def authors(self, authors):
        """
        设置作者列表
        """
        creator_doms = self._get_creators()
        if len(creator_doms) == 0:
            return
        parent_dom = creator_doms[0].getparent()
        index_at_parent = parent_dom.index(creator_doms[0])
        ns = {
            "dc": self._metadata.nsmap.get("dc"),
            "opf": self._metadata.nsmap.get("opf"),
        }
        # 先插入新作者节点
        for author in reversed(authors):
            creator_dom = Element(QName(ns["dc"], "creator"))
            creator_dom.set(QName(ns["opf"], "file-as"), author)
            creator_dom.set(QName(ns["opf"], "role"), "aut")
            creator_dom.text = _escape_ascii(author)
            parent_dom.insert(index_at_parent, creator_dom)
        # 再移除原有作者节点
        for creator_dom in creator_doms:
            parent_dom.remove(creator_dom)

    def _get_creators(self):
        """
        获取metadata中的creator节点
        """
        return self._metadata.xpath(
            "./dc:creator",
            namespaces={
                "dc": self._metadata.nsmap.get("dc"),
            },
        )

    def add_blank_chapter(
        self,
        new_filename: str,
        chapter_title: str = "New Chapter",
    ):
        """
        复制spine列表最后一个文件，仅保留head等body外内容，body清空，
        新文件保存到同级目录，并自动添加到opf的manifest、spine和toc.ncx。
        """
        # 自动获取spine列表最后一个文件的路径
        last_spine = self.spines[-1]
        template_spine_path = Path(last_spine["base_path"]) / last_spine["href"]
        print(template_spine_path)
        # 1. 解析模板文件，清空body内容
        new_content = self._generate_blank_chapter_content(template_spine_path)
        # 2. 保存新文件到同级目录
        new_path = self._save_new_chapter_file(
            template_spine_path, new_filename, new_content
        )
        # 3. 修改opf（manifest、spine）
        new_id = self._add_to_opf(new_filename)
        # 4. 修改toc.ncx，添加navPoint
        self._add_to_ncx(new_filename, chapter_title)
        # 保存opf
        self.save_content()
        return new_path

    def _generate_blank_chapter_content(self, template_spine_path: Path) -> str:
        """
        解析模板文件，清空body内容，返回新内容
        """
        parser = etree.HTMLParser(recover=True)
        tree = etree.parse(str(template_spine_path), parser=parser)
        root = tree.getroot()
        body = root.find(".//body")
        if body is not None:
            body.clear()
        new_content = etree.tostring(
            root, encoding="utf-8", method="xml", xml_declaration=True
        ).decode("utf-8")
        return new_content

    def _save_new_chapter_file(
        self, template_spine_path: Path, new_filename: str, new_content: str
    ) -> Path:
        """
        保存新章节文件到同级目录
        """
        new_path = template_spine_path.parent / new_filename
        with open(new_path, "w", encoding="utf-8") as f:
            f.write(new_content)
        return new_path

    def _add_to_opf(self, new_filename: str) -> str:
        """
        向opf的manifest和spine添加新章节
        """
        new_id = f"item_{new_filename.replace('.', '_')}"
        manifest = self._manifest
        item_elem = SubElement(manifest, "item")
        item_elem.set("href", new_filename)
        item_elem.set("id", new_id)
        item_elem.set("media-type", "application/xhtml+xml")
        spine = self._spine
        itemref_elem = SubElement(spine, "itemref")
        itemref_elem.set("idref", new_id)
        return new_id

    def _add_to_ncx(self, new_filename: str, chapter_title: str):
        """
        向toc.ncx添加navPoint
        """
        ncx_path = self.ncx_path
        if ncx_path and os.path.exists(ncx_path):
            ncx_tree = etree.parse(ncx_path)
            navMap = ncx_tree.find(".//{*}navMap")
            if navMap is not None:
                navpoints = navMap.findall(".//{*}navPoint")
                playOrder = str(len(navpoints) + 1)
                nav_id = f"navPoint-{playOrder}"
                navPoint = SubElement(navMap, "navPoint")
                navPoint.set("id", nav_id)
                navPoint.set("playOrder", playOrder)
                navLabel = SubElement(navPoint, "navLabel")
                text = SubElement(navLabel, "text")
                text.text = chapter_title
                content = SubElement(navPoint, "content")
                content.set("src", new_filename)
                ncx_tree.write(ncx_path, encoding="utf-8", pretty_print=True)


# =========================
# 工具函数
# =========================


def _escape_ascii(content: str) -> str:
    """
    转义html并将unicode转为字符
    """
    content = escape(content)
    content = re.sub(
        r"\\u([\da-fA-F]{4})",
        lambda x: chr(int(x.group(1), 16)),
        content,
    )
    return content
