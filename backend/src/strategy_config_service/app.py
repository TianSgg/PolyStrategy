"""Strategy Config Service — 策略配置管理微服务。

职责：
- 策略配置 CRUD（/api/strategy/*）
- PnL 查询与推送（/api/pnl/*, /ws/pnl）
- Performance 监控（/api/performance/*, /ws/performance）
- 前端状态 WebSocket（/ws/market）
- Consul 服务注册
"""
import asyncio
import logging
import os
import sys
from contextlib import asynccontextmanager

import uvicorn
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from auth.service import AUTH_COOKIE_NAME, get_auth_service
from performance.router import router as performance_router
from pnl.router import router as pnl_router
from pnl.service import get_pnl_service
from shared.consul import consul_lifespan
from shared.frontend_ws import get_frontend_ws_manager
from strategy.api import router as strategy_router

logger = logging.getLogger(__name__)

SERVICE_NAME = os.getenv("SERVICE_NAME", "strategy-config-service")
SERVICE_PORT = int(os.getenv("SERVICE_PORT", "8012"))
ENV = os.getenv("ENV", "dev")

CONSUL_TAGS = [
    "traefik.enable=true",
    "traefik.http.routers.strategy.rule=PathPrefix(`/api/strategy`) || PathPrefix(`/api/pnl`) || PathPrefix(`/api/performance`) || PathPrefix(`/ws`)",
    "traefik.http.routers.strategy.entrypoints=web",
    "traefik.http.routers.strategy.middlewares=forward-auth@file",
]

frontend_ws = get_frontend_ws_manager()


def _get_ws_user(websocket: WebSocket):
    token = websocket.cookies.get(AUTH_COOKIE_NAME)
    try:
        payload = get_auth_service().decode_token(token) if token else None
        return get_auth_service().get_user_by_id(int(payload.get("sub", 0))) if payload else None
    except (TypeError, ValueError):
        return None


@asynccontextmanager
async def lifespan(app: FastAPI):
    from performance import get_performance_service

    perf_svc = get_performance_service()
    await perf_svc.start()

    if ENV != "dev":
        pnl_svc = get_pnl_service()
        pnl_svc.start()

    logger.info(f"[StrategyConfigService] Started on port {SERVICE_PORT}")
    async with consul_lifespan(SERVICE_NAME, SERVICE_PORT, tags=CONSUL_TAGS):
        try:
            yield
        finally:
            if ENV != "dev":
                pnl_svc.stop()
            perf_svc.stop()
            logger.info("[StrategyConfigService] Stopped")


app = FastAPI(title="Strategy Config Service", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(strategy_router)
app.include_router(pnl_router)
app.include_router(performance_router)


@app.websocket("/ws/market")
async def websocket_market(websocket: WebSocket):
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
        pass
    finally:
        await frontend_ws.remove_connection(websocket)


@app.websocket("/ws/performance")
async def websocket_performance(websocket: WebSocket):
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
        pass


@app.websocket("/ws/pnl")
async def websocket_pnl(websocket: WebSocket):
    """PnL WebSocket：推送实时 poll 数据。"""
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


@app.get("/health")
async def health():
    return {"status": "ok", "service": SERVICE_NAME}


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(name)s - %(levelname)s - %(message)s")
    uvicorn.run("strategy_config_service.app:app", host="0.0.0.0", port=SERVICE_PORT, reload=False)
