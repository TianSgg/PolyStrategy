"""信号数据领域的核心数据契约。

SignalEnvelope 是两个信号服务（天气/Leader）广播给策略服务的唯一载体。
MarketContext 是信号触发时采样的订单簿上下文。
DeliveryResult 记录一次广播的投递结果。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any, Mapping, Optional, Tuple


@dataclass(frozen=True)
class SignalEnvelope:
    """信号服务向策略 WS 客户端广播的不可变消息。

    关键路径使用 dataclass；跨进程传输时用 msgpack 序列化。
    """
    event_id: str
    source: str  # "weather" | "leader"
    event_type: str
    received_at_ns: int
    occurred_at_ms: int
    token_id: str
    outcome: Optional[str] = None  # "yes" | "no"
    side: Optional[str] = None  # "BUY" | "SELL"
    leader_proxy_wallet: Optional[str] = None
    payload: Mapping[str, Any] = field(default_factory=dict)

    def dedup_key(self) -> str:
        return f"{self.source}:{self.event_id}"


@dataclass(frozen=True)
class MarketContext:
    """信号触发时采样的订单簿上下文 — 异步持久化，不阻塞入场。"""
    signal_event_id: str
    token_id: str
    observed_at_ms: int
    tick_size: Optional[Decimal] = None
    best_bid: Optional[Decimal] = None
    best_ask: Optional[Decimal] = None
    bid_depth: Optional[Decimal] = None
    ask_depth: Optional[Decimal] = None
    bid_levels: Optional[int] = None
    ask_levels: Optional[int] = None
    book_summary: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class DeliveryResult:
    """一次信号广播的投递摘要 — 记录哪些策略收到了信号。"""
    signal_event_id: str
    delivered_to: Tuple[str, ...] = ()
    dropped_reason: Optional[str] = None
    delivered_at_ns: int = 0
