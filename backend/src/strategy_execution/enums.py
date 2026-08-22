"""策略执行系统枚举定义。

所有枚举值使用 UPPER_SNAKE_CASE，数据库存储时直接使用 .value 字符串。
"""
from __future__ import annotations

from enum import Enum


class StrategyType(str, Enum):
    """策略类型 — 数据库存 VARCHAR(32)。"""
    SWEEP = "sweep"
    LEADER = "leader"
    SWEEP_LEADER = "sweep_leader"


class RunState(str, Enum):
    """策略运行状态机。

    CREATED → ENTRY_WORKING → EXIT_WORKING → CLOSED
                            ↘ WAITING_LEADER → EXIT_WORKING (sweep_leader only)
                            ↘ RISK_EXITING → CLOSED
    """
    CREATED = "CREATED"
    ENTRY_WORKING = "ENTRY_WORKING"
    WAITING_LEADER = "WAITING_LEADER"
    EXIT_WORKING = "EXIT_WORKING"
    RISK_EXITING = "RISK_EXITING"
    CLOSED = "CLOSED"


class RunStatus(str, Enum):
    """运行的高级状态 — 用于前端快速筛选。"""
    ACTIVE = "ACTIVE"
    CLOSED = "CLOSED"
    FAILED = "FAILED"
    RECOVERY_REQUIRED = "RECOVERY_REQUIRED"


class OrderPurpose(str, Enum):
    """订单用途 — 标明每笔订单在 run 中的角色。"""
    ENTRY = "entry"
    ADD = "add"
    EXIT_TICK = "exit_tick"
    EXIT_099 = "exit_099"
    RISK_EXIT = "risk_exit"


class ExecutionMode(str, Enum):
    """下单执行模式。"""
    FAST = "fast"
    NORMAL = "normal"


class OrderSide(str, Enum):
    BUY = "BUY"
    SELL = "SELL"


class OrderStatus(str, Enum):
    """CLOB 归一化订单状态。"""
    PENDING = "PENDING"
    LIVE = "LIVE"
    MATCHED = "MATCHED"
    PARTIALLY_MATCHED = "PARTIALLY_MATCHED"
    CANCELED = "CANCELED"
    FAILED = "FAILED"
    DELAYED = "DELAYED"


class CloseReason(str, Enum):
    """运行关闭原因。"""
    TICK_EXIT = "tick_exit"
    RISK_EXIT = "risk_exit"
    TIMEOUT_NO_FILL = "timeout_no_fill"
    ENTRY_CANCELED_NO_POSITION = "entry_canceled_no_position"
    SELL_FILLED = "sell_filled"
    MANUAL_FORCE_CLOSE = "manual_force_close"
    LEADER_NOT_CONFIRMED = "leader_not_confirmed"
    RECOVERY_CLOSED = "recovery_closed"


class RiskState(str, Enum):
    """风控会话状态。"""
    OFF = "OFF"
    ACTIVE = "ACTIVE"
    TRIGGERED = "TRIGGERED"
    FORCE_STOPPED = "FORCE_STOPPED"


class LedgerEntryType(str, Enum):
    """资金账本条目类型。"""
    BUY_RESERVED = "BUY_RESERVED"
    BUY_RELEASED = "BUY_RELEASED"
    FILL_BUY = "FILL_BUY"
    SELL_RESERVED = "SELL_RESERVED"
    SELL_RELEASED = "SELL_RELEASED"
    FILL_SELL = "FILL_SELL"
    RECONCILE = "RECONCILE"


class SignalRole(str, Enum):
    """信号在 run 中的角色 (strategy_run_signals.role)。"""
    ENTRY_SWEEP = "entry_sweep"
    LEADER_CONFIRM = "leader_confirm"
    IGNORED = "ignored"


class RunEventType(str, Enum):
    """运行时间线事件类型。"""
    SIGNAL_RECEIVED = "SIGNAL_RECEIVED"
    ORDER_SENT = "ORDER_SENT"
    ORDER_RESPONSE = "ORDER_RESPONSE"
    FILL = "FILL"
    CANCEL_SENT = "CANCEL_SENT"
    CANCEL_CONFIRMED = "CANCEL_CONFIRMED"
    TICK_CHECK_PASS = "TICK_CHECK_PASS"
    TICK_CHECK_FAIL = "TICK_CHECK_FAIL"
    RISK_TRIGGERED = "RISK_TRIGGERED"
    RISK_STOPPED = "RISK_STOPPED"
    LEADER_CONFIRMED = "LEADER_CONFIRMED"
    LEADER_TIMEOUT = "LEADER_TIMEOUT"
    STATE_TRANSITION = "STATE_TRANSITION"
    RUN_CLOSED = "RUN_CLOSED"
