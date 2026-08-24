"""扫单策略微服务入口 — port 8003。"""
import os
import sys
from pathlib import Path

_src_dir = str(Path(__file__).resolve().parent.parent)
if _src_dir not in sys.path:
    sys.path.insert(0, _src_dir)

from dotenv import load_dotenv

_env = os.getenv("ENV", "dev")
_backend_dir = Path(__file__).resolve().parent.parent.parent
load_dotenv(_backend_dir / f".env.{_env}", override=True)

from shared.logging_config import setup_logging
setup_logging("strategy_weather_sweep")

from base_strategy.app_factory import create_app
from base_strategy.container import SignalSourceConfig
from strategy_weather_sweep.strategy import SweepStrategy
from base_strategy.toolkit.signals.adapters.weather_adapter import WeatherSweepAdapter

PORT = int(os.getenv("STRATEGY_SWEEP_PORT", "8003"))
WEATHER_SIGNAL_URL = os.getenv("WEATHER_SIGNAL_WS_URL", "ws://localhost:8001/ws/signals")

app = create_app(
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
)

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=PORT)
