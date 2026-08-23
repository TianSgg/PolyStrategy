"""SingleStrategyRuntime — 单策略微服务运行时。

每个实例只负责一种策略类型，独立进程运行。
新增策略时只需启动新进程，不影响已运行的策略服务。
"""
from __future__ import annotations

import asyncio
import logging
import os
import uuid
from decimal import Decimal
from typing import Any, Dict, List, Optional

from shared.time_utils import now_utc8_dt
from signal_leader_activity.types import LeaderBuySignal
from signal_weather_orderbook.types import WeatherSweepSignal
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
from strategy_execution.base import BaseStrategy
from strategy_execution.repository import StrategyConfigRepository, StrategyRunRepository
from strategy_execution.risk.manager import RiskManager
from strategy_execution.run_state import RunSingleFlightGuard, RunStateMachine
from strategy_execution.signal_subscription import WeatherSignalClient, LeaderSignalClient

logger = logging.getLogger(__name__)

NEEDS_WEATHER = {StrategyType.SWEEP, StrategyType.SWEEP_LEADER}
NEEDS_LEADER = {StrategyType.LEADER, StrategyType.SWEEP_LEADER}


class SingleStrategyRuntime:
    """单策略运行时 — 只处理指定的 strategy_type。"""

    def __init__(self, strategy_type: StrategyType, strategy_class: type[BaseStrategy] | None = None) -> None:
        self._strategy_type = strategy_type
        self._strategy_class = strategy_class
        self._config_repo = StrategyConfigRepository()
        self._run_repo = StrategyRunRepository()
        self._ledger_manager = LedgerManager()
        self._order_executor = OrderExecutor()
        self._order_projection = OrderProjection()
        self._tick_verifier = TickVerifier()
        self._risk_manager = RiskManager()
        self._guard = RunSingleFlightGuard()

        self._strategies: Dict[str, BaseStrategy] = {}
        self._weather_sub: Optional[WeatherSignalClient] = None
        self._leader_sub: Optional[LeaderSignalClient] = None
        self._enabled_configs: Dict[int, Dict[str, Any]] = {}

    async def start(self) -> None:
        await self._load_enabled_configs()
        await self._start_signal_subscriptions()
        await self._recover_active_runs()
        logger.info(
            "SingleStrategyRuntime [%s] started: %d configs",
            self._strategy_type.value,
            len(self._enabled_configs),
        )

    async def stop(self) -> None:
        if self._weather_sub:
            await self._weather_sub.stop()
        if self._leader_sub:
            await self._leader_sub.stop()
        logger.info("SingleStrategyRuntime [%s] stopped", self._strategy_type.value)

    async def _load_enabled_configs(self) -> None:
        configs = self._config_repo.list_all_enabled(self._strategy_type)
        for cfg in configs:
            self._enabled_configs[cfg["id"]] = cfg

    async def _start_signal_subscriptions(self) -> None:
        if self._strategy_type in NEEDS_WEATHER:
            weather_url = os.getenv("WEATHER_SIGNAL_WS_URL", "ws://127.0.0.1:8001/ws/signal")
            self._weather_sub = WeatherSignalClient(
                url=weather_url,
                client_id=f"strategy_{self._strategy_type.value}_weather",
                handler=self._on_weather_signal,
            )
            await self._weather_sub.start()

        if self._strategy_type in NEEDS_LEADER:
            leader_url = os.getenv("LEADER_SIGNAL_WS_URL", "ws://127.0.0.1:8002/ws/signal")
            self._leader_sub = LeaderSignalClient(
                url=leader_url,
                client_id=f"strategy_{self._strategy_type.value}_leader",
                handler=self._on_leader_signal,
            )
            await self._leader_sub.start()

    async def _recover_active_runs(self) -> None:
        active_runs = self._run_repo.list_active()
        for run_data in active_runs:
            if run_data.get("strategy_type") != self._strategy_type.value:
                continue
            try:
                await self._restore_run(run_data)
            except Exception:
                logger.exception("Failed to recover run %s", run_data.get("id"))

    async def _restore_run(self, run_data: Dict[str, Any]) -> None:
        run_id = run_data["id"]
        machine = RunStateMachine(
            run_id=run_id,
            strategy_type=self._strategy_type,
            config_id=run_data["strategy_config_id"],
            token_id=run_data["token_id"],
            initial_state=RunState(run_data["state"]),
            initial_version=run_data["version"],
        )
        self._guard.register(machine)
        logger.info("Recovered run %s in state %s", run_id, run_data["state"])

    # ─── 信号处理 ─────────────────────────────────────────────────────────

    async def _on_weather_signal(self, signal: WeatherSweepSignal) -> None:
        for config_id, cfg in self._enabled_configs.items():
            outcome_filter = cfg.get("sweep_outcome_filter", "no")
            if not self._outcome_matches(signal.outcome, outcome_filter):
                continue
            await self._try_create_run(config_id, cfg, signal)

    async def _on_leader_signal(self, signal: LeaderBuySignal) -> None:
        for config_id, cfg in self._enabled_configs.items():
            if cfg.get("leader_proxy_wallet", "").lower() != signal.leader_proxy_wallet.lower():
                continue
            outcome_filter = cfg.get("leader_outcome_filter", "all")
            if not self._outcome_matches(signal.outcome, outcome_filter):
                continue

            if self._strategy_type == StrategyType.SWEEP_LEADER:
                active_key = f"{self._strategy_type.value}:{config_id}:{signal.token_id}"
                active_machine = self._guard.get_active(active_key)
                if active_machine and active_machine.state == RunState.WAITING_LEADER:
                    strategy = self._strategies.get(active_machine.run_id)
                    if strategy:
                        await strategy.on_leader_signal(signal)
            else:
                await self._try_create_run(config_id, cfg, signal)

    async def _try_create_run(
        self, config_id: int, config: Dict[str, Any], signal: Any
    ) -> None:
        active_key = f"{self._strategy_type.value}:{config_id}:{signal.token_id}"

        lock = await self._guard.acquire(active_key)
        if lock is None:
            return

        async with lock:
            if self._guard.get_active(active_key):
                return

            run_id = str(uuid.uuid4())
            account_id = config.get("account_id", 0)
            ledger = self._ledger_manager.get_or_create(account_id)

            machine = RunStateMachine(
                run_id=run_id,
                strategy_type=self._strategy_type,
                config_id=config_id,
                token_id=signal.token_id,
            )

            run_data = {
                "id": run_id,
                "strategy_type": self._strategy_type.value,
                "strategy_config_id": config_id,
                "token_id": signal.token_id,
                "market_slug": None,
                "question": None,
                "outcome": signal.outcome,
                "state": RunState.CREATED.value,
                "status": RunStatus.ACTIVE.value,
                "active_key": active_key,
                "params_snapshot_json": {
                    k: str(v) if isinstance(v, Decimal) else v
                    for k, v in config.items()
                    if k not in ("id", "owner_user_id", "account_id", "name", "enabled", "deleted_at", "created_at", "updated_at")
                },
                "started_by_signal_id": signal.event_id,
                "started_at": now_utc8_dt(),
            }
            self._run_repo.create(run_data)
            self._guard.register(machine)

            from account.service import get_account_service
            account = get_account_service().get_account(account_id)
            proxy_wallet = account.get("proxy_wallet", "") if account else ""

            strategy = self._create_strategy(
                run_machine=machine,
                ledger=ledger,
                proxy_wallet=proxy_wallet,
                params=config,
            )
            self._strategies[run_id] = strategy
            await strategy.on_entry_signal(signal)

    def _create_strategy(
        self,
        run_machine: RunStateMachine,
        ledger: AccountExecutionLedger,
        proxy_wallet: str,
        params: Dict[str, Any],
    ) -> BaseStrategy:
        if self._strategy_class is None:
            raise ValueError(f"No strategy_class provided for {self._strategy_type}")
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
        return self._strategy_class(**kwargs)

    @staticmethod
    def _outcome_matches(signal_outcome: Optional[str], filter_value: str) -> bool:
        if filter_value == "all":
            return True
        if signal_outcome is None:
            return False
        return signal_outcome.lower() == filter_value.lower()

    # ─── 公开接口 ─────────────────────────────────────────────────────────

    def get_active_runs(self) -> List[Dict[str, Any]]:
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
