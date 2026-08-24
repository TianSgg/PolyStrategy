from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, ClassVar, Protocol


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


class BaseStrategy(ABC):
    """策略最小约束 — 只规定生命周期，不规定内部实现。"""

    SUBSCRIBED_SIGNALS: ClassVar[set[str]] = set()

    @abstractmethod
    async def start(self, ctx: StrategyContext) -> None:
        """初始化：策略从 ctx 获取需要的工具，设置内部状态。"""

    @abstractmethod
    async def on_signal(self, signal: Signal) -> None:
        """收到信号 — 策略自行决定如何处理。"""

    @abstractmethod
    async def stop(self) -> None:
        """关闭：取消定时器、撤销挂单、释放资源。"""


@dataclass
class StrategyContext:
    """工具箱入口 — 策略按需使用，不强制全部依赖。"""

    executor: OrderExecutorProtocol
    state_store: StateStoreProtocol
    config: dict[str, Any]
    proxy_wallet: str
    run_id: str
    event_logger: Any = None


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
    status: str  # "filled", "partial", "live", "failed"
    filled_size: str
    filled_price: str | None = None
