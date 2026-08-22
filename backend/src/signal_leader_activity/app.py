"""Leader 活动信号独立微服务入口 — port 8002。

职责：
  - 监听 leader 链上活动（通过 copy_trading WS 检测 BUY）
  - 持久化事件到 leader_signal_events
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

logging.basicConfig(
    level=getattr(logging, os.getenv("LOG_LEVEL", "INFO").upper()),
    format="%(asctime)s - %(levelname)s - %(name)s - %(message)s",
)
for lib in ["websockets", "httpcore", "httpx", "urllib3"]:
    logging.getLogger(lib).setLevel(logging.WARNING)

logger = logging.getLogger(__name__)

from fastapi import FastAPI, WebSocket

from event_bus import get_event_bus
from signal_leader_activity.api import router as leader_router
from signal_leader_activity.ws_hub import LeaderSignalHub
from signal_leader_activity.signal_repository import LeaderSignalRepository

signal_hub = LeaderSignalHub()
_signal_repo = LeaderSignalRepository()


async def _event_bus_bridge() -> None:
    """从 EventBus 订阅 leader.buy 事件，持久化并转发到 WS hub 广播。"""
    bus = get_event_bus()
    queue = bus.subscribe("leader.buy")
    logger.info("Leader signal bridge started: leader.buy → WS hub")
    while True:
        try:
            payload = await queue.get()
            signal_dict = {
                "event_id": payload.get("event_id", f"leader:{int(time.time() * 1000)}"),
                "token_id": payload.get("token_id", ""),
                "outcome": payload.get("outcome", ""),
                "occurred_at_ms": payload.get("occurred_at_ms", int(time.time() * 1000)),
                "received_at_ns": time.time_ns(),
                "leader_proxy_wallet": payload.get("leader_proxy_wallet", ""),
                "leader_name": payload.get("leader_name"),
                "order_size": payload.get("order_size"),
                "order_price": payload.get("order_price"),
                "market_slug": payload.get("market_slug"),
                "extra": payload.get("extra", {}),
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
        except asyncio.CancelledError:
            break
        except Exception:
            logger.exception("Error in leader signal bridge")


@asynccontextmanager
async def lifespan(app: FastAPI):
    bridge_task = asyncio.create_task(_event_bus_bridge(), name="leader-signal-bridge")
    logger.info("Leader signal service started on port %s", os.getenv("LEADER_SIGNAL_PORT", "8002"))

    try:
        yield
    finally:
        bridge_task.cancel()
        try:
            await bridge_task
        except asyncio.CancelledError:
            pass


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
    """外部服务推入 leader buy 事件（HTTP push 入口）。"""
    bus = get_event_bus()
    bus.publish("leader.buy", data)
    return {"status": "queued"}


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(
        app,
        host="0.0.0.0",
        port=int(os.getenv("LEADER_SIGNAL_PORT", "8002")),
    )
