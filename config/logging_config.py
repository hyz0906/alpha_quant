import sys
from loguru import logger

def setup_logging(level="INFO", log_file="logs/alphaquant.log"):
    logger.remove()
    logger.add(sys.stderr, level=level)
    logger.add(log_file, rotation="10 MB", level=level)
    return logger
