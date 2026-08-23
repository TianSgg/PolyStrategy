from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI, WebSocket, WebSocketDisconnect

from strategy_runtime.container import SignalSourceConfig, StrategyContainer
from strategy_runtime.interfaces import BaseStrategy

logger = logging.getLogger(__name__)


def create_app(
    *,
    strategy_class: type[BaseStrategy],
    config: dict[str, Any],
    signal_sources: list[SignalSourceConfig],
    service_name: str,
    executor: Any = None,
    proxy_wallet: str = "",
) -> FastAPI:
    """创建标准化的策略微服务 FastAPI 应用。"""

    container = StrategyContainer(
        strategy_class=strategy_class,
        config=config,
        signal_sources=signal_sources,
        executor=executor,
        proxy_wallet=proxy_wallet,
    )

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        await container.start()
        app.state.container = container
        logger.info("%s started", service_name)
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
