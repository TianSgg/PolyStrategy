"""跟单策略微服务入口。"""
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
setup_logging("strategy_leader")

import logging

from fastapi import FastAPI

from framework.consul import consul_lifespan
from framework.strategy_runtime.interfaces import Signal, StrategyContext
from framework.strategy_runtime.order_executor import OrderExecutor
from framework.strategy_runtime.signal_client import SignalWSClient
from framework.strategy_runtime.state_store import MySQLStateStore
from framework.strategy_runtime.leader_adapter import LeaderBuyAdapter
from framework.trading.provider import set_client_provider

from account_service.service import get_account_service
from strategy_leader.strategy import LeaderStrategy

logger = logging.getLogger(__name__)

PORT = int(os.getenv("STRATEGY_LEADER_PORT", "8004"))
LEADER_SIGNAL_URL = os.getenv("LEADER_SIGNAL_WS_URL", "ws://localhost:8002/ws/signals")
PROXY_WALLET = os.getenv("PROXY_WALLET", "")

CONFIG = {
    "initial_cash": os.getenv("STRATEGY_INITIAL_CASH", "1000"),
    "fixed_entry_shares": os.getenv("LEADER_FIXED_SHARES", "100"),
    "entry_size_mode": os.getenv("LEADER_SIZE_MODE", "fixed"),
    "entry_wait_ms": int(os.getenv("LEADER_ENTRY_WAIT_MS", "30000")),
    "stop_loss_ratio": os.getenv("LEADER_STOP_LOSS_RATIO", "0.60"),
}

_strategy: LeaderStrategy | None = None
_signal_task: asyncio.Task | None = None


async def _dispatch_signal(signal: Signal) -> None:
    if _strategy:
        try:
            await _strategy.on_signal(signal)
        except Exception:
            logger.exception("Strategy error on signal")


@asynccontextmanager
async def lifespan(app: FastAPI):
    global _strategy, _signal_task

    set_client_provider(get_account_service())

    executor = OrderExecutor(proxy_wallet=PROXY_WALLET)
    await executor.ensure_poller(PROXY_WALLET)

    ctx = StrategyContext(
        executor=executor,
        state_store=MySQLStateStore(),
        config=CONFIG,
        proxy_wallet=PROXY_WALLET,
        run_id="leader-single",
    )

    _strategy = LeaderStrategy()
    await _strategy.start(ctx)

    signal_client = SignalWSClient(url=LEADER_SIGNAL_URL, adapter=LeaderBuyAdapter())
    _signal_task = asyncio.create_task(
        signal_client.listen(_dispatch_signal), name="ws:leader_signal"
    )

    async with consul_lifespan("strategy_leader", PORT):
        try:
            yield
        finally:
            _signal_task.cancel()
            try:
                await _signal_task
            except asyncio.CancelledError:
                pass
            await _strategy.stop()
            _strategy = None


app = FastAPI(title="strategy_leader", lifespan=lifespan)


@app.get("/health")
async def health():
    return {
        "status": "ok" if _strategy else "stopped",
        "strategy": "LeaderStrategy",
    }


@app.get("/api/status")
async def status():
    return await health()


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=PORT)
