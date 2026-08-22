"""Leader 活动信号 WebSocket 广播 Hub — 向订阅者推送 leader_buy 事件。"""
from __future__ import annotations

import asyncio
import json
import logging
from typing import Any, Dict, Set

from fastapi import WebSocket, WebSocketDisconnect

from .protocol import MSG_TYPE_PONG, WelcomeMessage, LeaderBuyMessage

logger = logging.getLogger(__name__)


class LeaderSignalHub:
    """管理 WS 连接，广播 leader_buy 信号。"""

    def __init__(self) -> None:
        self._clients: Set[WebSocket] = set()
        self._lock = asyncio.Lock()

    async def handle_connection(self, ws: WebSocket) -> None:
        await ws.accept()
        hello_raw = await asyncio.wait_for(ws.receive_text(), timeout=5.0)
        hello = json.loads(hello_raw)
        client_id = hello.get("client_id", "unknown")

        welcome = WelcomeMessage(server_id="leader_signal", subscriptions=hello.get("subscribe", []))
        await ws.send_text(json.dumps(welcome.to_dict()))

        async with self._lock:
            self._clients.add(ws)
        logger.info("Leader WS client connected: %s (total=%d)", client_id, len(self._clients))

        try:
            async for raw in ws.iter_text():
                msg = json.loads(raw)
                if msg.get("type") == "ping":
                    await ws.send_text(json.dumps({"type": MSG_TYPE_PONG}))
        except WebSocketDisconnect:
            pass
        except Exception as e:
            logger.debug("Leader WS client error: %s", e)
        finally:
            async with self._lock:
                self._clients.discard(ws)
            logger.info("Leader WS client disconnected: %s (total=%d)", client_id, len(self._clients))

    async def broadcast(self, signal_dict: Dict[str, Any]) -> None:
        msg = LeaderBuyMessage(signal=signal_dict)
        payload = json.dumps(msg.to_dict())
        async with self._lock:
            clients = list(self._clients)
        for ws in clients:
            try:
                await ws.send_text(payload)
            except Exception:
                async with self._lock:
                    self._clients.discard(ws)
