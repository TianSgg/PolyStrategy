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
from typing import Optional

from framework.strategy_runtime.interfaces import BaseStrategy, Signal, StrategyContext
from framework.strategy_runtime.toolkit.market.tick_verifier import TickVerifier
from framework.strategy_runtime.toolkit.risk.stop_loss import StopLossMonitor

logger = logging.getLogger(__name__)


class SweepStrategy(BaseStrategy):
    """策略 1: 扫单信号 → BUY@0.99 → tick exit SELL@0.999。"""

    SUBSCRIBED_SIGNALS = {"sweep"}

    async def start(self, ctx: StrategyContext) -> None:
        self.ctx = ctx
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

        self.risk = StopLossMonitor(
            ratio=Decimal(ctx.config.get("stop_loss_ratio", "0.60")),
            on_trigger=self._risk_exit,
        )
        self.tick_verifier = TickVerifier()

    async def drain(self) -> None:
        self._draining = True

    @property
    def is_idle(self) -> bool:
        return self.state in ("idle", "closed")

    @property
    def _el(self):
        return self.ctx.event_logger

    # ==================== Signal Dispatch ====================

    async def on_signal(self, signal: Signal) -> None:
        if signal.signal_type == "sweep" and self.state == "idle" and not self._draining:
            await self._enter(signal)
        elif signal.signal_type == "bbo_update" and self.state not in ("idle", "closed"):
            await self.risk.check(signal.payload)
            await self._check_tick_from_bbo(signal)

    # ==================== Lifecycle ====================

    async def stop(self) -> None:
        if self._entry_timer and not self._entry_timer.done():
            self._entry_timer.cancel()

    async def force_exit(self, reason: str = "config_disabled") -> None:
        """强制退出：撤买单 + 平仓卖出。由容器在配置变更/禁用时调用。"""
        if self.state in ("idle", "closed"):
            return

        if self._entry_timer and not self._entry_timer.done():
            self._entry_timer.cancel()

        if self._el:
            self._el.log_step("force_exit", {
                "reason": reason,
                "trigger": "user",
                "state_at_exit": self.state,
                "position_shares": str(self.position_shares),
            }, phase="exit_force")

        if self.entry_order_id:
            cancelled = await self.ctx.executor.cancel_order(self.entry_order_id)
            if self._el:
                self._el.log_step("buy_cancelled", {
                    "order_id": self.entry_order_id,
                    "success": cancelled,
                }, phase="exit_force")
            self.entry_order_id = None

        if self.position_shares > 0 and self.token_id:
            sell_price = Decimal("1") - self._tick_size
            result = await self.ctx.executor.place_order(
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
            }, phase="entry")

        fixed_shares = Decimal(self.ctx.config.get("fixed_entry_shares", "100"))
        buy_price = Decimal("0.99")
        available = self.ctx.executor.available_cash
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

        result = await self.ctx.executor.place_order(
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
            self.risk.start(entry_price=buy_price)
            if self._el:
                self._el.log_step("buy_filled", {
                    "order_id": result.order_id,
                    "filled_size": str(filled),
                    "total_position": str(self.position_shares),
                    "fill_price": str(buy_price),
                }, phase="entry")
                self._el.log_step("risk_started", {
                    "entry_price": str(buy_price),
                    "stop_loss_ratio": self.ctx.config.get("stop_loss_ratio", "0.60"),
                }, phase="monitor")

        entry_wait_ms = int(self.ctx.config.get("entry_wait_ms", 30000))
        self._entry_timer = asyncio.create_task(
            self._entry_timeout(entry_wait_ms / 1000.0)
        )

    async def _entry_timeout(self, wait_sec: float) -> None:
        await asyncio.sleep(wait_sec)
        if self.state == "closed":
            return

        if self.entry_order_id:
            await self.ctx.executor.cancel_order(self.entry_order_id)
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

    async def _check_tick_from_bbo(self, signal: Signal) -> None:
        if self._tick_verified or self.state not in ("entry_working", "exit_working"):
            return
        if not self.token_id:
            return

        tick_size = signal.payload.get("tick_size")
        if tick_size and Decimal(str(tick_size)) == Decimal("0.001"):
            self._tick_size = Decimal("0.001")
            if self._el:
                self._el.log_step("tick_detected", {
                    "tick_size": "0.001",
                    "source": "bbo_update",
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
        if self.position_shares <= 0 or not self.token_id:
            self._close_event("tick_exit", phase="exit")
            self.state = "closed"
            return

        sell_price = Decimal("1") - self._tick_size

        result = await self.ctx.executor.place_order(
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
        if self.state == "closed" or not self.token_id:
            return

        self.state = "risk_exiting"

        if self.position_shares <= 0:
            self._close_event("stop_loss", phase="exit_risk")
            self.state = "closed"
            return

        if self._el:
            self._el.log_step("stop_loss_triggered", {
                "entry_price": "0.99",
                "threshold": self.ctx.config.get("stop_loss_ratio", "0.60"),
                "sell_price": "0.01",
                "sell_size": str(self.position_shares),
            }, phase="exit_risk")

        result = await self.ctx.executor.place_order(
            token_id=self.token_id,
            side="SELL",
            price="0.01",
            size=str(self.position_shares),
            check_balance=False,
        )

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
