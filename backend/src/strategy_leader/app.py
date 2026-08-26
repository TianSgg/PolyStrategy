"""跟单策略微服务入口 — port 8004。"""
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
setup_logging("strategy_leader")

from framework.strategy_runtime.app_factory import create_app
from framework.strategy_runtime.container import SignalSourceConfig
from strategy_leader.strategy import LeaderStrategy
from framework.strategy_runtime.leader_adapter import LeaderBuyAdapter
from account_service.service import get_account_service

PORT = int(os.getenv("STRATEGY_LEADER_PORT", "8004"))
LEADER_SIGNAL_URL = os.getenv("LEADER_SIGNAL_WS_URL", "ws://localhost:8002/ws/signals")

app = create_app(
    client_provider=get_account_service(),
    strategy_class=LeaderStrategy,
    config={
        "initial_cash": os.getenv("STRATEGY_INITIAL_CASH", "1000"),
        "fixed_entry_shares": os.getenv("LEADER_FIXED_SHARES", "100"),
        "entry_size_mode": os.getenv("LEADER_SIZE_MODE", "fixed"),
        "entry_wait_ms": int(os.getenv("LEADER_ENTRY_WAIT_MS", "30000")),
        "stop_loss_ratio": os.getenv("LEADER_STOP_LOSS_RATIO", "0.60"),
    },
    signal_sources=[
        SignalSourceConfig(
            url=LEADER_SIGNAL_URL,
            adapter=LeaderBuyAdapter(),
            name="leader_activity",
        ),
    ],
    service_name="strategy_leader",
    proxy_wallet=os.getenv("PROXY_WALLET", ""),
)

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=PORT)
