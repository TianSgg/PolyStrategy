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
setup_logging("strategy_sweep")

from strategy_runtime.app_factory import create_app
from strategy_runtime.container import SignalSourceConfig
from strategy_sweep.strategy import SweepStrategy
from toolkit.signals.adapters.weather_adapter import WeatherSweepAdapter

PORT = int(os.getenv("STRATEGY_SWEEP_PORT", "8003"))
WEATHER_SIGNAL_URL = os.getenv("WEATHER_SIGNAL_WS_URL", "ws://localhost:8001/ws/signals")

app = create_app(
    strategy_class=SweepStrategy,
    config={
        "initial_cash": os.getenv("STRATEGY_INITIAL_CASH", "1000"),
        "fixed_entry_shares": os.getenv("SWEEP_FIXED_SHARES", "100"),
        "entry_wait_ms": int(os.getenv("SWEEP_ENTRY_WAIT_MS", "30000")),
        "stop_loss_ratio": os.getenv("SWEEP_STOP_LOSS_RATIO", "0.60"),
    },
    signal_sources=[
        SignalSourceConfig(
            url=WEATHER_SIGNAL_URL,
            adapter=WeatherSweepAdapter(),
            name="weather_orderbook",
        ),
    ],
    service_name="strategy_sweep",
    proxy_wallet=os.getenv("PROXY_WALLET", ""),
)

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=PORT)
