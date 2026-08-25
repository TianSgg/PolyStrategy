"""Leader 活动信号独立微服务入口 — port 8002。

职责：
  - 监听 leader 链上活动（通过 copy_trading WS 检测 BUY）
  - 持久化信号到 leader_signals
  - 通过 WS /ws/signal 向策略执行服务广播 leader_buy 信号
  - 提供 leader 管理 REST API
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

from shared.logging_config import setup_logging
setup_logging("signal_leader_activity")

logger = logging.getLogger(__name__)

from fastapi import FastAPI, WebSocket

from signal_leader_activity.api import router as leader_router
from signal_leader_activity.ws_hub import LeaderSignalHub
from signal_leader_activity.signal_repository import LeaderSignalRepository

signal_hub = LeaderSignalHub()
_signal_repo = LeaderSignalRepository()


@asynccontextmanager
async def lifespan(app: FastAPI):
    from shared.consul import consul_lifespan

    service_port = int(os.getenv("LEADER_SIGNAL_PORT", "8002"))
    consul_tags = [
        "traefik.enable=true",
        "traefik.http.routers.signal-leader.rule=PathPrefix(`/api/signals`)",
        "traefik.http.routers.signal-leader.entrypoints=web",
        "traefik.http.routers.signal-leader.middlewares=forward-auth@file",
    ]

    logger.info("Leader signal service started on port %s", service_port)

    async with consul_lifespan("signal-leader", service_port, tags=consul_tags):
        yield


app = FastAPI(title="Leader Signal Service", lifespan=lifespan)
app.include_router(leader_router)


@app.websocket("/ws/signal")
async def signal_websocket(ws: WebSocket):
    """策略执行服务连接此端点订阅 leader_buy 信号。"""
    await signal_hub.handle_connection(ws)


@app.get("/health")
async def health():
    return {"status": "ok", "service": "signal_leader_activity"}


@app.post("/api/signals/leader-buy")
async def publish_leader_buy(data: dict):
    """外部服务推入 leader buy 事件（HTTP push 入口），持久化并广播。"""
    signal_dict = {
        "event_id": data.get("event_id", f"leader:{int(time.time() * 1000)}"),
        "token_id": data.get("token_id", ""),
        "outcome": data.get("outcome", ""),
        "occurred_at_ms": data.get("occurred_at_ms", int(time.time() * 1000)),
        "received_at_ns": time.time_ns(),
        "leader_proxy_wallet": data.get("leader_proxy_wallet", ""),
        "leader_name": data.get("leader_name"),
        "order_size": data.get("order_size"),
        "order_price": data.get("order_price"),
        "market_slug": data.get("market_slug"),
        "extra": data.get("extra", {}),
    }

    _signal_repo.save({
        "id": signal_dict["event_id"],
        "event_type": "leader_buy",
        "token_id": signal_dict["token_id"],
        "outcome": signal_dict["outcome"],
        "leader_proxy_wallet": signal_dict["leader_proxy_wallet"],
        "leader_name": signal_dict.get("leader_name"),
        "order_size": signal_dict.get("order_size"),
        "order_price": signal_dict.get("order_price"),
        "market_slug": signal_dict.get("market_slug"),
        "occurred_at": None,
        "received_at": None,
        "received_monotonic_ns": signal_dict["received_at_ns"],
        "extra_json": signal_dict.get("extra", {}),
    })

    await signal_hub.broadcast(signal_dict)
    return {"status": "published"}


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(
        app,
        host="0.0.0.0",
        port=int(os.getenv("LEADER_SIGNAL_PORT", "8002")),
    )
