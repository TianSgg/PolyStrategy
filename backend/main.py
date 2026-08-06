import asyncio
import gzip
import glob
import logging
import os
import sys
from contextlib import asynccontextmanager
from datetime import datetime
from logging.handlers import RotatingFileHandler

from dotenv import load_dotenv

# 将 src/ 加入 Python 模块搜索路径
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "src"))

from shared.time_utils import UTC8, now_utc8_dt

# 根据 ENV 加载对应配置
_env = os.getenv("ENV", "dev")
load_dotenv(f".env.{_env}", override=True)

# 配置日志
os.makedirs("logs", exist_ok=True)
log_level = getattr(logging, os.getenv("LOG_LEVEL", "INFO").upper())
log_file = f"logs/app-{now_utc8_dt():%Y-%m-%d-%H-%M-%S}.log"

rotating_handler = RotatingFileHandler(
    log_file,
    maxBytes=100 * 1024 * 1024,
    backupCount=10,
    encoding="utf-8",
)
stream_handler = logging.StreamHandler(sys.stdout)


class UTC8Formatter(logging.Formatter):
    def formatTime(self, record, datefmt=None):
        dt = datetime.fromtimestamp(record.created, UTC8)
        if datefmt:
            return dt.strftime(datefmt)
        return f"{dt:%Y-%m-%d %H:%M:%S}.{dt.microsecond // 1000:03d}"


log_formatter = UTC8Formatter(
    fmt="%(asctime)s - %(levelname)s - %(message)s",
)
stream_handler.setFormatter(log_formatter)
rotating_handler.setFormatter(log_formatter)

# 压缩旧的滚动文件（启动时压缩上一次运行遗留的未压缩备份）
for old in glob.glob(log_file + ".*"):
    if not old.endswith(".gz"):
        try:
            with open(old, "rb") as f_in:
                with gzip.open(old + ".gz", "wb") as f_out:
                    f_out.writelines(f_in)
            os.remove(old)
        except Exception:
            pass

logging.basicConfig(
    level=log_level,
    handlers=[
        stream_handler,
        rotating_handler,
    ],
)
logger = logging.getLogger(__name__)
logger.info(f"use ENV={_env}")

# 抑制第三方库的 DEBUG 日志（放在 import 之后，因为有些库 import 时会初始化 logger）
for lib in ["websockets", "httpcore", "httpx", "hpack", "hyperframe", "urllib3", "requests"]:
    logging.getLogger(lib).setLevel(logging.WARNING)

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware

from account.api import router as account_router
from account.service import get_account_service
from auth.api import router as auth_router
from auth.migrations import run_auth_migrations
from auth.service import AUTH_COOKIE_NAME, get_auth_service
from copy_trading.api import router as copy_trading_router
from copy_trading.chain import get_copy_trading_chain_monitor
from copy_trading.predexon import get_copy_trading_predexon
from copy_trading.service import get_copy_trading_service
from copy_trading.ws import CopyTradingWS, add_copy_trading_ws, stop_all_copy_trading_ws
from leader.api import router as leader_router
from market.api import router as market_router
from performance.router import router as performance_router
from pnl.router import router as pnl_router
from pnl.service import get_pnl_service
from portfolio_group.api import router as portfolio_group_router
from shared.frontend_ws import get_frontend_ws_manager


@asynccontextmanager
async def lifespan(app: FastAPI):
    """管理应用生命周期"""
    run_auth_migrations()

    if _env == "prod":
        chain_monitor = get_copy_trading_chain_monitor()
        copy_trading_predexon = get_copy_trading_predexon()
        ct_service = get_copy_trading_service()
        await ct_service.initialize()
        chain_monitor_task = asyncio.create_task(chain_monitor.run())
        copy_trading_predexon_task = asyncio.create_task(copy_trading_predexon.start())

        account_svc = get_account_service()

        started_followers: set = set()
        for config in ct_service._config_id_to_config.values():
            f_addr = config.follower_proxy_wallet
            if f_addr in started_followers:
                continue
            creds = account_svc.get_account_credentials_by_proxy_wallet(f_addr)
            if creds:
                ws = CopyTradingWS(f_addr, creds)
                add_copy_trading_ws(ws)
                asyncio.create_task(ws.start())
                started_followers.add(f_addr)
    else:
        logger.info("非 prod 环境，跳过跟单服务启动")

    from performance import get_performance_service

    perf_svc = get_performance_service()
    await perf_svc.start()

    if _env == "prod":
        pnl_svc = get_pnl_service()
        pnl_svc.start()

    try:
        yield
    finally:
        if _env == "prod":
            copy_trading_predexon.stop()
            copy_trading_predexon_task.cancel()
            chain_monitor.stop()
            chain_monitor_task.cancel()
            await stop_all_copy_trading_ws()
            ct_service.stop()
            pnl_svc.stop()
        perf_svc.stop()


app = FastAPI(lifespan=lifespan)

# 注册业务路由
app.include_router(auth_router)
app.include_router(copy_trading_router)
app.include_router(leader_router)
app.include_router(account_router)
app.include_router(market_router)
app.include_router(performance_router)
app.include_router(pnl_router)
app.include_router(portfolio_group_router)

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
