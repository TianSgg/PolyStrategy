"""策略 1: Sweep 信号下单 — BUY@0.99 → tick exit SELL@0.999。

流程:
  1. 收到合格 sweep → 快速 BUY@0.99 固定份额
  2. 启动风控 + 订单簿监听
  3. entry_wait_ms 后撤销未成交 BUY
  4. 无持仓 → 关闭；有持仓 → 等待 tick=0.001
  5. HTTP 校验通过 → SELL@0.999；风控触发 → 强制退出

强制退出（配置变更/禁用）:
  - 撤销所有未成交买单
  - 已持份额按 1 - tick_size 的价格挂卖单
"""
from __future__ import annotations

import asyncio
import logging
import time
from decimal import Decimal
from typing import Any, Optional

from framework.strategy_runtime.interfaces import Signal
from framework.strategy_runtime.tick_verifier import TickVerifier
from strategy_weather_sweep.internal.risk_monitor import SweepRiskMonitor

logger = logging.getLogger(__name__)


class SweepStrategy:
    """策略 1: 扫单信号 → BUY@0.99 → tick exit SELL@0.999。"""

    async def start_with_tools(
        self,
        config: dict[str, Any],
        executor: Any,
        orderbook_ws: Any,
        event_logger: Any = None,
        proxy_wallet: str = "",
    ) -> None:
        """使用独立工具初始化策略。"""
        self._config = config
        self._executor = executor
        self._orderbook_ws = orderbook_ws
        self._el = event_logger
        self._proxy_wallet = proxy_wallet

        self.state = "idle"
        self._draining = False
        self.token_id: Optional[str] = None
        self.market_slug: Optional[str] = None
        self.position_shares = Decimal("0")
        self.entry_order_id: Optional[str] = None
        self._entry_timer: Optional[asyncio.Task] = None
        self._tick_verified = False
        self._tick_size = Decimal("0.01")
        self._event_start_ms: int = 0

        self.risk = SweepRiskMonitor(
            orderbook_ws=orderbook_ws,
            stop_loss_ratio=Decimal(config.get("stop_loss_ratio", "0.60")),
            on_trigger=self._risk_exit,
            on_tick_change=self._on_tick_change,
        )
        self.tick_verifier = TickVerifier()

    async def drain(self) -> None:
        self._draining = True

    @property
    def is_idle(self) -> bool:
        return self.state in ("idle", "closed")


    # ==================== Signal Dispatch ====================

    async def on_signal(self, signal: Signal) -> None:
        if signal.signal_type == "sweep" and self.state == "idle" and not self._draining:
            if self._should_accept_signal(signal):
                await self._enter(signal)

    def _should_accept_signal(self, signal: Signal) -> bool:
        """根据配置过滤信号：outcome、来源、阈值。"""
        payload = signal.payload
        cfg = self._config

        outcome_filter = cfg.get("sweep_outcome_filter", "no")
        if outcome_filter != "all" and payload.get("outcome", "") != outcome_filter:
            return False

        source_filter = cfg.get("signal_source_filter", "all")
        if source_filter == "main" and not payload.get("is_from_main", True):
            return False
        if source_filter == "next" and payload.get("is_from_main", True):
            return False

        threshold_filter = cfg.get("signal_threshold_filter", "all")
        if threshold_filter != "all":
            reason = payload.get("reason", "")
            if f"through_{threshold_filter}_cleared" not in reason:
                return False

        return True

    # ==================== Lifecycle ====================

    async def stop(self) -> None:
        if self._entry_timer and not self._entry_timer.done():
            self._entry_timer.cancel()
        await self.risk.stop()

    async def force_exit(self, reason: str = "config_disabled") -> None:
        """强制退出：撤买单 + 平仓卖出。由容器在配置变更/禁用时调用。"""
        if self.state in ("idle", "closed"):
            return

        if self._entry_timer and not self._entry_timer.done():
            self._entry_timer.cancel()
        await self.risk.stop()

        if self._el:
            self._el.log_step("force_exit", {
                "reason": reason,
                "trigger": "user",
                "state_at_exit": self.state,
                "position_shares": str(self.position_shares),
            }, phase="exit_force")

        if self.entry_order_id:
            cancelled = await self._executor.cancel_order(self.entry_order_id)
            if self._el:
                self._el.log_step("buy_cancelled", {
                    "order_id": self.entry_order_id,
                    "success": cancelled,
                }, phase="exit_force")
            self.entry_order_id = None

        if self.position_shares > 0 and self.token_id:
            sell_price = Decimal("1") - self._tick_size
            result = await self._executor.place_order(
                token_id=self.token_id,
                side="SELL",
                price=str(sell_price),
                size=str(self.position_shares),
                check_balance=False,
            )
            if self._el:
                self._el.log_step("force_sell_placed", {
                    "order_id": result.order_id,
                    "price": str(sell_price),
                    "size": str(self.position_shares),
                    "tick_size": str(self._tick_size),
                }, phase="exit_force")

            if result.status == "filled":
                filled = Decimal(result.filled_size)
                self.position_shares -= filled
                if self._el:
                    self._el.log_step("force_sell_filled", {
                        "order_id": result.order_id,
                        "filled_size": str(filled),
                        "fill_price": str(sell_price),
                        "remaining_position": str(self.position_shares),
                    }, phase="exit_force")

        self._close_event("force_exit", phase="exit_force")
        self.state = "closed"

    # ==================== Entry ====================

    async def _enter(self, signal: Signal) -> None:
        self.token_id = signal.token_id
        self.market_slug = signal.market_slug
        self._event_start_ms = int(time.time() * 1000)
        self._tick_size = Decimal("0.01")

        orderbook_snapshot = signal.payload.get("orderbook_snapshot", {})
        await self.risk.start(token_id=signal.token_id, orderbook_snapshot=orderbook_snapshot)

        if self._el:
            self._el.start_event(
                signal_id=signal.signal_id,
                token_id=signal.token_id,
                market_slug=signal.market_slug,
                event_slug=signal.payload.get("event_slug"),
            )
            self._el.log_step("signal_received", {
                "signal_id": signal.signal_id,
                "token_id": signal.token_id,
                "market_slug": signal.market_slug,
                "event_slug": signal.payload.get("event_slug"),
                "city": signal.payload.get("city"),
                "direction": signal.payload.get("direction"),
                "risk_ref_mid": str(self.risk.reference_mid),
                "risk_threshold": str(self.risk.threshold),
            }, phase="entry")

        fixed_shares = Decimal(self._config.get("fixed_entry_shares", "100"))
        buy_price = Decimal("0.99")
        available = self._executor.available_cash
        max_shares = int(available / buy_price) if buy_price > 0 else 0
        actual_shares = min(fixed_shares, Decimal(str(max_shares)))

        if actual_shares <= 0:
            logger.warning("No available cash, closing")
            if self._el:
                self._el.log_step("buy_failed", {
                    "reason": "no_cash",
                    "requested_size": str(fixed_shares),
                    "available_cash": str(available),
                }, phase="entry")
                self._close_event("buy_failed", phase="entry")
            self.state = "closed"
            return

        self.state = "entry_working"

        result = await self._executor.place_order(
            token_id=signal.token_id,
            side="BUY",
            price=str(buy_price),
            size=str(actual_shares),
        )
        self.entry_order_id = result.order_id

        if result.status in ("failed", "insufficient_balance"):
            if self._el:
                self._el.log_step("buy_failed", {
                    "reason": result.status,
                    "requested_size": str(actual_shares),
                }, phase="entry")
                self._close_event("buy_failed", phase="entry")
            self.state = "closed"
            return

        if self._el:
            self._el.log_step("buy_placed", {
                "order_id": result.order_id,
                "price": str(buy_price),
                "size": str(actual_shares),
            }, phase="entry")

        if result.status == "filled":
            filled = Decimal(result.filled_size)
            self.position_shares += filled
            self.entry_order_id = None
            if self._el:
                self._el.log_step("buy_filled", {
                    "order_id": result.order_id,
                    "filled_size": str(filled),
                    "total_position": str(self.position_shares),
                    "fill_price": str(buy_price),
                }, phase="entry")

        entry_wait_ms = int(self._config.get("entry_wait_ms", 30000))
        self._entry_timer = asyncio.create_task(
            self._entry_timeout(entry_wait_ms / 1000.0)
        )

    async def _entry_timeout(self, wait_sec: float) -> None:
        await asyncio.sleep(wait_sec)
        if self.state == "closed":
            return

        if self.entry_order_id:
            await self._executor.cancel_order(self.entry_order_id)
            self.entry_order_id = None

        if self._el:
            self._el.log_step("entry_timeout", {
                "wait_ms": int(wait_sec * 1000),
                "final_position": str(self.position_shares),
            }, phase="entry")

        if self.position_shares <= 0:
            self._close_event("timeout_no_fill", phase="entry")
            self.state = "closed"
        else:
            self.state = "exit_working"

    # ==================== Monitor & Exit ====================

    async def _on_tick_change(self, new_tick: Decimal) -> None:
        """风控 WS 检测到 tick_size 变更回调。"""
        if self._tick_verified or self.state not in ("entry_working", "exit_working"):
            return
        if not self.token_id:
            return

        if new_tick == Decimal("0.001"):
            self._tick_size = Decimal("0.001")
            if self._el:
                self._el.log_step("tick_detected", {
                    "tick_size": "0.001",
                    "source": "risk_ws",
                }, phase="monitor")

            result = await self.tick_verifier.verify(self.token_id)
            if result.confirmed:
                self._tick_verified = True
                if self._el:
                    self._el.log_step("tick_verified", {
                        "token_id": self.token_id,
                        "confirmed": True,
                    }, phase="monitor")
                await self._tick_exit()

    async def _tick_exit(self) -> None:
        await self.risk.stop()

        if self.position_shares <= 0 or not self.token_id:
            self._close_event("tick_exit", phase="exit")
            self.state = "closed"
            return

        sell_price = Decimal("1") - self._tick_size

        result = await self._executor.place_order(
            token_id=self.token_id,
            side="SELL",
            price=str(sell_price),
            size=str(self.position_shares),
            check_balance=False,
        )

        if self._el:
            self._el.log_step("sell_placed", {
                "order_id": result.order_id,
                "price": str(sell_price),
                "size": str(self.position_shares),
                "reason": "tick_exit",
            }, phase="exit")

        if result.status == "filled":
            filled = Decimal(result.filled_size)
            self.position_shares -= filled
            if self._el:
                self._el.log_step("sell_filled", {
                    "order_id": result.order_id,
                    "filled_size": str(filled),
                    "fill_price": str(sell_price),
                    "remaining_position": str(self.position_shares),
                }, phase="exit")

        self._close_event("tick_exit", phase="exit")
        self.state = "closed"

    # ==================== Risk Exit ====================

    async def _risk_exit(self) -> None:
        """风控触发：撤销挂单 + 强行平仓所有持仓。"""
        if self.state == "closed" or not self.token_id:
            return

        self.state = "risk_exiting"

        if self._entry_timer and not self._entry_timer.done():
            self._entry_timer.cancel()

        if self._el:
            self._el.log_step("risk_triggered", {
                "reference_mid": str(self.risk.reference_mid),
                "threshold": str(self.risk.threshold),
                "stop_loss_ratio": self._config.get("stop_loss_ratio", "0.50"),
                "state_at_trigger": self.state,
                "pending_buy": self.entry_order_id,
                "position_shares": str(self.position_shares),
            }, phase="exit_risk")

        if self.entry_order_id:
            cancelled = await self._executor.cancel_order(self.entry_order_id)
            if self._el:
                self._el.log_step("risk_cancel_buy", {
                    "order_id": self.entry_order_id,
                    "success": cancelled,
                }, phase="exit_risk")
            self.entry_order_id = None

        if self.position_shares > 0:
            result = await self._executor.place_order(
                token_id=self.token_id,
                side="SELL",
                price="0.01",
                size=str(self.position_shares),
                check_balance=False,
            )
            if self._el:
                self._el.log_step("risk_force_sell", {
                    "order_id": result.order_id,
                    "price": "0.01",
                    "size": str(self.position_shares),
                    "status": result.status,
                }, phase="exit_risk")
            if result.status == "filled":
                filled = Decimal(result.filled_size)
                self.position_shares -= filled

        self._close_event("stop_loss", phase="exit_risk")
        self.state = "closed"

    # ==================== Helpers ====================

    def _close_event(self, reason: str, phase: str = "exit") -> None:
        if not self._el:
            return
        duration_ms = int(time.time() * 1000) - self._event_start_ms if self._event_start_ms else 0
        self._el.log_step("event_closed", {
            "reason": reason,
            "total_position": str(self.position_shares),
            "duration_ms": duration_ms,
        }, phase=phase)
        self._el.end_event()
