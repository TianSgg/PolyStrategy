"""策略基类 — 共享的生命周期辅助函数。"""
from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from decimal import Decimal
from typing import Any, Dict, Optional

from signal_leader_activity.types import LeaderBuySignal
from signal_weather_orderbook.types import WeatherSweepSignal
from strategy_execution.execution.account_ledger import AccountExecutionLedger
from strategy_execution.execution.order_executor import OrderExecutor
from strategy_execution.execution.order_projection import OrderProjection
from strategy_execution.execution.tick_verifier import TickVerifier
from strategy_execution.risk.manager import RiskManager, RiskSession
from strategy_execution.run_state import RunStateMachine

logger = logging.getLogger(__name__)


class BaseStrategy(ABC):
    """所有策略的公共接口。"""

    def __init__(
        self,
        run_machine: RunStateMachine,
        ledger: AccountExecutionLedger,
        order_executor: OrderExecutor,
        order_projection: OrderProjection,
        tick_verifier: TickVerifier,
        risk_manager: RiskManager,
        proxy_wallet: str,
        params: Dict[str, Any],
    ) -> None:
        self.run = run_machine
        self.ledger = ledger
        self.executor = order_executor
        self.projection = order_projection
        self.tick_verifier = tick_verifier
        self.risk_manager = risk_manager
        self.proxy_wallet = proxy_wallet
        self.params = params
        self.risk_session: Optional[RiskSession] = None

    @abstractmethod
    async def on_entry_signal(self, signal: Any) -> None:
        """信号触发入场。WeatherSweepSignal 或 LeaderBuySignal。"""

    @abstractmethod
    async def on_leader_signal(self, signal: LeaderBuySignal) -> None:
        """Leader 确认信号（仅策略 3 使用）。"""

    @abstractmethod
    async def on_tick_candidate(self) -> None:
        """WS 检测到 tick=0.001 候选。"""

    @abstractmethod
    async def on_risk_triggered(self) -> None:
        """风控触发退出。"""

    async def cleanup(self) -> None:
        """运行关闭后清理。"""
        if self.risk_session:
            await self.risk_manager.remove_session(self.run.run_id)
