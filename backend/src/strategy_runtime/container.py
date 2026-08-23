from __future__ import annotations

import asyncio
import logging
import uuid
from dataclasses import dataclass
from typing import Any, Callable

from strategy_runtime.interfaces import BaseStrategy, Signal, StrategyContext
from strategy_runtime.state_store import MySQLStateStore

logger = logging.getLogger(__name__)


@dataclass
class SignalSourceConfig:
    """一个信号源的连接配置。"""

    url: str
    adapter: Any  # 实现 adapt(raw: dict) -> Signal
    name: str = ""


class StrategyContainer:
    """极简容器 — 生命周期 + 信号接收 + 健康检查。"""

    def __init__(
        self,
        strategy_class: type[BaseStrategy],
        config: dict[str, Any],
        signal_sources: list[SignalSourceConfig],
        executor: Any = None,
        proxy_wallet: str = "",
    ) -> None:
        self._strategy_class = strategy_class
        self._strategy: BaseStrategy | None = None
        self._config = config
        self._signal_sources = signal_sources
        self._executor = executor
        self._proxy_wallet = proxy_wallet
        self._ws_tasks: list[asyncio.Task] = []
        self._run_id = str(uuid.uuid4())
        self._running = False

    async def start(self) -> None:
        from toolkit.signals.ws_client import SignalWSClient

        state_store = MySQLStateStore()
        ctx = StrategyContext(
            executor=self._executor,
            state_store=state_store,
            config=self._config,
            proxy_wallet=self._proxy_wallet,
            run_id=self._run_id,
        )

        self._strategy = self._strategy_class()
        await self._strategy.start(ctx)

        for source in self._signal_sources:
            client = SignalWSClient(url=source.url, adapter=source.adapter)
            task = asyncio.create_task(
                client.listen(self._dispatch_signal),
                name=f"ws:{source.name or source.url}",
            )
            self._ws_tasks.append(task)

        self._running = True
        logger.info(
            "Container started: strategy=%s, run_id=%s, sources=%d",
            self._strategy_class.__name__,
            self._run_id,
            len(self._signal_sources),
        )

    async def _dispatch_signal(self, signal: Signal) -> None:
        if self._strategy is None:
            return
        try:
            await self._strategy.on_signal(signal)
        except Exception:
            logger.exception("Strategy error processing signal %s", signal.signal_id)

    async def stop(self) -> None:
        self._running = False
        for task in self._ws_tasks:
            task.cancel()
        await asyncio.gather(*self._ws_tasks, return_exceptions=True)
        self._ws_tasks.clear()

        if self._strategy:
            await self._strategy.stop()
        logger.info("Container stopped: run_id=%s", self._run_id)

    def health(self) -> dict[str, Any]:
        return {
            "status": "ok" if self._running else "stopped",
            "strategy": self._strategy_class.__name__,
            "run_id": self._run_id,
            "ws_connections": len(self._ws_tasks),
        }
