from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any, Protocol


@dataclass(frozen=True)
class Signal:
    """所有信号源必须输出此格式。"""

    signal_id: str
    signal_type: str
    token_id: str
    market_slug: str
    occurred_at_ms: int
    source: str
    payload: dict = field(default_factory=dict)


class BaseStrategy:
    """策略基类 — 可选继承，提供默认空实现。

    不再是 ABC，不强制子类实现任何方法。
    策略可以继承它获得类型提示，也可以完全不用它。
    """

    async def start(self, ctx: StrategyContext) -> None:
        """初始化：策略从 ctx 获取需要的工具，设置内部状态。"""

    async def on_signal(self, signal: Signal) -> None:
        """收到信号 — 策略自行决定如何处理。"""

    async def force_exit(self, reason: str = "config_disabled") -> None:
        """强制退出：撤买单 + 平仓。"""

    async def stop(self) -> None:
        """关闭：释放资源。"""


@dataclass
class StrategyContext:
    """工具箱入口 — 策略按需使用，不强制全部依赖。"""

    executor: OrderExecutorProtocol
    state_store: StateStoreProtocol
    config: dict[str, Any]
    proxy_wallet: str
    run_id: str
    event_logger: Any = None
    orderbook_ws: Any = None


class OrderExecutorProtocol(Protocol):
    async def place_order(
        self, token_id: str, side: str, price: str, size: str, **kwargs
    ) -> OrderResult: ...

    async def cancel_order(self, order_id: str) -> bool: ...


class StateStoreProtocol(Protocol):
    async def save(self, run_id: str, state: dict) -> None: ...
    async def load(self, run_id: str) -> dict | None: ...


@dataclass(frozen=True)
class OrderResult:
    order_id: str
    status: str  # "filled", "partial", "live", "failed", "insufficient_balance"
    filled_size: str
    filled_price: str | None = None
    error: str | None = None
    error_status_code: int | None = None
    error_message: str | None = None
    clob_status: str | None = None      # raw CLOB status: "matched", "live", "delayed"
    clob_taking: str | None = None      # raw takingAmount
    clob_making: str | None = None      # raw makingAmount


@dataclass(frozen=True)
class CancelResult:
    order_id: str
    cancelled: bool
    final_matched: Decimal
    status: str
    query_failed: bool = False
    cancel_error: str | None = None
    cancel_error_status_code: int | None = None
    cancel_error_message: str | None = None
    query_error: str | None = None
    query_error_status_code: int | None = None
    query_error_message: str | None = None
