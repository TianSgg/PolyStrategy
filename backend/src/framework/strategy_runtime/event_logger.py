"""策略执行事件日志写入器 — 将 step 记录到 strategy_weather_sweep_events 表。"""
from __future__ import annotations

import asyncio
import concurrent.futures
import json
import logging
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from framework.db import get_db

logger = logging.getLogger(__name__)

_write_pool = concurrent.futures.ThreadPoolExecutor(max_workers=2, thread_name_prefix="evlog")


@dataclass(frozen=True)
class EventStepHandle:
    """Reference to an asynchronously inserted event step."""

    event_id: str
    sequence_no: int
    write_future: Any = None


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

    def log_step(
        self,
        step: str,
        detail: dict[str, Any],
        phase: str = "entry",
        occurred_at_ms: int | None = None,
    ) -> EventStepHandle | None:
        """记录一个 step 到数据库（异步写入，不阻塞事件循环）。

        phase: entry / exit
        """
        if not self._event_id:
            logger.warning("log_step called before start_event, ignoring")
            return None

        self._sequence_no += 1
        event_id = self._event_id
        sequence_no = self._sequence_no
        now = (
            datetime.fromtimestamp(occurred_at_ms / 1000, tz=timezone.utc)
            if occurred_at_ms is not None
            else datetime.now(timezone.utc)
        )

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
            event_id,
            phase,
            step,
            sequence_no,
            json.dumps(detail, ensure_ascii=False, default=str),
            now.replace(tzinfo=None),
        )

        write_future = None
        try:
            loop = asyncio.get_running_loop()
            write_future = loop.run_in_executor(
                _write_pool, self._do_write, sql, params, step, event_id,
            )
        except RuntimeError:
            self._do_write(sql, params, step, event_id)

        return EventStepHandle(event_id, sequence_no, write_future)

    def _do_write(self, sql: str, params: tuple, step: str, event_id: str) -> None:
        try:
            with get_db() as conn:
                with conn.cursor() as cur:
                    cur.execute(sql, params)
                conn.commit()
        except Exception:
            logger.exception("Failed to log step %s for event %s", step, event_id)

    async def update_step_detail(
        self,
        handle: EventStepHandle,
        detail: dict[str, Any],
    ) -> None:
        """Update an already logged step without creating another event step.

        This is used for fields that are intentionally collected in the
        background, such as the post-order BBO. The handle remains valid after
        ``end_event`` because it contains the immutable event identity.
        """
        if handle.write_future is not None:
            try:
                await handle.write_future
            except Exception:
                logger.exception(
                    "Initial event step write failed: event=%s sequence=%s",
                    handle.event_id, handle.sequence_no,
                )

        sql = f"""
            UPDATE {self._table}
            SET detail = %s
            WHERE event_id = %s AND sequence_no = %s
        """
        params = (
            json.dumps(detail, ensure_ascii=False, default=str),
            handle.event_id,
            handle.sequence_no,
        )
        loop = asyncio.get_running_loop()
        await loop.run_in_executor(
            _write_pool,
            self._do_update,
            sql,
            params,
            handle.event_id,
            handle.sequence_no,
        )

    def _do_update(
        self,
        sql: str,
        params: tuple,
        event_id: str,
        sequence_no: int,
    ) -> None:
        try:
            with get_db() as conn:
                with conn.cursor() as cur:
                    cur.execute(sql, params)
                    if cur.rowcount != 1:
                        logger.warning(
                            "Event step update matched %s rows: event=%s sequence=%s",
                            cur.rowcount, event_id, sequence_no,
                        )
                conn.commit()
        except Exception:
            logger.exception(
                "Failed to update event step: event=%s sequence=%s",
                event_id,
                sequence_no,
            )

    def end_event(self) -> None:
        """标记当前 event 结束，重置状态以便复用。"""
        self._event_id = None
        self._signal_id = None
        self._token_id = None
        self._market_slug = None
        self._event_slug = None
        self._sequence_no = 0
