"""OrderbookWS — Polymarket 订单簿 WebSocket 连接与维护。

维护订阅、处理 tick_size_change 和 price_change 事件。
策略可继承重写 on_tick_change / on_price_change 方法。
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
from typing import Any, Callable, Coroutine, Dict, Optional, Set

logger = logging.getLogger(__name__)

POLYMARKET_WS_URL = "wss://ws-subscriptions-clob.polymarket.com/ws/market"

TickChangeCallback = Callable[[str, str], Coroutine[Any, Any, None]]
PriceChangeCallback = Callable[[str, dict], Coroutine[Any, Any, None]]


class OrderbookWS:
    """Polymarket 市场 WS — 订阅 token 的 tick_size/price 变更。"""

    def __init__(
        self,
        on_tick_change: Optional[TickChangeCallback] = None,
        on_price_change: Optional[PriceChangeCallback] = None,
    ) -> None:
        self._on_tick_change = on_tick_change
        self._on_price_change = on_price_change
        self._ws: Any = None
        self._running = False
        self._task: Optional[asyncio.Task] = None
        self._subscribed: Set[str] = set()
        self._tick_sizes: Dict[str, str] = {}

    @property
    def connected(self) -> bool:
        return self._ws is not None and self._running

    def get_tick_size(self, token_id: str) -> Optional[str]:
        return self._tick_sizes.get(token_id)

    async def start(self) -> None:
        if self._running:
            return
        self._running = True
        self._task = asyncio.create_task(self._ws_loop())

    async def stop(self) -> None:
        self._running = False
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass

    def subscribe(self, token_ids: list[str]) -> None:
        new_ids = [t for t in token_ids if t not in self._subscribed]
        if not new_ids:
            return
        self._subscribed.update(new_ids)
        if self._ws and self._running:
            asyncio.create_task(self._send_subscribe(new_ids))

    def unsubscribe(self, token_id: str) -> None:
        self._subscribed.discard(token_id)

    async def _ws_loop(self) -> None:
        import websockets

        delay = 1
        while self._running:
            try:
                async with websockets.connect(POLYMARKET_WS_URL, ping_interval=10) as ws:
                    self._ws = ws
                    delay = 1
                    if self._subscribed:
                        await self._send_subscribe(list(self._subscribed))
                    async for msg in ws:
                        if not self._running:
                            break
                        await self._handle_message(msg)
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.warning("OrderbookWS disconnected: %s", e)
            finally:
                self._ws = None

            if self._running:
                await asyncio.sleep(delay)
                delay = min(delay * 2, 60)

    async def _send_subscribe(self, token_ids: list[str]) -> None:
        if not self._ws:
            return
        msg = {
            "operation": "subscribe",
            "assets_ids": token_ids,
            "type": "market",
            "level": 2,
            "initial_dump": True,
            "custom_feature_enabled": True,
        }
        try:
            await self._ws.send(json.dumps(msg))
        except Exception as e:
            logger.error("OrderbookWS subscribe error: %s", e)

    async def _handle_message(self, raw: str) -> None:
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            return

        if isinstance(data, list):
            return

        event_type = data.get("event_type")
        if event_type == "tick_size_change":
            token_id = data.get("asset_id", "")
            new_tick = data.get("new_tick_size", "")
            if token_id and new_tick:
                self._tick_sizes[token_id] = new_tick
                if self._on_tick_change:
                    await self._on_tick_change(token_id, new_tick)
        elif event_type == "price_change":
            if self._on_price_change:
                for change in data.get("price_changes", []):
                    token_id = change.get("asset_id", "")
                    if token_id:
                        await self._on_price_change(token_id, change)
