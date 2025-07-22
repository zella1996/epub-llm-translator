import logging
from math import log
from ebooklib import epub, ITEM_DOCUMENT
from bs4 import BeautifulSoup
from pathlib import Path

# 初始化logger，输出到logs文件夹
logs_dir = Path(__file__).parent.parent / "logs"
logs_dir.mkdir(parents=True, exist_ok=True)
log_file = logs_dir / "translate.log"

logger = logging.getLogger("translate_logger")
logger.setLevel(logging.INFO)
file_handler = logging.FileHandler(log_file, encoding="utf-8")
formatter = logging.Formatter("%(asctime)s - %(levelname)s - %(message)s")
file_handler.setFormatter(formatter)
if not logger.hasHandlers():
    logger.addHandler(file_handler)


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
            soup = BeautifulSoup(content, "html.parser")
            p_tags = soup.find_all("p")
            for p in p_tags:
                text = p.get_text()
                if text:
                    logger.info(f"正文内容: {text}")
                else:
                    logger.info(f"！正文内容: {text}")
