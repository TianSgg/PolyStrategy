"""信号订阅客户端 — 两条独立 WS 连接，各自解析强类型信号。

每种信号源拥有独立的消息格式和解析逻辑，不共享通用 Envelope。
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
from collections import OrderedDict
from decimal import Decimal
from typing import Any, Callable, Coroutine, Dict, Optional

from signal_leader_activity.types import LeaderBuySignal
from signal_weather_orderbook.types import WeatherSweepSignal

logger = logging.getLogger(__name__)

DEDUP_TTL_SEC = 300
DEDUP_MAX_SIZE = 10000
RECONNECT_DELAY_SEC = 2.0
PING_INTERVAL_SEC = 15.0


class TTLDeduplicator:
    """基于时间的事件去重器。"""

    def __init__(self, ttl_sec: float = DEDUP_TTL_SEC, max_size: int = DEDUP_MAX_SIZE) -> None:
        self._seen: "OrderedDict[str, float]" = OrderedDict()
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


# ─── 天气信号订阅 ─────────────────────────────────────────────────────────────


WeatherSignalHandler = Callable[[WeatherSweepSignal], Coroutine[Any, Any, None]]


class WeatherSignalClient:
    """连接天气信号服务 WS，解析 WeatherSweepSignal。"""

    def __init__(self, url: str, client_id: str, handler: WeatherSignalHandler) -> None:
        self._url = url
        self._client_id = client_id
        self._handler = handler
        self._dedup = TTLDeduplicator()
        self._task: Optional[asyncio.Task] = None
        self._running = False
        self._connected = False

    @property
    def connected(self) -> bool:
        return self._connected

    async def start(self) -> None:
        self._running = True
        self._task = asyncio.create_task(self._connection_loop(), name=f"weather-sub-{self._client_id}")

    async def stop(self) -> None:
        self._running = False
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass

    async def _connection_loop(self) -> None:
        try:
            import websockets
        except ImportError:
            logger.error("websockets not installed, weather subscription disabled")
            return

        while self._running:
            try:
                async with websockets.connect(self._url) as ws:
                    self._connected = True
                    logger.info("Weather WS connected: %s", self._url)

                    hello = {"type": "hello", "client_id": self._client_id, "subscribe": ["sweep"]}
                    await ws.send(json.dumps(hello))

                    welcome_raw = await asyncio.wait_for(ws.recv(), timeout=5.0)
                    welcome = json.loads(welcome_raw)
                    if welcome.get("type") != "welcome":
                        logger.warning("Weather WS unexpected welcome: %s", welcome)

                    ping_task = asyncio.create_task(self._ping_loop(ws))
                    try:
                        async for raw in ws:
                            await self._handle_message(raw)
                    finally:
                        ping_task.cancel()

            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.warning("Weather WS disconnected: %s, reconnecting in %ss", e, RECONNECT_DELAY_SEC)
            finally:
                self._connected = False

            if self._running:
                await asyncio.sleep(RECONNECT_DELAY_SEC)

    async def _ping_loop(self, ws) -> None:
        try:
            while True:
                await asyncio.sleep(PING_INTERVAL_SEC)
                await ws.send(json.dumps({"type": "ping"}))
        except (asyncio.CancelledError, Exception):
            pass

    async def _handle_message(self, raw: str) -> None:
        try:
            msg = json.loads(raw)
        except json.JSONDecodeError:
            return

        msg_type = msg.get("type")
        if msg_type == "weather_sweep":
            data = msg.get("signal", {})
            signal = WeatherSweepSignal(
                event_id=data.get("event_id", ""),
                event_type=data.get("event_type", "sweep"),
                token_id=data.get("token_id", ""),
                outcome=data.get("outcome", ""),
                city=data.get("city", ""),
                event_slug=data.get("event_slug", ""),
                market_slug=data.get("market_slug"),
                temperature_label=data.get("temperature_label"),
                direction=data.get("direction"),
                reason=data.get("reason"),
                occurred_at_ms=data.get("occurred_at_ms", 0),
                received_at_ns=data.get("received_at_ns", 0),
                orderbook_snapshot=data.get("orderbook_snapshot", {}),
            )

            if self._dedup.is_duplicate(signal.dedup_key()):
                return

            try:
                await self._handler(signal)
            except Exception:
                logger.exception("Error handling weather signal: %s", signal.event_id)

        elif msg_type == "pong":
            pass
        elif msg_type == "error":
            logger.error("Weather signal error: code=%s msg=%s", msg.get("code"), msg.get("message"))


# ─── Leader 信号订阅 ──────────────────────────────────────────────────────────


LeaderSignalHandler = Callable[[LeaderBuySignal], Coroutine[Any, Any, None]]


class LeaderSignalClient:
    """连接 Leader 活动信号服务 WS，解析 LeaderBuySignal。"""

    def __init__(self, url: str, client_id: str, handler: LeaderSignalHandler) -> None:
        self._url = url
        self._client_id = client_id
        self._handler = handler
        self._dedup = TTLDeduplicator()
        self._task: Optional[asyncio.Task] = None
        self._running = False
        self._connected = False

    @property
    def connected(self) -> bool:
        return self._connected

    async def start(self) -> None:
        self._running = True
        self._task = asyncio.create_task(self._connection_loop(), name=f"leader-sub-{self._client_id}")

    async def stop(self) -> None:
        self._running = False
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass

    async def _connection_loop(self) -> None:
        try:
            import websockets
        except ImportError:
            logger.error("websockets not installed, leader subscription disabled")
            return

        while self._running:
            try:
                async with websockets.connect(self._url) as ws:
                    self._connected = True
                    logger.info("Leader WS connected: %s", self._url)

                    hello = {"type": "hello", "client_id": self._client_id, "subscribe": ["leader_buy"]}
                    await ws.send(json.dumps(hello))

                    welcome_raw = await asyncio.wait_for(ws.recv(), timeout=5.0)
                    welcome = json.loads(welcome_raw)
                    if welcome.get("type") != "welcome":
                        logger.warning("Leader WS unexpected welcome: %s", welcome)

                    ping_task = asyncio.create_task(self._ping_loop(ws))
                    try:
                        async for raw in ws:
                            await self._handle_message(raw)
                    finally:
                        ping_task.cancel()

            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.warning("Leader WS disconnected: %s, reconnecting in %ss", e, RECONNECT_DELAY_SEC)
            finally:
                self._connected = False

            if self._running:
                await asyncio.sleep(RECONNECT_DELAY_SEC)

    async def _ping_loop(self, ws) -> None:
        try:
            while True:
                await asyncio.sleep(PING_INTERVAL_SEC)
                await ws.send(json.dumps({"type": "ping"}))
        except (asyncio.CancelledError, Exception):
            pass

    async def _handle_message(self, raw: str) -> None:
        try:
            msg = json.loads(raw)
        except json.JSONDecodeError:
            return

        msg_type = msg.get("type")
        if msg_type == "leader_buy":
            data = msg.get("signal", {})
            signal = LeaderBuySignal(
                event_id=data.get("event_id", ""),
                token_id=data.get("token_id", ""),
                outcome=data.get("outcome", ""),
                occurred_at_ms=data.get("occurred_at_ms", 0),
                received_at_ns=data.get("received_at_ns", 0),
                leader_proxy_wallet=data.get("leader_proxy_wallet", ""),
                leader_name=data.get("leader_name"),
                order_size=Decimal(str(data["order_size"])) if data.get("order_size") else None,
                order_price=Decimal(str(data["order_price"])) if data.get("order_price") else None,
                market_slug=data.get("market_slug"),
                extra=data.get("extra", {}),
            )

            if self._dedup.is_duplicate(signal.dedup_key()):
                return

            try:
                await self._handler(signal)
            except Exception:
                logger.exception("Error handling leader signal: %s", signal.event_id)

        elif msg_type == "pong":
            pass
        elif msg_type == "error":
            logger.error("Leader signal error: code=%s msg=%s", msg.get("code"), msg.get("message"))
