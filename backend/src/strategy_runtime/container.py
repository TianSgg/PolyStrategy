from __future__ import annotations

import asyncio
import logging
import uuid
from dataclasses import dataclass
from typing import Any

from strategy_runtime.event_logger import EventLogger
from strategy_runtime.instance_manager import InstanceManager, StrategyInstanceConfig
from strategy_runtime.interfaces import BaseStrategy, Signal, StrategyContext
from strategy_runtime.state_store import MySQLStateStore

logger = logging.getLogger(__name__)


@dataclass
class SignalSourceConfig:
    """一个信号源的连接配置。"""

    url: str
    adapter: Any
    name: str = ""


class StrategyContainer:
    """多实例容器 — 管理 N 个策略实例，共享信号源 WS 连接。"""

    def __init__(
        self,
        strategy_class: type[BaseStrategy],
        signal_sources: list[SignalSourceConfig],
        instance_manager: InstanceManager,
        events_table: str = "weather_sweep_events",
        executor_factory: Any = None,
    ) -> None:
        self._strategy_class = strategy_class
        self._signal_sources = signal_sources
        self._instance_manager = instance_manager
        self._events_table = events_table
        self._executor_factory = executor_factory
        self._instances: dict[int, _RunningInstance] = {}
        self._ws_tasks: list[asyncio.Task] = []
        self._running = False

    async def start(self) -> None:
        from toolkit.signals.ws_client import SignalWSClient

        for source in self._signal_sources:
            client = SignalWSClient(url=source.url, adapter=source.adapter)
            task = asyncio.create_task(
                client.listen(self._dispatch_signal),
                name=f"ws:{source.name or source.url}",
            )
            self._ws_tasks.append(task)

        configs = self._instance_manager.load_enabled()
        for cfg in configs:
            await self._start_instance(cfg)

        self._running = True
        logger.info(
            "Container started: strategy=%s, instances=%d, sources=%d",
            self._strategy_class.__name__,
            len(self._instances),
            len(self._signal_sources),
        )

    async def _start_instance(self, cfg: StrategyInstanceConfig) -> None:
        if cfg.id in self._instances:
            return

        executor = None
        if self._executor_factory:
            executor = self._executor_factory(cfg.proxy_wallet)

        event_logger = EventLogger(
            table=self._events_table,
            owner_user_id=cfg.owner_user_id,
            proxy_wallet=cfg.proxy_wallet,
            config_id=cfg.id,
            config_snapshot=self._instance_manager.config_snapshot(cfg),
        )

        state_store = MySQLStateStore()
        run_id = str(uuid.uuid4())
        ctx = StrategyContext(
            executor=executor,
            state_store=state_store,
            config=cfg.params,
            proxy_wallet=cfg.proxy_wallet,
            run_id=run_id,
            event_logger=event_logger,
        )

        strategy = self._strategy_class()
        await strategy.start(ctx)
        self._instances[cfg.id] = _RunningInstance(
            config=cfg, strategy=strategy, ctx=ctx, run_id=run_id,
        )
        logger.info("Instance %d started: wallet=%s name=%s", cfg.id, cfg.proxy_wallet[:10], cfg.name)

    async def _stop_instance(self, config_id: int) -> None:
        inst = self._instances.pop(config_id, None)
        if inst:
            await inst.strategy.stop()
            logger.info("Instance %d stopped", config_id)

    async def _dispatch_signal(self, signal: Signal) -> None:
        for config_id, inst in list(self._instances.items()):
            try:
                await inst.strategy.on_signal(signal)
            except Exception:
                logger.exception("Instance %d error on signal %s", config_id, signal.signal_id)

    async def reload(self) -> dict[str, Any]:
        """热更新：从 DB 重新加载配置，新增/停止/重建实例。"""
        configs = self._instance_manager.load_enabled()
        config_map = {c.id: c for c in configs}

        stopped, started, reloaded = [], [], []

        for cid in list(self._instances.keys()):
            if cid not in config_map:
                await self._stop_instance(cid)
                stopped.append(cid)

        for cfg in configs:
            existing = self._instances.get(cfg.id)
            if existing is None:
                await self._start_instance(cfg)
                started.append(cfg.id)
            elif existing.config.params_version != cfg.params_version:
                await self._stop_instance(cfg.id)
                await self._start_instance(cfg)
                reloaded.append(cfg.id)

        result = {"stopped": stopped, "started": started, "reloaded": reloaded}
        logger.info("Reload complete: %s", result)
        return result

    async def stop(self) -> None:
        self._running = False
        for task in self._ws_tasks:
            task.cancel()
        await asyncio.gather(*self._ws_tasks, return_exceptions=True)
        self._ws_tasks.clear()

        for config_id in list(self._instances.keys()):
            await self._stop_instance(config_id)
        logger.info("Container stopped")

    def health(self) -> dict[str, Any]:
        return {
            "status": "ok" if self._running else "stopped",
            "strategy": self._strategy_class.__name__,
            "instances": len(self._instances),
            "ws_connections": len(self._ws_tasks),
            "instance_ids": list(self._instances.keys()),
        }


@dataclass
class _RunningInstance:
    config: StrategyInstanceConfig
    strategy: BaseStrategy
    ctx: StrategyContext
    run_id: str
