"""天气信号独立微服务入口 — port 8001。

职责：
  - 监听 Polymarket 订单簿变化
  - 持久化信号到 weather_orderbook_signals
  - 通过 WS /ws/signal 向策略执行服务广播 weather_sweep 信号
  - 提供天气相关 REST API
"""
import logging
import os
import sys

from contextlib import asynccontextmanager
from pathlib import Path

from dotenv import load_dotenv

_src_dir = str(Path(__file__).resolve().parent.parent)
if _src_dir not in sys.path:
    sys.path.insert(0, _src_dir)

_backend_dir = Path(__file__).resolve().parent.parent.parent
_env = os.getenv("ENV", "dev")
load_dotenv(_backend_dir / f".env.{_env}", override=True)

from framework.logging import setup_logging
setup_logging("signal_weather")
from framework.db import MYSQL_CONFIG
from signal_weather.internal.config_loader import load_service_config

logger = logging.getLogger(__name__)

from fastapi import FastAPI, WebSocket
import aiohttp
import asyncmy

from signal_weather.api import router as weather_router
from signal_weather.dao import WeatherDao
from signal_weather.internal.polymarket_client import PolymarketMarketClient
from signal_weather.service import WeatherService

SERVICE_CONFIG = load_service_config(Path(__file__).parent)
SERVICE_NAME = SERVICE_CONFIG["service"]["name"]
SERVICE_PORT = int(os.getenv("WEATHER_SIGNAL_PORT", SERVICE_CONFIG["service"]["port"]))
CONSUL_TAGS = SERVICE_CONFIG.get("consul", {}).get("tags", [])

@asynccontextmanager
async def lifespan(app: FastAPI):
    from framework.consul import consul_lifespan

    mysql_pool = await asyncmy.create_pool(
        host=MYSQL_CONFIG["host"], port=MYSQL_CONFIG["port"], user=MYSQL_CONFIG["user"],
        password=MYSQL_CONFIG["password"], db=MYSQL_CONFIG["database"], minsize=3,
        maxsize=10, pool_recycle=1800, autocommit=True, connect_timeout=5,
    )
    dao = WeatherDao(mysql_pool)
    cities = await dao.list_enabled()
    http_session = aiohttp.ClientSession()
    service = WeatherService(cities, dao, PolymarketMarketClient(http_session))
    app.state.weather_service = service
    app.state.weather_http_session = http_session
    app.state.weather_mysql_pool = mysql_pool
    await service.start()

    logger.info("Weather signal service started on port %s", SERVICE_PORT)

    async with consul_lifespan(SERVICE_NAME, SERVICE_PORT, tags=CONSUL_TAGS):
        try:
            yield
        finally:
            await service.stop()
            await http_session.close()
            mysql_pool.close()
            await mysql_pool.wait_closed()


app = FastAPI(title="Weather Signal Service", lifespan=lifespan)
app.include_router(weather_router)


@app.websocket("/ws/signal")
async def signal_websocket(ws: WebSocket):
    """策略执行服务连接此端点订阅 weather_sweep 信号。"""
    await ws.app.state.weather_service.handle_signal_websocket(ws)


@app.get("/health")
async def health():
    return {"status": "ok", "service": "signal-weather"}


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(
        app,
        host="0.0.0.0",
        port=SERVICE_PORT,
    )
