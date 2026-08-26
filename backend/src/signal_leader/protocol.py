"""Leader 活动信号服务 loopback WebSocket 协议。

Client → Server: {"type": "hello", "client_id": "...", "subscribe": ["leader_buy"]}
Server → Client: {"type": "welcome", "server_id": "leader_signal", "subscriptions": ["leader_buy"]}
Server → Client: {"type": "leader_buy", "signal": {...}}
Server ↔ Client: {"type": "ping"} / {"type": "pong"}
Server → Client: {"type": "error", "code": "...", "message": "..."}
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List


MSG_TYPE_HELLO = "hello"
MSG_TYPE_WELCOME = "welcome"
MSG_TYPE_LEADER_BUY = "leader_buy"
MSG_TYPE_PING = "ping"
MSG_TYPE_PONG = "pong"
MSG_TYPE_ERROR = "error"


@dataclass(frozen=True)
class HelloMessage:
    client_id: str
    subscribe: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return {"type": MSG_TYPE_HELLO, "client_id": self.client_id, "subscribe": self.subscribe}


@dataclass(frozen=True)
class WelcomeMessage:
    server_id: str
    subscriptions: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return {"type": MSG_TYPE_WELCOME, "server_id": self.server_id, "subscriptions": self.subscriptions}


@dataclass(frozen=True)
class LeaderBuyMessage:
    signal: Dict[str, Any]

    def to_dict(self) -> Dict[str, Any]:
        return {"type": MSG_TYPE_LEADER_BUY, "signal": self.signal}


@dataclass(frozen=True)
class ErrorMessage:
    code: str
    message: str

    def to_dict(self) -> Dict[str, Any]:
        return {"type": MSG_TYPE_ERROR, "code": self.code, "message": self.message}
