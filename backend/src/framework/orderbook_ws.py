"""Polymarket CLOB WebSocket 订单簿管理 — 维护完整 L2 book + 漂移检测。

功能：
  1. 连接 Polymarket CLOB Market WS，按 asset 维护完整 L2 订单簿
  2. 每次 price_change 用 server 端 BBO 对比本地 BBO，发现漂移自动重新同步
  3. 定时拉取 REST /book 全量数据与内存 book 校验（可配置间隔）
  4. 提供手动 resync 接口，微服务需要时可随时触发全量校验

用法：
    ws = OrderBookWS()
    await ws.start()

    sub_id = await ws.subscribe(asset_id, on_bbo=handler, on_tick=tick_handler)

    # 手动触发全量校验
    await ws.resync(asset_id)

    await ws.unsubscribe(sub_id)
    await ws.stop()
"""
from __future__ import annotations

import asyncio
import json
import logging
import random
import uuid
from decimal import Decimal
from typing import Any, Awaitable, Callable, Optional

import aiohttp

logger = logging.getLogger(__name__)

MARKET_WS_URL = "wss://ws-subscriptions-clob.polymarket.com/ws/market"
CLOB_REST_URL = "https://clob.polymarket.com"

BboCallback = Callable[[str, Optional[Decimal], Optional[Decimal]], Awaitable[None]]
TickCallback = Callable[[str, Decimal], Awaitable[None]]


class LocalOrderBook:
    """L2 订单簿 — 维护 bids/asks，支持增量更新和全量快照。"""

    __slots__ = ("bids", "asks")

    def __init__(self) -> None:
        self.bids: dict[float, float] = {}
        self.asks: dict[float, float] = {}

    def apply_snapshot(self, bids: list[dict], asks: list[dict]) -> None:
        self.bids = {float(r["price"]): float(r["size"]) for r in bids if float(r.get("size", 0)) > 0}
        self.asks = {float(r["price"]): float(r["size"]) for r in asks if float(r.get("size", 0)) > 0}

    def apply_change(self, price: str, size: str, side: str) -> None:
        levels = self.asks if side == "SELL" else self.bids if side == "BUY" else None
        if levels is None:
            return
        p, s = float(price), float(size)
        if s <= 0:
            levels.pop(p, None)
        else:
            levels[p] = s

    @property
    def best_bid(self) -> Optional[float]:
        return max(self.bids, default=None)

    @property
    def best_ask(self) -> Optional[float]:
        return min(self.asks, default=None)

    @property
    def mid_price(self) -> Optional[float]:
        b, a = self.best_bid, self.best_ask
        if b is not None and a is not None:
            return (b + a) / 2
        return b or a

    def to_dict(self) -> dict:
        return {
            "bids": sorted(self.bids.items(), reverse=True),
            "asks": sorted(self.asks.items()),
            "best_bid": self.best_bid,
            "best_ask": self.best_ask,
            "bid_levels": len(self.bids),
            "ask_levels": len(self.asks),
        }


class _Subscription:
    __slots__ = ("id", "asset_id", "on_bbo", "on_tick")

    def __init__(self, asset_id: str, on_bbo: Optional[BboCallback], on_tick: Optional[TickCallback]):
        self.id = uuid.uuid4().hex[:12]
        self.asset_id = asset_id
        self.on_bbo = on_bbo
        self.on_tick = on_tick


class OrderBookWS:
    """Polymarket CLOB Market Channel — 维护 L2 book + 漂移检测 + 事件分发。"""

    def __init__(self, resync_interval_sec: int = 300) -> None:
        self._subs: dict[str, _Subscription] = {}
        self._asset_subs: dict[str, list[_Subscription]] = {}
        self._subscribed_on_wire: set[str] = set()
        self._books: dict[str, LocalOrderBook] = {}
        self._tick_sizes: dict[str, Decimal] = {}
        self._min_order_sizes: dict[str, Decimal] = {}
        self._ws: Any = None
        self._running = False
        self._task: Optional[asyncio.Task] = None
        self._resync_task: Optional[asyncio.Task] = None
        self._resync_interval = resync_interval_sec
        self._lock = asyncio.Lock()
        self._http_session: Optional[aiohttp.ClientSession] = None

    # ==================== Public API ====================

    async def start(self) -> None:
        if self._task:
            return
        self._running = True
        self._http_session = aiohttp.ClientSession()
        self._task = asyncio.create_task(self._run(), name="framework-orderbook-ws")
        self._resync_task = asyncio.create_task(self._periodic_resync(), name="orderbook-resync")

    async def stop(self) -> None:
        self._running = False
        if self._resync_task:
            self._resync_task.cancel()
            try:
                await self._resync_task
            except asyncio.CancelledError:
                pass
            self._resync_task = None
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None
        if self._http_session:
            await self._http_session.close()
            self._http_session = None
        self._ws = None
        self._subscribed_on_wire.clear()
        self._books.clear()
        self._min_order_sizes.clear()

    async def subscribe(
        self,
        asset_id: str,
        on_bbo: Optional[BboCallback] = None,
        on_tick: Optional[TickCallback] = None,
    ) -> str:
        """订阅 asset 的 BBO/tick 更新，返回 sub_id。"""
        sub = _Subscription(asset_id, on_bbo, on_tick)
        async with self._lock:
            self._subs[sub.id] = sub
            self._asset_subs.setdefault(asset_id, []).append(sub)

            if asset_id not in self._subscribed_on_wire:
                self._subscribed_on_wire.add(asset_id)
                self._books[asset_id] = LocalOrderBook()
                if self._ws:
                    await self._send_subscribe([asset_id])

        return sub.id

    async def unsubscribe(self, sub_id: str) -> None:
        """取消订阅。asset 无订阅者时清理资源。"""
        async with self._lock:
            sub = self._subs.pop(sub_id, None)
            if not sub:
                return

            subs = self._asset_subs.get(sub.asset_id, [])
            subs[:] = [s for s in subs if s.id != sub_id]

            if not subs:
                self._asset_subs.pop(sub.asset_id, None)
                self._subscribed_on_wire.discard(sub.asset_id)
                self._books.pop(sub.asset_id, None)
                self._tick_sizes.pop(sub.asset_id, None)
                self._min_order_sizes.pop(sub.asset_id, None)
                if self._ws:
                    await self._send_unsubscribe(sub.asset_id)

    async def resync(self, asset_id: str) -> bool:
        """手动触发全量 orderbook 校验，返回是否成功。"""
        book = self._books.get(asset_id)
        if not book:
            return False
        return await self._fetch_and_replace(asset_id, book)

    def get_book(self, asset_id: str) -> Optional[LocalOrderBook]:
        """获取当前内存中的 orderbook（只读）。"""
        return self._books.get(asset_id)

    def get_bbo_snapshot(self, asset_id: str) -> Optional[dict[str, Optional[float]]]:
        """获取当前内存盘口的统一 BBO 快照。"""
        book = self._books.get(asset_id)
        if book is None:
            return None
        bids = sorted(book.bids.items(), reverse=True)
        asks = sorted(book.asks.items())
        return {
            "best_bid": bids[0][0] if bids else None,
            "best_bid_size": bids[0][1] if bids else None,
            "best_ask": asks[0][0] if asks else None,
            "best_ask_size": asks[0][1] if asks else None,
        }

    def get_tick_size(self, asset_id: str) -> Optional[Decimal]:
        return self._tick_sizes.get(asset_id)

    def get_min_order_size(self, asset_id: str) -> Optional[Decimal]:
        return self._min_order_sizes.get(asset_id)

    async def refresh_min_order_size(self, asset_id: str) -> Optional[Decimal]:
        """Refresh /book and return the market's minimum order size."""
        book = self._books.get(asset_id)
        if not book:
            return None
        await self.resync(asset_id)
        return self.get_min_order_size(asset_id)

    # ==================== WS Connection ====================

    async def _run(self) -> None:
        try:
            import websockets
        except ImportError:
            logger.error("websockets package not installed")
            return

        delay = 1
        while self._running:
            try:
                async with websockets.connect(
                    MARKET_WS_URL,
                    ping_interval=20,
                    ping_timeout=20,
                    max_size=2 * 1024 * 1024,
                ) as ws:
                    self._ws = ws
                    delay = 1
                    logger.info("[OrderBookWS] Connected")

                    if self._subscribed_on_wire:
                        await self._send_initial_subscribe(list(self._subscribed_on_wire))

                    async for raw in ws:
                        if not self._running:
                            break
                        self._dispatch(raw)

            except asyncio.CancelledError:
                raise
            except Exception as e:
                if not self._running:
                    break
                logger.warning("[OrderBookWS] Disconnected (%s), reconnecting in %ds", e, delay)
            finally:
                self._ws = None

            if self._running:
                await asyncio.sleep(delay)
                delay = min(delay * 2, 30)

    async def _send_initial_subscribe(self, asset_ids: list[str]) -> None:
        if not self._ws or not asset_ids:
            return
        msg = json.dumps({
            "assets_ids": asset_ids,
            "type": "market",
            "operation": "subscribe",
            "level": 2,
            "initial_dump": True,
            "custom_feature_enabled": True,
        })
        logger.debug("[OrderBookWS] Initial subscribe: %d assets", len(asset_ids))
        await self._ws.send(msg)

    async def _send_subscribe(self, asset_ids: list[str]) -> None:
        if not self._ws or not asset_ids:
            return
        msg = json.dumps({
            "assets_ids": asset_ids,
            "type": "market",
            "operation": "subscribe",
            "level": 2,
            "initial_dump": True,
            "custom_feature_enabled": True,
        })
        logger.debug("[OrderBookWS] Subscribe: %d assets", len(asset_ids))
        await self._ws.send(msg)

    async def _send_unsubscribe(self, asset_id: str) -> None:
        if not self._ws:
            return
        await self._ws.send(json.dumps({
            "assets_ids": [asset_id],
            "type": "market",
            "operation": "unsubscribe",
        }))

    # ==================== Message Dispatch ====================

    def _dispatch(self, raw: str) -> None:
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            return

        if isinstance(data, list):
            self._handle_snapshot(data)
            return

        event_type = data.get("event_type")

        if event_type == "price_change":
            self._handle_price_change(data)
        elif event_type == "tick_size_change":
            self._handle_tick_change(data)
        elif event_type == "book":
            asset_id = data.get("asset_id")
            if asset_id and asset_id in self._books:
                self._books[asset_id].apply_snapshot(
                    data.get("bids", []), data.get("asks", [])
                )
                self._cache_market_params(asset_id, data)
                self._notify_bbo(asset_id)

    def _handle_snapshot(self, data: list) -> None:
        for item in data:
            asset_id = item.get("asset_id")
            if asset_id and asset_id in self._books:
                self._books[asset_id].apply_snapshot(
                    item.get("bids", []), item.get("asks", [])
                )
                self._cache_market_params(asset_id, item)
                self._notify_bbo(asset_id)

    def _handle_price_change(self, data: dict) -> None:
        for change in data.get("price_changes", []):
            asset_id = change.get("asset_id")
            if not asset_id or asset_id not in self._books:
                continue

            book = self._books[asset_id]
            book.apply_change(change["price"], change["size"], change["side"])

            self._check_drift(asset_id, book, change)
            self._notify_bbo(asset_id)

    def _handle_tick_change(self, data: dict) -> None:
        asset_id = data.get("asset_id")
        tick_raw = data.get("tick_size") or data.get("new_tick_size")
        if not asset_id or tick_raw is None or asset_id not in self._asset_subs:
            return
        tick = Decimal(str(tick_raw))
        self._tick_sizes[asset_id] = tick
        for sub in self._asset_subs.get(asset_id, []):
            if sub.on_tick:
                asyncio.create_task(sub.on_tick(asset_id, tick))

    def _cache_market_params(self, asset_id: str, data: dict) -> None:
        tick_raw = (
            data.get("tick_size")
            or data.get("min_tick_size")
            or data.get("minimum_tick_size")
        )
        if tick_raw is not None:
            try:
                self._tick_sizes[asset_id] = Decimal(str(tick_raw))
            except Exception:
                logger.warning(
                    "[OrderBookWS] Invalid tick_size for %s: %s",
                    asset_id[:10], tick_raw,
                )

        min_order_raw = data.get("min_order_size") or data.get("minimum_order_size")
        if min_order_raw is not None:
            try:
                self._min_order_sizes[asset_id] = Decimal(str(min_order_raw))
            except Exception:
                logger.warning(
                    "[OrderBookWS] Invalid min_order_size for %s: %s",
                    asset_id[:10], min_order_raw,
                )

    # ==================== Drift Detection ====================

    _drift_cooldown: dict[str, float] = {}

    def _check_drift(self, asset_id: str, book: LocalOrderBook, change: dict) -> None:
        """用 price_change 中 server 端 BBO 对比本地 BBO。"""
        import time
        now = time.time()
        if now - self._drift_cooldown.get(asset_id, 0) < 30:
            return

        server_bid = change.get("bestBid") or change.get("best_bid")
        server_ask = change.get("bestAsk") or change.get("best_ask")
        if server_bid is None and server_ask is None:
            return

        drifted = False
        if server_bid is not None and book.best_bid is not None:
            if abs(book.best_bid - float(server_bid)) > 1e-9:
                drifted = True
        if server_ask is not None and book.best_ask is not None:
            if abs(book.best_ask - float(server_ask)) > 1e-9:
                drifted = True

        if drifted:
            self._drift_cooldown[asset_id] = now
            logger.warning(
                "[OrderBookWS] Drift detected: %s local=%s/%s server=%s/%s, resyncing",
                asset_id[:10], book.best_bid, book.best_ask, server_bid, server_ask,
            )
            asyncio.create_task(self.resync(asset_id))

    # ==================== Periodic Resync ====================

    async def _periodic_resync(self) -> None:
        """定时全量校验所有订阅中的 asset。加入随机抖动避免多实例同时请求。"""
        jitter = random.uniform(0, self._resync_interval * 0.2)
        await asyncio.sleep(jitter)
        while self._running:
            await asyncio.sleep(self._resync_interval + random.uniform(-10, 10))
            if not self._running:
                break
            for asset_id in list(self._subscribed_on_wire):
                book = self._books.get(asset_id)
                if book:
                    await self._fetch_and_replace(asset_id, book)
                    await asyncio.sleep(random.uniform(0.5, 2.0))

    async def _fetch_and_replace(self, asset_id: str, book: LocalOrderBook) -> bool:
        """从 REST API 拉取完整 orderbook 替换内存 book。"""
        if not self._http_session:
            return False
        try:
            async with self._http_session.get(
                f"{CLOB_REST_URL}/book",
                params={"token_id": asset_id},
                timeout=aiohttp.ClientTimeout(total=10),
            ) as resp:
                if resp.status != 200:
                    logger.warning("[OrderBookWS] REST /book %s returned %d", asset_id[:10], resp.status)
                    return False
                data = await resp.json()

            old_bid, old_ask = book.best_bid, book.best_ask
            book.apply_snapshot(data.get("bids", []), data.get("asks", []))
            self._cache_market_params(asset_id, data)
            new_bid, new_ask = book.best_bid, book.best_ask

            if old_bid != new_bid or old_ask != new_ask:
                logger.info(
                    "[OrderBookWS] Resync %s: bid %s→%s ask %s→%s",
                    asset_id[:10], old_bid, new_bid, old_ask, new_ask,
                )
                self._notify_bbo(asset_id)

            return True
        except Exception as e:
            logger.warning("[OrderBookWS] Resync failed for %s: %s", asset_id[:10], e)
            return False

    # ==================== Notify Subscribers ====================

    def _notify_bbo(self, asset_id: str) -> None:
        book = self._books.get(asset_id)
        if not book:
            return
        best_bid = Decimal(str(book.best_bid)) if book.best_bid is not None else None
        best_ask = Decimal(str(book.best_ask)) if book.best_ask is not None else None
        for sub in self._asset_subs.get(asset_id, []):
            if sub.on_bbo:
                asyncio.create_task(sub.on_bbo(asset_id, best_bid, best_ask))
