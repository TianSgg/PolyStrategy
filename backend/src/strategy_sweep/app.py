"""扫单策略微服务入口 — port 8003。"""
import asyncio
import logging
import os
import sys
from contextlib import asynccontextmanager
from pathlib import Path

_src_dir = str(Path(__file__).resolve().parent.parent)
if _src_dir not in sys.path:
    sys.path.insert(0, _src_dir)

from dotenv import load_dotenv

_env = os.getenv("ENV", "dev")
_backend_dir = Path(__file__).resolve().parent.parent.parent
load_dotenv(_backend_dir / f".env.{_env}", override=True)

from shared.logging_config import setup_logging
setup_logging("strategy_sweep")

logger = logging.getLogger(__name__)

from fastapi import FastAPI, WebSocket, WebSocketDisconnect

from strategy_execution.enums import StrategyType
from strategy_execution.runtime_single import SingleStrategyRuntime
from strategy_execution.ws import get_strategy_ws_manager
from strategy_sweep.strategy import SweepStrategy

PORT = int(os.getenv("STRATEGY_SWEEP_PORT", "8003"))


@asynccontextmanager
async def lifespan(app: FastAPI):
    strategy_ws = get_strategy_ws_manager()
    await strategy_ws.start()

    runtime = SingleStrategyRuntime(
        strategy_type=StrategyType.SWEEP,
        strategy_class=SweepStrategy,
    )
    await runtime.start()
    app.state.runtime = runtime

    logger.info("Strategy Sweep service started on port %d", PORT)

    try:
        yield
    finally:
        await runtime.stop()
        await strategy_ws.stop()


app = FastAPI(title="Strategy Service (sweep)", lifespan=lifespan)


@app.get("/health")
async def health():
    runtime: SingleStrategyRuntime = app.state.runtime
    return {
        "status": "ok",
        "service": "strategy_sweep",
        "active_runs": len(runtime.get_active_runs()),
        "signal_status": runtime.signal_subscription_status(),
    }


@app.get("/api/runs")
async def list_runs():
    runtime: SingleStrategyRuntime = app.state.runtime
    return {"runs": runtime.get_active_runs()}


@app.get("/api/ledgers")
async def list_ledgers():
    runtime: SingleStrategyRuntime = app.state.runtime
    return {"ledgers": runtime.get_ledger_snapshots()}


@app.websocket("/ws/events")
async def strategy_events_ws(ws: WebSocket):
    se_ws = get_strategy_ws_manager()
    await se_ws.add_connection(ws)
    try:
        async for _ in ws.iter_text():
            pass
    except WebSocketDisconnect:
        pass
    finally:
        await se_ws.remove_connection(ws)


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=PORT)
