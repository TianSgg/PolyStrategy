"""Sweep 策略风控 — 基于信号时刻 mid price 的跌幅止损。

设计：
  1. 收到信号时，记录订单簿快照的 mid_price = (best_bid + best_ask) / 2
  2. 通过 framework.orderbook_ws 持续接收 BBO 更新
  3. 实时 mid_price 跌至信号时刻的 stop_loss_ratio（默认 60%）→ 触发风控
  4. 风控触发后回调策略执行平仓逻辑
"""
from __future__ import annotations

import asyncio
import logging
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
        self._tick_size_service = tick_size_service or TickSizeService()

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

    async def start(self, token_id: str, orderbook_snapshot: dict) -> None:
        """激活风控：从信号时刻的订单簿快照计算参考 mid price，订阅 BBO。"""
        mid = self._extract_mid_price(orderbook_snapshot)
        if mid is None or mid <= 0:
            logger.warning("Cannot start risk monitor: invalid orderbook snapshot")
            return

        self._token_id = token_id
        self._reference_mid = mid
        self._active = True
        self._triggered = False

        self._sub_id = await self._orderbook_ws.subscribe(
            asset_id=token_id,
            on_bbo=self._on_bbo,
            on_tick=self._on_tick,
        )

        logger.info(
            "Risk monitor started: token=%s ref_mid=%s threshold=%s ratio=%s",
            token_id[:10], mid, self.threshold, self._stop_loss_ratio,
        )

        # 第一重保险：订阅后立即 HTTP 检查当前 tick_size
        asyncio.create_task(self._check_tick_now())

        # 第二重保险：定时 HTTP 轮询 tick_size
        self._tick_poll_task = asyncio.create_task(self._tick_poll_loop())

    async def stop(self) -> None:
        """停止监控，取消订阅。"""
        self._active = False
        if self._tick_poll_task and not self._tick_poll_task.done():
            self._tick_poll_task.cancel()
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

        bid = best_bid if best_bid is not None else Decimal("0")
        ask = best_ask if best_ask is not None else Decimal("1")
        current_mid = (bid + ask) / 2

        if self.threshold is None:
            return

        if current_mid <= self.threshold:
            logger.warning(
                "Risk triggered: mid=%s <= threshold=%s (ref=%s, ratio=%s)",
                current_mid, self.threshold, self._reference_mid, self._stop_loss_ratio,
            )
            self._triggered = True
            self._active = False
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
