"""Sweep 策略风控 — 基于信号后订单簿参考 mid price 的跌幅止损。

设计：
  1. 收到信号后同步订单簿，使用同步后的 mid_price 作为参考值
  2. 通过 framework.orderbook_ws 持续接收 BBO 更新
  3. 实时 mid_price 跌至参考 mid 的 stop_loss_ratio（默认 60%）→ 触发风控
  4. 风控触发后回调策略执行平仓逻辑
"""
from __future__ import annotations

import asyncio
import logging
import time
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any, Awaitable, Callable, Optional

from framework.orderbook_ws import OrderBookWS
from framework.strategy_runtime.tick_size_service import TickSizeService

logger = logging.getLogger(__name__)

TICK_POLL_INTERVAL_S = 30


class SweepRiskMonitor:
    """单 token 的实时 mid-price 风控监控器。"""

    def __init__(
        self,
        orderbook_ws: OrderBookWS,
        stop_loss_ratio: Decimal = Decimal("0.60"),
        on_trigger: Optional[Callable[[], Awaitable[None]]] = None,
        on_tick_change: Optional[Callable[[Decimal, str], Awaitable[None]]] = None,
        tick_size_service: Optional[TickSizeService] = None,
    ) -> None:
        self._orderbook_ws = orderbook_ws
        self._stop_loss_ratio = stop_loss_ratio
        self._on_trigger = on_trigger
        self._on_tick_change = on_tick_change
        self._reference_mid: Optional[Decimal] = None
        self._token_id: Optional[str] = None
        self._sub_id: Optional[str] = None
        self._active = False
        self._triggered = False
        self._tick_poll_task: Optional[asyncio.Task] = None
        self._tick_check_task: Optional[asyncio.Task] = None
        self._tick_size_service = tick_size_service or TickSizeService()
        # Event loop-bound objects are created when monitoring starts, not in __init__.
        self._first_bbo_event: Optional[asyncio.Event] = None
        self._first_bbo_snapshot: Optional[dict[str, Any]] = None
        self._started_at_ms: Optional[int] = None
        self._trigger_bbo: Optional[dict[str, Any]] = None

    @property
    def is_active(self) -> bool:
        return self._active

    @property
    def is_triggered(self) -> bool:
        return self._triggered

    @property
    def reference_mid(self) -> Optional[Decimal]:
        return self._reference_mid

    @property
    def threshold(self) -> Optional[Decimal]:
        if self._reference_mid is None:
            return None
        return self._reference_mid * self._stop_loss_ratio

    @property
    def started_at_ms(self) -> Optional[int]:
        return self._started_at_ms

    @property
    def trigger_bbo(self) -> Optional[dict[str, Any]]:
        return self._trigger_bbo

    async def start(
        self,
        token_id: str,
        orderbook_snapshot: Optional[dict] = None,
    ) -> None:
        """Subscribe and bootstrap risk monitoring without depending on the signal payload.

        The signal source may not carry a market snapshot. In that case the
        order-book subscription becomes the source of truth and tick polling is
        still started even if the first BBO has not arrived yet.
        """
        self._token_id = token_id
        # The signal snapshot is optional legacy input. It may be stale by the
        # time this service receives the signal, so risk always derives its
        # reference price from the post-signal order-book sync below.
        self._reference_mid = None
        self._active = True
        self._triggered = False
        self._first_bbo_event = asyncio.Event()
        self._first_bbo_event.clear()
        self._first_bbo_snapshot = None
        self._started_at_ms = None

        try:
            self._sub_id = await self._orderbook_ws.subscribe(
                asset_id=token_id,
                on_bbo=self._on_bbo,
                on_tick=self._on_tick,
            )
            self._started_at_ms = int(time.time() * 1000)

            # Prime the local order book after the signal. ``start`` itself
            # runs in a separate task, so waiting for this REST sync never
            # delays entry.
            resync = getattr(self._orderbook_ws, "resync", None)
            resync_ok = True
            if callable(resync):
                resync_ok = await self._resync_orderbook(resync, token_id)

            # Capture only after the post-signal resync. This avoids using a
            # stale shared book that was populated before this signal arrived.
            if resync_ok:
                self._capture_first_bbo(token_id)
            else:
                logger.warning(
                    "Risk monitor waiting for a fresh WS BBO after resync failure: token=%s",
                    token_id[:10],
                )
            self._set_reference_from_snapshot(self._first_bbo_snapshot)

            logger.info(
                "Risk monitor started: token=%s ref_mid=%s threshold=%s ratio=%s",
                token_id[:10], self._reference_mid, self.threshold, self._stop_loss_ratio,
            )

            # 第一重保险：订阅后立即 HTTP 检查当前 tick_size
            self._tick_check_task = asyncio.create_task(self._check_tick_now())

            # 第二重保险：定时 HTTP 轮询 tick_size
            self._tick_poll_task = asyncio.create_task(self._tick_poll_loop())
        except BaseException:
            self._active = False
            sub_id = self._sub_id
            self._sub_id = None
            if sub_id:
                try:
                    await asyncio.shield(self._orderbook_ws.unsubscribe(sub_id))
                except BaseException:
                    logger.warning(
                        "Failed to clean up risk subscription after startup failure: token=%s",
                        token_id[:10],
                        exc_info=True,
                    )
            raise

    async def wait_for_first_bbo(self, timeout: float = 2.0) -> bool:
        """等待本次风控订阅收到首个市场 WS BBO。"""
        if self._first_bbo_event is None:
            return False
        if self._first_bbo_event.is_set():
            return True
        if not self._active:
            return False
        try:
            await asyncio.wait_for(self._first_bbo_event.wait(), timeout=timeout)
        except asyncio.TimeoutError:
            return False
        return True

    def first_bbo_snapshot(self) -> Optional[dict[str, Any]]:
        """返回首个市场 WS BBO 快照的副本。"""
        return dict(self._first_bbo_snapshot) if self._first_bbo_snapshot else None

    async def stop(self) -> None:
        """停止监控，取消订阅。"""
        self._active = False
        if self._tick_check_task and not self._tick_check_task.done():
            self._tick_check_task.cancel()
            try:
                await self._tick_check_task
            except asyncio.CancelledError:
                pass
        self._tick_check_task = None
        if self._tick_poll_task and not self._tick_poll_task.done():
            self._tick_poll_task.cancel()
            try:
                await self._tick_poll_task
            except asyncio.CancelledError:
                pass
        self._tick_poll_task = None
        if self._sub_id:
            await self._orderbook_ws.unsubscribe(self._sub_id)
            self._sub_id = None

    def status(self) -> dict:
        return {
            "active": self._active,
            "triggered": self._triggered,
            "token_id": self._token_id,
            "reference_mid": str(self._reference_mid) if self._reference_mid else None,
            "threshold": str(self.threshold) if self.threshold else None,
            "stop_loss_ratio": str(self._stop_loss_ratio),
        }

    # ==================== Callbacks ====================

    async def _on_bbo(self, asset_id: str, best_bid: Optional[Decimal], best_ask: Optional[Decimal]) -> None:
        if not self._active or self._triggered:
            return

        self._capture_first_bbo(asset_id, best_bid, best_ask)

        current_mid = self._mid_from_prices(best_bid, best_ask)
        if current_mid is None or current_mid <= 0:
            return

        if self._reference_mid is None:
            self._reference_mid = current_mid

        if self.threshold is None:
            return

        if current_mid <= self.threshold:
            logger.warning(
                "Risk triggered: mid=%s <= threshold=%s (ref=%s, ratio=%s)",
                current_mid, self.threshold, self._reference_mid, self._stop_loss_ratio,
            )
            self._triggered = True
            self._active = False
            self._trigger_bbo = {
                "best_bid": str(best_bid) if best_bid is not None else None,
                "best_ask": str(best_ask) if best_ask is not None else None,
                "mid": str(current_mid),
            }
            if self._on_trigger:
                asyncio.create_task(self._on_trigger())

    async def _on_tick(self, asset_id: str, tick_size: Decimal) -> None:
        if not self._active:
            return
        self._tick_size_service.invalidate(asset_id)
        if self._on_tick_change:
            asyncio.create_task(self._on_tick_change(tick_size, "market_ws"))

    # ==================== Tick HTTP 保险 ====================

    async def _check_tick_now(self) -> None:
        """立即通过 HTTP 检查当前 tick_size，如果已经是 0.001 则触发回调。"""
        if not self._active or not self._token_id:
            return
        try:
            tick = await self._tick_size_service.refresh(self._token_id)
            if tick == Decimal("0.001") and self._on_tick_change and self._active:
                logger.info(
                    "[RiskMonitor] HTTP tick check: token=%s tick already 0.001, firing callback",
                    self._token_id[:10],
                )
                asyncio.create_task(self._on_tick_change(tick, "tick_size_api"))
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.warning("[RiskMonitor] HTTP tick check failed: %s", e)

    async def _tick_poll_loop(self) -> None:
        """定时 HTTP 轮询 tick_size，作为 WS 事件的后备。"""
        try:
            while self._active:
                await asyncio.sleep(TICK_POLL_INTERVAL_S)
                if not self._active or not self._token_id:
                    break
                try:
                    tick = await self._tick_size_service.refresh(self._token_id)
                    if tick == Decimal("0.001") and self._on_tick_change and self._active:
                        logger.info(
                            "[RiskMonitor] Tick poll: token=%s tick=0.001, firing callback",
                            self._token_id[:10],
                        )
                        asyncio.create_task(self._on_tick_change(tick, "tick_size_api"))
                        return
                except asyncio.CancelledError:
                    raise
                except Exception as e:
                    logger.warning("[RiskMonitor] Tick poll error: %s", e)
        except asyncio.CancelledError:
            pass

    # ==================== Helpers ====================

    async def _resync_orderbook(self, resync, token_id: str) -> bool:
        try:
            result = await resync(token_id)
            return result is not False
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.warning("Risk monitor orderbook resync failed: token=%s", token_id[:10], exc_info=True)
            return False

    @staticmethod
    def _mid_from_prices(
        best_bid: Optional[Decimal], best_ask: Optional[Decimal],
    ) -> Optional[Decimal]:
        """Calculate the normalized midpoint using boundary prices for gaps."""
        bid = best_bid if best_bid is not None else Decimal("0")
        ask = best_ask if best_ask is not None else Decimal("1")
        return (bid + ask) / Decimal("2")

    def _set_reference_from_snapshot(self, snapshot: Optional[dict]) -> None:
        if self._reference_mid is not None or not snapshot:
            return
        mid = self._extract_mid_price(snapshot)
        if mid is not None and mid > 0:
            self._reference_mid = mid

    def _extract_mid_price(self, snapshot: dict) -> Optional[Decimal]:
        best_bid = self._extract_price(snapshot, "best_bid")
        best_ask = self._extract_price(snapshot, "best_ask")

        if best_bid is not None and best_ask is not None:
            return (best_bid + best_ask) / 2
        if best_bid is not None:
            return best_bid
        if best_ask is not None:
            return best_ask
        return None

    @staticmethod
    def _extract_price(snapshot: dict, key: str) -> Optional[Decimal]:
        val = snapshot.get(key)
        if val is None:
            return None
        if isinstance(val, dict):
            price = val.get("price")
            if price is not None:
                return Decimal(str(price))
            return None
        return Decimal(str(val))

    def _capture_first_bbo(
        self,
        asset_id: str,
        best_bid: Optional[Decimal] = None,
        best_ask: Optional[Decimal] = None,
    ) -> None:
        if self._first_bbo_snapshot is not None:
            return

        snapshot = None
        getter = getattr(self._orderbook_ws, "get_bbo_snapshot", None)
        if getter:
            snapshot = getter(asset_id)
        if snapshot and not any(
            snapshot.get(key) is not None
            for key in ("best_bid", "best_ask")
        ):
            snapshot = None
        if snapshot is None and (best_bid is not None or best_ask is not None):
            snapshot = {
                "best_bid": float(best_bid) if best_bid is not None else None,
                "best_bid_size": None,
                "best_ask": float(best_ask) if best_ask is not None else None,
                "best_ask_size": None,
            }
        if snapshot is None:
            return

        snapshot["captured_at_ms"] = int(time.time() * 1000)
        snapshot["utc"] = self._utc_str()
        self._first_bbo_snapshot = snapshot
        if self._first_bbo_event is not None:
            self._first_bbo_event.set()

    @staticmethod
    def _utc_str() -> str:
        now = datetime.now(timezone.utc)
        return now.strftime("%Y-%m-%dT%H:%M:%S.") + f"{now.microsecond // 1000:03d}Z"
