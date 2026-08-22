"""策略执行独立微服务入口。

通过 STRATEGY_TYPE 环境变量指定运行哪种策略：
  - sweep        → port 8003
  - leader       → port 8004
  - sweep_leader → port 8005

每个实例只订阅自己需要的信号源，互不影响。
"""
import asyncio
import logging
import os
import sys

from contextlib import asynccontextmanager
from pathlib import Path

from dotenv import load_dotenv

_src_dir = str(Path(__file__).resolve().parent.parent)
if _src_dir not in sys.path:
    sys.path.insert(0, _src_dir)

_env = os.getenv("ENV", "dev")
_backend_dir = Path(__file__).resolve().parent.parent.parent
load_dotenv(_backend_dir / f".env.{_env}", override=True)

_strategy_type_raw = os.getenv("STRATEGY_TYPE", "sweep")
_service_name = f"strategy_{_strategy_type_raw}"

from shared.logging_config import setup_logging
setup_logging(_service_name)

logger = logging.getLogger(__name__)

from fastapi import FastAPI, WebSocket, WebSocketDisconnect

from strategy_execution.enums import StrategyType
from strategy_execution.runtime_single import SingleStrategyRuntime
from strategy_execution.ws import get_strategy_ws_manager

STRATEGY_TYPE = os.getenv("STRATEGY_TYPE", "sweep")
DEFAULT_PORTS = {"sweep": "8003", "leader": "8004", "sweep_leader": "8005"}
PORT = int(os.getenv("STRATEGY_PORT", DEFAULT_PORTS.get(STRATEGY_TYPE, "8003")))


@asynccontextmanager
async def lifespan(app: FastAPI):
    st = StrategyType(STRATEGY_TYPE)
    strategy_ws = get_strategy_ws_manager()
    await strategy_ws.start()

    runtime = SingleStrategyRuntime(strategy_type=st)
    await runtime.start()
    app.state.runtime = runtime

    logger.info("Strategy service [%s] started on port %d", STRATEGY_TYPE, PORT)

    try:
        yield
    finally:
        await runtime.stop()
        await strategy_ws.stop()


app = FastAPI(title=f"Strategy Service ({STRATEGY_TYPE})", lifespan=lifespan)


@app.get("/health")
async def health():
    runtime: SingleStrategyRuntime = app.state.runtime
    return {
        "status": "ok",
        "service": f"strategy_{STRATEGY_TYPE}",
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
    """策略执行事件实时推送。"""
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
