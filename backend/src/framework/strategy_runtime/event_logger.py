"""策略执行事件日志写入器 — 将 step 记录到 strategy_weather_sweep_events 表。"""
from __future__ import annotations

import asyncio
import concurrent.futures
import json
import logging
import uuid
from datetime import datetime, timezone
from typing import Any

from framework.db import get_db

logger = logging.getLogger(__name__)

_write_pool = concurrent.futures.ThreadPoolExecutor(max_workers=2, thread_name_prefix="evlog")


class EventLogger:
    """记录一次策略执行（event）中的多个 step。

    用法:
        el = EventLogger(table="strategy_weather_sweep_events", ...)
        el.start_event(signal_id=..., token_id=..., ...)
        el.log_step("signal_received", {...})
        el.log_step("buy_placed", {...})
        ...
    """

    def __init__(
        self,
        table: str,
        owner_user_id: int,
        proxy_wallet: str,
        config_id: int,
        config_snapshot: dict[str, Any] | None = None,
    ) -> None:
        self._table = table
        self._owner_user_id = owner_user_id
        self._proxy_wallet = proxy_wallet
        self._config_id = config_id
        self._config_snapshot = config_snapshot
        self._event_id: str | None = None
        self._signal_id: str | None = None
        self._token_id: str | None = None
        self._market_slug: str | None = None
        self._event_slug: str | None = None
        self._sequence_no = 0

    @property
    def event_id(self) -> str | None:
        return self._event_id

    def start_event(
        self,
        signal_id: str | None = None,
        token_id: str | None = None,
        market_slug: str | None = None,
        event_slug: str | None = None,
    ) -> str:
        """开始一个新 event，返回 event_id。"""
        self._event_id = str(uuid.uuid4())
        self._signal_id = signal_id
        self._token_id = token_id
        self._market_slug = market_slug
        self._event_slug = event_slug
        self._sequence_no = 0
        return self._event_id

    def log_step(self, step: str, detail: dict[str, Any], phase: str = "entry") -> None:
        """记录一个 step 到数据库（异步写入，不阻塞事件循环）。

        phase: entry / exit
        """
        if not self._event_id:
            logger.warning("log_step called before start_event, ignoring")
            return

        self._sequence_no += 1
        now = datetime.now(timezone.utc)

        if phase not in {"entry", "exit"}:
            raise ValueError(f"Unsupported event phase: {phase}")

        sql = f"""
            INSERT INTO {self._table} (
                event_id, phase, step, sequence_no, detail, occurred_at
            ) VALUES (
                %s, %s, %s, %s, %s, %s
            )
        """
        params = (
            self._event_id,
            phase,
            step,
            self._sequence_no,
            json.dumps(detail, ensure_ascii=False, default=str),
            now.replace(tzinfo=None),
        )

        try:
            loop = asyncio.get_running_loop()
            loop.run_in_executor(_write_pool, self._do_write, sql, params, step, self._event_id)
        except RuntimeError:
            self._do_write(sql, params, step, self._event_id)

    def _do_write(self, sql: str, params: tuple, step: str, event_id: str) -> None:
        try:
            with get_db() as conn:
                with conn.cursor() as cur:
                    cur.execute(sql, params)
                conn.commit()
        except Exception:
            logger.exception("Failed to log step %s for event %s", step, event_id)

    def end_event(self) -> None:
        """标记当前 event 结束，重置状态以便复用。"""
        self._event_id = None
        self._signal_id = None
        self._token_id = None
        self._market_slug = None
        self._event_slug = None
        self._sequence_no = 0
