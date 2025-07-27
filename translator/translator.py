from pathlib import Path
from typing import List, Tuple
from logs.logger import logger
from translator.epub_processor import EpubContent
from translator.llm_api import explain_sentence
from translator.sentence_analyzer import nlp, overall_difficulty
from lxml.etree import Element

# 常量定义
P_TAG = "p"
FOOTNOTE_START_ID = 1
DIFFICULTY_THRESHOLD = 45
FOOTNOTE_FILE_NAME = "译文参考.xhtml"
FOOTNOTE_TITLE = "译文参考"
FOOTNOTE_ID_TEMPLATE = "llmnote-{id}"
FOOTNOTE_ANCHOR_HREF_TEMPLATE = "译文参考.xhtml#llmnote-{id}"
FOOTNOTE_ANCHOR_CLASS = "nounder"
SENTENCE_END_PUNCT = {".", "!", "?", "。", "！", "？"}


def process_sentence(
    text: str, footnote_id: int, footnote_map: List[Tuple[int, str]]
) -> Tuple[str, List[Element], int]:
    """
    处理单个句子，返回（句子文本, [要插入的Element节点], 下一个footnote_id）。
    """
    elements = []

    # 句子基本筛选
    if len(text) < 5 or text[-1] not in SENTENCE_END_PUNCT:
        logger.debug(f"跳过非句子: {text}")
        return text, elements, footnote_id

    if len([w for w in text.split() if w.isalpha()]) <= 3:
        logger.debug(f"跳过短句: {text}")
        return text, elements, footnote_id

    if all((c in SENTENCE_END_PUNCT or c.isspace()) for c in text):
        logger.debug(f"跳过重复标点: {text}")
        return text, elements, footnote_id

    # 难度评分
    score = overall_difficulty(text)
    logger.info(f"难度评分: {score:.2f} - 句子: {text}\n")

    # 需要添加注脚的句子
    if score > DIFFICULTY_THRESHOLD:
        # result = explain_sentence(text)
        result = "test" + str(footnote_id)
        logger.info(f"解释结果: {result}")
        a = Element(
            "a",
            href=FOOTNOTE_ANCHOR_HREF_TEMPLATE.format(id=footnote_id),
            **{"class": FOOTNOTE_ANCHOR_CLASS},
        )
        a.text = "# "
        elements.append(a)
        footnote_map.append((footnote_id, result))
        footnote_id += 1

    return text, elements, footnote_id


def process_paragraph(p, footnote_id: int, footnote_map: List[Tuple[int, str]]) -> int:
    """
    处理单个<p>节点，按句子插入注脚标签，返回更新后的footnote_id
    """
    if not p.text:
        return footnote_id

    doc = nlp(p.text)
    first = True
    last_elem = p
    p.text = None  # 清空原始文本

    for sent in doc.sents:
        text = sent.text.strip()
        sent_text, elements, footnote_id = process_sentence(
            text, footnote_id, footnote_map
        )
        if first:
            p.text = sent_text
            last_elem = p
            first = False
        else:
            if last_elem.tail is None:
                last_elem.tail = sent_text
            else:
                last_elem.tail += sent_text
        for elem in elements:
            p.append(elem)
            last_elem = elem
    # 这里只处理了p.text，若p原本有子节点（如嵌套span），可根据需要扩展
    return footnote_id


def translate_epub_main(epub_path: str, output_path: str) -> None:
    """
    基于epub_processor实现的注脚添加主流程，按句子增量插入脚注标签。
    """
    epub_path = Path(epub_path)
    output_path = Path(output_path)
    epub_book = EpubContent(epub_path)

    footnote_id = FOOTNOTE_START_ID
    footnote_map: List[Tuple[int, str]] = []  # (id, content)

    # 遍历所有spine文件
    for spine_path in epub_book.search_spine_paths():
        html_file = epub_book.read_spine_file(spine_path)
        root = html_file._root
        for p in root.iter(P_TAG):
            footnote_id = process_paragraph(p, footnote_id, footnote_map)
        epub_book.write_spine_file(spine_path, html_file)

    add_explanation_result_chapter(epub_book, footnote_map)
    epub_book.archive(output_path)


def add_explanation_result_chapter(
    book: EpubContent, footnote_map: List[Tuple[int, str]]
) -> None:
    """
    生成译文参考章节，内容为所有注脚解释
    """
    new_path = book.add_blank_chapter(FOOTNOTE_FILE_NAME, FOOTNOTE_TITLE)
    book.write_chapter_body(new_path, footnote_map)
