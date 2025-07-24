import logging
from pathlib import Path

# 初始化logger，输出到logs文件夹
logs_dir = Path(__file__).parent.parent / "logs"
logs_dir.mkdir(parents=True, exist_ok=True)
log_file = logs_dir / "translate.log"

# 每次运行先清空log文件
with open(log_file, "w", encoding="utf-8"):
    pass

logger = logging.getLogger("translate_logger")
logger.setLevel(logging.INFO)
file_handler = logging.FileHandler(log_file, encoding="utf-8")
formatter = logging.Formatter("%(asctime)s - %(levelname)s - %(message)s")
file_handler.setFormatter(formatter)
if not logger.hasHandlers():
    logger.addHandler(file_handler)