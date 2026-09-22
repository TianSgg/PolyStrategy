"""跟随天气扫单者策略微服务入口。

信号源：Predexon WS（链上 leader 钱包监听）
执行引擎：SweepTrade（从 strategy_weather_sweep 复制）
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

import py_clob_client_v2

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from framework.consul import consul_lifespan
from framework.instance_pool import InstancePool, InstanceConfig
from framework.orderbook_ws import OrderBookWS
from framework.user_ws import stop_all_user_ws, _instances as _user_ws_instances
from framework.strategy_runtime.tick_size_service import TickSizeService
from framework.trading.provider import set_client_provider

from account_service.service import get_account_service
from strategy_follow_weather_sweeper.api import router as strategy_router
from strategy_follow_weather_sweeper.dao import FollowWeatherSweeperConfigDAO
from strategy_follow_weather_sweeper.internal.clob_book_bbo import ClobBookBboClient
from strategy_follow_weather_sweeper.predexon import PredexonAdapter, PredexonClient
from strategy_follow_weather_sweeper.service import FollowSweepStrategy

logger = logging.getLogger(__name__)


def _assert_vendored_clob_client() -> None:
    actual_path = Path(py_clob_client_v2.__file__).resolve()
    expected_root = Path(__file__).resolve().parents[2] / "vendor" / "py-clob-client-v2"
    try:
        actual_path.relative_to(expected_root.resolve())
    except ValueError:
        raise RuntimeError(
            f"py_clob_client_v2 must be imported from {expected_root}, got {actual_path}"
        )
    logger.info("Using vendored py_clob_client_v2: %s", actual_path)


_assert_vendored_clob_client()

tick_size_service = TickSizeService()

PORT = int(os.getenv("STRATEGY_FOLLOW_WEATHER_PORT", str(_cfg["service"]["port"])))

# ==================== 共享工具 ====================

orderbook_ws = OrderBookWS()
book_bbo_client = ClobBookBboClient()
_config_dao = FollowWeatherSweeperConfigDAO()

# ==================== 实例池 ====================


def _load_configs() -> list[InstanceConfig]:
    rows = _config_dao.list_all_enabled()
    return [
        InstanceConfig(id=r["id"], version=r["params_version"], data=r)
        for r in rows
    ]


async def _create_instance(cfg: InstanceConfig) -> FollowSweepStrategy:
    return await FollowSweepStrategy.create(
        cfg.data,
        orderbook_ws,
        tick_size_service=tick_size_service,
        book_bbo_client=book_bbo_client,
    )


async def _destroy_instance(strategy: FollowSweepStrategy, reason: str) -> None:
    await strategy.destroy(reason)


pool = InstancePool(
    config_loader=_load_configs,
    create_instance=_create_instance,
    destroy_instance=_destroy_instance,
)

# ==================== 信号分发 ====================

_adapter = PredexonAdapter()


async def _dispatch_signal(event_payload: dict) -> None:
    signal = _adapter.adapt(event_payload)
    if not signal:
        return
    for strategy in pool.all_instances():
        try:
            await strategy.on_signal(signal)
        except Exception:
            logger.exception("Strategy error on Predexon signal %s", signal.signal_id)


# ==================== FastAPI App ====================


@asynccontextmanager
async def lifespan(app: FastAPI):
    set_client_provider(get_account_service())
    await orderbook_ws.start()
    await book_bbo_client.start()
    await pool.start()

    predexon = PredexonClient()
    for strategy in pool.all_instances():
        if strategy._leader_wallet:
            predexon.add_leader(strategy._leader_wallet)
    predexon_task = asyncio.create_task(
        predexon.start(on_signal=_dispatch_signal), name="ws:predexon"
    )

    app.state.pool = pool
    app.state.predexon = predexon
    app.state.orderbook_ws = orderbook_ws

    async with consul_lifespan(_cfg["service"]["name"], PORT, tags=_cfg["consul"]["tags"]):
        try:
            yield
        finally:
            predexon.stop()
            predexon_task.cancel()
            try:
                await predexon_task
            except asyncio.CancelledError:
                pass
            await pool.stop()
            await stop_all_user_ws()
            await orderbook_ws.stop()
            await book_bbo_client.close()


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
    base = {"strategy": "FollowSweepStrategy", **pool.health()}
    user_ws = {addr[:8]: ws.snapshot() for addr, ws in _user_ws_instances.items()}
    if user_ws:
        base["user_ws"] = user_ws
    return base


@app.get("/api/status")
async def status():
    return await health()


@app.post("/internal/reload")
async def reload():
    result = await pool.reload()
    predexon = app.state.predexon
    if predexon:
        current_leaders: set[str] = set()
        for strategy in pool.all_instances():
            if strategy._leader_wallet:
                current_leaders.add(strategy._leader_wallet)
        predexon.sync_leaders(current_leaders)
    return {"status": "ok", **result}


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=PORT)
