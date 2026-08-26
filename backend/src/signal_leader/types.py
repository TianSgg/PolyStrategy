"""Leader 活动信号的强类型数据契约。"""
from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any, Mapping, Optional


@dataclass(frozen=True)
class LeaderBuySignal:
    """Leader 买入活动信号 — 检测到目标 leader 执行了 BUY。"""
    event_id: str
    token_id: str
    outcome: str  # "yes" | "no"
    occurred_at_ms: int
    received_at_ns: int
    leader_proxy_wallet: str
    leader_name: Optional[str] = None
    order_size: Optional[Decimal] = None
    order_price: Optional[Decimal] = None
    market_slug: Optional[str] = None
    extra: Mapping[str, Any] = field(default_factory=dict)

    def dedup_key(self) -> str:
        return f"leader:{self.event_id}"
