"""Polymarket User WebSocket — 认证 WS，追踪订单成交和取消事件。

与 orderbook_ws.py 平级：
  orderbook_ws.py — Market Channel（公开，订单簿）
  user_ws.py      — User Channel （认证，订单/成交）

用法：
    ws = await get_or_create_user_ws(proxy_wallet)
    ws.watch_order(order_id, token_id, "BUY", price=Decimal("0.99"),
                   on_fill=my_fill_handler)
    ...
    ws.unwatch_order(order_id)
    await stop_all_user_ws()
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from typing import Any, Awaitable, Callable, Optional

import websockets

from framework.trading.provider import get_clob_credentials, get_client

logger = logging.getLogger(__name__)

USER_WS_URL = "wss://ws-subscriptions-clob.polymarket.com/ws/user"
HEARTBEAT_INTERVAL = 10
RECONNECT_DELAY_INIT = 1
RECONNECT_DELAY_MAX = 30


# ============================================================
# Data types
# ============================================================


@dataclass(frozen=True)
class FillEvent:
    order_id: str
    token_id: str
    side: str
    fill_size: Decimal
    fill_price: Decimal
    total_matched: Decimal
    trade_id: str | None
    source: str  # "ws_order_update" | "reconnect_reconcile"
    timestamp_ms: int


@dataclass(frozen=True)
class CancelEvent:
    order_id: str
    token_id: str
    side: str
    size_matched: Decimal
    timestamp_ms: int


@dataclass
class OrderWatch:
    order_id: str
    token_id: str
    side: str
    price: Decimal
    last_matched: Decimal
    seen_trade_ids: set = field(default_factory=set)
    pending_trade_price: Decimal | None = None
    pending_trade_id: str | None = None
    on_fill: Optional[Callable[..., Awaitable[None]]] = None
    on_cancel: Optional[Callable[..., Awaitable[None]]] = None


# ============================================================
# UserWS
# ============================================================


class UserWS:
    """一个 proxy_wallet 一个实例。"""

    def __init__(self, proxy_wallet: str) -> None:
        self._proxy_wallet = proxy_wallet
        self._tag = f"[UserWS:{proxy_wallet[:8]}]"
        self._watches: dict[str, OrderWatch] = {}
        self._ws: Any = None
        self._running = False
        self._task: Optional[asyncio.Task] = None
        self._heartbeat_task: Optional[asyncio.Task] = None
        self._reconnect_count = 0

    # ==================== Lifecycle ====================

    async def start(self) -> None:
        if self._task:
            return
        self._running = True
        self._task = asyncio.create_task(self._run(), name=f"user-ws-{self._proxy_wallet[:8]}")
        logger.info("%s Started", self._tag)

    async def stop(self) -> None:
        self._running = False
        if self._heartbeat_task:
            self._heartbeat_task.cancel()
            try:
                await self._heartbeat_task
            except asyncio.CancelledError:
                pass
            self._heartbeat_task = None
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None
        self._ws = None
        self._watches.clear()
        logger.info("%s Stopped", self._tag)

    # ==================== Watch API ====================

    def watch_order(
        self,
        order_id: str,
        token_id: str,
        side: str,
        price: Decimal,
        initial_matched: Decimal = Decimal("0"),
        on_fill: Optional[Callable[..., Awaitable[None]]] = None,
        on_cancel: Optional[Callable[..., Awaitable[None]]] = None,
    ) -> None:
        self._watches[order_id] = OrderWatch(
            order_id=order_id,
            token_id=token_id,
            side=side,
            price=price,
            last_matched=initial_matched,
            on_fill=on_fill,
            on_cancel=on_cancel,
        )
        logger.debug("%s watch_order %s side=%s initial=%s", self._tag, order_id[:8], side, initial_matched)

    def unwatch_order(self, order_id: str) -> None:
        removed = self._watches.pop(order_id, None)
        if removed:
            logger.debug("%s unwatch_order %s", self._tag, order_id[:8])

    # ==================== WS Main Loop ====================

    async def _run(self) -> None:
        delay = RECONNECT_DELAY_INIT
        while self._running:
            try:
                async with websockets.connect(
                    USER_WS_URL,
                    ping_interval=None,
                    max_size=2 * 1024 * 1024,
                ) as ws:
                    self._ws = ws
                    delay = RECONNECT_DELAY_INIT
                    logger.info("%s Connected", self._tag)

                    await self._authenticate(ws)

                    if self._heartbeat_task:
                        self._heartbeat_task.cancel()
                    self._heartbeat_task = asyncio.create_task(
                        self._heartbeat(ws), name=f"user-ws-hb-{self._proxy_wallet[:8]}"
                    )

                    if self._reconnect_count > 0:
                        await self._on_reconnect()
                    self._reconnect_count += 1

                    async for raw in ws:
                        if not self._running:
                            break
                        await self._dispatch(raw)

            except asyncio.CancelledError:
                raise
            except Exception as e:
                if not self._running:
                    break
                logger.warning("%s Disconnected (%s), reconnect in %ds", self._tag, e, delay)
            finally:
                self._ws = None
                if self._heartbeat_task:
                    self._heartbeat_task.cancel()
                    self._heartbeat_task = None

            if self._running:
                await asyncio.sleep(delay)
                delay = min(delay * 2, RECONNECT_DELAY_MAX)

    async def _authenticate(self, ws) -> None:
        creds = get_clob_credentials(self._proxy_wallet)
        auth_msg = json.dumps({
            "auth": {
                "apiKey": creds.api_key,
                "secret": creds.api_secret,
                "passphrase": creds.api_passphrase,
            },
            "type": "user",
        })
        await ws.send(auth_msg)
        logger.debug("%s Auth message sent", self._tag)

    async def _heartbeat(self, ws) -> None:
        try:
            while True:
                await asyncio.sleep(HEARTBEAT_INTERVAL)
                await ws.send("PING")
        except asyncio.CancelledError:
            pass
        except Exception as e:
            logger.debug("%s Heartbeat stopped: %s", self._tag, e)

    # ==================== Message Dispatch ====================

    async def _dispatch(self, raw: str) -> None:
        if raw == "PONG":
            return

        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            return

        if isinstance(data, list):
            for item in data:
                await self._dispatch_single(item)
        else:
            await self._dispatch_single(data)

    async def _dispatch_single(self, data: dict) -> None:
        event_type = data.get("event_type", "")

        if event_type == "order":
            await self._on_order_event(data)
        elif event_type == "trade":
            await self._on_trade_event(data)

    async def _on_order_event(self, data: dict) -> None:
        order_id = data.get("id") or data.get("order_id", "")
        if not order_id:
            return

        operation = data.get("operation", "")

        if operation == "CANCELLATION":
            watch = self._watches.get(order_id)
            if watch and watch.on_cancel:
                try:
                    size_matched = Decimal(str(data.get("size_matched", "0")))
                except (InvalidOperation, ValueError):
                    size_matched = Decimal("0")
                event = CancelEvent(
                    order_id=order_id,
                    token_id=watch.token_id,
                    side=watch.side,
                    size_matched=size_matched,
                    timestamp_ms=int(time.time() * 1000),
                )
                await watch.on_cancel(event)
            return

        if operation in ("UPDATE", "PLACEMENT"):
            raw_matched = data.get("size_matched", "0")
            try:
                size_matched = Decimal(str(raw_matched))
            except (InvalidOperation, ValueError):
                logger.warning("%s Invalid size_matched: %r", self._tag, raw_matched)
                return
            self._handle_order_event(order_id, size_matched)

    def _handle_order_event(self, order_id: str, size_matched: Decimal) -> None:
        watch = self._watches.get(order_id)
        if not watch:
            return
        delta = size_matched - watch.last_matched
        if delta <= 0:
            return
        watch.last_matched = size_matched
        price = watch.pending_trade_price or watch.price
        trade_id = watch.pending_trade_id
        watch.pending_trade_price = None
        watch.pending_trade_id = None
        self._emit_fill(watch, delta, price, total_matched=size_matched,
                        trade_id=trade_id, source="ws_order_update")

    async def _on_trade_event(self, data: dict) -> None:
        order_id = data.get("taker_order_id") or data.get("maker_order_id", "")
        trade_id = data.get("trade_id") or data.get("id", "")
        if not order_id or not trade_id:
            return

        try:
            size = Decimal(str(data.get("size", "0")))
            price = Decimal(str(data.get("price", "0")))
        except (InvalidOperation, ValueError):
            return

        self._handle_trade_event(order_id, trade_id, size, price)

    def _handle_trade_event(self, order_id: str, trade_id: str,
                            size: Decimal, price: Decimal) -> None:
        watch = self._watches.get(order_id)
        if not watch or trade_id in watch.seen_trade_ids:
            return
        watch.seen_trade_ids.add(trade_id)
        watch.pending_trade_price = price
        watch.pending_trade_id = trade_id

    def _emit_fill(
        self,
        watch: OrderWatch,
        delta: Decimal,
        price: Decimal,
        total_matched: Decimal,
        trade_id: str | None,
        source: str,
    ) -> None:
        event = FillEvent(
            order_id=watch.order_id,
            token_id=watch.token_id,
            side=watch.side,
            fill_size=delta,
            fill_price=price,
            total_matched=total_matched,
            trade_id=trade_id,
            source=source,
            timestamp_ms=int(time.time() * 1000),
        )
        logger.info(
            "%s Fill: order=%s delta=%s matched=%s/%s price=%s source=%s",
            self._tag, watch.order_id[:8], delta, total_matched,
            "?", price, source,
        )
        if watch.on_fill:
            asyncio.ensure_future(watch.on_fill(event))

    # ==================== Reconnect Reconcile ====================

    async def _on_reconnect(self) -> None:
        if not self._watches:
            return
        logger.info("%s Reconnect reconcile: checking %d orders", self._tag, len(self._watches))
        for order_id, watch in list(self._watches.items()):
            try:
                client = get_client(self._proxy_wallet)
                info = await asyncio.to_thread(client.get_order, order_id)
                raw_matched = info.get("size_matched", "0")
                try:
                    server_matched = Decimal(str(raw_matched))
                except (InvalidOperation, ValueError):
                    continue
                delta = server_matched - watch.last_matched
                if delta > 0:
                    watch.last_matched = server_matched
                    logger.info(
                        "%s Reconnect reconcile: order=%s delta=%s new_matched=%s",
                        self._tag, order_id[:8], delta, server_matched,
                    )
                    self._emit_fill(watch, delta, watch.price, total_matched=server_matched,
                                    trade_id=None, source="reconnect_reconcile")
            except Exception as e:
                logger.warning("%s Reconnect reconcile failed for %s: %s", self._tag, order_id[:8], e)

    # ==================== Health ====================

    def snapshot(self) -> dict:
        return {
            "proxy_wallet": self._proxy_wallet[:8],
            "connected": self._ws is not None,
            "watched_orders": len(self._watches),
            "reconnect_count": self._reconnect_count,
        }


# ============================================================
# Global registry
# ============================================================

_instances: dict[str, UserWS] = {}


async def get_or_create_user_ws(proxy_wallet: str) -> UserWS:
    key = proxy_wallet.lower()
    if key not in _instances:
        ws = UserWS(key)
        await ws.start()
        _instances[key] = ws
    return _instances[key]


async def stop_all_user_ws() -> None:
    for ws in list(_instances.values()):
        await ws.stop()
    _instances.clear()
