"""不可变 DTO — 策略内核与外部交互的数据边界。

所有 dataclass 使用 frozen=True 以防止意外修改。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any, Literal, Mapping, Optional

from strategy_execution.enums import (
    CloseReason,
    ExecutionMode,
    OrderPurpose,
    OrderSide,
    OrderStatus,
    RunState,
)


# ─── 信号（各信号源的强类型契约定义在 signal/ 包中） ─────────────────────────
# WeatherSweepSignal: signal.weather_orderbook.contracts
# LeaderBuySignal: signal.leader_activity.contracts


# ─── 订单簿 ───────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class BBO:
    """Best Bid/Offer 快照。"""
    token_id: str
    best_bid: Optional[Decimal]
    best_ask: Optional[Decimal]
    tick_size: Optional[Decimal]
    observed_at_ns: int


# ─── 订单回报 ─────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class OrderReport:
    """来自 CLOB 或 User WS 的订单状态更新。"""
    client_order_id: str
    clob_order_id: Optional[str]
    status: OrderStatus
    matched_size: Decimal
    avg_matched_price: Optional[Decimal]
    timestamp_ms: int


# ─── 订单请求 ─────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class OrderRequest:
    """策略向执行器发出的下单请求。"""
    run_id: str
    client_order_id: str
    token_id: str
    side: OrderSide
    limit_price: Decimal
    size: Decimal
    purpose: OrderPurpose
    execution_mode: ExecutionMode


# ─── 运行事件 ─────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class RunEvent:
    """运行时间线中的一条不可变记录。"""
    run_id: str
    sequence_no: int
    event_type: str
    occurred_at_ms: int
    monotonic_ns: Optional[int] = None
    payload: Mapping[str, Any] = field(default_factory=dict)


# ─── 运行摘要 ─────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class RunSnapshot:
    """运行在某一时刻的快照 — 用于前端展示和诊断。"""
    run_id: str
    state: RunState
    token_id: str
    position_shares: Decimal
    pending_buy_shares: Decimal
    pending_sell_shares: Decimal
    avg_entry_price: Optional[Decimal]
    best_bid: Optional[Decimal]
    best_ask: Optional[Decimal]
    close_reason: Optional[CloseReason] = None
