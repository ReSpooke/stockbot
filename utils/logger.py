import logging
import sys
from logging.handlers import RotatingFileHandler

from rich.logging import RichHandler

import config


def get_logger(name: str = "stockbot") -> logging.Logger:
    logger = logging.getLogger(name)
    if logger.handlers:
        return logger

    level = getattr(logging, config.LOG_LEVEL.upper(), logging.INFO)
    logger.setLevel(level)

    # Rich console handler — pretty colours in terminal
    console_handler = RichHandler(
        rich_tracebacks=True,
        markup=True,
        show_time=True,
        show_path=False,
    )
    console_handler.setLevel(level)

    # Rotating file handler — keep last 5 × 5 MB
    file_handler = RotatingFileHandler(
        config.LOG_FILE,
        maxBytes=5 * 1024 * 1024,
        backupCount=5,
        encoding="utf-8",
    )
    file_handler.setFormatter(
        logging.Formatter("%(asctime)s [%(levelname)s] %(name)s: %(message)s")
    )
    file_handler.setLevel(level)

    logger.addHandler(console_handler)
    logger.addHandler(file_handler)
    return logger


log = get_logger()
