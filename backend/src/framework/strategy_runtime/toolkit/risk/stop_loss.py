"""StopLossMonitor — 基于 BBO 的止损风控组件。

检测 best_bid <= entry_price * stop_loss_ratio 时触发回调。
策略可继承重写 check() 实现不同风控逻辑。
"""
from __future__ import annotations

import asyncio
import logging
from decimal import Decimal
from typing import Any, Callable, Coroutine, Optional

logger = logging.getLogger(__name__)


class StopLossMonitor:
    """止损风控 — 策略可选组件。"""

    def __init__(
        self,
        ratio: Decimal = Decimal("0.60"),
        on_trigger: Optional[Callable[[], Coroutine[Any, Any, None]]] = None,
    ) -> None:
        self._ratio = ratio
        self._on_trigger = on_trigger
        self._entry_price: Optional[Decimal] = None
        self._active = False
        self._triggered = False

    @property
    def is_active(self) -> bool:
        return self._active

    @property
    def is_triggered(self) -> bool:
        return self._triggered

    def start(self, entry_price: Decimal) -> None:
        """激活止损监控。"""
        self._entry_price = entry_price
        self._active = True
        self._triggered = False

    def stop(self) -> None:
        self._active = False

    async def check(self, bbo: dict[str, Any]) -> None:
        """传入 BBO 数据检查止损条件。

        bbo 应包含: {"best_bid": str|Decimal, ...}
        """
        if not self._active or self._triggered:
            return
        if self._entry_price is None:
            return

        best_bid_raw = bbo.get("best_bid")
        if best_bid_raw is None:
            return

        best_bid = Decimal(str(best_bid_raw))
        threshold = self._entry_price * self._ratio

        if best_bid <= threshold:
            logger.warning(
                "Stop loss triggered: best_bid=%s <= threshold=%s",
                best_bid, threshold,
            )
            self._triggered = True
            self._active = False
            if self._on_trigger:
                asyncio.create_task(self._on_trigger())
