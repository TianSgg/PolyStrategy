from __future__ import annotations

import logging
import os
import uuid
from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI

from framework.strategy_runtime.container import SignalSourceConfig, StrategyContainer
from framework.strategy_runtime.instance_manager import InstanceManager
from framework.strategy_runtime.interfaces import BaseStrategy, StrategyContext
from framework.strategy_runtime.state_store import MySQLStateStore
from framework.consul import consul_lifespan
from framework.orderbook_ws import OrderBookWS
from framework.trading.provider import set_client_provider

logger = logging.getLogger(__name__)


def create_app(
    *,
    strategy_class: type[BaseStrategy],
    signal_sources: list[SignalSourceConfig],
    service_name: str,
    # 多实例模式参数（新）
    strategy_type: str | None = None,
    config_table: str | None = None,
    events_table: str = "weather_sweep_events",
    executor_factory: Any = None,
    # 单实例模式参数（旧，兼容）
    config: dict[str, Any] | None = None,
    proxy_wallet: str = "",
    executor: Any = None,
    # 扩展
    extra_routers: list | None = None,
    consul_tags: list[str] | None = None,
    # 依赖注入
    client_provider: Any = None,
) -> FastAPI:
    """创建标准化的策略微服务 FastAPI 应用。

    多实例模式：指定 strategy_type + config_table，从 DB 加载配置。
    单实例模式（兼容）：指定 config + proxy_wallet，使用环境变量配置。
    """

    if client_provider:
        set_client_provider(client_provider)

    if strategy_type and config_table:
        return _create_multi_instance_app(
            strategy_class=strategy_class,
            signal_sources=signal_sources,
            service_name=service_name,
            strategy_type=strategy_type,
            config_table=config_table,
            events_table=events_table,
            executor_factory=executor_factory,
            extra_routers=extra_routers,
            consul_tags=consul_tags,
        )

    return _create_single_instance_app(
        strategy_class=strategy_class,
        signal_sources=signal_sources,
        service_name=service_name,
        config=config or {},
        proxy_wallet=proxy_wallet,
        executor=executor,
        consul_tags=consul_tags,
    )


def _create_multi_instance_app(
    *,
    strategy_class: type[BaseStrategy],
    signal_sources: list[SignalSourceConfig],
    service_name: str,
    strategy_type: str,
    config_table: str,
    events_table: str,
    executor_factory: Any,
    extra_routers: list | None = None,
    consul_tags: list[str] | None = None,
) -> FastAPI:
    instance_manager = InstanceManager(
        strategy_type=strategy_type,
        config_table=config_table,
    )

    orderbook_ws = OrderBookWS()

    container = StrategyContainer(
        strategy_class=strategy_class,
        signal_sources=signal_sources,
        instance_manager=instance_manager,
        events_table=events_table,
        executor_factory=executor_factory,
        orderbook_ws=orderbook_ws,
    )

    service_port = int(os.getenv("SERVICE_PORT", os.getenv("PORT", "8003")))

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        await orderbook_ws.start()
        await container.start()
        app.state.container = container
        app.state.orderbook_ws = orderbook_ws
        logger.info("%s started (multi-instance)", service_name)
        async with consul_lifespan(service_name, service_port, tags=consul_tags):
            try:
                yield
            finally:
                await container.stop()
                await orderbook_ws.stop()

    app = FastAPI(title=service_name, lifespan=lifespan)

    @app.get("/health")
    async def health():
        return container.health()

    @app.get("/api/status")
    async def status():
        return container.health()

    @app.post("/internal/reload")
    async def reload():
        result = await container.reload()
        return {"status": "ok", **result}

    for r in (extra_routers or []):
        app.include_router(r)

    return app


def _create_single_instance_app(
    *,
    strategy_class: type[BaseStrategy],
    signal_sources: list[SignalSourceConfig],
    service_name: str,
    config: dict[str, Any],
    proxy_wallet: str,
    executor: Any,
    consul_tags: list[str] | None = None,
) -> FastAPI:
    """兼容旧的单实例模式。"""

    class _LegacyContainer:
        def __init__(self):
            self._strategy: BaseStrategy | None = None
            self._ws_tasks: list = []
            self._running = False
            self._run_id = str(uuid.uuid4())

        async def start(self):
            from framework.strategy_runtime.signal_client import SignalWSClient
            import asyncio

            state_store = MySQLStateStore()
            ctx = StrategyContext(
                executor=executor,
                state_store=state_store,
                config=config,
                proxy_wallet=proxy_wallet,
                run_id=self._run_id,
            )
            self._strategy = strategy_class()
            await self._strategy.start(ctx)

            for source in signal_sources:
                client = SignalWSClient(url=source.url, adapter=source.adapter)
                task = asyncio.create_task(
                    client.listen(self._dispatch),
                    name=f"ws:{source.name or source.url}",
                )
                self._ws_tasks.append(task)
            self._running = True

        async def _dispatch(self, signal):
            if self._strategy:
                try:
                    await self._strategy.on_signal(signal)
                except Exception:
                    logger.exception("Strategy error")

        async def stop(self):
            import asyncio
            self._running = False
            for t in self._ws_tasks:
                t.cancel()
            await asyncio.gather(*self._ws_tasks, return_exceptions=True)
            if self._strategy:
                await self._strategy.stop()

        def health(self):
            return {
                "status": "ok" if self._running else "stopped",
                "strategy": strategy_class.__name__,
                "run_id": self._run_id,
                "ws_connections": len(self._ws_tasks),
            }

    container = _LegacyContainer()
    service_port = int(os.getenv("SERVICE_PORT", os.getenv("PORT", "8003")))

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        await container.start()
        app.state.container = container
        logger.info("%s started (single-instance)", service_name)
        async with consul_lifespan(service_name, service_port, tags=consul_tags):
            try:
                yield
            finally:
                await container.stop()

    app = FastAPI(title=service_name, lifespan=lifespan)

    @app.get("/health")
    async def health():
        return container.health()

    @app.get("/api/status")
    async def status():
        return container.health()

    return app
