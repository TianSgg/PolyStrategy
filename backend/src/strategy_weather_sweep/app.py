"""扫单策略微服务入口。

职责：启动编排 — 创建 app、管理生命周期、注册路由。
不包含业务逻辑（在 service.py）和数据库操作（在 dao.py）。
"""
import asyncio
import os
import sys
from contextlib import asynccontextmanager
from pathlib import Path

import yaml

_src_dir = str(Path(__file__).resolve().parent.parent)
if _src_dir not in sys.path:
    sys.path.insert(0, _src_dir)

from dotenv import load_dotenv

_project_root = Path(__file__).resolve().parent.parent.parent.parent
load_dotenv(_project_root / ".env", override=True)

_config_path = Path(__file__).resolve().parent / "config.yml"
with open(_config_path) as f:
    _cfg = yaml.safe_load(f)

from framework.logging import setup_logging
setup_logging(_cfg["service"]["name"])

import logging

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from framework.consul import consul_lifespan
from framework.instance_pool import InstancePool, InstanceConfig
from framework.orderbook_ws import OrderBookWS
from framework.strategy_runtime.signal_client import SignalWSClient
from framework.strategy_runtime.weather_adapter import WeatherSweepAdapter
from framework.trading.provider import set_client_provider

from account_service.service import get_account_service
from strategy_weather_sweep.api import router as strategy_router
from strategy_weather_sweep.dao import WeatherSweepConfigDAO
from strategy_weather_sweep.service import SweepStrategy

logger = logging.getLogger(__name__)

PORT = int(os.getenv("STRATEGY_SWEEP_PORT", str(_cfg["service"]["port"])))
WEATHER_SIGNAL_URL = os.getenv("WEATHER_SIGNAL_WS_URL", "ws://localhost:8001/ws/signals")

# ==================== 共享工具 ====================

orderbook_ws = OrderBookWS()
_config_dao = WeatherSweepConfigDAO()

# ==================== 实例池（委托 service.py 工厂方法） ====================


def _load_configs() -> list[InstanceConfig]:
    rows = _config_dao.list_all_enabled()
    return [
        InstanceConfig(id=r["id"], version=r["params_version"], data=r)
        for r in rows
    ]


async def _create_instance(cfg: InstanceConfig) -> SweepStrategy:
    return await SweepStrategy.create(cfg.data, orderbook_ws)


async def _destroy_instance(strategy: SweepStrategy, reason: str) -> None:
    await strategy.destroy(reason)


pool = InstancePool(
    config_loader=_load_configs,
    create_instance=_create_instance,
    destroy_instance=_destroy_instance,
)

# ==================== 信号分发 ====================


async def _dispatch_signal(signal) -> None:
    for strategy in pool.all_instances():
        try:
            await strategy.on_signal(signal)
        except Exception:
            logger.exception("Strategy error on signal %s", signal.signal_id)


# ==================== FastAPI App ====================


@asynccontextmanager
async def lifespan(app: FastAPI):
    set_client_provider(get_account_service())
    await orderbook_ws.start()
    await pool.start()

    signal_client = SignalWSClient(url=WEATHER_SIGNAL_URL, adapter=WeatherSweepAdapter())
    signal_task = asyncio.create_task(
        signal_client.listen(_dispatch_signal), name="ws:weather_signal"
    )

    app.state.pool = pool
    app.state.orderbook_ws = orderbook_ws

    async with consul_lifespan(_cfg["service"]["name"], PORT, tags=_cfg["consul"]["tags"]):
        try:
            yield
        finally:
            signal_task.cancel()
            try:
                await signal_task
            except asyncio.CancelledError:
                pass
            await pool.stop()
            await orderbook_ws.stop()


app = FastAPI(title=_cfg["service"]["name"], lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(strategy_router)


@app.get("/health")
async def health():
    return {"strategy": "SweepStrategy", **pool.health()}


@app.get("/api/status")
async def status():
    return await health()


@app.post("/internal/reload")
async def reload():
    result = await pool.reload()
    return {"status": "ok", **result}


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=PORT)
