"""统一日志配置 — 每个微服务自行写入独立目录。

用法（在各服务 app.py 顶部）:
    from shared.logging_config import setup_logging
    setup_logging("signal_weather_orderbook")

日志输出:
    logs/signal_weather_orderbook/app.log      — 全量日志（RotatingFileHandler）
    logs/signal_weather_orderbook/error.log    — ERROR 及以上
    console                                    — 同步输出到 stdout
"""
from __future__ import annotations

import logging
import os
import sys
from datetime import datetime, timezone, timedelta
from logging.handlers import RotatingFileHandler
from pathlib import Path

LOG_FORMAT = "%(asctime)s - %(levelname)s - %(name)s - %(message)s"

MAX_BYTES = 50 * 1024 * 1024  # 50MB per file
BACKUP_COUNT = 5

QUIET_LIBS = ["websockets", "httpcore", "httpx", "urllib3", "asyncio",
              "hpack", "hyperframe", "requests"]

_UTC8 = timezone(timedelta(hours=8))


class _UTC8Formatter(logging.Formatter):
    """Timestamps in UTC+8."""

    def formatTime(self, record, datefmt=None):
        dt = datetime.fromtimestamp(record.created, _UTC8)
        if datefmt:
            return dt.strftime(datefmt)
        return f"{dt:%Y-%m-%d %H:%M:%S}.{dt.microsecond // 1000:03d}"


def setup_logging(service_name: str) -> None:
    """Configure root logger: file + console handlers, per-service log directory."""
    log_level = getattr(logging, os.getenv("LOG_LEVEL", "INFO").upper(), logging.INFO)

    backend_dir = Path(__file__).resolve().parent.parent.parent
    log_dir = backend_dir.parent / "logs" / service_name
    log_dir.mkdir(parents=True, exist_ok=True)

    root = logging.getLogger()
    root.setLevel(log_level)

    if root.handlers:
        root.handlers.clear()

    formatter = _UTC8Formatter(LOG_FORMAT)

    app_handler = RotatingFileHandler(
        log_dir / "app.log",
        maxBytes=MAX_BYTES,
        backupCount=BACKUP_COUNT,
        encoding="utf-8",
    )
    app_handler.setLevel(log_level)
    app_handler.setFormatter(formatter)
    root.addHandler(app_handler)

    error_handler = RotatingFileHandler(
        log_dir / "error.log",
        maxBytes=MAX_BYTES,
        backupCount=BACKUP_COUNT,
        encoding="utf-8",
    )
    error_handler.setLevel(logging.ERROR)
    error_handler.setFormatter(formatter)
    root.addHandler(error_handler)

    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setLevel(log_level)
    console_handler.setFormatter(formatter)
    root.addHandler(console_handler)

    for lib in QUIET_LIBS:
        logging.getLogger(lib).setLevel(logging.WARNING)
