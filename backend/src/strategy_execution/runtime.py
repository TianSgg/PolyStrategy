"""StrategyRuntime — 编排信号订阅、策略分发和运行生命周期。

这是策略执行系统的顶层编排器，不承载策略业务判断。
"""
from __future__ import annotations

import asyncio
import logging
import os
import uuid
from decimal import Decimal
from typing import Any, Dict, List, Optional

from shared.time_utils import now_utc8_dt
from signal_data.contracts import SignalEnvelope
from strategy_execution.config import validate_strategy_params, STRATEGY_PARAMS_MAP
from strategy_execution.contracts import Signal
from strategy_execution.enums import (
    CloseReason,
    RunState,
    RunStatus,
    StrategyType,
)
from strategy_execution.execution.account_ledger import AccountExecutionLedger, LedgerManager
from strategy_execution.execution.order_executor import OrderExecutor
from strategy_execution.execution.order_projection import OrderProjection
from strategy_execution.execution.tick_verifier import TickVerifier
from strategy_execution.repository import StrategyConfigRepository, StrategyRunRepository
from strategy_execution.risk.manager import RiskManager
from strategy_execution.run_state import RunSingleFlightGuard, RunStateMachine
from strategy_execution.signal_subscription import SignalSubscriptionClient
from strategy_execution.strategies.base import BaseStrategy
from strategy_execution.strategies.leader_strategy import LeaderStrategy
from strategy_execution.strategies.sweep_leader_strategy import SweepLeaderStrategy
from strategy_execution.strategies.sweep_strategy import SweepStrategy

logger = logging.getLogger(__name__)


class StrategyRuntime:
    """策略执行系统运行时。"""

    def __init__(self) -> None:
        self._config_repo = StrategyConfigRepository()
        self._run_repo = StrategyRunRepository()
        self._ledger_manager = LedgerManager()
        self._order_executor = OrderExecutor()
        self._order_projection = OrderProjection()
        self._tick_verifier = TickVerifier()
        self._risk_manager = RiskManager()
        self._guard = RunSingleFlightGuard()

        # 活跃策略实例: {run_id: BaseStrategy}
        self._strategies: Dict[str, BaseStrategy] = {}

        # 信号订阅客户端
        self._weather_sub: Optional[SignalSubscriptionClient] = None
        self._leader_sub: Optional[SignalSubscriptionClient] = None

        # 配置缓存: {(strategy_type, config_id): config_dict}
        self._enabled_configs: Dict[tuple, Dict[str, Any]] = {}

    async def start(self) -> None:
        """启动运行时：加载配置、连接信号服务、恢复活跃运行。"""
        await self._load_enabled_configs()
        await self._start_signal_subscriptions()
        await self._recover_active_runs()
        logger.info("StrategyRuntime started: %d configs loaded", len(self._enabled_configs))

    async def stop(self) -> None:
        """停止运行时。"""
        if self._weather_sub:
            await self._weather_sub.stop()
        if self._leader_sub:
            await self._leader_sub.stop()
        logger.info("StrategyRuntime stopped")

    async def _load_enabled_configs(self) -> None:
        """加载所有启用的策略配置。"""
        for st in StrategyType:
            configs = self._config_repo.list_all_enabled(st)
            for cfg in configs:
                key = (st, cfg["id"])
                self._enabled_configs[key] = cfg

    async def _start_signal_subscriptions(self) -> None:
        """连接天气和 Leader 信号服务的 WS。"""
        weather_url = os.getenv("WEATHER_SIGNAL_WS_URL", "ws://127.0.0.1:8001/ws/signal")
        leader_url = os.getenv("LEADER_SIGNAL_WS_URL", "ws://127.0.0.1:8002/ws/signal")

        self._weather_sub = SignalSubscriptionClient(
            url=weather_url,
            client_id="strategy_runtime_weather",
            subscribe_events=["sweep"],
            handler=self._on_weather_signal,
        )
        self._leader_sub = SignalSubscriptionClient(
            url=leader_url,
            client_id="strategy_runtime_leader",
            subscribe_events=["leader_buy"],
            handler=self._on_leader_signal,
        )

        await self._weather_sub.start()
        await self._leader_sub.start()

    async def _recover_active_runs(self) -> None:
        """启动时恢复非终态运行。"""
        active_runs = self._run_repo.list_active()
        for run_data in active_runs:
            try:
                await self._restore_run(run_data)
            except Exception:
                logger.exception("Failed to recover run %s", run_data.get("id"))

    async def _restore_run(self, run_data: Dict[str, Any]) -> None:
        """从数据库恢复单个运行的内存状态。"""
        run_id = run_data["id"]
        strategy_type = StrategyType(run_data["strategy_type"])
        machine = RunStateMachine(
            run_id=run_id,
            strategy_type=strategy_type,
            config_id=run_data["strategy_config_id"],
            token_id=run_data["token_id"],
            initial_state=RunState(run_data["state"]),
            initial_version=run_data["version"],
        )
        self._guard.register(machine)
        logger.info("Recovered run %s in state %s", run_id, run_data["state"])

    async def _on_weather_signal(self, envelope: SignalEnvelope) -> None:
        """处理天气信号 — 分发给 sweep 和 sweep_leader 策略。"""
        signal = self._envelope_to_signal(envelope)

        # 分发给 sweep 策略
        for (st, config_id), cfg in self._enabled_configs.items():
            if st == StrategyType.SWEEP:
                outcome_filter = cfg.get("sweep_outcome_filter", "no")
                if not self._outcome_matches(signal.outcome, outcome_filter):
                    continue
                await self._try_create_run(st, config_id, cfg, signal)

            elif st == StrategyType.SWEEP_LEADER:
                outcome_filter = cfg.get("sweep_outcome_filter", "no")
                if not self._outcome_matches(signal.outcome, outcome_filter):
                    continue
                await self._try_create_run(st, config_id, cfg, signal)

    async def _on_leader_signal(self, envelope: SignalEnvelope) -> None:
        """处理 Leader 信号 — 分发给 leader 和 sweep_leader 策略。"""
        signal = self._envelope_to_signal(envelope)

        for (st, config_id), cfg in self._enabled_configs.items():
            if st == StrategyType.LEADER:
                # 检查 leader 地址和 outcome 过滤
                if cfg.get("leader_proxy_wallet", "").lower() != (signal.leader_proxy_wallet or "").lower():
                    continue
                outcome_filter = cfg.get("leader_outcome_filter", "all")
                if not self._outcome_matches(signal.outcome, outcome_filter):
                    continue
                await self._try_create_run(st, config_id, cfg, signal)

            elif st == StrategyType.SWEEP_LEADER:
                if cfg.get("leader_proxy_wallet", "").lower() != (signal.leader_proxy_wallet or "").lower():
                    continue
                outcome_filter = cfg.get("leader_outcome_filter", "all")
                if not self._outcome_matches(signal.outcome, outcome_filter):
                    continue
                # 对 sweep_leader，leader 信号作为确认而非创建
                active_key = f"{st.value}:{config_id}:{signal.token_id}"
                active_machine = self._guard.get_active(active_key)
                if active_machine and active_machine.state == RunState.WAITING_LEADER:
                    strategy = self._strategies.get(active_machine.run_id)
                    if strategy:
                        await strategy.on_leader_signal(signal)

    async def _try_create_run(
        self,
        strategy_type: StrategyType,
        config_id: int,
        config: Dict[str, Any],
        signal: Signal,
    ) -> None:
        """尝试创建新运行（单飞保护）。"""
        active_key = f"{strategy_type.value}:{config_id}:{signal.token_id}"

        # 检查是否已有活跃运行
        lock = await self._guard.acquire(active_key)
        if lock is None:
            logger.debug("Active run exists for %s, skipping", active_key)
            return

        async with lock:
            # 双重检查
            if self._guard.get_active(active_key):
                return

            run_id = str(uuid.uuid4())
            account_id = config.get("account_id", 0)

            # 获取或创建账本
            ledger = self._ledger_manager.get_or_create(account_id)

            # 创建状态机
            machine = RunStateMachine(
                run_id=run_id,
                strategy_type=strategy_type,
                config_id=config_id,
                token_id=signal.token_id,
            )

            # 持久化运行记录
            run_data = {
                "id": run_id,
                "strategy_type": strategy_type.value,
                "strategy_config_id": config_id,
                "token_id": signal.token_id,
                "market_slug": None,
                "question": None,
                "outcome": signal.outcome,
                "state": RunState.CREATED.value,
                "status": RunStatus.ACTIVE.value,
                "active_key": active_key,
                "params_snapshot_json": {k: str(v) if isinstance(v, Decimal) else v for k, v in config.items() if k not in ("id", "owner_user_id", "account_id", "name", "enabled", "deleted_at", "created_at", "updated_at")},
                "started_by_signal_id": signal.event_id,
                "started_at": now_utc8_dt(),
            }
            self._run_repo.create(run_data)
            self._guard.register(machine)

            # 获取 proxy_wallet
            from account.service import get_account_service
            account = get_account_service().get_account(account_id)
            proxy_wallet = account.get("proxy_wallet", "") if account else ""

            # 创建策略实例
            strategy = self._create_strategy(
                strategy_type=strategy_type,
                run_machine=machine,
                ledger=ledger,
                proxy_wallet=proxy_wallet,
                params=config,
            )
            self._strategies[run_id] = strategy

            # 触发入场
            await strategy.on_entry_signal(signal)

    def _create_strategy(
        self,
        strategy_type: StrategyType,
        run_machine: RunStateMachine,
        ledger: AccountExecutionLedger,
        proxy_wallet: str,
        params: Dict[str, Any],
    ) -> BaseStrategy:
        """根据策略类型创建对应实例。"""
        kwargs = {
            "run_machine": run_machine,
            "ledger": ledger,
            "order_executor": self._order_executor,
            "order_projection": self._order_projection,
            "tick_verifier": self._tick_verifier,
            "risk_manager": self._risk_manager,
            "proxy_wallet": proxy_wallet,
            "params": params,
        }
        if strategy_type == StrategyType.SWEEP:
            return SweepStrategy(**kwargs)
        elif strategy_type == StrategyType.LEADER:
            return LeaderStrategy(**kwargs)
        elif strategy_type == StrategyType.SWEEP_LEADER:
            return SweepLeaderStrategy(**kwargs)
        raise ValueError(f"Strategy not implemented: {strategy_type}")

    def _envelope_to_signal(self, envelope: SignalEnvelope) -> Signal:
        """SignalEnvelope → 策略内核 Signal。"""
        return Signal(
            event_id=envelope.event_id,
            source=envelope.source,
            event_type=envelope.event_type,
            received_at_ns=envelope.received_at_ns,
            occurred_at_ms=envelope.occurred_at_ms,
            token_id=envelope.token_id,
            outcome=envelope.outcome,
            side=envelope.side,
            leader_proxy_wallet=envelope.leader_proxy_wallet,
            payload=dict(envelope.payload),
        )

    @staticmethod
    def _outcome_matches(signal_outcome: Optional[str], filter_value: str) -> bool:
        if filter_value == "all":
            return True
        if signal_outcome is None:
            return False
        return signal_outcome.lower() == filter_value.lower()

    # ─── 公开接口 ─────────────────────────────────────────────────────────

    def get_active_runs(self) -> List[Dict[str, Any]]:
        """返回所有活跃运行的摘要。"""
        return [
            {
                "run_id": m.run_id,
                "strategy_type": m.strategy_type.value,
                "token_id": m.token_id,
                "state": m.state.value,
                "entry_shares": str(m.entry_shares),
                "exited_shares": str(m.exited_shares),
            }
            for m in self._guard.all_active()
        ]

    def get_ledger_snapshots(self) -> Dict[int, Dict[str, Any]]:
        return self._ledger_manager.all_snapshots()

    def signal_subscription_status(self) -> Dict[str, bool]:
        return {
            "weather_connected": self._weather_sub.connected if self._weather_sub else False,
            "leader_connected": self._leader_sub.connected if self._leader_sub else False,
        }
