"""RiskManager — 每个活跃 run 的风控会话。

职责：
  1. 跟踪平均成交价 (avg_entry_price)
  2. 订阅 BBO，检测 best_bid <= avg_entry_price * stop_loss_ratio
  3. 触发风险退出（仅迁移状态，实际退出由策略/执行器负责）
  4. 检测 tick_size=0.001 候选事件（WS 层）
"""
from __future__ import annotations

import asyncio
import logging
from decimal import Decimal
from typing import Callable, Coroutine, Dict, Optional

from strategy_execution.contracts import BBO
from strategy_execution.enums import RiskState, RunEventType, RunState
from strategy_execution.run_state import RunStateMachine

logger = logging.getLogger(__name__)


class RiskSession:
    """单个运行的风控会话。"""

    def __init__(
        self,
        run_machine: RunStateMachine,
        stop_loss_ratio: Decimal = Decimal("0.60"),
        on_risk_triggered: Optional[Callable[[], Coroutine]] = None,
        on_tick_candidate: Optional[Callable[[], Coroutine]] = None,
    ) -> None:
        self._run = run_machine
        self._stop_loss_ratio = stop_loss_ratio
        self._on_risk_triggered = on_risk_triggered
        self._on_tick_candidate = on_tick_candidate
        self._active = False
        self._triggered = False

    @property
    def is_active(self) -> bool:
        return self._active

    @property
    def is_triggered(self) -> bool:
        return self._triggered

    async def start(self) -> None:
        """激活风控。"""
        self._active = True
        await self._run.set_risk_state(RiskState.ACTIVE)

    async def stop(self) -> None:
        """正常关闭风控。"""
        self._active = False
        await self._run.set_risk_state(RiskState.FORCE_STOPPED)

    async def on_bbo_update(self, bbo: BBO) -> None:
        """收到新的 BBO 时检查止损条件和 tick 候选。"""
        if not self._active or self._triggered:
            return

        # tick=0.001 候选检测
        if bbo.tick_size == Decimal("0.001") and self._on_tick_candidate:
            asyncio.create_task(self._on_tick_candidate())

        # 止损检查
        if bbo.best_bid is None:
            return

        ref_price = self._run.avg_entry_price
        if ref_price is None:
            return

        threshold = ref_price * self._stop_loss_ratio
        if bbo.best_bid <= threshold:
            logger.warning(
                "Risk triggered for run %s: best_bid=%s <= threshold=%s (avg=%s * ratio=%s)",
                self._run.run_id, bbo.best_bid, threshold, ref_price, self._stop_loss_ratio,
            )
            self._triggered = True
            self._active = False
            await self._run.set_risk_state(RiskState.TRIGGERED)
            await self._run.emit_event(RunEventType.RISK_TRIGGERED, {
                "best_bid": str(bbo.best_bid),
                "avg_entry_price": str(ref_price),
                "stop_loss_ratio": str(self._stop_loss_ratio),
                "threshold": str(threshold),
            })
            if self._on_risk_triggered:
                asyncio.create_task(self._on_risk_triggered())

    def can_close(self) -> bool:
        """风控可关闭条件：没有活跃 BUY、没有持仓。"""
        return (
            self._run.entry_shares - self._run.exited_shares <= Decimal("0")
            and not self._active
        )


class RiskManager:
    """管理多个活跃运行的风控会话。"""

    def __init__(self) -> None:
        self._sessions: Dict[str, RiskSession] = {}

    async def create_session(
        self,
        run_machine: RunStateMachine,
        stop_loss_ratio: Decimal = Decimal("0.60"),
        on_risk_triggered: Optional[Callable[[], Coroutine]] = None,
        on_tick_candidate: Optional[Callable[[], Coroutine]] = None,
    ) -> RiskSession:
        """为新运行创建并启动风控会话。"""
        session = RiskSession(
            run_machine=run_machine,
            stop_loss_ratio=stop_loss_ratio,
            on_risk_triggered=on_risk_triggered,
            on_tick_candidate=on_tick_candidate,
        )
        self._sessions[run_machine.run_id] = session
        await session.start()
        return session

    def get_session(self, run_id: str) -> Optional[RiskSession]:
        return self._sessions.get(run_id)

    async def remove_session(self, run_id: str) -> None:
        session = self._sessions.pop(run_id, None)
        if session and session.is_active:
            await session.stop()

    async def dispatch_bbo(self, token_id: str, bbo: BBO) -> None:
        """将 BBO 分发给订阅该 token 的所有风控会话。"""
        for session in list(self._sessions.values()):
            if session._run.token_id == token_id and session.is_active:
                await session.on_bbo_update(bbo)

    def active_tokens(self) -> set:
        """返回当前有活跃风控的 token 集合。"""
        return {s._run.token_id for s in self._sessions.values() if s.is_active}
