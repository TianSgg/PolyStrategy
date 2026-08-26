from __future__ import annotations

import asyncio
import json
import logging
from typing import Any, Callable, Coroutine, Protocol

from framework.strategy_runtime.interfaces import Signal
from framework.strategy_runtime.toolkit.signals.dedup import TTLDeduplicator

logger = logging.getLogger(__name__)

RECONNECT_DELAY_SEC = 2.0
PING_INTERVAL_SEC = 15.0


class SignalAdapter(Protocol):
    """适配器协议：将信号源原始消息转为通用 Signal。"""

    def adapt(self, raw: dict) -> Signal | None: ...


SignalCallback = Callable[[Signal], Coroutine[Any, Any, None]]


class SignalWSClient:
    """通用 WS 信号接收器 — 连接信号源微服务，使用适配器转换为 Signal。"""

    def __init__(
        self,
        url: str,
        adapter: SignalAdapter,
        *,
        client_id: str = "strategy",
        subscribe_types: list[str] | None = None,
    ) -> None:
        self._url = url
        self._adapter = adapter
        self._client_id = client_id
        self._subscribe_types = subscribe_types or []
        self._dedup = TTLDeduplicator()
        self._running = False
        self._connected = False

    @property
    def connected(self) -> bool:
        return self._connected

    async def listen(self, callback: SignalCallback) -> None:
        """持续监听信号源，适配后回调。自动重连。"""
        try:
            import websockets
        except ImportError:
            logger.error("websockets not installed")
            return

        self._running = True
        while self._running:
            try:
                async with websockets.connect(self._url) as ws:
                    self._connected = True
                    logger.info("Signal WS connected: %s", self._url)

                    hello = {
                        "type": "hello",
                        "client_id": self._client_id,
                        "subscribe": self._subscribe_types,
                    }
                    await ws.send(json.dumps(hello))

                    welcome_raw = await asyncio.wait_for(ws.recv(), timeout=5.0)
                    welcome = json.loads(welcome_raw)
                    if welcome.get("type") != "welcome":
                        logger.warning("Unexpected welcome: %s", welcome)

                    ping_task = asyncio.create_task(self._ping_loop(ws))
                    try:
                        async for raw in ws:
                            await self._handle_message(raw, callback)
                    finally:
                        ping_task.cancel()

            except asyncio.CancelledError:
                self._running = False
                raise
            except Exception as e:
                logger.warning(
                    "Signal WS disconnected (%s): %s, reconnecting in %ss",
                    self._url, e, RECONNECT_DELAY_SEC,
                )
            finally:
                self._connected = False

            if self._running:
                await asyncio.sleep(RECONNECT_DELAY_SEC)

    async def stop(self) -> None:
        self._running = False

    async def _ping_loop(self, ws) -> None:
        try:
            while True:
                await asyncio.sleep(PING_INTERVAL_SEC)
                await ws.send(json.dumps({"type": "ping"}))
        except (asyncio.CancelledError, Exception):
            pass

    async def _handle_message(self, raw: str, callback: SignalCallback) -> None:
        try:
            msg = json.loads(raw)
        except json.JSONDecodeError:
            return

        if msg.get("type") in ("pong", "welcome"):
            return

        if msg.get("type") == "error":
            logger.error("Signal source error: %s", msg.get("message"))
            return

        signal = self._adapter.adapt(msg)
        if signal is None:
            return

        if self._dedup.is_duplicate(signal.signal_id):
            return

        await callback(signal)
