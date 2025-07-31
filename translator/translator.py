from pathlib import Path
from typing import List, Tuple
from logs.logger import logger
from translator.epub_processor import EpubContent
from translator.llm_api import explain_sentence
from translator.sentence_analyzer import nlp, overall_difficulty
from lxml.etree import Element

# ===== 常量定义 =====
P_TAG = "p"
DIFFICULTY_THRESHOLD = 45
FOOTNOTE_ANCHOR_HREF_TEMPLATE = "{chapter_name}_ext.html#llmnote-{id}"
FOOTNOTE_ANCHOR_CLASS = "nounder"
FOOTNOTE_ANCHOR_TEXT = "#"
SENTENCE_END_PUNCT = {".", "!", "?", "。", "！", "？"}
FOOTNOTE_SPLIT_CHAR = "|||LLMNOTE|||"


def process_sentence(text: str, footnote_id: int) -> Tuple[bool, str]:
    """
    处理单个句子，返回（是否需要注脚, 注脚内容）。
    """
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
    # logger.debug(f"难度评分: {score:.2f} - 句子: {text}\n")

    if score > DIFFICULTY_THRESHOLD:
        # result = explain_sentence(text)
        result = "test" + str(footnote_id)
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
    p, note_id: int, para_notes_map: list, chapter_name: str = None
) -> bool:
    """
    递归处理<p>及其所有子节点，能正确解析混合标签文本，段落末尾加#锚点，注脚合并。
    返回是否有注脚。
    """
    # chapter_name 不能为空，为空直接返回
    if not chapter_name:
        logger.warning("process_paragraph: chapter_name 不能为空，否则无法生成注脚锚点。已跳过该段落。")
        return False

    full_text = extract_text_recursive(p).strip()
    if not full_text:
        return False

    doc = nlp(full_text)
    notes_in_para = []
    for sent in doc.sents:
        text = sent.text.strip()
        need_note, note_content = process_sentence(text, note_id)
        if need_note:
            notes_in_para.append(note_content)
    if notes_in_para:
        # 生成正确的锚点链接
        anchor_href = FOOTNOTE_ANCHOR_HREF_TEMPLATE.format(
            chapter_name=chapter_name, id=f"note-{note_id}"
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
        logger.debug(f"注脚锚点已添加，锚点链接: {anchor_href}")
        return True
    return False


def translate_epub_main(epub_path: str, output_path: str) -> None:
    """
    主流程：递归处理所有段落，不跳过任何段，能正确解析混合标签文本。
    为每个章节创建对应的扩展章节来存放脚注。
    """
    epub_path = Path(epub_path)
    output_path = Path(output_path)
    epub_book = EpubContent(epub_path)

    # 获取所有spine信息，用于创建扩展章节
    spines = epub_book.spines
    spine_index = 0

    for spine_path in epub_book.search_spine_paths():
        # 获取当前spine信息
        current_spine = spines[spine_index]
        spine_index += 1

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
                current_spine, f"{Path(current_spine['href']).stem} 译文参考"
            )

            # 将当前章节的脚注写入对应的扩展章节
            footnote_map = [
                (nid, FOOTNOTE_SPLIT_CHAR.join(notes)) for nid, notes in para_notes_map
            ]
            epub_book.write_chapter_body(
                extended_chapter_path,
                f"{Path(current_spine['href']).stem} 译文参考",
                footnote_map,
                FOOTNOTE_SPLIT_CHAR,
            )

    epub_book.archive(output_path)
