"""SignalSubscription — 连接信号服务 WS，心跳/重连/去重。

每个策略服务实例维护到天气信号和 Leader 信号的两条 WS 连接。
信号去重使用内存 TTL 集合（event_id），保证 at-most-once 投递给策略。
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
from collections import OrderedDict
from typing import Any, Callable, Coroutine, Dict, Optional, Set

from signal_data.contracts import SignalEnvelope

logger = logging.getLogger(__name__)

DEDUP_TTL_SEC = 300
DEDUP_MAX_SIZE = 10000
RECONNECT_DELAY_SEC = 2.0
PING_INTERVAL_SEC = 15.0


class TTLDeduplicator:
    """基于时间的事件去重器。"""

    def __init__(self, ttl_sec: float = DEDUP_TTL_SEC, max_size: int = DEDUP_MAX_SIZE) -> None:
        self._seen: OrderedDict[str, float] = OrderedDict()
        self._ttl = ttl_sec
        self._max_size = max_size

    def is_duplicate(self, key: str) -> bool:
        now = time.time()
        self._evict(now)
        if key in self._seen:
            return True
        self._seen[key] = now
        if len(self._seen) > self._max_size:
            self._seen.popitem(last=False)
        return False

    def _evict(self, now: float) -> None:
        while self._seen:
            oldest_key, oldest_time = next(iter(self._seen.items()))
            if now - oldest_time > self._ttl:
                self._seen.popitem(last=False)
            else:
                break


SignalHandler = Callable[[SignalEnvelope], Coroutine[Any, Any, None]]


class SignalSubscriptionClient:
    """单条到信号服务的 WS 连接。"""

    def __init__(
        self,
        url: str,
        client_id: str,
        subscribe_events: list,
        handler: SignalHandler,
    ) -> None:
        self._url = url
        self._client_id = client_id
        self._subscribe_events = subscribe_events
        self._handler = handler
        self._dedup = TTLDeduplicator()
        self._task: Optional[asyncio.Task] = None
        self._running = False
        self._connected = False

    @property
    def connected(self) -> bool:
        return self._connected

    async def start(self) -> None:
        """启动连接循环。"""
        self._running = True
        self._task = asyncio.create_task(self._connection_loop(), name=f"signal-sub-{self._client_id}")

    async def stop(self) -> None:
        """停止连接。"""
        self._running = False
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass

    async def _connection_loop(self) -> None:
        """持续尝试连接并接收消息。"""
        try:
            import websockets
        except ImportError:
            logger.error("websockets package not installed, signal subscription disabled")
            return

        while self._running:
            try:
                async with websockets.connect(self._url) as ws:
                    self._connected = True
                    logger.info("Signal WS connected: %s", self._url)

                    # 发送 hello
                    hello = {
                        "type": "hello",
                        "client_id": self._client_id,
                        "subscribe": self._subscribe_events,
                    }
                    await ws.send(json.dumps(hello))

                    # 等待 welcome
                    welcome_raw = await asyncio.wait_for(ws.recv(), timeout=5.0)
                    welcome = json.loads(welcome_raw)
                    if welcome.get("type") != "welcome":
                        logger.warning("Unexpected welcome: %s", welcome)

                    # 消息循环
                    ping_task = asyncio.create_task(self._ping_loop(ws))
                    try:
                        async for raw in ws:
                            await self._handle_message(raw)
                    finally:
                        ping_task.cancel()

            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.warning("Signal WS disconnected (%s): %s, reconnecting in %ss", self._url, e, RECONNECT_DELAY_SEC)
            finally:
                self._connected = False

            if self._running:
                await asyncio.sleep(RECONNECT_DELAY_SEC)

    async def _ping_loop(self, ws) -> None:
        """周期性发送 ping。"""
        try:
            while True:
                await asyncio.sleep(PING_INTERVAL_SEC)
                await ws.send(json.dumps({"type": "ping"}))
        except asyncio.CancelledError:
            pass
        except Exception:
            pass

    async def _handle_message(self, raw: str) -> None:
        """解析消息并分发。"""
        try:
            msg = json.loads(raw)
        except json.JSONDecodeError:
            return

        msg_type = msg.get("type")
        if msg_type == "signal":
            envelope_data = msg.get("envelope", {})
            envelope = SignalEnvelope(
                event_id=envelope_data.get("event_id", ""),
                source=envelope_data.get("source", ""),
                event_type=envelope_data.get("event_type", ""),
                received_at_ns=envelope_data.get("received_at_ns", 0),
                occurred_at_ms=envelope_data.get("occurred_at_ms", 0),
                token_id=envelope_data.get("token_id", ""),
                outcome=envelope_data.get("outcome"),
                side=envelope_data.get("side"),
                leader_proxy_wallet=envelope_data.get("leader_proxy_wallet"),
                payload=envelope_data.get("payload", {}),
            )

            # 去重
            if self._dedup.is_duplicate(envelope.dedup_key()):
                logger.debug("Duplicate signal ignored: %s", envelope.dedup_key())
                return

            try:
                await self._handler(envelope)
            except Exception:
                logger.exception("Error handling signal: %s", envelope.event_id)

        elif msg_type == "pong":
            pass
        elif msg_type == "error":
            logger.error("Signal server error: code=%s msg=%s", msg.get("code"), msg.get("message"))
