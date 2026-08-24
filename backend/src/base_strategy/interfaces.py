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
    """策略最小约束 — 只规定生命周期，不规定内部实现。

    生命周期: start → on_signal* → force_exit (配置变更时) → stop
    """

    SUBSCRIBED_SIGNALS: ClassVar[set[str]] = set()

    @abstractmethod
    async def start(self, ctx: StrategyContext) -> None:
        """初始化：策略从 ctx 获取需要的工具，设置内部状态。"""

    @abstractmethod
    async def on_signal(self, signal: Signal) -> None:
        """收到信号 — 策略自行决定如何处理。"""

    async def force_exit(self, reason: str = "config_disabled") -> None:
        """强制退出：撤买单 + 平仓。配置禁用/修改时由容器调用。

        子类必须实现：
        1. 撤销所有未成交买单
        2. 已持份额按 1 - tick_size 最大价格挂卖单
        3. 记录 event_closed
        """

    @abstractmethod
    async def stop(self) -> None:
        """关闭：释放资源（force_exit 之后调用）。"""


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
