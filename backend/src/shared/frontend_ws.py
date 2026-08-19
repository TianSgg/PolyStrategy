import json
import logging
from typing import Set
from fastapi import WebSocket

logger = logging.getLogger(__name__)


class FrontendWSManager:
    _instance = None

    def __init__(self):
        self._connections: Set[WebSocket] = set()

    @classmethod
    def get_instance(cls):
        if cls._instance is None:
            cls._instance = cls()
        return cls._instance

    async def add_connection(self, websocket: WebSocket):
        await websocket.accept()
        self._connections.add(websocket)
        logger.debug(f"[WS] Client connected. Total: {len(self._connections)}")

    async def remove_connection(self, websocket: WebSocket):
        self._connections.discard(websocket)
        logger.debug(f"[WS] Client disconnected. Total: {len(self._connections)}")

    async def broadcast(self, data: dict):
        if not self._connections:
            return
        message = json.dumps(data)
        disconnected = set()
        for client in self._connections:
            try:
                await client.send_text(message)
            except Exception:
                disconnected.add(client)
        for client in disconnected:
            self._connections.discard(client)

    @property
    def connection_count(self) -> int:
        return len(self._connections)


def get_frontend_ws_manager() -> FrontendWSManager:
    return FrontendWSManager.get_instance()
