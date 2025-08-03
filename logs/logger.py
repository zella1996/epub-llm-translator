import logging
from pathlib import Path
from datetime import datetime

# 初始化logger，输出到logs文件夹
logs_dir = Path(__file__).parent.parent / "logs"
logs_dir.mkdir(parents=True, exist_ok=True)

# 使用时间戳创建新的日志文件名
timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
log_file = logs_dir / f"translate_{timestamp}.log"

logger = logging.getLogger("translate_logger")
logger.setLevel(logging.INFO)
file_handler = logging.FileHandler(log_file, encoding="utf-8")
formatter = logging.Formatter("%(asctime)s - %(levelname)s - %(message)s")
file_handler.setFormatter(formatter)
if not logger.hasHandlers():
    logger.addHandler(file_handler)