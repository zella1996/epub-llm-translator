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
FOOTNOTE_FILE_NAME = "译文参考.xhtml"
FOOTNOTE_TITLE = "译文参考"
FOOTNOTE_ANCHOR_HREF_TEMPLATE = "译文参考.xhtml#llmnote-{id}"
FOOTNOTE_ANCHOR_CLASS = "nounder"
FOOTNOTE_ANCHOR_TEXT = "#"
SENTENCE_END_PUNCT = {".", "!", "?", "。", "！", "？"}


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
    logger.info(f"难度评分: {score:.2f} - 句子: {text}\n")

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


def process_paragraph(p, note_id: int, para_notes_map: list) -> bool:
    """
    递归处理<p>及其所有子节点，能正确解析混合标签文本，段落末尾加#锚点，注脚合并。
    返回是否有注脚。
    """
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
        a = Element(
            "a",
            href=FOOTNOTE_ANCHOR_HREF_TEMPLATE.format(id=f"note-{note_id}"),
            **{"class": FOOTNOTE_ANCHOR_CLASS},
        )
        a.text = FOOTNOTE_ANCHOR_TEXT
        p.append(a)
        para_notes_map.append((note_id, notes_in_para))
        return True
    return False


def translate_epub_main(epub_path: str, output_path: str) -> None:
    """
    主流程：递归处理所有段落，不跳过任何段，能正确解析混合标签文本。
    注脚编号note_id为连续编号。
    """
    epub_path = Path(epub_path)
    output_path = Path(output_path)
    epub_book = EpubContent(epub_path)

    para_notes_map = []  # (note_id, [内容1, 内容2, ...])
    note_id = 1

    for spine_path in epub_book.search_spine_paths():
        html_file = epub_book.read_spine_file(spine_path)
        root = html_file._root
        for p in root.iter(P_TAG):
            if process_paragraph(p, note_id, para_notes_map):
                note_id += 1
        epub_book.write_spine_file(spine_path, html_file)

    # 合并注脚内容
    footnote_map = [(nid, "; ".join(notes)) for nid, notes in para_notes_map]

    add_explanation_result_chapter(epub_book, footnote_map)
    epub_book.archive(output_path)


def add_explanation_result_chapter(
    book: EpubContent, footnote_map: List[Tuple[int, str]]
) -> None:
    """
    生成译文参考章节，内容为所有注脚解释
    """
    new_path = book.add_blank_chapter(FOOTNOTE_FILE_NAME, FOOTNOTE_TITLE)
    book.write_chapter_body(new_path, FOOTNOTE_TITLE, footnote_map)
