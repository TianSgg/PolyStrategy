"""策略 2: Leader 信号下单 — 完整生命周期实现。

流程:
  1. 收到指定 leader 的 BUY activity → 快速 BUY@0.99 (固定份额或可用余额最大份额)
  2. 启动风控 + 订单簿监听
  3. entry_wait_ms 后撤销未成交 BUY
  4. 无持仓 → 关闭；有持仓 → 等待 tick=0.001
  5. HTTP 校验通过 → SELL@0.999；风控触发 → 强制退出
"""
from __future__ import annotations

import asyncio
import logging
from decimal import Decimal
from typing import Any, Dict, Optional

from strategy_execution.contracts import OrderRequest, Signal
from strategy_execution.enums import (
    CloseReason,
    ExecutionMode,
    OrderPurpose,
    OrderSide,
    OrderStatus,
    RunEventType,
    RunState,
)
from strategy_execution.execution.order_executor import generate_client_order_id
from strategy_execution.strategies.base import BaseStrategy

logger = logging.getLogger(__name__)


class LeaderStrategy(BaseStrategy):
    """策略 2: Leader BUY → BUY@0.99 → tick exit SELL@0.999。"""

    def __init__(self, **kwargs) -> None:
        super().__init__(**kwargs)
        self._entry_order_id: Optional[str] = None
        self._entry_clob_id: Optional[str] = None
        self._exit_order_id: Optional[str] = None
        self._entry_timer: Optional[asyncio.Task] = None
        self._tick_verified = False

    async def on_entry_signal(self, signal: Signal) -> None:
        """收到 leader BUY 信号，执行快速 BUY。"""
        # 计算份额
        size_mode = self.params.get("entry_size_mode", "fixed")
        if size_mode == "max_available":
            actual_shares = self.ledger.max_buy_shares(Decimal("0.99"))
        else:
            fixed_shares = Decimal(str(self.params.get("fixed_entry_shares", "100")))
            max_shares = self.ledger.max_buy_shares(Decimal("0.99"))
            actual_shares = min(fixed_shares, max_shares)

        if actual_shares <= 0:
            logger.warning("No available cash for leader run %s, closing", self.run.run_id)
            await self.run.transition(RunState.CLOSED, close_reason=CloseReason.ENTRY_CANCELED_NO_POSITION)
            return

        # 保留资金
        client_order_id = generate_client_order_id()
        reserved = await self.ledger.reserve_buy(
            run_id=self.run.run_id,
            order_id=client_order_id,
            token_id=self.run.token_id,
            price=Decimal("0.99"),
            shares=actual_shares,
        )
        if not reserved:
            await self.run.transition(RunState.CLOSED, close_reason=CloseReason.ENTRY_CANCELED_NO_POSITION)
            return

        await self.run.transition(RunState.ENTRY_WORKING)

        # 启动风控
        stop_loss = Decimal(str(self.params.get("stop_loss_ratio", "0.60")))
        self.risk_session = await self.risk_manager.create_session(
            run_machine=self.run,
            stop_loss_ratio=stop_loss,
            on_risk_triggered=self.on_risk_triggered,
            on_tick_candidate=self.on_tick_candidate,
        )

        # 快速下单
        request = OrderRequest(
            run_id=self.run.run_id,
            client_order_id=client_order_id,
            token_id=self.run.token_id,
            side=OrderSide.BUY,
            limit_price=Decimal("0.99"),
            size=actual_shares,
            purpose=OrderPurpose.ENTRY,
            execution_mode=ExecutionMode.FAST,
        )
        self._entry_order_id = client_order_id

        report = await self.executor.place_fast_buy(
            proxy_wallet=self.proxy_wallet,
            request=request,
        )

        await self.run.emit_event(RunEventType.ORDER_SENT, {
            "client_order_id": client_order_id,
            "side": "BUY",
            "price": "0.99",
            "size": str(actual_shares),
            "purpose": "entry",
            "size_mode": size_mode,
        })

        if report.status == OrderStatus.FAILED:
            await self.ledger.release_buy(
                run_id=self.run.run_id,
                order_id=client_order_id,
                token_id=self.run.token_id,
                price=Decimal("0.99"),
                unrealized_shares=actual_shares,
            )
            await self.run.transition(RunState.CLOSED, close_reason=CloseReason.ENTRY_CANCELED_NO_POSITION)
            return

        self._entry_clob_id = report.clob_order_id

        if report.status == OrderStatus.MATCHED and report.matched_size > 0:
            await self.ledger.confirm_buy_fill(
                run_id=self.run.run_id,
                order_id=client_order_id,
                token_id=self.run.token_id,
                price=Decimal("0.99"),
                filled_shares=report.matched_size,
                trade_id=f"immediate:{client_order_id}",
            )
            await self.run.record_fill(Decimal("0.99"), report.matched_size, is_buy=True)

        # 启动入场等待定时器
        entry_wait_ms = self.params.get("entry_wait_ms", 30000)
        self._entry_timer = asyncio.create_task(self._entry_timeout(entry_wait_ms / 1000.0))

    async def _entry_timeout(self, wait_sec: float) -> None:
        """入场等待超时后撤单。"""
        await asyncio.sleep(wait_sec)
        if self.run.is_closed:
            return

        if self._entry_clob_id:
            await self.executor.cancel_order(
                self.proxy_wallet,
                self._entry_clob_id,
                self._entry_order_id,
            )
            await self.run.emit_event(RunEventType.CANCEL_SENT, {
                "client_order_id": self._entry_order_id,
                "clob_order_id": self._entry_clob_id,
            })

        position = self.run.entry_shares - self.run.exited_shares
        if position <= 0:
            await self.run.transition(RunState.CLOSED, close_reason=CloseReason.ENTRY_CANCELED_NO_POSITION)
            await self.cleanup()
        else:
            await self.run.transition(RunState.EXIT_WORKING)

    async def on_tick_candidate(self) -> None:
        """WS 检测到 tick=0.001 — 执行 HTTP 校验后 SELL。"""
        if self._tick_verified or self.run.is_closed:
            return
        if self.run.state not in (RunState.EXIT_WORKING, RunState.ENTRY_WORKING):
            return

        result = await self.tick_verifier.verify(self.run.token_id, run_machine=self.run)
        if not result.confirmed:
            return

        self._tick_verified = True
        await self._execute_tick_exit()

    async def _execute_tick_exit(self) -> None:
        """执行 0.999 SELL 全部可卖份额。"""
        position = self.run.entry_shares - self.run.exited_shares
        if position <= 0:
            await self.run.transition(RunState.CLOSED, close_reason=CloseReason.TICK_EXIT)
            await self.cleanup()
            return

        client_order_id = generate_client_order_id()
        reserved = await self.ledger.reserve_sell(
            run_id=self.run.run_id,
            order_id=client_order_id,
            token_id=self.run.token_id,
            shares=position,
        )
        if not reserved:
            return

        self._exit_order_id = client_order_id
        request = OrderRequest(
            run_id=self.run.run_id,
            client_order_id=client_order_id,
            token_id=self.run.token_id,
            side=OrderSide.SELL,
            limit_price=Decimal("0.999"),
            size=position,
            purpose=OrderPurpose.EXIT_TICK,
            execution_mode=ExecutionMode.NORMAL,
        )

        report = await self.executor.place_normal_order(
            proxy_wallet=self.proxy_wallet,
            request=request,
        )

        await self.run.emit_event(RunEventType.ORDER_SENT, {
            "client_order_id": client_order_id,
            "side": "SELL",
            "price": "0.999",
            "size": str(position),
            "purpose": "exit_tick",
        })

        if report.status == OrderStatus.MATCHED and report.matched_size >= position:
            await self.ledger.confirm_sell_fill(
                run_id=self.run.run_id,
                order_id=client_order_id,
                token_id=self.run.token_id,
                price=Decimal("0.999"),
                filled_shares=report.matched_size,
                trade_id=f"immediate:{client_order_id}",
            )
            await self.run.record_fill(Decimal("0.999"), report.matched_size, is_buy=False)
            await self.run.transition(RunState.CLOSED, close_reason=CloseReason.TICK_EXIT)
            await self.cleanup()

    async def on_risk_triggered(self) -> None:
        """风控触发退出。"""
        if self.run.is_closed:
            return

        transitioned = await self.run.transition(RunState.RISK_EXITING, reason="stop_loss_triggered")
        if not transitioned:
            return

        position = self.run.entry_shares - self.run.exited_shares
        if position <= 0:
            await self.run.transition(RunState.CLOSED, close_reason=CloseReason.RISK_EXIT)
            await self.cleanup()
            return

        client_order_id = generate_client_order_id()
        await self.ledger.reserve_sell(
            run_id=self.run.run_id,
            order_id=client_order_id,
            token_id=self.run.token_id,
            shares=position,
        )

        request = OrderRequest(
            run_id=self.run.run_id,
            client_order_id=client_order_id,
            token_id=self.run.token_id,
            side=OrderSide.SELL,
            limit_price=Decimal("0.01"),
            size=position,
            purpose=OrderPurpose.RISK_EXIT,
            execution_mode=ExecutionMode.NORMAL,
        )

        report = await self.executor.place_normal_order(
            proxy_wallet=self.proxy_wallet,
            request=request,
        )

        if report.status == OrderStatus.MATCHED:
            await self.ledger.confirm_sell_fill(
                run_id=self.run.run_id,
                order_id=client_order_id,
                token_id=self.run.token_id,
                price=Decimal("0.01"),
                filled_shares=report.matched_size,
                trade_id=f"immediate:{client_order_id}",
            )
            await self.run.record_fill(Decimal("0.01"), report.matched_size, is_buy=False)

        await self.run.transition(RunState.CLOSED, close_reason=CloseReason.RISK_EXIT)
        await self.cleanup()

    async def on_leader_signal(self, signal: Signal) -> None:
        """策略 2 不处理额外 leader 信号（入场只响应第一个）。"""
        pass
