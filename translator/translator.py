from ebooklib import epub, ITEM_DOCUMENT
from bs4 import BeautifulSoup
from pathlib import Path
from translator.llm_api import explain_sentence
from logs.logger import logger
from translator.sentence_analyzer import overall_difficulty, nlp
from bs4 import NavigableString

# 常量定义
P_TAG = "p"  # 只处理<p>标签
FOOTNOTE_START_ID = 1  # 脚注编号起始值
DIFFICULTY_THRESHOLD = 45  # 难度评分阈值
FOOTNOTE_FILE_NAME = "译文参考.html"
FOOTNOTE_TITLE = "译文参考"
FOOTNOTE_LANG = "zh"
FOOTNOTE_ANCHOR_TEMPLATE = '<a href="译文参考.html#llmnote-{id}" class="nounder">#</a>'
FOOTNOTE_ID_TEMPLATE = "llmnote-{id}"
HTML_PARSER = "html.parser"
ENCODING = "utf-8"


def translate_epub_main(epub_path, output_path):
    """
    翻译主方法，解析epub文件，仅获取<p>标签的正文内容，并打印在日志中
    编辑后的epub另存为output_path
    """
    epub_path = Path(epub_path)
    output_path = Path(output_path)

    try:
        book = epub.read_epub(str(epub_path))
        logger.info(f"成功打开EPUB文件: {epub_path}")
    except Exception as e:
        logger.error(f"无法打开EPUB文件: {epub_path}, 错误: {e}")
        return

    footnote_id = FOOTNOTE_START_ID
    footnote_map = []

    for item in book.get_items():
        if item.get_type() != ITEM_DOCUMENT:
            continue
        logger.info(f"当前处理的item名称: {item.get_name()}")

        content = item.get_content()
        soup_tr = BeautifulSoup(content, HTML_PARSER)
        p_tags = soup_tr.find_all(P_TAG)
        for p in p_tags:
            # 新内容列表
            new_contents = []
            for elem in p.contents:
                if isinstance(elem, (str, NavigableString)):
                    text = str(elem)
                    doc = nlp(text)
                    sentences = [sent.text for sent in doc.sents]
                    for sent in sentences:
                        score = overall_difficulty(sent)
                        logger.info(f"难度评分: {score:.2f} - 句子: {sent}\n")
                        if score > DIFFICULTY_THRESHOLD:
                            result = explain_sentence(sent)
                            logger.info(f"解释结果: {result}")
                            footnote_anchor = FOOTNOTE_ANCHOR_TEMPLATE.format(
                                id=footnote_id
                            )
                            new_contents.append(sent)
                            # 直接插入soup对象，避免转义
                            new_contents.append(
                                BeautifulSoup(footnote_anchor, HTML_PARSER)
                            )
                            footnote_map.append((footnote_id, result))
                            footnote_id += 1
                        else:
                            new_contents.append(sent)
                else:
                    # 其他子标签保持不变
                    new_contents.append(elem)
            # 替换<p>内容
            p.clear()
            for nc in new_contents:
                if isinstance(nc, str):
                    p.append(nc)
                else:
                    p.append(nc)
        item.set_content(str(soup_tr).encode(ENCODING))

    if footnote_map:
        add_explanation_result_page(book, footnote_map)

    epub.write_epub(str(output_path), book)


def add_explanation_result_page(book, footnote_map):
    """
    生成译文参考页面，展示所有高难度句子的解释
    """
    translated_result = epub.EpubHtml(
        title=FOOTNOTE_TITLE, file_name=FOOTNOTE_FILE_NAME, lang=FOOTNOTE_LANG
    )
    soup = BeautifulSoup(features=HTML_PARSER)
    h1_tag = soup.new_tag("h1")
    h1_tag.string = FOOTNOTE_TITLE
    soup.append(h1_tag)
    for id, a_content in footnote_map:
        paragraph = soup.new_tag(P_TAG)
        a_tag = soup.new_tag(
            "a", id=FOOTNOTE_ID_TEMPLATE.format(id=id), **{"class": "nounder"}
        )
        a_tag.string = f"#{id} {a_content}"
        paragraph.append(a_tag)
        soup.append(paragraph)
    translated_result.content = str(soup)
    book.add_item(translated_result)
    book.spine.append(translated_result)
    # 添加到目录
    if hasattr(book, "toc") and book.toc:
        book.toc = list(book.toc) + [translated_result]
    else:
        book.toc = [translated_result]
