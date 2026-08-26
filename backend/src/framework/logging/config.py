"""统一日志配置 — 每个微服务自行写入独立目录。

用法:
    from framework.logging import setup_logging
    setup_logging("signal_leader")
"""
from __future__ import annotations

import logging
import os
import sys
from datetime import datetime, timezone, timedelta
from logging.handlers import TimedRotatingFileHandler
from pathlib import Path

LOG_FORMAT = "%(asctime)s - %(levelname)s - %(name)s - %(message)s"

KEEP_DAYS = 30

QUIET_LIBS = ["websockets", "httpcore", "httpx", "urllib3", "asyncio",
              "hpack", "hyperframe", "requests"]

_UTC8 = timezone(timedelta(hours=8))


class _UTC8Formatter(logging.Formatter):
    def formatTime(self, record, datefmt=None):
        dt = datetime.fromtimestamp(record.created, _UTC8)
        if datefmt:
            return dt.strftime(datefmt)
        return f"{dt:%Y-%m-%d %H:%M:%S}.{dt.microsecond // 1000:03d}"


class _UTC8TimedRotatingFileHandler(TimedRotatingFileHandler):
    def computeRollover(self, currentTime):
        dt = datetime.fromtimestamp(currentTime, _UTC8)
        next_midnight = dt.replace(hour=0, minute=0, second=0, microsecond=0) + timedelta(days=1)
        return int(next_midnight.timestamp())


def setup_logging(service_name: str) -> None:
    log_level = getattr(logging, os.getenv("LOG_LEVEL", "INFO").upper(), logging.INFO)

    src_dir = Path(__file__).resolve().parent.parent.parent
    log_dir = src_dir.parent.parent / "logs" / service_name
    log_dir.mkdir(parents=True, exist_ok=True)

    root = logging.getLogger()
    root.setLevel(log_level)

    if root.handlers:
        root.handlers.clear()

    formatter = _UTC8Formatter(LOG_FORMAT)

    app_handler = _UTC8TimedRotatingFileHandler(
        log_dir / "app.log",
        when="midnight",
        backupCount=KEEP_DAYS,
        encoding="utf-8",
    )
    app_handler.suffix = "%Y-%m-%d"
    app_handler.setLevel(log_level)
    app_handler.setFormatter(formatter)
    root.addHandler(app_handler)

    error_handler = _UTC8TimedRotatingFileHandler(
        log_dir / "error.log",
        when="midnight",
        backupCount=KEEP_DAYS,
        encoding="utf-8",
    )
    error_handler.suffix = "%Y-%m-%d"
    error_handler.setLevel(logging.ERROR)
    error_handler.setFormatter(formatter)
    root.addHandler(error_handler)

    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setLevel(log_level)
    console_handler.setFormatter(formatter)
    root.addHandler(console_handler)

    for lib in QUIET_LIBS:
        logging.getLogger(lib).setLevel(logging.WARNING)
