"""策略 3: Sweep + Leader 确认 — 完整生命周期实现。

流程:
  1. 收到 sweep → 固定试探份额快速 BUY@0.99，进入 WAITING_LEADER
  2. 确认窗口内收到匹配 leader BUY → 追加可用余额最大份额 BUY@0.99
  3. 等待 post_confirm_wait_ms 后撤销所有未成交 BUY
  4. 未确认超时 → 撤销 BUY，无持仓关闭 / 有持仓按退出模式处理
  5. 退出模式: risk_tick_exit (0.999+风控) 或 sell_099_then_risk (先0.99卖，剩余风控)
"""
from __future__ import annotations

import asyncio
import logging
from decimal import Decimal
from typing import Any, Dict, Optional

from signal_leader_activity.types import LeaderBuySignal
from signal_weather_orderbook.types import WeatherSweepSignal
from strategy_execution.contracts import OrderRequest
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
from strategy_execution.base import BaseStrategy

logger = logging.getLogger(__name__)


class SweepLeaderStrategy(BaseStrategy):
    """策略 3: sweep 试探 + leader 确认追加。"""

    def __init__(self, **kwargs) -> None:
        super().__init__(**kwargs)
        self._probe_order_id: Optional[str] = None
        self._probe_clob_id: Optional[str] = None
        self._add_order_id: Optional[str] = None
        self._add_clob_id: Optional[str] = None
        self._exit_order_id: Optional[str] = None
        self._confirm_timer: Optional[asyncio.Task] = None
        self._post_confirm_timer: Optional[asyncio.Task] = None
        self._leader_confirmed = False
        self._tick_verified = False

    async def on_entry_signal(self, signal: WeatherSweepSignal) -> None:
        """收到 sweep 信号 — 固定试探份额 BUY。"""
        fixed_probe = Decimal(str(self.params.get("fixed_probe_shares", "50")))
        max_shares = self.ledger.max_buy_shares(Decimal("0.99"))
        actual_shares = min(fixed_probe, max_shares)

        if actual_shares <= 0:
            await self.run.transition(RunState.CLOSED, close_reason=CloseReason.ENTRY_CANCELED_NO_POSITION)
            return

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
        self._probe_order_id = client_order_id

        report = await self.executor.place_fast_buy(
            proxy_wallet=self.proxy_wallet,
            request=request,
        )

        await self.run.emit_event(RunEventType.ORDER_SENT, {
            "client_order_id": client_order_id,
            "side": "BUY",
            "price": "0.99",
            "size": str(actual_shares),
            "purpose": "entry_probe",
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

        self._probe_clob_id = report.clob_order_id

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

        # 进入 WAITING_LEADER 状态
        await self.run.transition(RunState.WAITING_LEADER)

        # 启动确认窗口超时定时器
        confirm_window_ms = self.params.get("leader_confirm_window_ms", 60000)
        self._confirm_timer = asyncio.create_task(
            self._confirm_timeout(confirm_window_ms / 1000.0)
        )

    async def on_leader_signal(self, signal: LeaderBuySignal) -> None:
        """收到 leader 确认信号 — 追加下单。"""
        if self._leader_confirmed or self.run.is_closed:
            return
        if self.run.state != RunState.WAITING_LEADER:
            return

        self._leader_confirmed = True

        # 取消确认超时定时器
        if self._confirm_timer and not self._confirm_timer.done():
            self._confirm_timer.cancel()

        await self.run.emit_event(RunEventType.LEADER_CONFIRMED, {
            "leader_event_id": signal.event_id,
            "leader_proxy_wallet": signal.leader_proxy_wallet,
        })

        # 回到 ENTRY_WORKING 执行追加
        await self.run.transition(RunState.ENTRY_WORKING, reason="leader_confirmed")

        # 追加：用可用余额最大份额
        add_shares = self.ledger.max_buy_shares(Decimal("0.99"))
        if add_shares <= 0:
            logger.info("No cash for add after leader confirm, run %s", self.run.run_id)
            await self._start_exit()
            return

        client_order_id = generate_client_order_id()
        reserved = await self.ledger.reserve_buy(
            run_id=self.run.run_id,
            order_id=client_order_id,
            token_id=self.run.token_id,
            price=Decimal("0.99"),
            shares=add_shares,
        )
        if not reserved:
            await self._start_exit()
            return

        request = OrderRequest(
            run_id=self.run.run_id,
            client_order_id=client_order_id,
            token_id=self.run.token_id,
            side=OrderSide.BUY,
            limit_price=Decimal("0.99"),
            size=add_shares,
            purpose=OrderPurpose.ADD,
            execution_mode=ExecutionMode.FAST,
        )
        self._add_order_id = client_order_id

        report = await self.executor.place_fast_buy(
            proxy_wallet=self.proxy_wallet,
            request=request,
        )

        await self.run.emit_event(RunEventType.ORDER_SENT, {
            "client_order_id": client_order_id,
            "side": "BUY",
            "price": "0.99",
            "size": str(add_shares),
            "purpose": "add",
        })

        if report.status == OrderStatus.FAILED:
            await self.ledger.release_buy(
                run_id=self.run.run_id,
                order_id=client_order_id,
                token_id=self.run.token_id,
                price=Decimal("0.99"),
                unrealized_shares=add_shares,
            )
        else:
            self._add_clob_id = report.clob_order_id
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

        # 启动 post_confirm 等待
        post_wait_ms = self.params.get("post_confirm_wait_ms", 30000)
        self._post_confirm_timer = asyncio.create_task(
            self._post_confirm_timeout(post_wait_ms / 1000.0)
        )

    async def _confirm_timeout(self, wait_sec: float) -> None:
        """确认窗口超时 — 未收到 leader。"""
        await asyncio.sleep(wait_sec)
        if self._leader_confirmed or self.run.is_closed:
            return

        await self.run.emit_event(RunEventType.LEADER_TIMEOUT, {
            "window_sec": wait_sec,
        })

        # 撤销所有未成交 BUY
        await self._cancel_all_buys()

        position = self.run.entry_shares - self.run.exited_shares
        if position <= 0:
            await self.run.transition(RunState.CLOSED, close_reason=CloseReason.LEADER_NOT_CONFIRMED)
            await self.cleanup()
        else:
            await self._start_exit()

    async def _post_confirm_timeout(self, wait_sec: float) -> None:
        """追加后等待超时 — 撤销未成交并开始退出。"""
        await asyncio.sleep(wait_sec)
        if self.run.is_closed:
            return

        await self._cancel_all_buys()
        await self._start_exit()

    async def _cancel_all_buys(self) -> None:
        """撤销所有未成交 BUY 挂单。"""
        for clob_id, client_id in [
            (self._probe_clob_id, self._probe_order_id),
            (self._add_clob_id, self._add_order_id),
        ]:
            if clob_id and client_id:
                await self.executor.cancel_order(self.proxy_wallet, clob_id, client_id)

    async def _start_exit(self) -> None:
        """根据退出模式开始退场。"""
        await self.run.transition(RunState.EXIT_WORKING)

        exit_mode = self.params.get("exit_mode", "risk_tick_exit")
        if exit_mode == "sell_099_then_risk":
            await self._exit_sell_099()
        # risk_tick_exit 模式下等待 tick 候选或风控触发

    async def _exit_sell_099(self) -> None:
        """退出模式: 先以 0.99 挂 SELL。"""
        position = self.run.entry_shares - self.run.exited_shares
        if position <= 0:
            await self.run.transition(RunState.CLOSED, close_reason=CloseReason.SELL_FILLED)
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

        request = OrderRequest(
            run_id=self.run.run_id,
            client_order_id=client_order_id,
            token_id=self.run.token_id,
            side=OrderSide.SELL,
            limit_price=Decimal("0.99"),
            size=position,
            purpose=OrderPurpose.EXIT_099,
            execution_mode=ExecutionMode.NORMAL,
        )

        report = await self.executor.place_normal_order(
            proxy_wallet=self.proxy_wallet,
            request=request,
        )

        await self.run.emit_event(RunEventType.ORDER_SENT, {
            "client_order_id": client_order_id,
            "side": "SELL",
            "price": "0.99",
            "size": str(position),
            "purpose": "exit_099",
        })

        if report.status == OrderStatus.MATCHED and report.matched_size >= position:
            await self.ledger.confirm_sell_fill(
                run_id=self.run.run_id,
                order_id=client_order_id,
                token_id=self.run.token_id,
                price=Decimal("0.99"),
                filled_shares=report.matched_size,
                trade_id=f"immediate:{client_order_id}",
            )
            await self.run.record_fill(Decimal("0.99"), report.matched_size, is_buy=False)
            await self.run.transition(RunState.CLOSED, close_reason=CloseReason.SELL_FILLED)
            await self.cleanup()
        # 未全部成交 → 继续等待 tick/风控

    async def on_tick_candidate(self) -> None:
        """tick=0.001 候选 → HTTP 校验后 SELL@0.999。"""
        if self._tick_verified or self.run.is_closed:
            return
        if self.run.state != RunState.EXIT_WORKING:
            return

        result = await self.tick_verifier.verify(self.run.token_id, run_machine=self.run)
        if not result.confirmed:
            return

        self._tick_verified = True

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

        # 取消所有定时器
        for timer in [self._confirm_timer, self._post_confirm_timer]:
            if timer and not timer.done():
                timer.cancel()

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
