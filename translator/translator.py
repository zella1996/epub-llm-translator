from pathlib import Path
from typing import Tuple
from logs.logger import logger
from translator.epub_processor import EpubContent
from translator.llm_api import LLMTranslator
from translator.sentence_analyzer import nlp, overall_difficulty
from lxml.etree import Element
import re

# ===== 全局变量与常量定义 =====
# 全局LLM翻译器实例，只初始化一次
_llm_translator = None

P_TAG = "p"
DIFFICULTY_THRESHOLD = 45
FOOTNOTE_ANCHOR_HREF_TEMPLATE = "{chapter_name}_ext.html#llmnote-{id}"
FOOTNOTE_ANCHOR_CLASS = "nounder"
FOOTNOTE_ANCHOR_TEXT = "#"
SENTENCE_END_PUNCT = {".", "!", "?", "。", "！", "？", "’", '"', "*"}
FOOTNOTE_SPLIT_CHAR = "|||LLMNOTE|||"


def process_sentence(text: str) -> Tuple[bool, str]:
    """
    处理单个句子，返回（是否需要注脚, 注脚内容）。
    """
    global _llm_translator

    # 基本筛选
    if len(text) < 5 or text[-1] not in SENTENCE_END_PUNCT:
        logger.debug(f"跳过非句子: {text}")
        return False, ""
    if sum(1 for w in text.split() if w.isalpha()) <= 3:
        logger.debug(f"跳过短句: {text}")
        return False, ""
    if all(c in SENTENCE_END_PUNCT or c.isspace() for c in text):
        logger.debug(f"跳过重复标点: {text}")
        return False, ""

    # 难度评分
    score = overall_difficulty(text)
    logger.debug(f"难度评分: {score:.2f} - 句子: {text}")

    if score > DIFFICULTY_THRESHOLD:
        result = _llm_translator.explain_sentence(text)
        return True, result
    return False, ""


def extract_text_recursive(element) -> str:
    """
    递归提取element及其所有子节点的文本内容，拼接为完整字符串。
    """
    text = element.text or ""
    for child in element:
        text += extract_text_recursive(child)
        text += child.tail or ""
    return text


def process_paragraph(
    p, note_id: int, para_notes_map: list, chapter_name: str
) -> bool:
    """
    递归处理<p>及其所有子节点，能正确解析混合标签文本，段落末尾加#锚点，注脚合并。
    返回是否有注脚。
    """
    # chapter_name 不能为空，为空直接返回
    if not chapter_name:
        logger.warning(
            "process_paragraph: chapter_name 不能为空，否则无法生成注脚锚点。已跳过该段落。"
        )
        return False

    full_text = extract_text_recursive(p).strip()
    if not full_text:
        return False

    doc = nlp(full_text)
    notes_in_para = []
    for sent in doc.sents:
        text = sent.text.strip()
        need_note, note_content = process_sentence(text)
        if need_note:
            notes_in_para.append((text, note_content))
    if notes_in_para:
        # 生成正确的锚点链接
        anchor_href = FOOTNOTE_ANCHOR_HREF_TEMPLATE.format(
            chapter_name=chapter_name, id=note_id
        )

        a = Element(
            "a",
            href=anchor_href,
            **{"class": FOOTNOTE_ANCHOR_CLASS},
        )
        a.text = FOOTNOTE_ANCHOR_TEXT
        # 将锚点插入到 <p> 标签外部（作为兄弟节点）
        p.addnext(a)
        para_notes_map.append((note_id, notes_in_para))
        # logger.debug(f"注脚锚点已添加，锚点链接: {anchor_href}")
        return True
    return False


def translate_epub_main(
    epub_path: str,
    output_path: str,
    filename_pattern: str = None,
    book_name: str = None,
) -> None:
    """
    主流程：递归处理所有段落，不跳过任何段，能正确解析混合标签文本。
    为每个章节创建对应的扩展章节来存放脚注。

    Args:
        epub_path: EPUB文件路径
        output_path: 输出文件路径
        filename_pattern: 可选的正则表达式，仅处理匹配的文件名（不包括后缀）
        book_name: 可选的书名，如果不提供则从epub文件中读取
    """
    epub_path = Path(epub_path)
    output_path = Path(output_path)
    epub_book = EpubContent(epub_path)

    # 获取epub的title，如果book_name未提供则使用epub中的title
    epub_title = epub_book.title
    if not book_name and epub_title:
        book_name = epub_title
        logger.info(f"从epub文件中读取到书名: {book_name}")
    elif not book_name:
        book_name = "未知书籍"
        logger.warning("未提供书名且无法从epub文件中读取，使用默认书名")

    # 初始化全局LLM翻译器（只初始化一次）
    global _llm_translator
    if _llm_translator is None:
        _llm_translator = LLMTranslator(book_name)

    # 获取所有spine信息，用于创建扩展章节
    spines = epub_book.spines
    spine_index = 0

    for spine_path in epub_book.search_spine_paths():
        # 获取当前spine信息
        current_spine = spines[spine_index]
        spine_index += 1

        # 检查文件名是否匹配正则表达式
        filename = Path(current_spine["href"]).stem
        if filename_pattern and not re.search(filename_pattern, filename):
            logger.info(f"跳过不匹配的文件: {filename}")
            continue

        logger.info(f"处理章节: {spine_path}")

        para_notes_map = []  # (note_id, [内容1, 内容2, ...])
        note_id = 1

        html_file = epub_book.read_spine_file(spine_path)
        root = html_file._root
        chapter_name = Path(current_spine["href"]).stem
        for p in root.iter(P_TAG):
            if process_paragraph(p, note_id, para_notes_map, chapter_name):
                note_id += 1
        epub_book.write_spine_file(spine_path, html_file)

        # 只有当有脚注时才创建扩展章节
        if para_notes_map:
            # 为当前章节创建扩展章节
            extended_chapter_path = epub_book.append_blank_chapter(
                current_spine, f"{chapter_name} 译文参考"
            )

            # 将当前章节的脚注写入对应的扩展章节
            footnote_map = [
                (nid, [(text, note) for text, note in notes]) for nid, notes in para_notes_map
            ]
            epub_book.write_chapter_body(
                extended_chapter_path,
                f"{chapter_name} 译文参考",
                footnote_map,
            )

    epub_book.archive(output_path)
    logger.info(f"处理完毕")
