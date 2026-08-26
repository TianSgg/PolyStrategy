"""扫单+跟单组合策略微服务入口 — port 8005。"""
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
setup_logging("strategy_sweep_leader")

from framework.strategy_runtime.app_factory import create_app
from framework.strategy_runtime.container import SignalSourceConfig
from strategy_sweep_leader.strategy import SweepLeaderStrategy
from framework.strategy_runtime.leader_adapter import LeaderBuyAdapter
from framework.strategy_runtime.weather_adapter import WeatherSweepAdapter

PORT = int(os.getenv("STRATEGY_SWEEP_LEADER_PORT", "8005"))
WEATHER_SIGNAL_URL = os.getenv("WEATHER_SIGNAL_WS_URL", "ws://localhost:8001/ws/signals")
LEADER_SIGNAL_URL = os.getenv("LEADER_SIGNAL_WS_URL", "ws://localhost:8002/ws/signals")

app = create_app(
    strategy_class=SweepLeaderStrategy,
    config={
        "initial_cash": os.getenv("STRATEGY_INITIAL_CASH", "1000"),
        "fixed_probe_shares": os.getenv("SWEEP_LEADER_PROBE_SHARES", "50"),
        "leader_confirm_window_ms": int(os.getenv("SWEEP_LEADER_CONFIRM_WINDOW_MS", "60000")),
        "post_confirm_wait_ms": int(os.getenv("SWEEP_LEADER_POST_CONFIRM_MS", "30000")),
        "stop_loss_ratio": os.getenv("SWEEP_LEADER_STOP_LOSS_RATIO", "0.60"),
    },
    signal_sources=[
        SignalSourceConfig(
            url=WEATHER_SIGNAL_URL,
            adapter=WeatherSweepAdapter(),
            name="weather_orderbook",
        ),
        SignalSourceConfig(
            url=LEADER_SIGNAL_URL,
            adapter=LeaderBuyAdapter(),
            name="leader_activity",
        ),
    ],
    service_name="strategy_sweep_leader",
    proxy_wallet=os.getenv("PROXY_WALLET", ""),
)

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=PORT)
