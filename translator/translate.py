from ebooklib import epub, ITEM_DOCUMENT
from bs4 import BeautifulSoup
from pathlib import Path
from translator.llm_api import translate
from logs.logger import logger



# 常量定义
P_TAG = "p"


def translate_epub_main(epub_path):
    """
    翻译主方法，解析epub文件，仅获取<p>标签的正文内容，并打印在日志中
    """
    # 使用Path对象处理epub_path
    epub_path = Path(epub_path)

    try:
        book = epub.read_epub(str(epub_path))
        logger.info(f"成功打开EPUB文件: {epub_path}")
    except Exception as e:
        logger.error(f"无法打开EPUB文件: {epub_path}, 错误: {e}")
        return

    for item in book.get_items():
        if item.get_type() == ITEM_DOCUMENT:
            content = item.get_content()
            soup_tr = BeautifulSoup(content, "html.parser")
            p_tags = soup_tr.find_all(P_TAG)
            for p in p_tags:
                text = p.get_text()
                if text:
                    logger.debug(f"正文内容: {text[:50]}")
                    logger.info(f"解析结果: \n{translate(text)}\n")

                else:
                    logger.debug(f"！正文内容: {text}")

    def add_translated_result_page(id, content):
        def append_content(soup_tr, id, a_content):
            paragraph = soup_tr.new_tag(P_TAG)
            a_tag = soup_tr.new_tag("a", id=id)
            a_tag.string = a_content
            paragraph.append(a_tag)
            soup_tr.append(paragraph)

        if not hasattr(add_translated_result_page, "translated_result"):
            # 第一次调用，创建页面
            translated_result = epub.EpubHtml(
                title="译文参考", file_name="译文参考.xhtml", lang="zh"
            )
            soup_tr = BeautifulSoup(features="html.parser")
            h1_tag = soup_tr.new_tag("h1")
            h1_tag.string = "译文参考"
            soup_tr.append(h1_tag)
            append_content(soup_tr, id, content)
            translated_result.content = str(soup_tr)
            book.add_item(translated_result)
            book.spine.append(translated_result)

            add_translated_result_page.translated_result = translated_result
            add_translated_result_page.soup_tr = soup_tr
        else:
            # 后续调用，追加内容
            soup_tr = add_translated_result_page.soup_tr
            append_content(soup_tr, id, content)
            add_translated_result_page.translated_result.content = str(soup_tr)
