import asyncio
import logging
import os
import sys
from contextlib import asynccontextmanager

from dotenv import load_dotenv

# 将 src/ 加入 Python 模块搜索路径
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "src"))

# 根据 ENV 加载对应配置
_env = os.getenv("ENV", "dev")
load_dotenv(f".env.{_env}", override=True)

from shared.logging_config import setup_logging
setup_logging("gateway")

logger = logging.getLogger(__name__)
logger.info("use ENV=%s", _env)

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware

from account.api import router as account_router
from auth.api import router as auth_router
from auth.migrations import run_auth_migrations
from auth.service import AUTH_COOKIE_NAME, get_auth_service
from market.api import router as market_router
from performance.router import router as performance_router
from pnl.router import router as pnl_router
from pnl.service import get_pnl_service
from shared.frontend_ws import get_frontend_ws_manager
from strategy.api import router as strategy_router


@asynccontextmanager
async def lifespan(app: FastAPI):
    """管理应用生命周期"""
    run_auth_migrations()

    from performance import get_performance_service

    perf_svc = get_performance_service()
    await perf_svc.start()

    if _env != "dev":
        pnl_svc = get_pnl_service()
        pnl_svc.start()

    try:
        yield
    finally:
        if _env != "dev":
            pnl_svc.stop()
        perf_svc.stop()


app = FastAPI(lifespan=lifespan)

app.include_router(auth_router)
app.include_router(account_router)
app.include_router(market_router)
app.include_router(performance_router)
app.include_router(pnl_router)
app.include_router(strategy_router)

# CORS 配置
app.add_middleware(
    CORSMiddleware,
    allow_origins=[os.getenv("FRONTEND_ORIGIN")],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# 前端 WS 管理器（单例）
frontend_ws = get_frontend_ws_manager()


def _get_ws_user(websocket: WebSocket):
    token = websocket.cookies.get(AUTH_COOKIE_NAME)
    try:
        payload = get_auth_service().decode_token(token) if token else None
        return get_auth_service().get_user_by_id(int(payload.get("sub", 0))) if payload else None
    except (TypeError, ValueError):
        return None


@app.websocket("/ws/market")
async def websocket_endpoint(websocket: WebSocket):
    """前端状态 WebSocket 端点。"""
    user = _get_ws_user(websocket)
    if not user or not user.enabled:
        await websocket.close(code=1008)
        return
    await frontend_ws.add_connection(websocket)
    try:
        async for _ in websocket.iter_text():
            pass
    except WebSocketDisconnect:
        logger.info("Client disconnected")
    finally:
        await frontend_ws.remove_connection(websocket)


@app.websocket("/ws/performance")
async def performance_websocket_endpoint(websocket: WebSocket):
    """管理员性能监控 WebSocket 端点。"""
    user = _get_ws_user(websocket)
    if not user or not user.enabled or user.role not in ("admin", "root"):
        await websocket.close(code=1008)
        return

    await websocket.accept()
    from performance import get_performance_service

    perf_svc = get_performance_service()
    try:
        while True:
            await websocket.send_json({
                "event_type": "cache_summary",
                "data": perf_svc.get_cache_summary(),
            })
            await asyncio.sleep(5)
    except WebSocketDisconnect:
        logger.info("Performance client disconnected")


@app.websocket("/ws/pnl")
async def pnl_websocket_endpoint(websocket: WebSocket):
    """PnL WebSocket：前端通过 REST 获取历史，WS 仅推送后续新 poll 数据。"""
    user = _get_ws_user(websocket)
    if not user or not user.enabled:
        await websocket.close(code=1008)
        return

    await websocket.accept()
    pnl_svc = get_pnl_service()
    queue: asyncio.Queue = asyncio.Queue(maxsize=64)
    pnl_svc.add_ws_queue(queue)
    try:
        while True:
            msg = await queue.get()
            await websocket.send_json(msg)
    except WebSocketDisconnect:
        pass
    finally:
        pnl_svc.remove_ws_queue(queue)


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=int(os.getenv("PORT", 8000)))
