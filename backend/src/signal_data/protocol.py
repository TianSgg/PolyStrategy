"""信号服务 loopback WebSocket 协议定义。

消息格式：msgpack 编码的 dict，type 字段区分消息类型。
策略服务作为 WS Client 连接信号服务的 WS Server。

协议流程:
  Client → Server: {"type": "hello", "client_id": "strategy_sweep_1", "subscribe": ["sweep"]}
  Server → Client: {"type": "welcome", "server_id": "weather_signal", "subscriptions": ["sweep"]}
  Server → Client: {"type": "signal", "envelope": {...}}
  Server ↔ Client: {"type": "ping"} / {"type": "pong"}
  Server → Client: {"type": "error", "code": "...", "message": "..."}
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List


MSG_TYPE_HELLO = "hello"
MSG_TYPE_WELCOME = "welcome"
MSG_TYPE_SIGNAL = "signal"
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
class SignalMessage:
    envelope: Dict[str, Any]

    def to_dict(self) -> Dict[str, Any]:
        return {"type": MSG_TYPE_SIGNAL, "envelope": self.envelope}


@dataclass(frozen=True)
class ErrorMessage:
    code: str
    message: str

    def to_dict(self) -> Dict[str, Any]:
        return {"type": MSG_TYPE_ERROR, "code": self.code, "message": self.message}
