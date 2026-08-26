"""扫单策略微服务入口 — port 8003。"""
import os
import sys
from pathlib import Path

_src_dir = str(Path(__file__).resolve().parent.parent)
if _src_dir not in sys.path:
    sys.path.insert(0, _src_dir)

from dotenv import load_dotenv

_backend_dir = Path(__file__).resolve().parent.parent.parent
load_dotenv(_backend_dir / ".env", override=True)

from framework.logging import setup_logging
setup_logging("strategy_weather_sweep")

from fastapi.middleware.cors import CORSMiddleware

from framework.strategy_runtime.app_factory import create_app
from framework.strategy_runtime.container import SignalSourceConfig
from strategy_weather_sweep.service import SweepStrategy
from strategy_weather_sweep.api import router as strategy_router
from framework.strategy_runtime.weather_adapter import WeatherSweepAdapter
from account_service.service import get_account_service

PORT = int(os.getenv("STRATEGY_SWEEP_PORT", "8003"))
WEATHER_SIGNAL_URL = os.getenv("WEATHER_SIGNAL_WS_URL", "ws://localhost:8001/ws/signals")

CONSUL_TAGS = [
    "traefik.enable=true",
    "traefik.http.routers.strategy-sweep.rule=PathPrefix(`/api/strategy`)",
    "traefik.http.routers.strategy-sweep.entrypoints=web",
    "traefik.http.routers.strategy-sweep.middlewares=forward-auth@file",
]

app = create_app(
    client_provider=get_account_service(),
    strategy_class=SweepStrategy,
    signal_sources=[
        SignalSourceConfig(
            url=WEATHER_SIGNAL_URL,
            adapter=WeatherSweepAdapter(),
            name="weather_orderbook",
        ),
    ],
    service_name="strategy_weather_sweep",
    strategy_type="weather_sweep",
    config_table="weather_sweep_configs",
    events_table="weather_sweep_events",
    extra_routers=[strategy_router],
    consul_tags=CONSUL_TAGS,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=PORT)
