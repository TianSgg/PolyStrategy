"""天气信号 WebSocket 广播 Hub — 向订阅者推送 weather_sweep 事件。"""
from __future__ import annotations

import asyncio
import json
import logging
from typing import Any, Dict, Set

from fastapi import WebSocket, WebSocketDisconnect

from .protocol import MSG_TYPE_PONG, WelcomeMessage, WeatherSweepMessage

logger = logging.getLogger(__name__)


class _ClientInfo:
    __slots__ = ("ws", "client_id", "main_only")

    def __init__(self, ws: WebSocket, client_id: str, main_only: bool) -> None:
        self.ws = ws
        self.client_id = client_id
        self.main_only = main_only


class WeatherSignalHub:
    """管理 WS 连接，广播 weather_sweep 信号。"""

    def __init__(self) -> None:
        self._clients: Dict[WebSocket, _ClientInfo] = {}
        self._lock = asyncio.Lock()

    async def handle_connection(self, ws: WebSocket) -> None:
        await ws.accept()
        hello_raw = await asyncio.wait_for(ws.receive_text(), timeout=5.0)
        hello = json.loads(hello_raw)
        client_id = hello.get("client_id", "unknown")
        main_only = bool(hello.get("main_only", False))

        welcome = WelcomeMessage(server_id="weather_signal", subscriptions=hello.get("subscribe", []))
        await ws.send_text(json.dumps(welcome.to_dict()))

        info = _ClientInfo(ws=ws, client_id=client_id, main_only=main_only)
        async with self._lock:
            self._clients[ws] = info
        logger.info("Weather WS client connected: %s main_only=%s (total=%d)", client_id, main_only, len(self._clients))

        try:
            async for raw in ws.iter_text():
                msg = json.loads(raw)
                if msg.get("type") == "ping":
                    await ws.send_text(json.dumps({"type": MSG_TYPE_PONG}))
        except WebSocketDisconnect:
            pass
        except Exception as e:
            logger.debug("Weather WS client error: %s", e)
        finally:
            async with self._lock:
                self._clients.pop(ws, None)
            logger.info("Weather WS client disconnected: %s (total=%d)", client_id, len(self._clients))

    async def broadcast(self, signal_dict: Dict[str, Any]) -> None:
        msg = WeatherSweepMessage(signal=signal_dict)
        payload = json.dumps(msg.to_dict())
        is_from_main = signal_dict.get("is_from_main", True)
        async with self._lock:
            clients = list(self._clients.values())
        for info in clients:
            if info.main_only and not is_from_main:
                continue
            try:
                await info.ws.send_text(payload)
            except Exception:
                async with self._lock:
                    self._clients.pop(info.ws, None)
