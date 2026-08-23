"""策略 2: Leader 信号下单 — BUY@0.99 → tick exit SELL@0.999。

流程:
  1. 收到指定 leader 的 BUY activity → 快速 BUY@0.99
  2. 启动风控 + 订单簿监听
  3. entry_wait_ms 后撤销未成交 BUY
  4. 无持仓 → 关闭；有持仓 → 等待 tick=0.001
  5. HTTP 校验通过 → SELL@0.999；风控触发 → 强制退出
"""
from __future__ import annotations

import asyncio
import logging
from decimal import Decimal
from typing import Optional

from strategy_runtime.interfaces import BaseStrategy, Signal, StrategyContext
from toolkit.execution.account_ledger import AccountLedger
from toolkit.market.tick_verifier import TickVerifier
from toolkit.risk.stop_loss import StopLossMonitor

logger = logging.getLogger(__name__)


class LeaderStrategy(BaseStrategy):
    """策略 2: Leader BUY → BUY@0.99 → tick exit SELL@0.999。"""

    SUBSCRIBED_SIGNALS = {"leader_buy"}

    async def start(self, ctx: StrategyContext) -> None:
        self.ctx = ctx
        self.state = "idle"
        self.token_id: Optional[str] = None
        self.position_shares = Decimal("0")
        self.entry_order_id: Optional[str] = None
        self._entry_timer: Optional[asyncio.Task] = None
        self._tick_verified = False

        self.ledger = AccountLedger(
            initial_cash=Decimal(ctx.config.get("initial_cash", "1000")),
        )
        self.risk = StopLossMonitor(
            ratio=Decimal(ctx.config.get("stop_loss_ratio", "0.60")),
            on_trigger=self._risk_exit,
        )
        self.tick_verifier = TickVerifier()

    async def on_signal(self, signal: Signal) -> None:
        if signal.signal_type == "leader_buy" and self.state == "idle":
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

        size_mode = self.ctx.config.get("entry_size_mode", "fixed")
        if size_mode == "max_available":
            actual_shares = self.ledger.max_buy_shares(Decimal("0.99"))
        else:
            fixed_shares = Decimal(self.ctx.config.get("fixed_entry_shares", "100"))
            max_shares = self.ledger.max_buy_shares(Decimal("0.99"))
            actual_shares = min(fixed_shares, max_shares)

        if actual_shares <= 0:
            logger.warning("No available cash, closing")
            self.state = "closed"
            return

        reserved = await self.ledger.reserve_buy(Decimal("0.99"), actual_shares)
        if not reserved:
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
            self.state = "closed"
            return

        if result.status == "filled":
            filled = Decimal(result.filled_size)
            await self.ledger.confirm_buy_fill(signal.token_id, Decimal("0.99"), filled)
            self.position_shares += filled
            self.risk.start(entry_price=Decimal("0.99"))

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

        if self.position_shares <= 0:
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
            result = await self.tick_verifier.verify(self.token_id)
            if result.confirmed:
                self._tick_verified = True
                await self._tick_exit()

    async def _tick_exit(self) -> None:
        if self.position_shares <= 0 or not self.token_id:
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

        if result.status == "filled":
            filled = Decimal(result.filled_size)
            await self.ledger.confirm_sell_fill(
                self.token_id, Decimal("0.999"), filled
            )
            self.position_shares -= filled

        self.state = "closed"

    async def _risk_exit(self) -> None:
        if self.state == "closed" or not self.token_id:
            return

        self.state = "risk_exiting"

        if self.position_shares <= 0:
            self.state = "closed"
            return

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

        self.state = "closed"
