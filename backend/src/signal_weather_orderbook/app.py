"""天气信号独立微服务入口 — port 8001。

职责：
  - 监听 Polymarket 订单簿变化（WeatherBootstrap）
  - 持久化事件到 weather_signal_events
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

logging.basicConfig(
    level=getattr(logging, os.getenv("LOG_LEVEL", "INFO").upper()),
    format="%(asctime)s - %(levelname)s - %(name)s - %(message)s",
)
for lib in ["websockets", "httpcore", "httpx", "urllib3"]:
    logging.getLogger(lib).setLevel(logging.WARNING)

logger = logging.getLogger(__name__)

from fastapi import FastAPI, WebSocket

from event_bus import get_event_bus
from signal_weather_orderbook.api import router as weather_router
from signal_weather_orderbook.bootstrap import WeatherBootstrap
from signal_weather_orderbook.ws_hub import WeatherSignalHub

signal_hub = WeatherSignalHub()


async def _event_bus_bridge() -> None:
    """从 EventBus 订阅 weather.sweep 事件，转发到 WS hub 广播。"""
    bus = get_event_bus()
    queue = bus.subscribe("weather.sweep")
    logger.info("Weather signal bridge started: weather.sweep → WS hub")
    while True:
        try:
            payload = await queue.get()
            asset = payload.get("asset", {})
            current_ob = payload.get("current_orderbook", {})
            previous_ob = payload.get("previous_orderbook")

            spread_before = "0"
            spread_after = "0"
            if previous_ob and previous_ob.get("best_ask") and previous_ob.get("best_bid"):
                spread_before = str(
                    float(previous_ob["best_ask"]["price"]) - float(previous_ob["best_bid"]["price"])
                )
            if current_ob.get("best_ask") and current_ob.get("best_bid"):
                spread_after = str(
                    float(current_ob["best_ask"]["price"]) - float(current_ob["best_bid"]["price"])
                )

            signal_dict = {
                "event_id": f"weather:{asset.get('event_slug', '')}:{int(time.time() * 1000)}",
                "token_id": asset.get("asset_id", ""),
                "outcome": asset.get("outcome", ""),
                "occurred_at_ms": current_ob.get("observed_at_unix_ms", int(time.time() * 1000)),
                "received_at_ns": time.time_ns(),
                "city": asset.get("city", ""),
                "spread_before": spread_before,
                "spread_after": spread_after,
                "volume_spike": False,
                "extra": {
                    "event_slug": asset.get("event_slug"),
                    "market_slug": asset.get("market_slug"),
                    "reason": payload.get("reason", ""),
                },
            }
            await signal_hub.broadcast(signal_dict)
        except asyncio.CancelledError:
            break
        except Exception:
            logger.exception("Error in weather signal bridge")


@asynccontextmanager
async def lifespan(app: FastAPI):
    weather_bootstrap = WeatherBootstrap()
    weather_service = await weather_bootstrap.start()
    app.state.weather_service = weather_service
    app.state.weather_notification_repository = weather_bootstrap.notification_repository

    bridge_task = asyncio.create_task(_event_bus_bridge(), name="weather-signal-bridge")
    logger.info("Weather signal service started on port %s", os.getenv("WEATHER_SIGNAL_PORT", "8001"))

    try:
        yield
    finally:
        bridge_task.cancel()
        try:
            await bridge_task
        except asyncio.CancelledError:
            pass
        await weather_bootstrap.stop()


app = FastAPI(title="Weather Signal Service", lifespan=lifespan)
app.include_router(weather_router)


@app.websocket("/ws/signal")
async def signal_websocket(ws: WebSocket):
    """策略执行服务连接此端点订阅 weather_sweep 信号。"""
    await signal_hub.handle_connection(ws)


@app.get("/health")
async def health():
    return {"status": "ok", "service": "signal_weather_orderbook"}


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(
        app,
        host="0.0.0.0",
        port=int(os.getenv("WEATHER_SIGNAL_PORT", "8001")),
    )
