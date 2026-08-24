"""策略 1: Sweep 信号下单 — BUY@0.99 → tick exit SELL@0.999。

流程:
  1. 收到合格 sweep → 快速 BUY@0.99 固定份额
  2. 启动风控 + 订单簿监听
  3. entry_wait_ms 后撤销未成交 BUY
  4. 无持仓 → 关闭；有持仓 → 等待 tick=0.001
  5. HTTP 校验通过 → SELL@0.999；风控触发 → 强制退出
"""
from __future__ import annotations

import asyncio
import logging
import time
from decimal import Decimal
from typing import Optional

from strategy_runtime.interfaces import BaseStrategy, Signal, StrategyContext
from toolkit.execution.account_ledger import AccountLedger
from toolkit.market.tick_verifier import TickVerifier
from toolkit.risk.stop_loss import StopLossMonitor

logger = logging.getLogger(__name__)


class SweepStrategy(BaseStrategy):
    """策略 1: 扫单信号 → BUY@0.99 → tick exit SELL@0.999。"""

    SUBSCRIBED_SIGNALS = {"sweep"}

    async def start(self, ctx: StrategyContext) -> None:
        self.ctx = ctx
        self.state = "idle"
        self.token_id: Optional[str] = None
        self.market_slug: Optional[str] = None
        self.position_shares = Decimal("0")
        self.entry_order_id: Optional[str] = None
        self._entry_timer: Optional[asyncio.Task] = None
        self._tick_verified = False
        self._event_start_ms: int = 0

        self.ledger = AccountLedger(
            initial_cash=Decimal(ctx.config.get("initial_cash", "1000")),
        )
        self.risk = StopLossMonitor(
            ratio=Decimal(ctx.config.get("stop_loss_ratio", "0.60")),
            on_trigger=self._risk_exit,
        )
        self.tick_verifier = TickVerifier()

    @property
    def _el(self):
        return self.ctx.event_logger

    async def on_signal(self, signal: Signal) -> None:
        if signal.signal_type == "sweep" and self.state == "idle":
            await self._enter(signal)
        elif signal.signal_type == "bbo_update":
            await self.risk.check(signal.payload)
            await self._check_tick_from_bbo(signal)

    async def stop(self) -> None:
        if self._entry_timer and not self._entry_timer.done():
            self._entry_timer.cancel()
        if self.entry_order_id:
            await self.ctx.executor.cancel_order(self.entry_order_id)

    async def _enter(self, signal: Signal) -> None:
        self.token_id = signal.token_id
        self.market_slug = signal.market_slug
        self._event_start_ms = int(time.time() * 1000)

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
            })

        fixed_shares = Decimal(self.ctx.config.get("fixed_entry_shares", "100"))
        max_shares = self.ledger.max_buy_shares(Decimal("0.99"))
        actual_shares = min(fixed_shares, max_shares)

        if actual_shares <= 0:
            logger.warning("No available cash, closing")
            if self._el:
                self._el.log_step("buy_failed", {
                    "reason": "no_cash",
                    "requested_size": str(fixed_shares),
                    "available_cash": str(self.ledger.available_cash),
                })
                self._close_event("buy_failed")
            self.state = "closed"
            return

        reserved = await self.ledger.reserve_buy(Decimal("0.99"), actual_shares)
        if not reserved:
            if self._el:
                self._el.log_step("buy_failed", {"reason": "reserve_failed"})
                self._close_event("buy_failed")
            self.state = "closed"
            return

        self.state = "entry_working"

        result = await self.ctx.executor.place_order(
            token_id=signal.token_id,
            side="BUY",
            price="0.99",
            size=str(actual_shares),
        )
        self.entry_order_id = result.order_id

        if result.status == "failed":
            await self.ledger.release_buy(Decimal("0.99"), actual_shares)
            if self._el:
                self._el.log_step("buy_failed", {"reason": "api_error", "requested_size": str(actual_shares)})
                self._close_event("buy_failed")
            self.state = "closed"
            return

        if self._el:
            self._el.log_step("buy_placed", {
                "order_id": result.order_id,
                "price": "0.99",
                "size": str(actual_shares),
            })

        if result.status == "filled":
            filled = Decimal(result.filled_size)
            await self.ledger.confirm_buy_fill(signal.token_id, Decimal("0.99"), filled)
            self.position_shares += filled
            self.risk.start(entry_price=Decimal("0.99"))
            if self._el:
                self._el.log_step("buy_filled", {
                    "order_id": result.order_id,
                    "filled_size": str(filled),
                    "total_position": str(self.position_shares),
                    "fill_price": "0.99",
                })
                self._el.log_step("risk_started", {
                    "entry_price": "0.99",
                    "stop_loss_ratio": self.ctx.config.get("stop_loss_ratio", "0.60"),
                })

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

        if self._el:
            self._el.log_step("entry_timeout", {
                "wait_ms": int(wait_sec * 1000),
                "order_id": self.entry_order_id,
                "final_position": str(self.position_shares),
            })

        if self.position_shares <= 0:
            self._close_event("timeout_no_fill")
            self.state = "closed"
        else:
            self.state = "exit_working"

    async def _check_tick_from_bbo(self, signal: Signal) -> None:
        if self._tick_verified or self.state not in ("entry_working", "exit_working"):
            return
        if not self.token_id:
            return

        tick_size = signal.payload.get("tick_size")
        if tick_size and Decimal(str(tick_size)) == Decimal("0.001"):
            if self._el:
                self._el.log_step("tick_detected", {"tick_size": "0.001", "source": "bbo_update"})

            result = await self.tick_verifier.verify(self.token_id)
            if result.confirmed:
                self._tick_verified = True
                if self._el:
                    self._el.log_step("tick_verified", {
                        "token_id": self.token_id,
                        "confirmed": True,
                    })
                await self._tick_exit()

    async def _tick_exit(self) -> None:
        if self.position_shares <= 0 or not self.token_id:
            self._close_event("tick_exit")
            self.state = "closed"
            return

        reserved = await self.ledger.reserve_sell(self.token_id, self.position_shares)
        if not reserved:
            return

        result = await self.ctx.executor.place_order(
            token_id=self.token_id,
            side="SELL",
            price="0.999",
            size=str(self.position_shares),
        )

        if self._el:
            self._el.log_step("sell_placed", {
                "order_id": result.order_id,
                "price": "0.999",
                "size": str(self.position_shares),
                "reason": "tick_exit",
            })

        if result.status == "filled":
            filled = Decimal(result.filled_size)
            await self.ledger.confirm_sell_fill(
                self.token_id, Decimal("0.999"), filled
            )
            self.position_shares -= filled
            if self._el:
                self._el.log_step("sell_filled", {
                    "order_id": result.order_id,
                    "filled_size": str(filled),
                    "fill_price": "0.999",
                    "remaining_position": str(self.position_shares),
                })

        self._close_event("tick_exit")
        self.state = "closed"

    async def _risk_exit(self) -> None:
        if self.state == "closed" or not self.token_id:
            return

        self.state = "risk_exiting"

        if self.position_shares <= 0:
            self._close_event("stop_loss")
            self.state = "closed"
            return

        if self._el:
            self._el.log_step("stop_loss_triggered", {
                "entry_price": "0.99",
                "threshold": self.ctx.config.get("stop_loss_ratio", "0.60"),
                "sell_price": "0.01",
                "sell_size": str(self.position_shares),
            })

        await self.ledger.reserve_sell(self.token_id, self.position_shares)
        result = await self.ctx.executor.place_order(
            token_id=self.token_id,
            side="SELL",
            price="0.01",
            size=str(self.position_shares),
        )

        if result.status == "filled":
            filled = Decimal(result.filled_size)
            await self.ledger.confirm_sell_fill(self.token_id, Decimal("0.01"), filled)
            self.position_shares -= filled

        self._close_event("stop_loss")
        self.state = "closed"

    def _close_event(self, reason: str) -> None:
        if not self._el:
            return
        duration_ms = int(time.time() * 1000) - self._event_start_ms if self._event_start_ms else 0
        self._el.log_step("event_closed", {
            "reason": reason,
            "total_position": str(self.position_shares),
            "duration_ms": duration_ms,
        })
        self._el.end_event()
