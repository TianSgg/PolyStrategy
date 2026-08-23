"""策略 3: Sweep + Leader 确认 — 试探 + 追加。

流程:
  1. 收到 sweep → 固定试探份额快速 BUY@0.99，进入 waiting_leader
  2. 确认窗口内收到匹配 leader BUY → 追加可用余额最大份额 BUY@0.99
  3. 等待 post_confirm_wait_ms 后撤销所有未成交 BUY
  4. 未确认超时 → 撤销 BUY，无持仓关闭 / 有持仓按退出模式处理
  5. 退出: tick=0.001 → SELL@0.999；风控触发 → 强制退出
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


class SweepLeaderStrategy(BaseStrategy):
    """策略 3: sweep 试探 + leader 确认追加。"""

    SUBSCRIBED_SIGNALS = {"sweep", "leader_buy"}

    async def start(self, ctx: StrategyContext) -> None:
        self.ctx = ctx
        self.state = "idle"
        self.token_id: Optional[str] = None
        self.position_shares = Decimal("0")
        self._probe_order_id: Optional[str] = None
        self._add_order_id: Optional[str] = None
        self._confirm_timer: Optional[asyncio.Task] = None
        self._post_confirm_timer: Optional[asyncio.Task] = None
        self._leader_confirmed = False
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
        if signal.signal_type == "sweep" and self.state == "idle":
            await self._enter_probe(signal)
        elif signal.signal_type == "leader_buy" and self.state == "waiting_leader":
            await self._on_leader_confirm(signal)
        elif signal.signal_type == "bbo_update":
            await self.risk.check(signal.payload)
            await self._check_tick_from_bbo(signal)

    async def stop(self) -> None:
        for timer in [self._confirm_timer, self._post_confirm_timer]:
            if timer and not timer.done():
                timer.cancel()
        for order_id in [self._probe_order_id, self._add_order_id]:
            if order_id:
                await self.ctx.executor.cancel_order(order_id)

    async def _enter_probe(self, signal: Signal) -> None:
        """收到 sweep — 固定试探份额 BUY。"""
        self.token_id = signal.token_id
        fixed_probe = Decimal(self.ctx.config.get("fixed_probe_shares", "50"))
        max_shares = self.ledger.max_buy_shares(Decimal("0.99"))
        actual_shares = min(fixed_probe, max_shares)

        if actual_shares <= 0:
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
        self._probe_order_id = result.order_id

        if result.status == "failed":
            await self.ledger.release_buy(Decimal("0.99"), actual_shares)
            self.state = "closed"
            return

        if result.status == "filled":
            filled = Decimal(result.filled_size)
            await self.ledger.confirm_buy_fill(signal.token_id, Decimal("0.99"), filled)
            self.position_shares += filled
            self.risk.start(entry_price=Decimal("0.99"))

        self.state = "waiting_leader"

        confirm_window_ms = int(self.ctx.config.get("leader_confirm_window_ms", 60000))
        self._confirm_timer = asyncio.create_task(
            self._confirm_timeout(confirm_window_ms / 1000.0)
        )

    async def _on_leader_confirm(self, signal: Signal) -> None:
        """Leader 确认 — 追加下单。"""
        if self._leader_confirmed or self.state != "waiting_leader":
            return

        self._leader_confirmed = True
        if self._confirm_timer and not self._confirm_timer.done():
            self._confirm_timer.cancel()

        self.state = "entry_working"

        add_shares = self.ledger.max_buy_shares(Decimal("0.99"))
        if add_shares <= 0:
            await self._start_exit()
            return

        reserved = await self.ledger.reserve_buy(Decimal("0.99"), add_shares)
        if not reserved:
            await self._start_exit()
            return

        result = await self.ctx.executor.place_order(
            token_id=self.token_id or signal.token_id,
            side="BUY",
            price="0.99",
            size=str(add_shares),
        )
        self._add_order_id = result.order_id

        if result.status == "failed":
            await self.ledger.release_buy(Decimal("0.99"), add_shares)
        elif result.status == "filled":
            filled = Decimal(result.filled_size)
            token = self.token_id or signal.token_id
            await self.ledger.confirm_buy_fill(token, Decimal("0.99"), filled)
            self.position_shares += filled

        post_wait_ms = int(self.ctx.config.get("post_confirm_wait_ms", 30000))
        self._post_confirm_timer = asyncio.create_task(
            self._post_confirm_timeout(post_wait_ms / 1000.0)
        )

    async def _confirm_timeout(self, wait_sec: float) -> None:
        """确认窗口超时 — 未收到 leader。"""
        await asyncio.sleep(wait_sec)
        if self._leader_confirmed or self.state == "closed":
            return

        await self._cancel_all_buys()

        if self.position_shares <= 0:
            self.state = "closed"
        else:
            await self._start_exit()

    async def _post_confirm_timeout(self, wait_sec: float) -> None:
        """追加后等待超时 — 撤销未成交并开始退出。"""
        await asyncio.sleep(wait_sec)
        if self.state == "closed":
            return
        await self._cancel_all_buys()
        await self._start_exit()

    async def _cancel_all_buys(self) -> None:
        for order_id in [self._probe_order_id, self._add_order_id]:
            if order_id:
                await self.ctx.executor.cancel_order(order_id)

    async def _start_exit(self) -> None:
        self.state = "exit_working"

    async def _check_tick_from_bbo(self, signal: Signal) -> None:
        if self._tick_verified or self.state != "exit_working":
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
        for timer in [self._confirm_timer, self._post_confirm_timer]:
            if timer and not timer.done():
                timer.cancel()

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
