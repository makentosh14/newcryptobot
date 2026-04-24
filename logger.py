"""
logger.py
---------
Centralized logging setup.

Features:
- Colored console output (only when TTY).
- Rotating file handler for `logs/bot.log`.
- Separate `logs/errors.log` for WARNING+ only.
- Single `get_logger(name)` entry point.

Usage:
    from logger import get_logger
    log = get_logger(__name__)
    log.info("hello")
"""

from __future__ import annotations

import logging
import sys
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Dict

from config import settings


_LOGGERS: Dict[str, logging.Logger] = {}
_CONFIGURED = False


class _ColorFormatter(logging.Formatter):
    """Minimal ANSI color formatter for console output."""

    COLORS = {
        "DEBUG": "\033[36m",    # cyan
        "INFO": "\033[32m",     # green
        "WARNING": "\033[33m",  # yellow
        "ERROR": "\033[31m",    # red
        "CRITICAL": "\033[35m", # magenta
    }
    RESET = "\033[0m"

    def format(self, record: logging.LogRecord) -> str:
        base = super().format(record)
        if sys.stderr.isatty():
            color = self.COLORS.get(record.levelname, "")
            return f"{color}{base}{self.RESET}"
        return base


def _configure_root() -> None:
    """Configure the root logger once."""
    global _CONFIGURED
    if _CONFIGURED:
        return

    log_dir: Path = settings.log_dir_path
    level = getattr(logging, settings.LOG_LEVEL, logging.INFO)

    fmt = "%(asctime)s | %(levelname)-8s | %(name)s | %(message)s"
    datefmt = "%Y-%m-%d %H:%M:%S"

    root = logging.getLogger()
    root.setLevel(level)

    for h in list(root.handlers):
        root.removeHandler(h)

    console = logging.StreamHandler(sys.stderr)
    console.setLevel(level)
    console.setFormatter(_ColorFormatter(fmt, datefmt=datefmt))
    root.addHandler(console)

    file_h = RotatingFileHandler(
        log_dir / "bot.log",
        maxBytes=10 * 1024 * 1024,
        backupCount=5,
        encoding="utf-8",
    )
    file_h.setLevel(level)
    file_h.setFormatter(logging.Formatter(fmt, datefmt=datefmt))
    root.addHandler(file_h)

    err_h = RotatingFileHandler(
        log_dir / "errors.log",
        maxBytes=5 * 1024 * 1024,
        backupCount=3,
        encoding="utf-8",
    )
    err_h.setLevel(logging.WARNING)
    err_h.setFormatter(logging.Formatter(fmt, datefmt=datefmt))
    root.addHandler(err_h)

    for noisy in ("urllib3", "websockets", "httpx", "httpcore", "asyncio"):
        logging.getLogger(noisy).setLevel(logging.WARNING)

    _CONFIGURED = True


def get_logger(name: str) -> logging.Logger:
    """Return a named logger. Safe to call many times."""
    _configure_root()
    if name in _LOGGERS:
        return _LOGGERS[name]
    logger = logging.getLogger(name)
    _LOGGERS[name] = logger
    return logger
