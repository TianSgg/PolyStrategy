"""天气信号独立微服务入口 — port 8001。

职责：
  - 监听 Polymarket 订单簿变化（WeatherBootstrap）
  - 持久化信号到 weather_orderbook_signals
  - 通过 WS /ws/signal 向策略执行服务广播 weather_sweep 信号
  - 提供天气相关 REST API
"""
import asyncio
import logging
import os
import sys
import time

from contextlib import asynccontextmanager
from pathlib import Path

from dotenv import load_dotenv

_src_dir = str(Path(__file__).resolve().parent.parent)
if _src_dir not in sys.path:
    sys.path.insert(0, _src_dir)

_env = os.getenv("ENV", "dev")
_backend_dir = Path(__file__).resolve().parent.parent.parent
load_dotenv(_backend_dir / f".env.{_env}", override=True)

from framework.logging import setup_logging
setup_logging("signal_weather")

logger = logging.getLogger(__name__)

from fastapi import FastAPI, WebSocket

from signal_weather.api import router as weather_router
from signal_weather.bootstrap import WeatherBootstrap
from signal_weather.ws_hub import WeatherSignalHub

signal_hub = WeatherSignalHub()


async def _broadcast_weather_signal(event_type: str, payload: dict) -> None:
    """Coordinator 回调 — 将事件转为 WS 信号格式并广播到策略服务。"""
    asset = payload.get("asset", {})
    current_ob = payload.get("current_orderbook", {})
    event_slug = asset.get("event_slug", "")
    direction = event_slug.split("-temperature-in-", 1)[0] if "-temperature-in-" in event_slug else ""

    signal_dict = {
        "event_id": f"{event_slug}:{asset.get('asset_id', '')}:{int(time.time() * 1000)}",
        "event_type": event_type,
        "token_id": asset.get("asset_id", ""),
        "outcome": asset.get("outcome", ""),
        "city": asset.get("city", ""),
        "event_slug": event_slug,
        "market_slug": asset.get("market_slug"),
        "temperature_label": asset.get("temperature_label"),
        "direction": direction,
        "reason": payload.get("reason", ""),
        "is_from_main": payload.get("is_from_main", True),
        "occurred_at_ms": current_ob.get("observed_at_unix_ms", int(time.time() * 1000)),
        "received_at_ns": time.time_ns(),
        "orderbook_snapshot": current_ob,
    }
    if payload.get("next_candidate_orderbook"):
        signal_dict["next_candidate_orderbook"] = payload["next_candidate_orderbook"]
    await signal_hub.broadcast(signal_dict)


@asynccontextmanager
async def lifespan(app: FastAPI):
    from framework.consul import consul_lifespan

    service_port = int(os.getenv("WEATHER_SIGNAL_PORT", "8001"))
    consul_tags = [
        "traefik.enable=true",
        "traefik.http.routers.signal-weather.rule=PathPrefix(`/api/weather`)",
        "traefik.http.routers.signal-weather.entrypoints=web",
        "traefik.http.routers.signal-weather.middlewares=forward-auth@file",
    ]

    weather_bootstrap = WeatherBootstrap()
    weather_service = await weather_bootstrap.start(on_broadcast=_broadcast_weather_signal)
    app.state.weather_service = weather_service
    app.state.weather_signal_event_repository = weather_bootstrap.signal_event_repository

    logger.info("Weather signal service started on port %s", service_port)

    async with consul_lifespan("signal-weather", service_port, tags=consul_tags):
        try:
            yield
        finally:
            await weather_bootstrap.stop()


app = FastAPI(title="Weather Signal Service", lifespan=lifespan)
app.include_router(weather_router)


@app.websocket("/ws/signal")
async def signal_websocket(ws: WebSocket):
    """策略执行服务连接此端点订阅 weather_sweep 信号。"""
    await signal_hub.handle_connection(ws)


@app.get("/health")
async def health():
    return {"status": "ok", "service": "signal-weather"}


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(
        app,
        host="0.0.0.0",
        port=int(os.getenv("WEATHER_SIGNAL_PORT", "8001")),
    )
