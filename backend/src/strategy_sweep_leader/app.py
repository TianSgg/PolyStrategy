"""扫单+跟单组合策略微服务入口。"""
from __future__ import annotations

import asyncio
import os
import sys
from contextlib import asynccontextmanager
from pathlib import Path

_src_dir = str(Path(__file__).resolve().parent.parent)
if _src_dir not in sys.path:
    sys.path.insert(0, _src_dir)

from dotenv import load_dotenv

_backend_dir = Path(__file__).resolve().parent.parent.parent
load_dotenv(_backend_dir / ".env", override=True)

from framework.logging import setup_logging
setup_logging("strategy_sweep_leader")

import logging

from fastapi import FastAPI

from framework.consul import consul_lifespan
from framework.strategy_runtime.interfaces import Signal, StrategyContext
from framework.strategy_runtime.order_executor import OrderExecutor
from framework.strategy_runtime.signal_client import SignalWSClient
from framework.strategy_runtime.state_store import MySQLStateStore
from framework.strategy_runtime.leader_adapter import LeaderBuyAdapter
from framework.strategy_runtime.weather_adapter import WeatherSweepAdapter
from framework.trading.provider import set_client_provider

from account_service.service import get_account_service
from strategy_sweep_leader.strategy import SweepLeaderStrategy

logger = logging.getLogger(__name__)

PORT = int(os.getenv("STRATEGY_SWEEP_LEADER_PORT", "8005"))
WEATHER_SIGNAL_URL = os.getenv("WEATHER_SIGNAL_WS_URL", "ws://localhost:8001/ws/signals")
LEADER_SIGNAL_URL = os.getenv("LEADER_SIGNAL_WS_URL", "ws://localhost:8002/ws/signals")
PROXY_WALLET = os.getenv("PROXY_WALLET", "")

CONFIG = {
    "initial_cash": os.getenv("STRATEGY_INITIAL_CASH", "1000"),
    "fixed_probe_shares": os.getenv("SWEEP_LEADER_PROBE_SHARES", "50"),
    "leader_confirm_window_ms": int(os.getenv("SWEEP_LEADER_CONFIRM_WINDOW_MS", "60000")),
    "post_confirm_wait_ms": int(os.getenv("SWEEP_LEADER_POST_CONFIRM_MS", "30000")),
    "stop_loss_ratio": os.getenv("SWEEP_LEADER_STOP_LOSS_RATIO", "0.60"),
}

_strategy: SweepLeaderStrategy | None = None
_signal_tasks: list[asyncio.Task] = []


async def _dispatch_signal(signal: Signal) -> None:
    if _strategy:
        try:
            await _strategy.on_signal(signal)
        except Exception:
            logger.exception("Strategy error on signal")


@asynccontextmanager
async def lifespan(app: FastAPI):
    global _strategy

    set_client_provider(get_account_service())

    executor = OrderExecutor(proxy_wallet=PROXY_WALLET)
    await executor.ensure_poller(PROXY_WALLET)

    ctx = StrategyContext(
        executor=executor,
        state_store=MySQLStateStore(),
        config=CONFIG,
        proxy_wallet=PROXY_WALLET,
        run_id="sweep-leader-single",
    )

    _strategy = SweepLeaderStrategy()
    await _strategy.start(ctx)

    weather_client = SignalWSClient(url=WEATHER_SIGNAL_URL, adapter=WeatherSweepAdapter())
    leader_client = SignalWSClient(url=LEADER_SIGNAL_URL, adapter=LeaderBuyAdapter())

    _signal_tasks.append(asyncio.create_task(
        weather_client.listen(_dispatch_signal), name="ws:weather_signal"
    ))
    _signal_tasks.append(asyncio.create_task(
        leader_client.listen(_dispatch_signal), name="ws:leader_signal"
    ))

    async with consul_lifespan("strategy_sweep_leader", PORT):
        try:
            yield
        finally:
            for t in _signal_tasks:
                t.cancel()
            await asyncio.gather(*_signal_tasks, return_exceptions=True)
            _signal_tasks.clear()
            await _strategy.stop()
            _strategy = None


app = FastAPI(title="strategy_sweep_leader", lifespan=lifespan)


@app.get("/health")
async def health():
    return {
        "status": "ok" if _strategy else "stopped",
        "strategy": "SweepLeaderStrategy",
    }


@app.get("/api/status")
async def status():
    return await health()


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=PORT)
