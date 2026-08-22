"""策略执行系统 — 前端实时推送 WebSocket 管理。

提供 /ws/strategy-execution 端点，向前端推送：
- run 状态变更
- 订单事件（下单/成交/撤单）
- 风控触发
- leader 确认/超时
"""
from __future__ import annotations

import asyncio
import json
import logging
from typing import Any, Dict, Set

from fastapi import WebSocket

logger = logging.getLogger(__name__)


class StrategyExecutionWSManager:
    """策略执行事件 WebSocket 广播管理器。"""

    _instance = None

    def __init__(self) -> None:
        self._connections: Set[WebSocket] = set()
        self._queue: asyncio.Queue = asyncio.Queue(maxsize=1024)
        self._broadcast_task = None

    @classmethod
    def get_instance(cls) -> "StrategyExecutionWSManager":
        if cls._instance is None:
            cls._instance = cls()
        return cls._instance

    async def start(self) -> None:
        if self._broadcast_task is None:
            self._broadcast_task = asyncio.create_task(
                self._broadcast_loop(), name="se-ws-broadcast"
            )

    async def stop(self) -> None:
        if self._broadcast_task:
            self._broadcast_task.cancel()
            try:
                await self._broadcast_task
            except asyncio.CancelledError:
                pass
            self._broadcast_task = None

    async def add_connection(self, websocket: WebSocket) -> None:
        await websocket.accept()
        self._connections.add(websocket)
        logger.debug("Strategy WS client connected. Total: %d", len(self._connections))

    async def remove_connection(self, websocket: WebSocket) -> None:
        self._connections.discard(websocket)
        logger.debug("Strategy WS client disconnected. Total: %d", len(self._connections))

    def publish(self, event: Dict[str, Any]) -> None:
        """非阻塞入队 — 策略内核调用此方法推送事件。"""
        try:
            self._queue.put_nowait(event)
        except asyncio.QueueFull:
            logger.warning("Strategy WS queue full, dropping event")

    async def _broadcast_loop(self) -> None:
        """从队列取出事件并广播给所有连接。"""
        while True:
            try:
                event = await self._queue.get()
            except asyncio.CancelledError:
                break

            if not self._connections:
                continue

            message = json.dumps(event, default=str)
            disconnected: Set[WebSocket] = set()
            for client in self._connections:
                try:
                    await client.send_text(message)
                except Exception:
                    disconnected.add(client)
            for client in disconnected:
                self._connections.discard(client)

    @property
    def connection_count(self) -> int:
        return len(self._connections)


def get_strategy_ws_manager() -> StrategyExecutionWSManager:
    return StrategyExecutionWSManager.get_instance()
